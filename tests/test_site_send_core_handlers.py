from typing import Any, Mapping

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from plugin_core_adapters.first_party import (
    build_production_first_party_core_handler_map,
)


_SECRET = b"site-send-closed-handler-secret-value"


class _Manager:
    def require_active_binding_descriptor(self, account_id: str) -> dict[str, str]:
        assert account_id == "site-account"
        return {
            "account_id": account_id,
            "system": "ronghui",
            "session_profile": "site-profile",
        }

    def require_authenticated_binding(self, _account_id: str) -> dict[str, str]:
        raise AssertionError("production describe_account must not authenticate online")


def _context(
    *,
    action: str,
    role: str,
    account_ids: tuple[str, ...] = (),
    resource_id: str | None = None,
) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="site-instance",
        plugin_version="1.0.0",
        tool_name="sync_site_send_list",
        operation="browser.invoke" if action.startswith("ronghui.") else "network.request",
        action=action,
        role=role,
        account_ids=account_ids,
        resource_id=resource_id,
        account_bindings={role: account_ids} if account_ids else {},
        resource_bindings={role: resource_id} if resource_id else {},
    )


def test_production_site_source_uses_exact_page_and_detail_primitives(monkeypatch):
    class Session:
        pass

    session = Session()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.login_manager.TMSAuth.login_and_get_session",
        lambda self: session,
    )

    def fetch_page(current_session, payload, headers, timeout, page_index):
        assert current_session is session
        assert payload["pageIndex"] == "0"
        assert payload["pageSize"] == "100"
        assert "2026/08/15 00:00:00" in payload["SCAN_DATE"]
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        assert timeout == 20
        assert page_index == 0
        calls.append(("page", dict(payload)))
        return {
            "data": [
                {
                    "BILL_CODE": "WB-1",
                    "SCAN_SITE_NAME": "发货站",
                    "DESTINATION_NAME": "目的站",
                    "PIECE_NUMBER": "2",
                    "SETTLEMENT_WEIGHT": "3.5",
                }
            ],
            "total": 1,
        }

    def fetch_detail(current_session, bill_code, *, is_encryption, timeout):
        assert current_session is session
        assert bill_code == "WB-1"
        assert is_encryption is True
        assert timeout == 20
        calls.append(("detail", bill_code))
        return "<html>verified source</html>"

    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_wangdiansendlist.fetch_page",
        fetch_page,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_wangdiansendlist.fetch_bill_info_html",
        fetch_detail,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_wangdiansendlist.parse_bill_info_html",
        lambda html: {"运单编号": "WB-1", "包装类型": "纸箱"},
    )
    monkeypatch.setattr(
        "tools.site_send_list_sync_tool.run_site_send_list_sync",
        lambda params: (_ for _ in ()).throw(AssertionError("whole-tool fallback")),
    )
    handlers = build_production_first_party_core_handler_map(
        account_manager=_Manager(),
        cursor_secret=_SECRET,
    )

    result = handlers[("browser.invoke", "ronghui.site_send.read_page")](
        _context(
            action="ronghui.site_send.read_page",
            role="account_id",
            account_ids=("site-account",),
        ),
        {"cursor": None, "page_size": 100, "target_date": "2026-08-15"},
    )

    assert result["target_date"] == "2026-08-15"
    assert result["pagination_complete"] is True
    assert result["items"] == [
        {
            "tracking_number": "WB-1",
            "send_site": "发货站",
            "package_type": "纸箱",
            "destination": "目的站",
            "pieces": "2",
            "weight": "3.5",
        }
    ]
    assert [item[0] for item in calls] == ["page", "detail"]


