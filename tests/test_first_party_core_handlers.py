from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from agent.automation_plugins import first_party_handlers as handler_module
from agent.automation_plugins.broker import BrokerGrant, VERIFIED_WRITE_NOOP_FIELD
from agent.automation_plugins.core_adapter import (
    AccountManagerSessionResolver,
    CoreBrokerInvocationContext,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party_handlers import (
    FirstPartyCoreHandlerPorts,
    build_first_party_core_handler_map,
)
from agent.tms_runtime.errors import TMSAuthStateError
from plugin_core_adapters.first_party import (
    build_production_first_party_core_handler_map,
)


_SECRET = b"first-party-handler-tests-secret-value"
_ARRIVE_FIELDS = (
    "tracking_number",
    "goods_name",
    "package_type",
    "delivery_method",
    "quantity",
    "receipt_number",
    "actual_weight",
    "volume",
    "remarks",
    "destination_station",
    "recipient_name",
    "recipient_phone",
    "recipient_address",
    "settlement_weight",
    "volumetric_weight",
    "shipping_fee",
    "payment_type",
    "pay_on_arrival",
)


class _Manager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def require_authenticated_binding(self, account_id: str) -> dict[str, str]:
        self.calls.append(account_id)
        systems = {
            "customer-rh": "ronghui",
            "customer-yd": "yunda",
            "arrive-rh": "ronghui",
            "clock-rh": "ronghui",
            "yunda-a": "yunda",
        }
        return {
            "account_id": account_id,
            "system": systems[account_id],
            "account_purpose": "customer_service",
            "session_profile": f"profile-{account_id}",
        }


def _context(
    *,
    tool_name: str,
    role: str,
    account_ids: tuple[str, ...],
    action: str,
    operation: str = "browser.invoke",
    resource_id: str | None = None,
    mark_write_started: Any = None,
) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id=f"instance-{tool_name}",
        plugin_version="1.0.0",
        tool_name=tool_name,
        operation=operation,
        action=action,
        role=role,
        account_ids=account_ids,
        resource_id=resource_id,
        mark_write_started=mark_write_started,
    )


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _arrive_record(code: str) -> dict[str, Any]:
    result = {field: "" for field in _ARRIVE_FIELDS}
    result.update(
        {
            "tracking_number": code,
            "quantity": 1,
            "actual_weight": "1.00",
            "volume": "0.001",
            "settlement_weight": "1.00",
            "volumetric_weight": "0.00",
            "shipping_fee": "0.00",
            "pay_on_arrival": "0.00",
        }
    )
    return result


def _arrival_stats_record(code: str) -> dict[str, Any]:
    return {**_arrive_record(code), "arrived_quantity": 1}


def _yunda_dispatch_record() -> dict[str, Any]:
    """Keep the signed field names code-owned; do not duplicate mojibake literals."""
    fields = handler_module._YUNDA_DISPATCH_FIELDS
    values: tuple[object, ...] = (
        "YD-MAIN-1",
        1,
        1,
        "1.00",
        "0.001",
        "package",
        "",
        24,
        "destination",
        "",
        "2026-08-16 12:00:00",
    )
    return dict(zip(fields, values, strict=True))


def _yunda_dispatch_source_record() -> dict[str, Any]:
    """Return the source-reviewed Yunda response fields before sink mapping."""
    return {
        "ship_id": "YD-MAIN-1",
        "unit_cnt": 1,
        "scan_cnt": 1,
        "frgt_wgt": "1.00",
        "frgt_vol": "0.001",
        "pkg_lod_typ": "package",
        "fld_tm": "",
        "plan_tlns": 24,
        "rcv_cust_addr": "destination",
        "est_arv_tm": "",
        "due_delv_dt": "2026-08-16 12:00:00",
    }


def test_production_map_registers_only_available_closed_primitives() -> None:
    handlers = build_production_first_party_core_handler_map(
        account_manager=_Manager(),
        cursor_secret=_SECRET,
    )
    assert set(handlers) == {
        ("ledger.invoke", "daily_sign.authoritative_sync"),
        ("ledger.invoke", "sync_daily_send_orders.lock.acquire"),
        ("ledger.invoke", "sync_daily_send_orders.lock.release"),
        ("ledger.invoke", "finance.batch.acquire"),
        ("ledger.invoke", "finance.source_snapshot.write"),
        ("ledger.invoke", "finance.projection.commit"),
        ("ledger.invoke", "daily_sign.problem_event.upsert"),
        ("browser.invoke", "ronghui.clock.precheck"),
        ("browser.invoke", "ronghui.clock.submit"),
        ("browser.invoke", "ronghui.clock.verify"),
        ("browser.invoke", "customer_problem.list_page"),
        ("browser.invoke", "customer_problem.detail"),
        ("browser.invoke", "ronghui.arrive_list.read_page"),
        ("browser.invoke", "ronghui.scan.read_page"),
        ("browser.invoke", "ronghui.site_send.read_page"),
        ("browser.invoke", "ronghui.scan_next.submit"),
        ("browser.invoke", "ronghui.scan_next.verify"),
        ("browser.invoke", "ronghui.waybill_detail.read"),
        ("browser.invoke", "ronghui.delivery_status.read"),
        ("browser.invoke", "ronghui.send_order.read_page"),
        ("browser.invoke", "ronghui.finance.capture_page"),
        ("browser.invoke", "ronghui.finance.verify_source_totals"),
        ("browser.invoke", "ronghui.problem.query"),
        ("browser.invoke", "ronghui.problem.create"),
        ("browser.invoke", "ronghui.problem.verify"),
        ("browser.invoke", "ronghui.complaint.query"),
        ("browser.invoke", "ronghui.complaint.create"),
        ("browser.invoke", "ronghui.complaint.verify"),
        ("projection.invoke", "waybill.snapshot.replace"),
        ("projection.invoke", "arrival.forecast_snapshot.replace"),
        ("projection.invoke", "scan.snapshot.replace"),
        ("projection.invoke", "scan.snapshot.read"),
        ("projection.invoke", "scan.snapshot.cleanup"),
        ("projection.invoke", "arrival.snapshot.completed_before"),
        ("projection.invoke", "waybill.pending.read"),
        ("projection.invoke", "arrival.snapshot.replace"),
        ("projection.invoke", "split_pending.snapshot.refresh"),
        ("projection.invoke", "split_pending.snapshot.read"),
        ("projection.invoke", "split_pending.snapshot.replace"),
        ("projection.invoke", "split_pending.result.upsert"),
        ("network.request", "feishu.sheet.replace"),
        ("network.request", "feishu.sheet.read_rows"),
        ("network.request", "feishu.sheet.replace_rows"),
        ("network.request", "feishu.sheet.add"),
        ("network.request", "feishu.bitable.list_views"),
        ("network.request", "feishu.bitable.list_records"),
        ("network.request", "feishu.bitable.write_records"),
        ("network.request", "feishu.bitable.replace_snapshot"),
        ("browser.invoke", "yunda.dispatch_forecast.read_page"),
        ("browser.invoke", "yunda.send_waybill.list_page"),
        ("browser.invoke", "yunda.special_line.list_page"),
        ("browser.invoke", "yunda.waybill.tracking_detail"),
        ("browser.invoke", "yunda.waybill.original_data"),
        ("browser.invoke", "yunda.send_waybill.renderer_detail"),
        (
            "network.request",
            "feishu.bitable.append_yunda_dispatch_forecast",
        ),
        (
            "network.request",
            "feishu.bitable.replace_yunda_send_waybills_date",
        ),
        ("network.request", "feishu.sheet.replace_yunda_send_waybills"),
        ("projection.invoke", "waybill.yunda.replace_date"),
        ("projection.invoke", "waybill.delivery_status.update"),
        ("projection.invoke", "waybill.ronghui.replace_date"),
        ("network.request", "feishu.bitable.delete_records"),
    }
    assert all(
        "run" not in action and "execute" not in action
        for _operation, action in handlers
    )
    assert ("browser.invoke", "ronghui.child_count.read") not in handlers


