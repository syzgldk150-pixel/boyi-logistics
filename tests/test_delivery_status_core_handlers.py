from __future__ import annotations

from typing import Any, Mapping
from unittest.mock import MagicMock, patch

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party_handlers import (
    FirstPartyCoreHandlerPorts,
    build_first_party_core_handler_map,
)
from plugin_core_adapters.first_party import (
    build_production_first_party_core_handler_map,
)
from tools.phase7_mysql_store import CONSOLE_WAYBILL_FIELDS


_SECRET = b"delivery-status-handler-test-secret"


def _context(
    *,
    operation: str,
    action: str,
    role: str,
    account_ids: tuple[str, ...] = (),
    resource_id: str | None = None,
) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="delivery-status-east",
        plugin_version="1.0.0",
        tool_name="sync_delivery_status",
        operation=operation,
        action=action,
        role=role,
        account_ids=account_ids,
        resource_id=resource_id,
        account_bindings={role: account_ids} if account_ids else {},
        resource_bindings={role: resource_id} if resource_id else {},
    )


def test_closed_delivery_handlers_bind_cursor_account_and_resource() -> None:
    page_calls: list[tuple[str, str, int, int]] = []
    writes: list[tuple[str, list[dict[str, str]]]] = []
    projections: list[tuple[list[str], str]] = []

    def read_page(resource_id: str, view_id: str, page: int, size: int) -> Mapping[str, Any]:
        page_calls.append((resource_id, view_id, page, size))
        return {
            "items": [
                {
                    "record_id": f"record-{page}",
                    "waybill_no": f"R{page + 1:03d}",
                    "status": "未签收",
                }
            ],
            "returned": 1,
            "total": 2,
            "total_authoritative": True,
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=lambda account_id: {
                "account_id": account_id,
                "system": "ronghui",
                "session_profile": "profile-ronghui",
            },
            delivery_list_views=lambda resource_id: [
                {"view_id": "view-pending", "view_name": "未签收明细"}
            ],
            delivery_list_records=read_page,
            delivery_status_read=lambda descriptor, codes: [
                {"bill_code": code, "status": "已签收"} for code in codes
            ],
            delivery_write_records=lambda resource_id, records: (
                writes.append((resource_id, records))
                or {"ok": True, "record_count": len(records)}
            ),
            delivery_projection_update=lambda codes, status: (
                projections.append((codes, status))
                or {"ok": True, "updated": len(codes)}
            ),
        ),
        cursor_secret=_SECRET,
    )
    resource_context = _context(
        operation="network.request",
        action="feishu.bitable.list_views",
        role="delivery_status_bitable",
        resource_id="phase7.delivery_status_bitable",
    )
    views = handlers[("network.request", "feishu.bitable.list_views")](
        resource_context,
        {},
    )
    assert views["items"] == [{"view_id": "view-pending", "view_name": "未签收明细"}]

    page_context = _context(
        operation="network.request",
        action="feishu.bitable.list_records",
        role="delivery_status_bitable",
        resource_id="phase7.delivery_status_bitable",
    )
    first = handlers[("network.request", "feishu.bitable.list_records")](
        page_context,
        {"view_id": "view-pending", "cursor": None, "page_size": 1},
    )
    assert first["pagination_complete"] is False
    second = handlers[("network.request", "feishu.bitable.list_records")](
        page_context,
        {"view_id": "view-pending", "cursor": first["next_cursor"], "page_size": 1},
    )
    assert second["pagination_complete"] is True
    assert page_calls == [
        ("phase7.delivery_status_bitable", "view-pending", 0, 1),
        ("phase7.delivery_status_bitable", "view-pending", 1, 1),
    ]

    wrong_resource_context = _context(
        operation="network.request",
        action="feishu.bitable.list_records",
        role="delivery_status_bitable",
        resource_id="phase7.other_bitable",
    )
    with pytest.raises(PluginExecutionError) as changed_resource:
        handlers[("network.request", "feishu.bitable.list_records")](
            wrong_resource_context,
            {"view_id": "view-pending", "cursor": first["next_cursor"], "page_size": 1},
        )
    assert changed_resource.value.code == "BROKER_CURSOR_INVALID"

    account_context = _context(
        operation="browser.invoke",
        action="ronghui.delivery_status.read",
        role="account_id",
        account_ids=("ronghui-east",),
    )
    status_result = handlers[("browser.invoke", "ronghui.delivery_status.read")](
        account_context,
        {"bill_codes": ["R001"]},
    )
    assert status_result["items"] == [{"bill_code": "R001", "status": "已签收"}]

    write_context = _context(
        operation="network.request",
        action="feishu.bitable.write_records",
        role="delivery_status_bitable",
        resource_id="phase7.delivery_status_bitable",
    )
    assert handlers[("network.request", "feishu.bitable.write_records")](
        write_context,
        {"records": [{"record_id": "record-0", "status": "已签收"}]},
    )["committed"] is True
    assert writes == [
        (
            "phase7.delivery_status_bitable",
            [{"record_id": "record-0", "status": "已签收"}],
        )
    ]

    projection_context = _context(
        operation="projection.invoke",
        action="waybill.delivery_status.update",
        role="account_id",
        account_ids=("ronghui-east",),
    )
    assert handlers[("projection.invoke", "waybill.delivery_status.update")](
        projection_context,
        {"bill_codes": ["R001"], "status": "signed"},
    )["committed"] is True
    assert projections == [(["R001"], "signed")]


