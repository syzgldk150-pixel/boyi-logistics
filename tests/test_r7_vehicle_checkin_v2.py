"""Plugin-owned R7 protocol over actual ZIP, sandbox and generic Host HTTP."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.developer_v2 import build_service_v2_package
from agent.automation_plugins.errors import PluginExecutionError
from service_v2_plugins.r7_vehicle_checkin_v2.payload.action import date_range, run
from service_v2_plugins.r7_vehicle_checkin_v2.payload.r7_client import (
    AUTH,
    DETAIL,
    PAGE,
    PUNCH,
    R7TaskError,
    R7VehicleClient,
)
from service_v2_plugins._shared.boyi_plugin_sdk import BrokerCallResult
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_automation_plugin_connector_runtime_v2 import _AccountResolver


def detail(number="RH_TEST_1", status=55):
    day = date_range()["end_time"][:10]
    return {
        "id": "task_" + number,
        "taskNumber": number,
        "taskStatus": status,
        "headPlanGoTime": day + " 06:30:00",
        "taskReleaseTime": "2000-01-01 00:00:00",
        "departureStationCode": "origin",
        "arrivalStationCode": "destination",
        "headAmount": 1,
        "trunkAmount": None,
        "taskViaList": [
            {"id": "via_start", "viaStationCode": "origin"},
            {
                "id": "via_end",
                "viaStationCode": "destination",
                "carHeadRealArriveTimeDriver": day + " 10:00:00",
                "carHeadRealArriveTimeCenter": None,
            },
        ],
    }


class BusinessServer:
    def __init__(self, statuses=(55,), *, invalid_readback=False, timeout=False):
        self.rows = [detail("RH_TEST_" + str(i), status) for i, status in enumerate(statuses)]
        self.calls = []
        self.invalid_readback = invalid_readback
        self.timeout = timeout

    def request(self, name, body):
        self.calls.append((name, deepcopy(body)))
        if name == AUTH:
            data = {"siteCode": "destination", "siteTypeCode": 110}
        elif name == PAGE:
            assert body["publishStatus_CondList"] == ["20"]
            page = body["currentPage"]
            data = {
                "currentPage": page,
                "pageSize": 200,
                "total": len(self.rows),
                "data": self.rows[(page - 1) * 200 : page * 200],
            }
        elif name == PUNCH:
            assert body["clockType"] == 2 and body["restockTag"] is False
            assert body["changeReason"] == "" and body["amount"] == [1, None]
            assert body["taskViaList"][1]["carHeadRealArriveTimeCenter"] == body["clockTime"]
            if self.timeout:
                raise RuntimeError("WRITE_OUTCOME_UNKNOWN")
            row = next(row for row in self.rows if row["id"] == body["id"])
            row["taskViaList"][1]["carHeadRealArriveTimeCenter"] = date_range()["end_time"][:10] + " 11:01:07"
            row["taskStatus"] = 58
            data = row
        elif name == DETAIL:
            data = deepcopy(next(row for row in self.rows if row["id"] == body["id"]))
            if self.invalid_readback and data["taskStatus"] == 58:
                data["taskViaList"][1]["carHeadRealArriveTimeCenter"] = None
        else:
            raise AssertionError(name)
        return {"status_code": 200, "response": {"code": 200, "data": deepcopy(data)}}

    def broker(self, operation, *, action, role, arguments):
        assert operation == "http.request" and role == "r7_operator"
        assert action == ("write_json" if arguments["request"] == PUNCH else "read_json")
        data = self.request(arguments["request"], arguments["body"])
        return BrokerCallResult(data, host_evidence_ref=f"host-{len(self.calls)}")


def test_plugin_query_and_independent_server_time_readback():
    server = BusinessServer()
    client = R7VehicleClient(server.broker)
    assert client.read_page(**date_range(), page=1)["total"] == 1
    result = client.arrive(**date_range(), task_id="task_RH_TEST_0", task_number="RH_TEST_0")
    assert result["confirmed"] and result["arrival_time"].endswith("11:01:07")
    assert [name for name, _ in server.calls] == [PAGE, AUTH, DETAIL, PUNCH, DETAIL]
    assert len(client.proofs) == 1


@pytest.mark.parametrize("failure", ["readback", "timeout"])
def test_unknown_write_never_retries(failure):
    server = BusinessServer(invalid_readback=failure == "readback", timeout=failure == "timeout")
    with pytest.raises(RuntimeError, match="WRITE_OUTCOME_UNKNOWN"):
        R7VehicleClient(server.broker).arrive(**date_range(), task_id="task_RH_TEST_0", task_number="RH_TEST_0")
    assert sum(name == PUNCH for name, _ in server.calls) == 1


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("carHeadRealArriveTimeDriver", None, "R7_DRIVER_ARRIVAL_CONFIRMATION_REQUIRED"),
        ("carHeadRealArriveTimeCenter", "2026-10-08 10:10:00", "R7_EXISTING_PUNCH_CONFIRMATION_REQUIRED"),
    ],
)
def test_extra_confirmation_boundaries_prevent_write(field, value, code):
    server = BusinessServer()
    server.rows[0]["taskViaList"][1][field] = value
    with pytest.raises(R7TaskError, match=code):
        R7VehicleClient(server.broker).arrive(**date_range(), task_id="task_RH_TEST_0", task_number="RH_TEST_0")
    assert all(name != PUNCH for name, _ in server.calls)


def test_status_changed_after_query_stops_before_submission():
    server = BusinessServer([58])
    with pytest.raises(R7TaskError, match="R7_TASK_CHANGED"):
        R7VehicleClient(server.broker).arrive(**date_range(), task_id="task_RH_TEST_0", task_number="RH_TEST_0")
    assert all(name != PUNCH for name, _ in server.calls)


def test_dates_use_three_shanghai_calendar_days_at_utc_boundary():
    assert date_range(datetime(2026, 10, 7, 17, tzinfo=timezone.utc)) == {
        "start_time": "2026-10-06 00:00:00",
        "end_time": "2026-10-08 23:59:59",
    }


def test_all_pages_complete_before_writes_and_only_status_55():
    server = BusinessServer([55] + [58] * 199 + [90])
    result = run(server.broker)
    assert result["status"] == "SUCCESS" and result["data"]["checked_in"] == 1
    assert [name for name, _ in server.calls[:2]] == [PAGE, PAGE]
    assert sum(name == PUNCH for name, _ in server.calls) == 1


@pytest.mark.parametrize("changed", ["duplicate", "total", "incomplete"])
def test_bad_pagination_fails_before_writes(changed):
    server = BusinessServer([55] * 201)
    if changed == "duplicate":
        server.rows[200] = deepcopy(server.rows[0])

    def broker(operation, **kwargs):
        result = server.broker(operation, **kwargs)
        if kwargs["arguments"]["request"] == PAGE and kwargs["arguments"]["body"]["currentPage"] == 2:
            if changed == "total":
                result["response"]["data"]["total"] = 202
            elif changed == "incomplete":
                result["response"]["data"]["data"] = []
        return result

    result = run(broker)
    assert result["status"] == "FAILED" and result["meta"]["write_outcome"] == "NOT_APPLIED"
    assert all(name != PUNCH for name, _ in server.calls)


def packaged_host(tmp_path, monkeypatch, statuses, *, unknown=False, account="selected-r7"):
    monkeypatch.setattr("tests.service_v2_production_protocol_support.build_plugin_zip", build_service_v2_package)
    server = BusinessServer(statuses, timeout=unknown)
    accounts = []

    class Transport:
        def __init__(self, *, system, account_id):
            assert system == "r7" and account_id == account
            accounts.append(account_id)

        def exchange(self, declaration, body, *, mark_write_started):
            if declaration["action"] == "write_json":
                mark_write_started()
            try:
                return server.request(declaration["name"], body)
            except RuntimeError as exc:
                raise PluginExecutionError("test write timeout", code=str(exc)) from exc

        def close(self):
            pass

    monkeypatch.setattr("agent.automation_plugins.account_http.AccountHTTPTransport", Transport)
    context = CoreBrokerInvocationContext(
        automation_id="isolated-r7",
        plugin_version="1.1.0",
        tool_name="r7_vehicle_checkin_v2",
        operation="http.request",
        action="write_json",
        role="r7_operator",
        account_bindings={"r7_operator": (account,)},
    )
    host = PackagedConnectorHost(
        tmp_path, "r7_vehicle_checkin_v2", ConnectorRegistry(), context, account_resolver=_AccountResolver(system="r7")
    )
    return host, server, accounts


@pytest.mark.parametrize("entrypoint", ["console", "scheduler", "feishu"])
def test_real_zip_account_binding_entrypoints_and_write_evidence(tmp_path, monkeypatch, entrypoint):
    host, server, accounts = packaged_host(tmp_path, monkeypatch, [40, 55, 60, 90], account="chosen-r7-second")
    result = host.execute({}, operation="run", entrypoint=entrypoint)
    assert result["status"] == "SUCCESS", result
    assert result["data"]["checked_in"] == 1 and len(host.receipts) == 1
    assert len(accounts) == 5 and sum(name == PUNCH for name, _ in server.calls) == 1
    assert len(result["meta"]["http_write_verifications"]) == 1


@pytest.mark.parametrize("statuses", [[], [40, 58, 60, 90]])
def test_real_zip_zero_candidates_is_verified_no_write(tmp_path, monkeypatch, statuses):
    host, server, _ = packaged_host(tmp_path, monkeypatch, statuses)
    result = host.execute({}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert result["meta"]["write_outcome"] == "NOT_APPLIED" and not host.receipts
    assert all(name != PUNCH for name, _ in server.calls)


def test_real_zip_unknown_write_stops_remaining_candidates(tmp_path, monkeypatch):
    host, server, _ = packaged_host(tmp_path, monkeypatch, [55, 55], unknown=True)
    result = host.execute({}, operation="run")
    assert result["status"] == "FAILED" and result["meta"]["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert len(host.receipts) == 1 and sum(name == PUNCH for name, _ in server.calls) == 1
    assert result["data"]["failed_task"] == {"task_id": "task_RH_TEST_0", "task_number": "RH_TEST_0"}
    assert result["meta"]["pagination_complete"] is True
    assert result["error"]["code"] == "WRITE_OUTCOME_UNKNOWN"