def test_production_handler_maps_second_account_check_race_to_login_block() -> None:
    class ExpiredManager(_Manager):
        def require_authenticated_binding(self, account_id: str) -> dict[str, str]:
            raise TMSAuthStateError("AUTH_REQUIRED", "expired")

    handlers = build_production_first_party_core_handler_map(
        account_manager=ExpiredManager(),
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="sync_customer_service_problems",
        role="customer_service_source",
        account_ids=("customer-rh",),
        action="customer_problem.list_page",
    )
    with pytest.raises(PluginExecutionError) as exc:
        handlers[("browser.invoke", "customer_problem.list_page")](
            context,
            {"direction": "received", "cursor": None, "page_size": 200},
        )
    assert exc.value.code == "BLOCKED_LOGIN"


def test_customer_handlers_own_bound_source_pagination_and_opaque_identity() -> None:
    manager = _Manager()
    calls: list[dict[str, Any]] = []

    def customer_action(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        params = dict(arguments)
        calls.append(params)
        if params["action"] == "detail":
            return {
                "ok": True,
                "account_id": params["account_id"],
                "details": [{"status": "已关闭", "session_note": "must be removed"}],
            }
        external = f"{params['platform']}-{params['direction']}"
        return {
            "ok": True,
            "rows": [
                {
                    "platform": params["platform"],
                    "account_id": params["account_id"],
                    "account_label": params["account_id"],
                    "source_direction": params["direction"],
                    "external_id": external,
                    "waybill_no": f"WB-{external}",
                    "status": "待处理",
                }
            ],
            "stats": {
                "total": 1,
                "returned": 1,
                "total_authoritative": True,
            },
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            customer_action=customer_action,
        ),
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="sync_customer_service_problems",
        role="customer_service_source",
        account_ids=("customer-rh", "customer-yd"),
        action="customer_problem.list_page",
    )
    cursor = None
    pages: list[Mapping[str, Any]] = []
    while True:
        page = handlers[("browser.invoke", "customer_problem.list_page")](
            context,
            {"direction": "both", "cursor": cursor, "page_size": 200},
        )
        pages.append(page)
        if page["pagination_complete"] is True:
            break
        cursor = page["next_cursor"]

    assert len(pages) == 4
    assert [call["direction"] for call in calls] == [
        "received",
        "registered",
        "query",
        "published",
    ]
    records = [item for page in pages for item in page["items"]]
    assert len(records) == 4
    assert all(str(item["dedupe_key"]).startswith("problem:v1:") for item in records)
    assert all("customer-rh" not in str(item) and "customer-yd" not in str(item) for item in records)

    detail_context = _context(
        tool_name="sync_customer_service_problems",
        role="customer_service_source",
        account_ids=("customer-rh", "customer-yd"),
        action="customer_problem.detail",
    )
    selected = records[0]
    detail = handlers[("browser.invoke", "customer_problem.detail")](
        detail_context,
        {
            "dedupe_key": selected["dedupe_key"],
            "platform": selected["platform"],
            "source_direction": selected["source_direction"],
            "external_id": selected["external_id"],
            "waybill_no": selected["waybill_no"],
        },
    )
    assert detail["dedupe_key"] == selected["dedupe_key"]
    assert not any(
        key.lower() == "account_id"
        or key.lower().endswith("_account_id")
        or "session" in key.lower()
        for key in _walk_keys(detail)
    )


