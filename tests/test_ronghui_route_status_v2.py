"""Ronghui in-transit route refresh: plugin wording, Host projection port and real ZIP."""

from copy import deepcopy

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.developer_v2 import build_service_v2_package
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.route_status_connectors_v2 import build_route_status_connectors
from agent.automation_plugins.route_status_handlers import (
    PENDING_ACTION, ROLE, TOOL, UPDATE_ACTION, RouteStatusHandlerPorts, build_route_status_handler_map,
)
from service_v2_plugins._shared.boyi_plugin_sdk import BrokerCallResult
from service_v2_plugins.sync_ronghui_route_status_v2.payload.action import latest_route, route_text, run
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_automation_plugin_connector_runtime_v2 import _AccountResolver

MAIN_A = "RH0000000001"
MAIN_B = "RH0000000002"


def scan(code, scan_type, site, when, following=""):
    return {"BILL_CODE": code, "SCAN_TYPE": scan_type, "SCAN_SITE": site, "SCAN_DATE": when,
            "PRE_OR_NEXT_STATION": following}


@pytest.mark.parametrize("scan_type,following,expected", [
    ("装车", "天津分拨", "快件在长沙分拨已发出，发往天津分拨"),
    ("到件", "", "快件已到达长沙分拨"),
    ("派件", "", "快件在长沙分拨派送中"),
    ("签收", "", "快件已在长沙分拨签收"),
    ("收件", "", "快件已在长沙分拨揽收"),
    ("留仓", "", "快件在长沙分拨留仓"),
])
def test_route_wording(scan_type, following, expected):
    assert route_text(scan("x", scan_type, "长沙分拨", "2026-10-15 01:00:00", following)) == expected


def test_latest_scan_includes_children_but_only_main_signature_signs():
    rows = [
        scan(MAIN_A, "装车", "邵阳", "2026-10-14 20:00:00", "长沙分拨"),
        scan(MAIN_A + "0001", "到件", "长沙分拨", "2026-10-15 02:00:00"),
        scan(MAIN_B, "到件", "天津分拨", "2026-10-15 03:00:00"),
        scan(MAIN_A + "0002", "签收", "长沙分拨", "2026-10-15 01:00:00"),
    ]
    assert latest_route(rows, MAIN_A) == ("快件已到达长沙分拨", False)
    assert latest_route(rows + [scan(MAIN_A, "签收", "长沙", "2026-10-15 09:00:00")], MAIN_A) == ("快件已在长沙签收", True)
    assert latest_route(rows, "RH0000000009") is None
    assert latest_route([scan(MAIN_A, "到件", "长沙", "")], MAIN_A) is None


class Fake:
    """Projection rows and Ronghui scans behind the plugin's two broker calls."""

    def __init__(self, pending, scans, *, too_large=False, login=False):
        self.pending = pending
        self.scans = scans
        self.too_large = too_large
        self.login = login
        self.track_calls = []
        self.updates = []

    def projection(self, name, arguments):
        if name == "list_pending":
            assert arguments == {"limit": 2000}
            return {"items": deepcopy(self.pending), "truncated": False}
        assert name == "update_routes"
        self.updates.append(deepcopy(arguments["items"]))
        return {"committed": True, "record_count": len(arguments["items"]), "updated": len(arguments["items"])}

    def track(self, codes):
        self.track_calls.append(list(codes))
        if self.login:
            raise RuntimeError("BLOCKED_LOGIN")
        if self.too_large and len(codes) > 1:
            raise RuntimeError("HTTP_RESPONSE_TOO_LARGE")
        return [row for row in self.scans if any(row["BILL_CODE"].startswith(code) for code in codes)]

    def broker(self, operation, *, action, role, arguments):
        if operation == "service.invoke":
            assert role == "__system__" and arguments["service"] == "connector.boyi.ronghui_route_projection@1"
            data = self.projection(arguments["operation"], arguments["arguments"])
        else:
            assert (operation, action, role) == ("http.request", "read_json", "route_source")
            assert arguments["request"] == "track_rows"
            data = {"status_code": 200, "response": self.track(arguments["body"]["BILL_CODE"].split(","))}
        return BrokerCallResult(data, host_evidence_ref=f"host-{len(self.track_calls)}-{len(self.updates)}-{operation}")