def test_production_delivery_handlers_use_only_low_level_ports() -> None:
    manager = MagicMock()
    manager.require_active_binding_descriptor.return_value = {
        "account_id": "ronghui-east",
        "system": "ronghui",
        "session_profile": "profile-ronghui-east",
    }
    manager.require_authenticated_binding.side_effect = AssertionError(
        "production describe_account must not authenticate online"
    )
    auth = MagicMock()
    auth.login_and_get_session.return_value = object()
    resource = {
        "resource_kind": "feishu_bitable",
        "base_token": "base-test",
        "table_id": "table-test",
        "view_id": "view-pending",
        "view_name": "未签收明细",
        "_meta": {"resource_key": "phase7.delivery_status_bitable"},
    }

    bitable_status = "未签收"
    projection_status = "in_transit"

    def feishu(action: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal bitable_status
        if action == "list_views":
            return {
                "ok": True,
                "items": [{"view_id": "view-pending", "view_name": "未签收明细"}],
            }
        if action == "list_records":
            return {
                "ok": True,
                "items": [
                        {
                            "record_id": "record-1",
                            "fields": {"运单编号": "R001", "签收状态": bitable_status},
                    }
                ],
            }
        if action == "get_record":
            assert params["record_id"] == "record-1"
            return {
                "ok": True,
                "data": {
                    "record": {
                        "record_id": "record-1",
                        "fields": {
                            "运单编号": "R001",
                            "签收状态": bitable_status,
                        },
                    }
                },
            }
        if action == "write_records":
            assert params["records"] == [
                {"record_id": "record-1", "fields": {"签收状态": "已签收"}}
            ]
            bitable_status = "已签收"
            return {"ok": True, "written": 1}
        raise AssertionError(action)

    def read_projection(numbers: list[str]) -> list[dict[str, str]]:
        assert numbers == ["R001"]
        row = {field: "" for field in CONSOLE_WAYBILL_FIELDS}
        row.update(waybill_no="R001", status=projection_status)
        return [row]

    def write_projection(
        numbers: list[str],
        status: str,
        *,
        mark_write_started=None,
    ) -> dict[str, Any]:
        nonlocal projection_status
        if mark_write_started is not None:
            mark_write_started()
        assert numbers == ["R001"]
        projection_status = status
        return {"ok": True, "updated": 1}

    with (
        patch("agent.workflow_resource_store.get_workflow_resource", return_value=resource),
        patch("tools.feishu_cli_tool.feishu_operation", side_effect=feishu),
        patch("agent.tms_runtime.scripts.login_manager.TMSAuth", return_value=auth),
        patch(
            "agent.tms_runtime.scripts.Delivery_status.iter_pages",
            return_value=[
                {
                    "total": 1,
                    "data": [
                        {"BILL_CODE": "R001", "BL_SIGNS_MARKING_TEXT": "签收"}
                    ],
                }
            ],
        ),
        patch("agent.tms_runtime.scripts.Delivery_status.run_once") as legacy_run,
            patch(
                "tools.phase7_mysql_store.update_console_waybill_statuses",
                side_effect=write_projection,
            ),
            patch(
                "tools.phase7_mysql_store.list_console_waybills_by_numbers",
                side_effect=read_projection,
            ),
    ):
        handlers = build_production_first_party_core_handler_map(
            account_manager=manager,
            cursor_secret=_SECRET,
        )
        views = handlers[("network.request", "feishu.bitable.list_views")](
            _context(
                operation="network.request",
                action="feishu.bitable.list_views",
                role="delivery_status_bitable",
                resource_id="phase7.delivery_status_bitable",
            ),
            {},
        )
        assert len(views["items"]) == 1
        page = handlers[("network.request", "feishu.bitable.list_records")](
            _context(
                operation="network.request",
                action="feishu.bitable.list_records",
                role="delivery_status_bitable",
                resource_id="phase7.delivery_status_bitable",
            ),
            {"view_id": "view-pending", "cursor": None, "page_size": 200},
        )
        assert page["items"][0]["waybill_no"] == "R001"
        statuses = handlers[("browser.invoke", "ronghui.delivery_status.read")](
            _context(
                operation="browser.invoke",
                action="ronghui.delivery_status.read",
                role="account_id",
                account_ids=("ronghui-east",),
            ),
            {"bill_codes": ["R001"]},
        )
        assert statuses["items"] == [{"bill_code": "R001", "status": "签收"}]
        write = handlers[("network.request", "feishu.bitable.write_records")](
            _context(
                operation="network.request",
                action="feishu.bitable.write_records",
                role="delivery_status_bitable",
                resource_id="phase7.delivery_status_bitable",
            ),
            {"records": [{"record_id": "record-1", "status": "已签收"}]},
        )
        assert write["written"] == 1
        projection = handlers[("projection.invoke", "waybill.delivery_status.update")](
            _context(
                operation="projection.invoke",
                action="waybill.delivery_status.update",
                role="account_id",
                account_ids=("ronghui-east",),
            ),
            {"bill_codes": ["R001"], "status": "signed"},
        )
        assert projection["updated"] == 1

    legacy_run.assert_not_called()
    manager.require_authenticated_binding.assert_not_called()