def test_arrive_handlers_page_and_commit_only_exact_validated_records() -> None:
    manager = _Manager()
    source_calls: list[tuple[str, int, int]] = []
    projection_calls: list[tuple[str, int]] = []
    sheet_calls: list[tuple[str, int, str | None]] = []

    def read_page(descriptor, target_date, page_index, page_size):
        assert descriptor["account_id"] == "arrive-rh"
        source_calls.append((target_date, page_index, page_size))
        rows = [_arrive_record("A-1"), _arrive_record("A-2")] if page_index == 0 else [_arrive_record("A-3")]
        return {
            "items": rows,
            "returned": len(rows),
            "total": 3,
            "total_authoritative": True,
        }

    def replace_waybill(records, target_date):
        projection_calls.append((f"waybill:{target_date}", len(records)))
        return {"ok": True, "verified": True, "record_count": len(records)}

    def replace_forecast(records, target_date):
        projection_calls.append((f"forecast:{target_date}", len(records)))
        return {"ok": True, "verified": True, "rows": len(records)}

    def replace_sheet(resource_id, rows, target_date):
        sheet_calls.append((resource_id, len(rows), target_date))
        return {"ok": True, "verified": True, "record_count": len(rows)}

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            arrive_list_read_page=read_page,
            replace_waybill_snapshot=replace_waybill,
            replace_arrival_forecast_snapshot=replace_forecast,
            replace_arrive_sheet_resource=replace_sheet,
        ),
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="sync_arrive_list",
        role="account_id",
        account_ids=("arrive-rh",),
        action="ronghui.arrive_list.read_page",
    )
    first = handlers[("browser.invoke", "ronghui.arrive_list.read_page")](
        context,
        {"target_date": "2026-08-15", "cursor": None, "page_size": 2},
    )
    assert first["pagination_complete"] is False
    second = handlers[("browser.invoke", "ronghui.arrive_list.read_page")](
        context,
        {"target_date": "2026-08-15", "cursor": first["next_cursor"], "page_size": 2},
    )
    assert second["pagination_complete"] is True
    records = [*first["items"], *second["items"]]

    projection_context = _context(
        tool_name="sync_arrive_list",
        role="account_id",
        account_ids=("arrive-rh",),
        action="waybill.snapshot.replace",
        operation="projection.invoke",
    )
    committed = handlers[("projection.invoke", "waybill.snapshot.replace")](
        projection_context,
        {"records": records, "target_date": "2026-08-15"},
    )
    assert committed["committed"] is True
    forecast = handlers[("projection.invoke", "arrival.forecast_snapshot.replace")](
        projection_context,
        {"records": records, "target_date": "2026-08-15"},
    )
    assert forecast["committed"] is True

    sheet_context = _context(
        tool_name="sync_arrive_list",
        role="arrive_primary_sheet",
        account_ids=("arrive-rh",),
        action="feishu.sheet.replace",
        operation="network.request",
        resource_id="resource-arrive-primary",
    )
    rows = [[record[field] for field in _ARRIVE_FIELDS] for record in records]
    sheet = handlers[("network.request", "feishu.sheet.replace")](
        sheet_context,
        {
            "resource_slot": "arrive_primary_sheet",
            "values": rows,
            "target_date": "2026-08-15",
        },
    )
    assert sheet["committed"] is True
    assert source_calls == [("2026-08-15", 0, 2), ("2026-08-15", 1, 2)]
    assert projection_calls == [
        ("waybill:2026-08-15", 3),
        ("forecast:2026-08-15", 3),
    ]
    assert sheet_calls == [("resource-arrive-primary", 3, "2026-08-15")]


def test_registered_broker_revalidates_accounts_before_real_handler() -> None:
    manager = _Manager()

    def customer_action(arguments):
        return {
            "ok": True,
            "rows": [],
            "stats": {"total": 0, "returned": 0, "total_authoritative": True},
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            customer_action=customer_action,
        ),
        cursor_secret=_SECRET,
    )
    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers=handlers,
        account_resolver=AccountManagerSessionResolver(manager),
    )
    grant = BrokerGrant(
        automation_id="customer-project",
        plugin_version="1.0.0",
        tool_name="sync_customer_service_problems",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        runtime_permissions={
            "broker_operations": [
                {
                    "operation": "browser.invoke",
                    "action": "customer_problem.list_page",
                    "roles": ["customer_service_source"],
                    "effect": "read",
                }
            ]
        },
        account_roles=(
            {
                "role": "customer_service_source",
                "allowed_systems": ["ronghui", "yunda"],
            },
        ),
        resource_roles=(),
        account_bindings={"customer_service_source": ["customer-rh"]},
        resource_bindings={},
    )
    result = asyncio.run(
        adapter.invoke(
            grant=grant,
            operation="browser.invoke",
            action="customer_problem.list_page",
            role="customer_service_source",
            binding=["customer-rh"],
            arguments={"direction": "received", "cursor": None, "page_size": 200},
        )
    )
    assert result["pagination_complete"] is True
    # Generic broker validation and the closed handler independently recheck
    # the same exact binding; neither may substitute another account.
    assert manager.calls == ["customer-rh", "customer-rh"]


def test_clock_stays_fail_closed_when_closed_port_is_absent() -> None:
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=_Manager().require_authenticated_binding,
        ),
        cursor_secret=_SECRET,
    )
    assert ("browser.invoke", "ronghui.clock.precheck") not in handlers
    assert ("browser.invoke", "ronghui.clock.submit") not in handlers
    assert ("browser.invoke", "ronghui.clock.verify") not in handlers


def test_clock_handlers_bind_submit_to_fresh_read_verification() -> None:
    manager = _Manager()
    calls: list[dict[str, Any]] = []

    def clock_action(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        values = dict(arguments)
        calls.append(values)
        if values["action"] == "precheck":
            return {"ready": True}
        if values["action"] == "submit":
            return {"accepted": True, "submitted_at": "2026-08-15 01:02:03"}
        return {
            "confirmed": True,
            "clock_type": values["clock_type"],
            "observed_at": "2026-08-15 01:02:03",
            "record_id": "row-1",
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            clock_action=clock_action,
        ),
        cursor_secret=_SECRET,
    )
    site = {
        "sitecode": "7390004",
        "sitefbcode": "73901",
        "sitename": "site",
        "sitefbname": "yard",
    }
    context = _context(
        tool_name="clock_in_dual",
        role="account_id",
        account_ids=("clock-rh",),
        action="ronghui.clock.precheck",
    )
    assert handlers[("browser.invoke", "ronghui.clock.precheck")](
        context,
        {"site": site, "clock_types": ["交件到港", "接件离港"]},
    )["ready"] is True
    submitted = handlers[("browser.invoke", "ronghui.clock.submit")](
        context,
        {"site": site, "clock_type": "交件到港"},
    )
    verified = handlers[("browser.invoke", "ronghui.clock.verify")](
        context,
        {
            "site": site,
            "clock_type": "交件到港",
            "operation_id": submitted["operation_id"],
        },
    )
    assert verified["confirmed"] is True
    assert [call["action"] for call in calls] == ["precheck", "submit", "verify"]
    assert all(call["account_id"] == "clock-rh" for call in calls)
    assert all(call["session_profile"] == "profile-clock-rh" for call in calls)

    tampered = str(submitted["operation_id"])[:-1] + "A"
    with pytest.raises(PluginExecutionError) as exc:
        handlers[("browser.invoke", "ronghui.clock.verify")](
            context,
            {
                "site": site,
                "clock_type": "交件到港",
                "operation_id": tampered,
            },
        )
    assert exc.value.code == "BROKER_CURSOR_INVALID"