def pending(*codes, text=""):
    return [{"waybill_no": code, "scan_status": text} for code in codes]


def test_batches_skip_unchanged_and_write_only_changes():
    codes = [f"RH00000000{i:02d}" for i in range(1, 8)]
    scans = [scan(code, "到件", "长沙分拨", "2026-10-15 02:00:00") for code in codes[:6]]
    scans.append(scan(codes[1], "签收", "邵阳", "2026-10-15 05:00:00"))
    items = pending(*codes)
    items[2]["scan_status"] = "快件已到达长沙分拨"
    fake = Fake(items, scans)
    result = run(fake.broker)
    assert result["status"] == "SUCCESS", result
    assert [len(call) for call in fake.track_calls] == [5, 2]
    assert result["data"] | {} == result["data"] and {k: result["data"][k] for k in
        ("checked", "updated", "signed", "unchanged", "no_track", "requests")} == {
        "checked": 7, "updated": 5, "signed": 1, "unchanged": 1, "no_track": 1, "requests": 2}
    written = {item["waybill_no"]: item for item in fake.updates[0]}
    assert written[codes[1]] == {"waybill_no": codes[1], "scan_status": "快件已在邵阳签收", "signed": True}
    assert codes[2] not in written and codes[6] not in written


def test_nothing_changed_writes_nothing():
    fake = Fake(pending(MAIN_A, text="快件已到达长沙"), [scan(MAIN_A, "到件", "长沙", "2026-10-15 02:00:00")])
    result = run(fake.broker)
    assert result["status"] == "SUCCESS" and not fake.updates and result["data"]["unchanged"] == 1


def test_oversized_batch_falls_back_to_single_waybills():
    fake = Fake(pending(MAIN_A, MAIN_B), [scan(MAIN_A, "到件", "长沙", "2026-10-15 02:00:00"),
                                          scan(MAIN_B, "到件", "天津", "2026-10-15 02:00:00")], too_large=True)
    result = run(fake.broker)
    assert result["status"] == "SUCCESS" and fake.track_calls == [[MAIN_A, MAIN_B], [MAIN_A], [MAIN_B]]
    assert len(fake.updates[0]) == 2


def test_login_failure_stops_before_any_write():
    fake = Fake(pending(MAIN_A), [], login=True)
    result = run(fake.broker)
    assert result["status"] == "FAILED" and result["error"]["code"] == "BLOCKED_LOGIN" and not fake.updates


def _context(action):
    return CoreBrokerInvocationContext(automation_id="route", plugin_version="1.0.0", tool_name=TOOL,
                                       operation=action[0], action=action[1], role=ROLE)


def test_host_handlers_validate_arguments_and_require_fresh_readback():
    calls = []
    verified = {"value": True}
    ports = RouteStatusHandlerPorts(
        list_pending=lambda limit: [{"waybill_no": MAIN_A, "scan_status": None}],
        update_routes=lambda items, mark: calls.append(items) or {
            "ok": True, "verified": verified["value"], "updated": len(items), "record_count": len(items)},
    )
    handlers = build_route_status_handler_map(ports, cursor_secret=b"s" * 32)
    listed = handlers[PENDING_ACTION](_context(PENDING_ACTION), {"limit": 10})
    assert listed["items"] == [{"waybill_no": MAIN_A, "scan_status": ""}] and listed["evidence_ref"]
    item = {"waybill_no": MAIN_A, "scan_status": "快件已到达长沙", "signed": False}
    assert handlers[UPDATE_ACTION](_context(UPDATE_ACTION), {"items": [item]})["updated"] == 1
    for bad in ({"items": []}, {"items": [{**item, "scan_status": ""}]}, {"items": [item, item]},
                {"items": [{**item, "signed": "no"}]}, {"items": [{**item, "scan_status": "x" * 129}]}):
        with pytest.raises(PluginExecutionError, match="invalid"):
            handlers[UPDATE_ACTION](_context(UPDATE_ACTION), bad)
    with pytest.raises(PluginExecutionError, match="context"):
        handlers[UPDATE_ACTION](_context(PENDING_ACTION), {"items": [item]})
    verified["value"] = False
    with pytest.raises(PluginExecutionError) as error:
        handlers[UPDATE_ACTION](_context(UPDATE_ACTION), {"items": [item]})
    assert error.value.code == "WRITE_OUTCOME_UNKNOWN" and len(calls) == 2