def test_production_site_resource_handlers_use_exact_bound_resource_ids(monkeypatch):
    calls: list[tuple[str, str, object]] = []
    bitable_rows: list[dict[str, Any]] = []
    sheet_rows: list[list[Any]] = []

    resources = {
        "custom.site-send.bitable": {
            "resource_kind": "feishu_bitable",
            "base_token": "base-test",
            "table_id": "table-test",
            "_meta": {"resource_key": "custom.site-send.bitable"},
        },
        "custom.site-send.sheet": {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "sheet-test",
            "range": "Data!A2:F100",
            "clear_range": "Data!A2:F100",
            "_meta": {"resource_key": "custom.site-send.sheet"},
        },
    }

    def feishu(action, params):
        if action == "list_records":
            return {"items": list(bitable_rows), "has_more": False}
        if action == "read_sheet":
            return {"values": list(sheet_rows)}
        raise AssertionError(action)

    def bitable(resource_id, records, params, *, mark_write_started=None):
        nonlocal bitable_rows
        if mark_write_started is not None:
            mark_write_started()
        calls.append(("bitable", resource_id, records))
        assert params == {
            "base_token": "base-test",
            "table_id": "table-test",
            "target_date": "2026-08-15",
            "as": "bot",
            "dry_run": False,
        }
        bitable_rows = [
            {"record_id": f"record-{index}", "fields": dict(row["fields"])}
            for index, row in enumerate(records)
        ]
        return {"ok": True, "written": 1}

    def sheet(resource_id, rows, params, *, mark_write_started=None):
        nonlocal sheet_rows
        if mark_write_started is not None:
            mark_write_started()
        calls.append(("sheet", resource_id, rows))
        assert params == {
            "spreadsheet_token": "sheet-test",
            "range": "Data!A2:F100",
            "clear_range": "Data!A2:F100",
            "target_date": "2026-08-15",
            "as": "bot",
            "dry_run": False,
        }
        sheet_rows = [list(row) for row in rows]
        return {"ok": True, "rows": 1}

    monkeypatch.setattr(
        "agent.workflow_resource_store.get_workflow_resource",
        lambda resource_id: resources[resource_id],
    )
    monkeypatch.setattr("tools.feishu_cli_tool.feishu_operation", feishu)
    monkeypatch.setattr("tools.phase7_sync_common.sync_bitable_snapshot", bitable)
    monkeypatch.setattr("tools.phase7_sync_common.sync_sheet_snapshot", sheet)
    handlers = build_production_first_party_core_handler_map(
        account_manager=_Manager(),
        cursor_secret=_SECRET,
    )
    canonical = {
        "tracking_number": "WB-1",
        "send_site": "发货站",
        "package_type": "纸箱",
        "destination": "目的站",
        "pieces": 2,
        "weight": 3.5,
    }

    bitable_result = handlers[("network.request", "feishu.bitable.replace_snapshot")](
        _context(
            action="feishu.bitable.replace_snapshot",
            role="site_send_bitable",
            resource_id="custom.site-send.bitable",
        ),
        {
            "records": [{"fields": canonical}],
            "target_date": "2026-08-15",
        },
    )
    sheet_result = handlers[("network.request", "feishu.sheet.replace")](
        _context(
            action="feishu.sheet.replace",
            role="site_send_sheet",
            resource_id="custom.site-send.sheet",
        ),
        {
            "values": [["WB-1", "发货站", "纸箱", 2, 3.5, "目的站"]],
            "target_date": "2026-08-15",
        },
    )

    assert bitable_result["record_count"] == 1
    assert sheet_result["record_count"] == 1
    assert calls == [
        (
            "bitable",
            "custom.site-send.bitable",
            [
                {
                    "fields": {
                        "运单编号": "WB-1",
                        "发货网点": "发货站",
                        "包装类型": "纸箱",
                        "目的网点": "目的站",
                        "件数": 2,
                        "重量": 3.5,
                    }
                }
            ],
        ),
        (
            "sheet",
            "custom.site-send.sheet",
            [["WB-1", "发货站", "纸箱", 2, 3.5, "目的站"]],
        ),
    ]


@pytest.mark.parametrize(
    ("role", "resource_id"),
    [
        ("account_id", "custom.site-send.bitable"),
        ("site_send_bitable", None),
    ],
)
def test_site_bitable_handler_rejects_untrusted_resource_context(role, resource_id):
    handlers = build_production_first_party_core_handler_map(
        account_manager=_Manager(),
        cursor_secret=_SECRET,
    )
    with pytest.raises(PluginExecutionError):
        handlers[("network.request", "feishu.bitable.replace_snapshot")](
            _context(
                action="feishu.bitable.replace_snapshot",
                role=role,
                resource_id=resource_id,
            ),
            {"records": []},
        )


def test_site_source_fails_closed_when_package_type_detail_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.login_manager.TMSAuth.login_and_get_session",
        lambda self: object(),
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_wangdiansendlist.fetch_page",
        lambda *args, **kwargs: {"data": [{"BILL_CODE": "WB-1"}], "total": 1},
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_wangdiansendlist.fetch_bill_info_html",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("source down")),
    )
    handlers = build_production_first_party_core_handler_map(
        account_manager=_Manager(),
        cursor_secret=_SECRET,
    )
    with pytest.raises(PluginExecutionError) as exc:
        handlers[("browser.invoke", "ronghui.site_send.read_page")](
            _context(
                action="ronghui.site_send.read_page",
                role="account_id",
                account_ids=("site-account",),
            ),
            {"cursor": None, "page_size": 100, "target_date": "2026-08-15"},
        )
    assert exc.value.code == "BROKER_SOURCE_UNAVAILABLE"


def test_site_source_rejects_package_type_detail_for_another_waybill(monkeypatch):
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.login_manager.TMSAuth.login_and_get_session",
        lambda self: object(),
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_wangdiansendlist.fetch_page",
        lambda *args, **kwargs: {"data": [{"BILL_CODE": "WB-1"}], "total": 1},
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_wangdiansendlist.fetch_bill_info_html",
        lambda *args, **kwargs: "<html>another waybill</html>",
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_wangdiansendlist.parse_bill_info_html",
        lambda html: {"运单编号": "WB-2", "包装类型": "纸箱"},
    )
    handlers = build_production_first_party_core_handler_map(
        account_manager=_Manager(),
        cursor_secret=_SECRET,
    )
    with pytest.raises(PluginExecutionError) as exc:
        handlers[("browser.invoke", "ronghui.site_send.read_page")](
            _context(
                action="ronghui.site_send.read_page",
                role="account_id",
                account_ids=("site-account",),
            ),
            {"cursor": None, "page_size": 100, "target_date": "2026-08-15"},
        )
    assert exc.value.code == "BROKER_SOURCE_INVALID"