def test_production_clock_uses_exact_low_level_write_and_readback(monkeypatch) -> None:
    from agent.tms_runtime.scripts import clock_in_dual as clock_module

    session = object()
    calls: list[tuple[str, object]] = []

    class Auth:
        def __init__(self, *, profile: str) -> None:
            calls.append(("auth", profile))

        def login_and_get_session(self):
            return session

    fixed_now = datetime(2026, 8, 15, 1, 2, 3)
    monkeypatch.setattr(clock_module, "TMSAuth", Auth)
    monkeypatch.setattr(
        clock_module,
        "_resolve_clockin_page_context",
        lambda value: calls.append(("context", value)) or {"reviewed": "context"},
    )
    monkeypatch.setattr(
        clock_module,
        "_load_user_info",
        lambda value: {
            "loginSiteName": "operator-site",
            "loginSiteCode": "operator-code",
            "loginEmpName": "operator",
            "loginEmpCode": "operator-id",
        },
    )
    monkeypatch.setattr(clock_module, "localnow", lambda: fixed_now)

    def submit(records, source_session, page_context):
        calls.append(("submit", records))
        assert source_session is session
        assert page_context == {"reviewed": "context"}
        return {"success": True}

    def verify(source_session, page_context, **kwargs):
        calls.append(("verify", kwargs))
        assert source_session is session
        assert page_context == {"reviewed": "context"}
        return {
            "record_id": "source-row-1",
            "clock_type": kwargs["clock_in_type"],
            "clock_result": "交件及时",
            "observed_at": "2026-08-15 01:02:03",
        }

    monkeypatch.setattr(clock_module, "submit_clockin", submit)
    monkeypatch.setattr(clock_module, "verify_clockin_record", verify)
    monkeypatch.setattr(
        clock_module,
        "submit_dual_clockin",
        lambda *args, **kwargs: pytest.fail("whole-tool clock workflow must not run"),
    )

    handlers = build_production_first_party_core_handler_map(
        account_manager=_Manager(),
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="clock_in_dual",
        role="account_id",
        account_ids=("clock-rh",),
        action="ronghui.clock.submit",
    )
    site = {
        "sitecode": "7390004",
        "sitefbcode": "73901",
        "sitename": "site",
        "sitefbname": "yard",
    }
    submitted = handlers[("browser.invoke", "ronghui.clock.submit")](
        context,
        {"site": site, "clock_type": "交件到港"},
    )
    verified = handlers[("browser.invoke", "ronghui.clock.verify")](
        context,
        {
            "site": site,
            "clock_type": "交件到港",
            "operation_id": submitted["operation_id"],
        },
    )
    assert verified["confirmed"] is True
    submitted_record = next(value for name, value in calls if name == "submit")[0]
    assert submitted_record["SITE_CODE"] == "7390004"
    assert submitted_record["REACH_OR_LEAVE_PORT_TYPE"] == "交件到港"
    verified_arguments = next(value for name, value in calls if name == "verify")
    assert verified_arguments["submitted_at"] == fixed_now


def test_production_customer_binding_calls_low_level_endpoint_adapter(monkeypatch) -> None:
    manager = _Manager()
    calls: list[dict[str, Any]] = []

    def lower_run_once(arguments):
        calls.append(dict(arguments))
        return {
            "ok": True,
            "rows": [],
            "stats": {"total": 0, "returned": 0, "total_authoritative": True},
        }

    monkeypatch.setattr(
        "agent.tms_runtime.scripts.customer_service_problem.run_once",
        lower_run_once,
    )
    handlers = build_production_first_party_core_handler_map(
        account_manager=manager,
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="sync_customer_service_problems",
        role="customer_service_source",
        account_ids=("customer-rh",),
        action="customer_problem.list_page",
    )
    result = handlers[("browser.invoke", "customer_problem.list_page")](
        context,
        {"direction": "received", "cursor": None, "page_size": 200},
    )
    assert result["pagination_complete"] is True
    assert calls[0]["action"] == "query"
    assert calls[0]["filters"] == {"direction": "received", "page": 1, "rows": 200}


def test_projection_rejects_schema_drift_before_any_write() -> None:
    manager = _Manager()
    writes: list[object] = []
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            replace_waybill_snapshot=lambda records, target: writes.append((records, target)) or {
                "ok": True,
                "record_count": len(records),
            },
        ),
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="sync_arrive_list",
        role="account_id",
        account_ids=("arrive-rh",),
        action="waybill.snapshot.replace",
        operation="projection.invoke",
    )
    invalid = _arrive_record("A-1")
    invalid["unexpected"] = "not signed"
    with pytest.raises(PluginExecutionError) as exc:
        handlers[("projection.invoke", "waybill.snapshot.replace")](
            context,
            {"records": [invalid], "target_date": "2026-08-15"},
        )
    assert exc.value.code == "BROKER_ARGUMENT_INVALID"
    assert writes == []


def test_arrival_stats_waybill_projection_closes_known_optional_empty_fields() -> None:
    manager = _Manager()
    writes: list[object] = []

    def replace(records, target):
        writes.append((records, target))
        return {"ok": True, "verified": True, "record_count": len(records)}

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            replace_waybill_snapshot=replace,
        ),
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="sync_arrival_stats",
        role="account_id",
        account_ids=("arrive-rh",),
        action="waybill.snapshot.replace",
        operation="projection.invoke",
    )
    record = _arrive_record("A-1")
    del record["receipt_number"]
    del record["remarks"]

    result = handlers[("projection.invoke", "waybill.snapshot.replace")](
        context,
        {"records": [record], "target_date": "2026-08-15"},
    )

    assert result["committed"] is True
    assert writes[0][0][0]["receipt_number"] == ""
    assert writes[0][0][0]["remarks"] == ""
    assert set(writes[0][0][0]) == set(_arrive_record("A-1"))


def test_arrive_projection_without_exact_readback_is_unknown_after_port_call() -> None:
    manager = _Manager()
    writes: list[object] = []
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            replace_waybill_snapshot=lambda records, target: writes.append((records, target))
            or {"ok": True, "record_count": len(records)},
        ),
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="sync_arrive_list",
        role="account_id",
        account_ids=("arrive-rh",),
        action="waybill.snapshot.replace",
        operation="projection.invoke",
    )

    with pytest.raises(PluginExecutionError) as exc:
        handlers[("projection.invoke", "waybill.snapshot.replace")](
            context,
            {"records": [_arrive_record("A-1")], "target_date": "2026-08-15"},
        )

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert len(writes) == 1


