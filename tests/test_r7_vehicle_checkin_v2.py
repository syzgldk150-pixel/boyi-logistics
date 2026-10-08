"""Original-page-shaped HTTP fixtures and a real isolated ZIP/Broker run."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.developer_v2 import build_service_v2_package
from agent.automation_plugins.r7_vehicle_connectors_v2 import build_r7_vehicle_connectors
from agent.tms_runtime.scripts.r7_vehicle_tasks import AUTH, DETAIL, PAGE, PUNCH, R7TaskError, R7VehicleClient
from agent.tms_runtime.sso_session_persistence import default_sso_state_path
from service_v2_plugins.r7_vehicle_checkin_v2.payload.action import date_range, run
from service_v2_plugins._shared.boyi_plugin_sdk import BrokerCallResult
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_automation_plugin_connector_runtime_v2 import _AccountResolver

INTERVAL = {"start_time": "2026-10-06 00:00:00", "end_time": "2026-10-08 23:59:59"}


def detail(number="RH_TEST_1", status=55):
    return {
        "id": "task_" + number,
        "taskNumber": number,
        "taskStatus": status,
        "headPlanGoTime": "2026-10-08 06:30:00",
        "taskReleaseTime": "2026-10-01 00:00:00",
        "departureStationCode": "origin",
        "arrivalStationCode": "destination",
        "headAmount": 1,
        "trunkAmount": None,
        "taskViaList": [
            {"id": "via_start", "viaStationCode": "origin"},
            {
                "id": "via_end",
                "viaStationCode": "destination",
                "carHeadRealArriveTimeDriver": "2026-10-08 10:00:00",
                "carHeadRealArriveTimeCenter": None,
            },
        ],
    }


class Session:
    def __init__(self, *, invalid_readback=False, timeout=False, row=None):
        self.row = deepcopy(row or detail())
        self.calls = []
        self.invalid_readback = invalid_readback
        self.timeout = timeout

    def post(self, url, *, json, timeout, allow_redirects):
        path = url.removeprefix("https://r7.ronghuiwl.com")
        self.calls.append((path, deepcopy(json)))
        assert timeout == (10, 25) and allow_redirects is False
        if path == AUTH:
            data = {"siteCode": "destination", "siteTypeCode": 110}
        elif path == PAGE:
            assert json["headPlanGoTime_CondStart"] == INTERVAL["start_time"]
            assert json["publishStatus_CondList"] == ["20"]
            data = {"currentPage": 1, "pageSize": 200, "total": 1, "data": [self.row]}
        elif path == PUNCH:
            assert json["clockType"] == 2 and json["restockTag"] is False
            assert json["changeReason"] == "" and json["amount"] == [1, None]
            assert json["taskViaList"][1]["carHeadRealArriveTimeCenter"] == json["clockTime"]
            if self.timeout:
                raise requests.Timeout()
            self.row["taskViaList"][1]["carHeadRealArriveTimeCenter"] = "2026-10-08 11:01:07"
            self.row["taskStatus"] = 58
            data = deepcopy(self.row)
        elif path == DETAIL:
            data = deepcopy(self.row)
            if self.invalid_readback and self.row["taskStatus"] == 58:
                data["taskViaList"][1]["carHeadRealArriveTimeCenter"] = None
        else:
            raise AssertionError(path)
        return SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": deepcopy(data)})

    def close(self):
        pass


def test_http_query_and_independent_server_time_readback():
    session = Session()
    client = R7VehicleClient(session)
    page = client.read_page(**INTERVAL, page=1)
    assert page["complete"] and page["total"] == 1
    result = client.arrive(**INTERVAL, task_id="task_RH_TEST_1", task_number="RH_TEST_1")
    assert result["confirmed"] and result["arrival_time"] == "2026-10-08 11:01:07"
    assert [p for p, _ in session.calls] == [PAGE, AUTH, DETAIL, PUNCH, DETAIL]


@pytest.mark.parametrize("restored", [False, True])
def test_saved_login_uses_only_selected_account_without_fresh_login(monkeypatch, restored):
    closed = []
    session = SimpleNamespace(headers={}, close=lambda: closed.append(True))

    class Auth:
        def __init__(self, *, config_path, state_path):
            assert config_path == ""
            assert state_path == default_sso_state_path("selected-r7-second")
            self.session = session
            self.last_token = "fixture-session-token"

        def _verify_authenticated(self):
            raise AssertionError("not an extra network login check")

        def restore_persisted_session(self, *, validate, validator, attach_bearer):
            assert not validate and not attach_bearer
            return restored

    monkeypatch.setattr("agent.tms_runtime.scripts.r7_vehicle_tasks.R7SSOAuth", Auth)
    if restored:
        client = R7VehicleClient.from_account("selected-r7-second")
        assert client.session is session and "Authorization" not in session.headers
        client.close()
    else:
        with pytest.raises(R7TaskError, match="BLOCKED_LOGIN"):
            R7VehicleClient.from_account("selected-r7-second")
    assert closed == [True]


def test_status_changed_after_query_stops_before_submission():
    session = Session(row=detail(status=58))
    with pytest.raises(R7TaskError, match="R7_TASK_CHANGED"):
        R7VehicleClient(session).arrive(**INTERVAL, task_id="task_RH_TEST_1", task_number="RH_TEST_1")
    assert all(path != PUNCH for path, _ in session.calls)


@pytest.mark.parametrize("failure", ["readback", "timeout"])
def test_unknown_write_never_retries(failure):
    session = Session(invalid_readback=failure == "readback", timeout=failure == "timeout")
    with pytest.raises(R7TaskError, match="WRITE_OUTCOME_UNKNOWN"):
        R7VehicleClient(session).arrive(**INTERVAL, task_id="task_RH_TEST_1", task_number="RH_TEST_1")
    assert sum(path == PUNCH for path, _ in session.calls) == 1


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("carHeadRealArriveTimeDriver", None, "R7_DRIVER_ARRIVAL_CONFIRMATION_REQUIRED"),
        ("carHeadRealArriveTimeCenter", "2026-10-08 10:10:00", "R7_EXISTING_PUNCH_CONFIRMATION_REQUIRED"),
    ],
)
def test_extra_confirmation_boundaries_prevent_write(field, value, code):
    row = detail()
    row["taskViaList"][1][field] = value
    session = Session(row=row)
    with pytest.raises(R7TaskError, match=code):
        R7VehicleClient(session).arrive(**INTERVAL, task_id=row["id"], task_number=row["taskNumber"])
    assert all(path != PUNCH for path, _ in session.calls)


def test_dates_use_three_shanghai_calendar_days_at_utc_boundary():
    assert date_range(datetime(2026, 10, 7, 17, tzinfo=timezone.utc)) == INTERVAL


def test_all_pages_complete_before_writes_and_only_status_55():
    calls = []

    def broker(_, *, action, role, arguments):
        calls.append(action)
        if action == "read_page":
            page = arguments["arguments"]["page"]
            data = {
                "page": page,
                "total": 3,
                "complete": page == 2,
                "items": [
                    {"task_id": "first", "task_number": "N1", "status": 55},
                    {"task_id": "second", "task_number": "N2", "status": 58},
                ]
                if page == 1
                else [{"task_id": "third", "task_number": "N3", "status": 90}],
            }
        else:
            assert arguments["arguments"]["task_id"] == "first"
            data = {
                "task_id": "first",
                "task_number": "N1",
                "confirmed": True,
                "arrival_time": "2026-10-08 11:00:00",
                "status": 58,
            }
        return BrokerCallResult(data, host_evidence_ref=f"host-{len(calls)}")

    result = run(broker)
    assert result["status"] == "SUCCESS" and result["data"]["checked_in"] == 1
    assert calls == ["read_page", "read_page", "arrive"]


@pytest.mark.parametrize("changed", ["duplicate", "total", "incomplete"])
def test_bad_pagination_fails_before_writes(changed):
    def broker(_, *, action, role, arguments):
        assert action == "read_page"
        p = arguments["arguments"]["page"]
        row = {"task_id": "same" if changed == "duplicate" else str(p), "task_number": "N" + str(p), "status": 55}
        data = {
            "page": p,
            "total": 2 if p == 1 or changed != "total" else 3,
            "items": [] if changed == "incomplete" else [row],
            "complete": p == 2,
        }
        return BrokerCallResult(data, host_evidence_ref="host-page" + str(p))

    result = run(broker)
    assert result["status"] == "FAILED" and result["meta"]["write_outcome"] == "NOT_APPLIED"


def packaged_host(tmp_path, monkeypatch, statuses, *, unknown=False, account="selected-r7"):
    monkeypatch.setattr("tests.service_v2_production_protocol_support.build_plugin_zip", build_service_v2_package)
    calls = []

    class Client:
        def __init__(self, bound):
            calls.append(("account", bound))
            assert bound == account

        def read_page(self, **args):
            return {
                "page": 1,
                "total": len(statuses),
                "complete": True,
                "items": [
                    {
                        "task_id": "task_" + str(i),
                        "task_number": "RH_TEST_" + str(i),
                        "status": status,
                        "planned_departure": args["end_time"],
                    }
                    for i, status in enumerate(statuses)
                ],
            }

        def arrive(self, **args):
            calls.append(("write", args["task_id"]))
            if unknown:
                raise R7TaskError("WRITE_OUTCOME_UNKNOWN")
            return {
                "task_id": args["task_id"],
                "task_number": args["task_number"],
                "confirmed": True,
                "arrival_time": "2026-10-08 11:00:00",
                "status": 58,
            }

        def close(self):
            pass

    registry = ConnectorRegistry(build_r7_vehicle_connectors(Client))
    context = CoreBrokerInvocationContext(
        automation_id="isolated-r7",
        plugin_version="1.0.0",
        tool_name="r7_vehicle_checkin_v2",
        operation="service.invoke",
        action="run",
        role="__system__",
        account_bindings={"r7_operator": (account,)},
    )
    return PackagedConnectorHost(
        tmp_path, "r7_vehicle_checkin_v2", registry, context, account_resolver=_AccountResolver(system="r7")
    ), calls


@pytest.mark.parametrize("entrypoint", ["console", "scheduler", "feishu"])
def test_real_zip_account_binding_entrypoints_and_write_evidence(tmp_path, monkeypatch, entrypoint):
    host, calls = packaged_host(tmp_path, monkeypatch, [40, 55, 60, 90], account="chosen-r7-second")
    result = host.execute({}, operation="run", entrypoint=entrypoint)
    assert result["status"] == "SUCCESS", result
    assert result["data"]["checked_in"] == 1 and len(host.receipts) == 1
    assert [c for c in calls if c[0] == "write"] == [("write", "task_1")]


@pytest.mark.parametrize("statuses", [[], [40, 58, 60, 90]])
def test_real_zip_zero_candidates_is_verified_no_write(tmp_path, monkeypatch, statuses):
    host, calls = packaged_host(tmp_path, monkeypatch, statuses)
    result = host.execute({}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert result["meta"]["write_outcome"] == "NOT_APPLIED" and not host.receipts
    assert all(c[0] != "write" for c in calls)


def test_real_zip_unknown_write_stops_remaining_candidates(tmp_path, monkeypatch):
    host, calls = packaged_host(tmp_path, monkeypatch, [55, 55], unknown=True)
    result = host.execute({}, operation="run")
    assert result["status"] == "FAILED" and result["meta"]["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert len(host.receipts) == 1 and sum(c[0] == "write" for c in calls) == 1
    assert result["data"]["failed_task"] == {"task_id": "task_0", "task_number": "RH_TEST_0"}
    assert result["meta"]["pagination_complete"] is True
    assert result["error"]["code"] == "WRITE_OUTCOME_UNKNOWN"