class ProjectionStore:
    def __init__(self, rows):
        self.rows = rows

    def list_pending(self, limit):
        return [dict(row) for row in self.rows if row["status"] not in {"signed", "cancelled"}][:limit]

    def update(self, items, _mark):
        for item in items:
            for row in self.rows:
                if row["waybill_no"] == item["waybill_no"] and row["status"] not in {"signed", "cancelled"}:
                    row["scan_status"] = item["scan_status"]
                    row["status"] = "signed" if item["signed"] else row["status"]
        return {"ok": True, "verified": True, "updated": len(items), "record_count": len(items)}


def packaged_host(tmp_path, monkeypatch, store, scans):
    monkeypatch.setattr("tests.service_v2_production_protocol_support.build_plugin_zip", build_service_v2_package)
    account = "selected-ronghui"
    track_calls = []

    class Transport:
        def __init__(self, *, system, account_id):
            assert system == "ronghui" and account_id == account

        def exchange(self, declaration, body, *, mark_write_started):
            assert declaration["action"] == "read_json" and declaration["query"] == {"id": "FIND_SACN_TRACK_BY_CODE_MAIN"}
            codes = body["BILL_CODE"].split(",")
            track_calls.append(codes)
            return {"status_code": 200, "response": [row for row in scans if row["BILL_CODE"][:12] in codes]}

        def close(self):
            pass

    monkeypatch.setattr("agent.automation_plugins.account_http.AccountHTTPTransport", Transport)
    reviewed = build_route_status_handler_map(
        RouteStatusHandlerPorts(list_pending=store.list_pending, update_routes=store.update), cursor_secret=b"k" * 32)
    context = CoreBrokerInvocationContext(
        automation_id="isolated-route", plugin_version="1.0.0", tool_name="sync_ronghui_route_status_v2",
        operation="http.request", action="read_json", role="route_source",
        account_bindings={"route_source": (account,)},
    )
    host = PackagedConnectorHost(tmp_path, "sync_ronghui_route_status_v2",
                                 ConnectorRegistry(build_route_status_connectors(reviewed)), context,
                                 account_resolver=_AccountResolver(system="ronghui"))
    return host, track_calls


@pytest.mark.parametrize("entrypoint", ["console", "scheduler"])
def test_real_zip_refreshes_unsigned_rows_and_marks_signed(tmp_path, monkeypatch, entrypoint):
    store = ProjectionStore([
        {"waybill_no": MAIN_A, "scan_status": "", "status": "in_transit"},
        {"waybill_no": MAIN_B, "scan_status": "", "status": "in_transit"},
        {"waybill_no": "RH0000000003", "scan_status": "快件已在邵阳签收", "status": "signed"},
    ])
    scans = [scan(MAIN_A, "装车", "长沙分拨", "2026-10-15 02:27:27", "天津分拨"),
             scan(MAIN_B, "签收", "邵阳", "2026-10-15 05:00:00")]
    host, track_calls = packaged_host(tmp_path, monkeypatch, store, scans)
    result = host.execute({}, operation="run", entrypoint=entrypoint)
    assert result["status"] == "SUCCESS", result
    assert result["meta"]["write_outcome"] == "WRITE_VERIFIED" and len(host.receipts) == 1
    assert track_calls == [[MAIN_A, MAIN_B]]
    assert store.rows[0] == {"waybill_no": MAIN_A, "scan_status": "快件在长沙分拨已发出，发往天津分拨", "status": "in_transit"}
    assert store.rows[1]["status"] == "signed"


def test_real_zip_without_changes_is_not_applied(tmp_path, monkeypatch):
    store = ProjectionStore([{"waybill_no": MAIN_A, "scan_status": "快件已到达长沙", "status": "in_transit"}])
    host, _ = packaged_host(tmp_path, monkeypatch, store, [scan(MAIN_A, "到件", "长沙", "2026-10-15 02:00:00")])
    result = host.execute({}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert result["meta"]["write_outcome"] == "NOT_APPLIED" and not host.receipts