def test_arrival_stats_sheet_and_archive_use_exact_instance_resource_roles() -> None:
    manager = _Manager()
    sheet_calls: list[tuple[object, ...]] = []
    archive_calls: list[tuple[object, ...]] = []

    def replace_sheet(resource_id, layout, records, target_date):
        sheet_calls.append((resource_id, layout, list(records), target_date))
        return {"ok": True, "verified": True, "record_count": len(records)}

    def archive(resource_id, records, target_date):
        archive_calls.append((resource_id, list(records), target_date))
        return {"ok": True, "verified": True, "record_count": len(records)}

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            replace_arrival_stats_sheet=replace_sheet,
            archive_arrival_stats_sheet=archive,
        ),
        cursor_secret=_SECRET,
    )
    record = _arrival_stats_record("A-1")
    del record["receipt_number"]
    del record["remarks"]
    records = [record]

    for slot, role, resource_id, expected_layout in (
        (
            "arrival_stats_primary",
            "arrival_stats_primary_sheet",
            "resource-stats-primary",
            "stats",
        ),
        (
            "arrival_stats_split_pending",
            "arrival_stats_split_pending_sheet",
            "resource-split-pending",
            "split_pending",
        ),
    ):
        result = handlers[("network.request", "feishu.sheet.replace")](
            _context(
                tool_name="sync_arrival_stats",
                role=role,
                account_ids=(),
                resource_id=resource_id,
                action="feishu.sheet.replace",
                operation="network.request",
            ),
            {
                "resource_slot": slot,
                "records": records,
                "target_date": "2026-08-15",
            },
        )
        assert result["committed"] is True
        assert result["verified"] is True

    archived = handlers[("network.request", "feishu.sheet.add")](
        _context(
            tool_name="sync_arrival_stats",
            role="arrival_stats_archive_sheet",
            account_ids=(),
            resource_id="resource-stats-archive",
            action="feishu.sheet.add",
            operation="network.request",
        ),
        {"records": records, "target_date": "2026-08-15"},
    )
    assert archived["committed"] is True
    assert sheet_calls[0][0:2] == ("resource-stats-primary", "stats")
    assert sheet_calls[1][0:2] == ("resource-split-pending", "split_pending")
    assert sheet_calls[0][2][0]["receipt_number"] == ""
    assert sheet_calls[0][2][0]["remarks"] == ""
    assert archive_calls[0][0] == "resource-stats-archive"
    assert archive_calls[0][1][0]["receipt_number"] == ""
    assert archive_calls[0][1][0]["remarks"] == ""
    assert archive_calls[0][2] == "2026-08-15"


def test_arrival_stats_sheet_unverified_or_role_mismatch_fails_closed() -> None:
    manager = _Manager()
    calls: list[object] = []

    def replace_sheet(resource_id, layout, records, target_date):
        calls.append((resource_id, layout, records, target_date))
        return {"ok": True, "record_count": len(records)}

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            replace_arrival_stats_sheet=replace_sheet,
        ),
        cursor_secret=_SECRET,
    )
    arguments = {
        "resource_slot": "arrival_stats_primary",
        "records": [_arrival_stats_record("A-1")],
        "target_date": "2026-08-15",
    }

    with pytest.raises(PluginExecutionError) as mismatch:
        handlers[("network.request", "feishu.sheet.replace")](
            _context(
                tool_name="sync_arrival_stats",
                role="arrival_stats_secondary_sheet",
                account_ids=(),
                resource_id="resource-stats-secondary",
                action="feishu.sheet.replace",
                operation="network.request",
            ),
            arguments,
        )
    assert mismatch.value.code == "BROKER_CONTEXT_INVALID"
    assert calls == []

    with pytest.raises(PluginExecutionError) as unknown:
        handlers[("network.request", "feishu.sheet.replace")](
            _context(
                tool_name="sync_arrival_stats",
                role="arrival_stats_primary_sheet",
                account_ids=(),
                resource_id="resource-stats-primary",
                action="feishu.sheet.replace",
                operation="network.request",
            ),
            arguments,
        )
    assert unknown.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert len(calls) == 1


def test_production_arrive_handlers_bind_page_projection_and_sheet_adapters(
    monkeypatch,
) -> None:
    manager = _Manager()
    source_calls: list[tuple[str, int, int]] = []
    waybill_writes: list[list[dict[str, Any]]] = []
    forecast_writes: list[tuple[date, list[dict[str, Any]], bool]] = []
    sheet_writes: list[tuple[str, list[list[Any]], dict[str, Any]]] = []

    class Session:
        pass

    class Auth:
        def __init__(self, *, profile):
            assert profile == "profile-arrive-rh"

        def login_and_get_session(self):
            return Session()

    source_row = list(_arrive_record("A-100").values())

    def fetch_page(_session, *, login_site_code, date_range, page_index, page_size):
        assert login_site_code == "73901"
        assert date_range == {"start": "2026/08/15 00:00:00", "end": "2026/08/15 23:59:59"}
        source_calls.append((login_site_code, page_index, page_size))
        return {"data": [source_row], "total": 1}

    monkeypatch.setattr("agent.tms_runtime.scripts.login_manager.TMSAuth", Auth)
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.resolve_login_site_code",
        lambda _session: "73901",
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.fetch_dispatch_records",
        fetch_page,
    )
    from plugin_core_adapters import arrival as arrival_adapter
    from tools.daily_sign_store import snapshot_fingerprint

    monkeypatch.setattr(
        arrival_adapter,
        "_write_waybills",
        lambda records: waybill_writes.append(list(records)) or {"ok": True},
    )
    monkeypatch.setattr(arrival_adapter, "_read_waybills", lambda: list(waybill_writes[-1]))

    forecast_runs: list[dict[str, Any]] = []

    def save_forecast(business_date, records):
        forecast_writes.append((business_date, list(records), False))
        run = {
            "run_id": "forecast-run-1",
            "business_date": business_date.isoformat(),
            "status": "success",
            "row_count": len(records),
            "fingerprint": snapshot_fingerprint(records),
            "items": list(records),
        }
        forecast_runs.append(run)
        return {"ok": True, "run_id": run["run_id"]}

    monkeypatch.setattr(arrival_adapter, "_write_forecast", save_forecast)
    monkeypatch.setattr(
        arrival_adapter,
        "_read_forecast_runs",
        lambda _target_date: list(forecast_runs),
    )

    def write_sheet(resource_id, rows, target_date):
        sheet_writes.append((resource_id, list(rows), {"target_date": target_date}))
        return {"ok": True, "verified": True, "record_count": len(rows)}

    monkeypatch.setattr(arrival_adapter, "_replace_arrive_sheet", write_sheet)

    handlers = build_production_first_party_core_handler_map(
        account_manager=manager,
        cursor_secret=_SECRET,
    )
    source_context = _context(
        tool_name="sync_arrive_list",
        role="account_id",
        account_ids=("arrive-rh",),
        action="ronghui.arrive_list.read_page",
    )
    page = handlers[("browser.invoke", "ronghui.arrive_list.read_page")](
        source_context,
        {"target_date": "2026-08-15", "cursor": None, "page_size": 200},
    )
    assert page["pagination_complete"] is True
    record = dict(zip(_ARRIVE_FIELDS, page["items"][0]))
    projection_context = _context(
        tool_name="sync_arrive_list",
        role="account_id",
        account_ids=("arrive-rh",),
        action="waybill.snapshot.replace",
        operation="projection.invoke",
    )
    handlers[("projection.invoke", "waybill.snapshot.replace")](
        projection_context,
        {"records": [record], "target_date": "2026-08-15"},
    )
    handlers[("projection.invoke", "arrival.forecast_snapshot.replace")](
        projection_context,
        {"records": [record], "target_date": "2026-08-15"},
    )
    sheet_context = _context(
        tool_name="sync_arrive_list",
        role="arrive_primary_sheet",
        account_ids=("arrive-rh",),
        action="feishu.sheet.replace",
        operation="network.request",
        resource_id="resource-arrive-primary",
    )
    handlers[("network.request", "feishu.sheet.replace")](
        sheet_context,
        {
            "resource_slot": "arrive_primary_sheet",
            "values": [page["items"][0]],
            "target_date": "2026-08-15",
        },
    )
    assert source_calls == [("73901", 0, 200)]
    assert waybill_writes == [[record]]
    assert forecast_writes == [(date(2026, 8, 15), [record], False)]
    assert sheet_writes == [
        (
            "resource-arrive-primary",
            [page["items"][0]],
            {"target_date": "2026-08-15"},
        )
    ]


