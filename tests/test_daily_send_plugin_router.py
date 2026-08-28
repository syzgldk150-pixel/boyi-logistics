from __future__ import annotations

from copy import deepcopy

from agent.automation_plugins.daily_send_handlers import build_daily_send_handler_map
from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.daily_send import build_production_daily_send_ports
from tests.test_first_party_action_payloads import (
    _ExactResourceResolver,
    _execute_yunda_write_generation,
    _load_action,
    _prepare_yunda_generation,
)


_ACCOUNT_ID = "daily-send-ronghui"
_RESOURCE_ID = "phase7.send_order_bitable"
_TARGET_DATE = "2026-05-12"
_SOURCE_ROW = {
    "BILL_CODE": "R001",
    "INSERT_DATE": f"{_TARGET_DATE} 08:00:00",
    "BL_SIGNS_MARKING_TEXT": "已签收",
    "DESTINATION": "长沙",
    "ACCEPT_COUNTY": "大祥区",
    "ACCEPT_MAN_ADDRESS": "测试地址",
    "SEND_MAN": "寄件人",
    "SEND_MAN_PHONE": "13000000000",
    "ACCEPT_MAN": "收件人",
    "ACCEPT_MAN_PHONE": "13100000000",
    "GOODS_NAME": "配件",
    "PACK_TYPE": "纸箱",
    "DISPATCH_MODE": "送货",
    "PIECE_NUMBER": "2",
    "FEE_WEIGHT": "10.50",
    "GUEST_FREIGHT": "12.50",
    "R_BILLCODE": "",
    "REMARK": "测试",
    "PAYMENT_TYPE": "现付",
    "VOLUME_WEIGHT": "11.25",
    "VOLUME": "0.25",
    "SETTLEMENT_WEIGHT": "11",
    "TOPAYMENT": "3.40",
    "SCAN_TYPE": "签收扫描",
}


class _AccountManager:
    def require_active_binding_descriptor(self, account_id: str):
        assert account_id == _ACCOUNT_ID
        return {
            "account_id": account_id,
            "system": "ronghui",
            "account_purpose": "daily_send",
            "session_profile": "profile-daily-send",
        }

    def require_authenticated_binding(self, _account_id: str):
        raise AssertionError("production describe_account must not authenticate online")


def test_signed_daily_send_package_runs_production_ports_through_write_verifier(
    tmp_path,
) -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_daily_send_orders"]
    manifest_mapping = manifest.to_mapping()
    resource_roles = deepcopy(list(manifest_mapping["resource_roles"]))
    capability = _prepare_yunda_generation(
        manifest=manifest,
        tmp_path=tmp_path,
        automation_id="daily-send-east-instance",
        account_bindings={"account_id": [_ACCOUNT_ID]},
        resource_bindings={"send_order_bitable": _RESOURCE_ID},
        resource_roles=resource_roles,
        broker_operations=deepcopy(list(manifest_mapping["runtime_permissions"]["broker_operations"])),
    )

    bitable_rows: list[dict[str, object]] = []
    projection_rows: list[dict[str, object]] = []

    def resource_loader(resource_id: str):
        assert resource_id == _RESOURCE_ID
        return {
            "resource_kind": "feishu_bitable",
            "base_token": "core-only-base",
            "table_id": "core-only-table",
        }

    def feishu_operation(action: str, params: dict[str, object]):
        assert params["base_token"] == "core-only-base"
        assert params["table_id"] == "core-only-table"
        if action == "list_records":
            offset = int(params["offset"])
            limit = int(params["limit"])
            return {"ok": True, "items": deepcopy(bitable_rows[offset : offset + limit])}
        if action == "write_records":
            records = params["records"]
            assert isinstance(records, list)
            bitable_rows.extend(
                {
                    "record_id": f"record-{index}",
                    "fields": deepcopy(record["fields"]),
                }
                for index, record in enumerate(records, start=1)
            )
            return {"ok": True, "written": len(records)}
        if action == "delete_records":
            record_ids = {str(item) for item in params["record_ids"]}
            bitable_rows[:] = [row for row in bitable_rows if str(row["record_id"]) not in record_ids]
            return {"ok": True, "deleted": len(record_ids)}
        raise AssertionError(action)

    def projection_sync(records, target_date):
        assert target_date == _TARGET_DATE
        projection_rows[:] = deepcopy(list(records))
        return {"ok": True, "upserted": len(records)}

    manager = _AccountManager()
    ports = build_production_daily_send_ports(
        account_manager=manager,
        resource_loader=resource_loader,
        feishu_operation=feishu_operation,
        source_page=lambda descriptor, target_date, page_index, page_size: {
            "items": [deepcopy(_SOURCE_ROW)] if page_index == 0 else [],
            "total": 1,
        },
        projection_sync=projection_sync,
        projection_read=lambda target_date: deepcopy(projection_rows),
        projection_lookup=lambda waybill_no: next(
            (deepcopy(row) for row in projection_rows if row.get("waybill_no") == waybill_no),
            None,
        ),
    )
    handlers = build_daily_send_handler_map(
        ports,
        cursor_secret=b"daily-send-router-generation-secret-v1",
    )

    def direct_broker(operation, *, action, role, arguments):
        context = CoreBrokerInvocationContext(
            automation_id="daily-send-east-instance",
            plugin_version="1.0.0",
            tool_name="sync_daily_send_orders",
            operation=operation,
            action=action,
            role=role,
            account_ids=(_ACCOUNT_ID,) if role == "account_id" else (),
            resource_id=_RESOURCE_ID if role == "send_order_bitable" else None,
            account_bindings={"account_id": (_ACCOUNT_ID,)},
            resource_bindings={"send_order_bitable": _RESOURCE_ID},
        )
        return handlers[(operation, action)](context, arguments)

    direct = _load_action("sync_daily_send_orders").run_action(
        {"target_date": _TARGET_DATE},
        direct_broker,
    )
    assert direct["status"] == "SUCCESS"
    bitable_rows.clear()
    projection_rows.clear()

    resolver = _ExactResourceResolver({_RESOURCE_ID: "feishu_bitable"})
    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=handlers,
        manager=manager,
        resource_resolver=resolver,
        arguments={"target_date": _TARGET_DATE},
    )

    assert raw["status"] == "SUCCESS"
    assert raw["data"]["fetched"] == 1
    assert raw["data"]["written"] == 1
    assert raw["data"]["sql_upserted"] == 1
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert leases.finalized and leases.finalized[0]["outcome"] == "WRITE_VERIFIED"
    assert resolver.calls and set(resolver.calls) == {(_RESOURCE_ID, ("feishu_bitable",))}
    assert _ACCOUNT_ID not in repr(raw)
    assert _RESOURCE_ID not in repr(raw)