def test_yunda_handlers_use_exact_account_resource_roles_and_closed_schemas() -> None:
    manager = _Manager()
    source_calls: list[tuple[object, ...]] = []
    resource_calls: list[tuple[object, ...]] = []
    projection_calls: list[tuple[object, ...]] = []

    def dispatch_page(descriptor, target_date, dest_brch, page_index, page_size):
        source_calls.append(
            ("dispatch", descriptor["account_id"], target_date, dest_brch, page_index, page_size)
        )
        return {
            "items": [_yunda_dispatch_source_record()],
            "returned": 1,
            "total": 1,
            "total_authoritative": True,
        }

    def send_page(descriptor, target_date, page_number, page_size):
        source_calls.append(
            ("send", descriptor["account_id"], target_date, page_number, page_size)
        )
        return {
            "items": [{"Logistics_Id": "YD-1"}],
            "returned": 1,
            "total": 1,
            "total_authoritative": True,
        }

    def detail(descriptor, bill_code):
        source_calls.append(("detail", descriptor["account_id"], bill_code))
        return {"Logistics_Id": bill_code, "account_id": "must-be-scrubbed"}

    def renderer(descriptor, bill_code, created_dot_code):
        source_calls.append(
            ("renderer", descriptor["account_id"], bill_code, created_dot_code)
        )
        return {"price": {"Total": "1.00"}}

    def resource_commit(resource_id, records, target_date, ensure_fields):
        resource_calls.append((resource_id, list(records), target_date, ensure_fields))
        return {
            "ok": True,
            "record_count": len(records),
            "written": len(records),
            "deleted": 0,
            "verified": True,
            "readback_count": len(records),
            "readback_sha256": "a" * 64,
        }

    def projection_commit(records, target_date):
        projection_calls.append((list(records), target_date))
        return {
            "ok": True,
            "record_count": len(records),
            "upserted": len(records),
            "deleted_stale": 0,
            "verified": True,
            "readback_count": len(records),
            "readback_sha256": "b" * 64,
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            yunda_dispatch_read_page=dispatch_page,
            yunda_send_read_page=send_page,
            yunda_special_line_read_page=send_page,
            yunda_tracking_detail_read=detail,
            yunda_original_data_read=detail,
            yunda_renderer_detail_read=renderer,
            append_yunda_dispatch_bitable=resource_commit,
            replace_yunda_send_bitable=resource_commit,
            replace_yunda_send_sheet=resource_commit,
            replace_yunda_waybill_projection=projection_commit,
        ),
        cursor_secret=_SECRET,
    )
    dispatch_context = _context(
        tool_name="sync_yunda_dispatch_forecast",
        role="account_id",
        account_ids=("yunda-a",),
        action="yunda.dispatch_forecast.read_page",
    )
    page = handlers[("browser.invoke", "yunda.dispatch_forecast.read_page")](
        dispatch_context,
        {
            "target_date": "2026-08-16",
            "dest_brch": "56739382",
            "cursor": None,
            "page_size": 200,
        },
    )
    assert page["pagination_complete"] is True
    assert page["items"] == [_yunda_dispatch_source_record()]

    send_context = _context(
        tool_name="sync_yunda_send_waybills",
        role="account_id",
        account_ids=("yunda-a",),
        action="yunda.send_waybill.list_page",
    )
    send = handlers[("browser.invoke", "yunda.send_waybill.list_page")](
        send_context,
        {"target_date": "2026-08-15", "cursor": None, "page_size": 200},
    )
    assert send["items"] == [{"Logistics_Id": "YD-1"}]
    detail_result = handlers[("browser.invoke", "yunda.waybill.tracking_detail")](
        send_context,
        {"bill_code": "YD-1"},
    )
    assert detail_result["record"] == {"Logistics_Id": "YD-1"}
    handlers[("browser.invoke", "yunda.send_waybill.renderer_detail")](
        send_context,
        {"bill_code": "YD-1", "created_dot_code": "56739382"},
    )

    dispatch_record = {
        "主单号": "YD-MAIN-1",
        "开单件数": 1,
        "扫描件数": 1,
        "重量/kg": "1.00",
        "体积/m3": "0.001",
        "包装类型": "纸箱",
        "清场时间": "",
        "规划时效": 24,
        "开单目的地址": "长沙",
        "预计到达时间": "",
        "应派时间": "2026-08-16 12:00:00",
    }
    dispatch_sink_context = _context(
        tool_name="sync_yunda_dispatch_forecast",
        role="dispatch_forecast_bitable",
        account_ids=(),
        action="feishu.bitable.append_yunda_dispatch_forecast",
        operation="network.request",
        resource_id="resource-dispatch",
    )
    committed = handlers[
        ("network.request", "feishu.bitable.append_yunda_dispatch_forecast")
    ](
        dispatch_sink_context,
        {
            "records": [dispatch_record],
            "target_date": "2026-08-16",
            "ensure_fields": True,
        },
    )
    assert committed["committed"] is True

    send_record = {
        "5.14编号": "YD-1",
        "目的网点": "长沙",
        "收件区/县": "岳麓区",
        "收件地址": "地址",
        "寄件人": "发件人",
        "寄件手机": "07310000000",
        "收货人": "收件人",
        "收货电话": "13800000000",
        "货物名称": "配件",
        "包装类型": "纸箱",
        "派送方式": "不上楼",
        "件数": "1",
        "实际重量": "1.00",
        "现付": "1.00",
        "月结": "",
        "提付": "",
        "中转运费": "0.50",
        "回单号": "",
        "备注": "",
        "结算重量": "1.00",
        "体积": "0.001",
        "支付类型": "现金",
        "体积重": "1.00",
        "到付款": "0.00",
        "日期": "2026-08-15",
    }
    bitable_context = _context(
        tool_name="sync_yunda_send_waybills",
        role="send_waybills_bitable",
        account_ids=(),
        action="feishu.bitable.replace_yunda_send_waybills_date",
        operation="network.request",
        resource_id="resource-bitable",
    )
    handlers[
        ("network.request", "feishu.bitable.replace_yunda_send_waybills_date")
    ](
        bitable_context,
        {
            "records": [send_record],
            "target_date": "2026-08-15",
            "ensure_fields": True,
        },
    )
    projection_context = _context(
        tool_name="sync_yunda_send_waybills",
        role="account_id",
        account_ids=("yunda-a",),
        action="waybill.yunda.replace_date",
        operation="projection.invoke",
    )
    handlers[("projection.invoke", "waybill.yunda.replace_date")](
        projection_context,
        {
            "records": [send_record],
            "target_date": "2026-08-15",
            "ensure_fields": False,
        },
    )
    assert source_calls[0] == (
        "dispatch",
        "yunda-a",
        "2026-08-16",
        "56739382",
        0,
        200,
    )
    assert [call[0] for call in resource_calls] == [
        "resource-dispatch",
        "resource-bitable",
    ]
    assert projection_calls == [([send_record], "2026-08-15")]

    invalid = dict(send_record)
    invalid["日期"] = "2026-08-14"
    with pytest.raises(PluginExecutionError) as exc:
        handlers[
            ("network.request", "feishu.bitable.replace_yunda_send_waybills_date")
        ](
            bitable_context,
            {
                "records": [invalid],
                "target_date": "2026-08-15",
                "ensure_fields": True,
            },
        )
    assert exc.value.code == "BROKER_ARGUMENT_INVALID"


def test_empty_yunda_dispatch_append_is_a_closed_verified_noop() -> None:
    write_markers: list[str] = []

    def append_noop(
        resource_id: str,
        records: list[dict[str, Any]],
        target_date: str,
        ensure_fields: bool,
    ) -> Mapping[str, Any]:
        assert resource_id == "resource-dispatch"
        assert records == []
        assert target_date == "2026-08-16"
        assert ensure_fields is True
        return {
            "ok": True,
            "record_count": 0,
            "written": 0,
            "verified": True,
            "readback_count": 0,
            "readback_sha256": "0" * 64,
            "no_op": True,
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=lambda _account_id: {},
            append_yunda_dispatch_bitable=append_noop,
        ),
        cursor_secret=_SECRET,
    )
    result = handlers[(
        "network.request",
        "feishu.bitable.append_yunda_dispatch_forecast",
    )](
        _context(
            tool_name="sync_yunda_dispatch_forecast",
            role="dispatch_forecast_bitable",
            account_ids=(),
            action="feishu.bitable.append_yunda_dispatch_forecast",
            operation="network.request",
            resource_id="resource-dispatch",
            mark_write_started=lambda: write_markers.append("started"),
        ),
        {
            "records": [],
            "target_date": "2026-08-16",
            "ensure_fields": True,
        },
    )

    assert write_markers == []
    assert result[VERIFIED_WRITE_NOOP_FIELD] is True
    assert result["committed"] is True
    assert result["verified"] is True


def test_production_yunda_sources_use_exact_profile_and_low_level_primitives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _Manager()
    profiles: list[str] = []
    source_calls: list[tuple[object, ...]] = []
    session = object()

    class SessionBroker:
        def __init__(self, profile: str) -> None:
            profiles.append(profile)

        def build_requests_session(self, *, validate: bool):
            assert validate is True
            return session

    monkeypatch.setattr(
        "agent.tms_runtime.session_broker.get_session_broker",
        lambda profile: SessionBroker(profile),
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_dispatch_forecast.run_once",
        lambda _arguments: pytest.fail("whole dispatch tool must not be called"),
    )

    def dispatch_page(bound_session, params, *, target_date, limit, offset):
        assert bound_session is session
        source_calls.append(("dispatch", dict(params), target_date, limit, offset))
        return {"items": [_yunda_dispatch_source_record()], "total": 1}

    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_dispatch_forecast.fetch_page",
        dispatch_page,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_dispatch_forecast._extract_rows",
        lambda payload: payload["items"],
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_dispatch_forecast._extract_total",
        lambda payload: payload["total"],
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_send_waybills.run_once",
        lambda _arguments: pytest.fail("whole send tool must not be called"),
    )

    def send_page(bound_session, params, *, target_date, page, page_size):
        assert bound_session is session
        source_calls.append(("send", dict(params), target_date, page, page_size))
        return {"items": [{"Logistics_Id": "YD-1"}], "total": 1}

    def special_page(bound_session, params, *, target_date, page, page_size):
        assert bound_session is session
        source_calls.append(("special", dict(params), target_date, page, page_size))
        return {"items": [{"Logistics_Id": "YD-2"}], "total": 1}

    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_send_waybills.fetch_send_page",
        send_page,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_send_waybills.fetch_special_line_page",
        special_page,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_send_waybills._extract_rows",
        lambda payload: payload["items"],
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_send_waybills._extract_total",
        lambda payload: payload["total"],
    )

    def tracking(bound_session, bill_code, params):
        assert bound_session is session
        source_calls.append(("tracking", bill_code, dict(params)))
        return {"Logistics_Id": bill_code}

    def original(bound_session, bill_code, params):
        assert bound_session is session
        source_calls.append(("original", bill_code, dict(params)))
        return {
            "Logistics_Id": bill_code,
            "Buyer_Address": "address",
            "Buyer_Mobile": "13800000000",
            "Buyer_Name": "buyer",
            "Buyer_Phone": "07390000000",
            "Sender_Address": "sender-address",
            "Sender_Mobile": "13900000000",
            "Sender_Name": "sender",
            "Sender_Phone": "07390000001",
        }

    def renderer(bound_session, bill_code, row, params):
        assert bound_session is session
        source_calls.append(("renderer", bill_code, dict(row), dict(params)))
        return {"Logistics_Id": bill_code, "price": {"Total": "1.00"}}

    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_send_waybills.fetch_waybill_detail",
        tracking,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_send_waybills.fetch_original_data",
        original,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.yunda_send_waybills.fetch_send_waybill_renderer",
        renderer,
    )

    handlers = build_production_first_party_core_handler_map(
        account_manager=manager,
        cursor_secret=_SECRET,
    )
    dispatch_context = _context(
        tool_name="sync_yunda_dispatch_forecast",
        role="account_id",
        account_ids=("yunda-a",),
        action="yunda.dispatch_forecast.read_page",
    )
    dispatch = handlers[("browser.invoke", "yunda.dispatch_forecast.read_page")](
        dispatch_context,
        {
            "target_date": "2026-08-16",
            "dest_brch": "56739382",
            "cursor": None,
            "page_size": 200,
        },
    )
    assert dispatch["items"] == [_yunda_dispatch_source_record()]

    send_context = _context(
        tool_name="sync_yunda_send_waybills",
        role="account_id",
        account_ids=("yunda-a",),
        action="yunda.send_waybill.list_page",
    )
    for action in (
        "yunda.send_waybill.list_page",
        "yunda.special_line.list_page",
    ):
        page = handlers[("browser.invoke", action)](
            send_context,
            {"target_date": "2026-08-15", "cursor": None, "page_size": 200},
        )
        assert page["pagination_complete"] is True
    tracking_detail = handlers[("browser.invoke", "yunda.waybill.tracking_detail")](
        send_context,
        {"bill_code": "YD-1"},
    )
    assert tracking_detail["record"]["Logistics_Id"] == "YD-1"
    original_detail = handlers[("browser.invoke", "yunda.waybill.original_data")](
        send_context,
        {"bill_code": "YD-1"},
    )
    assert original_detail["record"] == {
        "Buyer_Address": "address",
        "Buyer_Mobile": "13800000000",
        "Buyer_Name": "buyer",
        "Buyer_Phone": "07390000000",
        "Sender_Address": "sender-address",
        "Sender_Mobile": "13900000000",
        "Sender_Name": "sender",
        "Sender_Phone": "07390000001",
    }
    rendered = handlers[("browser.invoke", "yunda.send_waybill.renderer_detail")](
        send_context,
        {"bill_code": "YD-1", "created_dot_code": "56739382"},
    )
    assert rendered["record"] == {
        "Logistics_Id": "YD-1",
        "price": {"Total": "1.00"},
    }
    assert profiles == ["profile-yunda-a"] * 6
    assert source_calls == [
        ("dispatch", {"dest_brch": "56739382"}, date(2026, 8, 16), 200, 0),
        ("send", {}, date(2026, 8, 15), 1, 200),
        ("special", {}, date(2026, 8, 15), 1, 200),
        ("tracking", "YD-1", {}),
        ("original", "YD-1", {}),
        ("renderer", "YD-1", {"Created_Dot_Code": "56739382"}, {}),
    ]


def test_production_yunda_resource_primitive_never_substitutes_a_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _Manager()
    requested_resources: list[str] = []
    writes: list[tuple[str, dict[str, Any]]] = []
    resource: dict[str, Any] = {
        "resource_kind": "feishu_bitable",
        "base_token": "base-exact",
        "table_id": "table-exact",
        "_meta": {"resource_key": "different-resource"},
    }

    def resource_provider(resource_id: str):
        requested_resources.append(resource_id)
        return dict(resource)

    monkeypatch.setattr(
        "agent.workflow_resource_store.get_workflow_resource",
        resource_provider,
    )
    monkeypatch.setattr(
        "tools.yunda_dispatch_forecast_sync_tool._ensure_fields",
        lambda _base, _table, _params: {
            "created": [],
            "primary_field_name": "main",
            "has_explicit_main_field": True,
        },
    )
    monkeypatch.setattr(
        "tools.yunda_dispatch_forecast_sync_tool._build_records",
        lambda records, **_kwargs: [{"fields": dict(records[0])}],
    )

    list_calls = 0

    def write(operation: str, arguments: dict[str, Any]):
        nonlocal list_calls
        if operation == "list_records":
            list_calls += 1
            items = []
            if list_calls == 2:
                items = [
                    {
                        "record_id": "fresh-dispatch-record",
                        "fields": _yunda_dispatch_record(),
                    }
                ]
            return {"data": {"items": items, "has_more": False}}
        writes.append((operation, dict(arguments)))
        return {"written": 1}

    monkeypatch.setattr("tools.feishu_cli_tool.feishu_operation", write)
    handlers = build_production_first_party_core_handler_map(
        account_manager=manager,
        cursor_secret=_SECRET,
    )
    context = _context(
        tool_name="sync_yunda_dispatch_forecast",
        role="dispatch_forecast_bitable",
        account_ids=(),
        action="feishu.bitable.append_yunda_dispatch_forecast",
        operation="network.request",
        resource_id="resource-dispatch",
    )
    arguments = {
        "records": [_yunda_dispatch_record()],
        "target_date": "2026-08-16",
        "ensure_fields": True,
    }
    with pytest.raises(PluginExecutionError) as exc:
        handlers[(
            "network.request",
            "feishu.bitable.append_yunda_dispatch_forecast",
        )](context, arguments)
    assert exc.value.code == "BROKER_RESOURCE_MISMATCH"
    assert requested_resources == ["resource-dispatch"]
    assert writes == []

    resource["_meta"] = {"resource_key": "resource-dispatch"}
    result = handlers[(
        "network.request",
        "feishu.bitable.append_yunda_dispatch_forecast",
    )](context, arguments)
    assert result["committed"] is True
    assert requested_resources == ["resource-dispatch", "resource-dispatch"]
    assert writes[0][0] == "write_records"
    assert writes[0][1]["base_token"] == "base-exact"
    assert writes[0][1]["table_id"] == "table-exact"

    ambiguous_calls: list[str] = []

    def ambiguous_write(operation: str, _arguments: dict[str, Any]):
        ambiguous_calls.append(operation)
        if operation == "list_records":
            return {"data": {"items": [], "has_more": False}}
        return {"error": "ambiguous write response"}

    monkeypatch.setattr(
        "tools.feishu_cli_tool.feishu_operation",
        ambiguous_write,
    )
    with pytest.raises(PluginExecutionError) as unknown_exc:
        handlers[(
            "network.request",
            "feishu.bitable.append_yunda_dispatch_forecast",
        )](context, arguments)
    assert unknown_exc.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert ambiguous_calls == ["list_records", "write_records", "list_records"]
