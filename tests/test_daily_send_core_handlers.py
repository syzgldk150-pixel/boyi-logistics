from __future__ import annotations

from dataclasses import replace

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.daily_send_handlers import (
    DailySendHandlerPorts,
    build_daily_send_handler_map,
)
from agent.automation_plugins.errors import PluginExecutionError


_SECRET = b"daily-send-tests-use-a-stable-secret-value"
_ACCOUNT_ID = "account-1"
_RESOURCE_ID = "resource-1"
_FIELDS = {
    "运单编号": "R001",
    "发件日期": 1_778_515_200_000,
    "签收状态": "已签收",
    "目的网点": "长沙",
    "收件区/县": "大祥区",
    "收件地址": "测试地址",
    "寄件人": "寄件人",
    "寄件手机": "13000000000",
    "收货人": "收货人",
    "收货电话": "13100000000",
    "货物名称": "配件",
    "包装类型": "纸箱",
    "派送方式": "送货",
    "件数": 2,
    "实际重量": "10.50",
    "录单金额": "12.50",
    "回单号": "",
    "备注": "测试",
    "支付类型": "现付",
    "体积重量": "11.25",
    "体积": "0.25",
    "结算重量": "11",
    "到付款": "3.40",
}
_PROJECTION = {
    "waybill_no": "R001",
    "destination_site": "长沙",
    "open_date": "2026-05-12",
    "receiver_address": "测试地址",
    "receiver_name": "收货人",
    "receiver_phone": "13100000000",
    "sender_name": "寄件人",
    "sender_phone": "13000000000",
    "goods_name_lines": "配件",
    "package_type_lines": "纸箱",
    "quantity_lines": "2",
    "weight_volume": "实际重量 10.50",
    "delivery_method": "送货",
    "freight_fee": "12.50",
    "pickup_fee": "",
    "delivery_fee": "",
    "transfer_fee": "",
    "payment_method": "现付",
    "insurance_amount": "",
    "cod_amount": "3.40",
    "remark": "测试",
    "scan_status": "签收扫描",
    "status": "signed",
}


def _context(operation: str, action: str, role: str) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="daily-send-instance",
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


def _ports(**overrides):
    values = {
        "describe_account": lambda account_id: {
            "account_id": account_id,
            "system": "ronghui",
            "session_profile": "profile",
        },
        "source_page": lambda _descriptor, _date, _index, _size: {
            "items": [{"BILL_CODE": "R001", "COOKIE": "must-not-cross"}],
            "total": 1,
        },
        "bitable_list": lambda _resource, _offset, _size, fields: [
            {
                "record_id": "external-record-1",
                "fields": {field: _FIELDS[field] for field in fields},
            }
        ],
        "bitable_delete": lambda _resource, record_ids: {
            "ok": True,
            "verified": True,
            "deleted": len(record_ids),
        },
        "bitable_write": lambda _resource, records: {
            "ok": True,
            "verified": True,
            "written": len(records),
        },
        "projection_replace": lambda records, _target_date: {
            "ok": True,
            "verified": True,
            "upserted": len(records),
            "updates": 0,
            "creates": len(records),
            "deleted_stale": 0,
        },
    }
    values.update(overrides)
    return DailySendHandlerPorts(**values)


def test_handler_map_is_exact_and_source_projects_only_signed_fields():
    handlers = build_daily_send_handler_map(_ports(), cursor_secret=_SECRET)
    assert set(handlers) == {
        ("ledger.invoke", "sync_daily_send_orders.lock.acquire"),
        ("ledger.invoke", "sync_daily_send_orders.lock.release"),
        ("browser.invoke", "ronghui.send_order.read_page"),
        ("network.request", "feishu.bitable.list_records"),
        ("network.request", "feishu.bitable.delete_records"),
        ("network.request", "feishu.bitable.write_records"),
        ("projection.invoke", "waybill.ronghui.replace_date"),
    }
    result = handlers[("browser.invoke", "ronghui.send_order.read_page")](
        _context("browser.invoke", "ronghui.send_order.read_page", "account_id"),
        {"target_date": "2026-05-12", "page_index": 0, "page_size": 100},
    )
    assert result["items"] == [{"BILL_CODE": "R001"}]
    assert "COOKIE" not in repr(result)


def test_lock_is_single_owner_and_only_exact_opaque_lease_can_release():
    handlers = build_daily_send_handler_map(_ports(), cursor_secret=_SECRET)
    acquire_context = _context(
        "ledger.invoke",
        "sync_daily_send_orders.lock.acquire",
        "account_id",
    )
    release_context = _context(
        "ledger.invoke",
        "sync_daily_send_orders.lock.release",
        "account_id",
    )
    acquired = handlers[(acquire_context.operation, acquire_context.action)](
        acquire_context,
        {},
    )
    with pytest.raises(PluginExecutionError) as conflict:
        handlers[(acquire_context.operation, acquire_context.action)](acquire_context, {})
    assert conflict.value.code == "BROKER_CONCURRENCY_BLOCKED"
    with pytest.raises(PluginExecutionError) as forged:
        handlers[(release_context.operation, release_context.action)](
            release_context,
            {"lease_ref": "forged"},
        )
    assert forged.value.code == "BROKER_CURSOR_INVALID"
    released = handlers[(release_context.operation, release_context.action)](
        release_context,
        {"lease_ref": acquired["lease_ref"]},
    )
    assert released["released"] is True


def test_record_references_are_opaque_and_delete_requires_fresh_verification():
    handlers = build_daily_send_handler_map(
        _ports(
            bitable_delete=lambda _resource, _record_ids: {
                "ok": True,
                "verified": False,
                "deleted": 1,
            }
        ),
        cursor_secret=_SECRET,
    )
    list_context = _context(
        "network.request",
        "feishu.bitable.list_records",
        "send_order_bitable",
    )
    listed = handlers[(list_context.operation, list_context.action)](
        list_context,
        {"offset": 0, "page_size": 200, "fields": ["运单编号", "发件日期"]},
    )
    record_ref = listed["items"][0]["record_ref"]
    assert "external-record-1" not in record_ref
    delete_context = replace(
        list_context,
        action="feishu.bitable.delete_records",
    )
    with pytest.raises(PluginExecutionError) as unknown:
        handlers[(delete_context.operation, delete_context.action)](
            delete_context,
            {"record_refs": [record_ref]},
        )
    assert unknown.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_write_and_projection_require_independent_verified_postconditions():
    handlers = build_daily_send_handler_map(
        _ports(
            bitable_write=lambda _resource, records: {
                "ok": True,
                "verified": False,
                "written": len(records),
            },
            projection_replace=lambda records, _target_date: {
                "ok": True,
                "verified": False,
                "upserted": len(records),
                "updates": 0,
                "creates": len(records),
                "deleted_stale": 0,
            },
        ),
        cursor_secret=_SECRET,
    )
    write_context = _context(
        "network.request",
        "feishu.bitable.write_records",
        "send_order_bitable",
    )
    with pytest.raises(PluginExecutionError) as write_unknown:
        handlers[(write_context.operation, write_context.action)](
            write_context,
            {"records": [{"fields": dict(_FIELDS)}]},
        )
    assert write_unknown.value.code == "WRITE_OUTCOME_UNKNOWN"

    projection_context = _context(
        "projection.invoke",
        "waybill.ronghui.replace_date",
        "account_id",
    )
    with pytest.raises(PluginExecutionError) as projection_unknown:
        handlers[(projection_context.operation, projection_context.action)](
            projection_context,
            {"records": [dict(_PROJECTION)], "target_date": "2026-05-12"},
        )
    assert projection_unknown.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_write_marker_is_after_late_argument_validation_and_immediately_before_port():
    events: list[str] = []

    def write(_resource: str, records: list[dict[str, object]]):
        assert events == ["marker"]
        events.append("port")
        return {
            "ok": True,
            "verified": True,
            "written": len(records),
        }

    handlers = build_daily_send_handler_map(
        _ports(bitable_write=write),
        cursor_secret=_SECRET,
    )
    context = replace(
        _context(
            "network.request",
            "feishu.bitable.write_records",
            "send_order_bitable",
        ),
        mark_write_started=lambda: events.append("marker"),
    )

    with pytest.raises(PluginExecutionError) as invalid:
        handlers[(context.operation, context.action)](
            context,
            {"records": [{"fields": dict(_FIELDS)}, {"fields": dict(_FIELDS)}]},
        )

    assert invalid.value.code == "BROKER_ARGUMENT_INVALID"
    assert events == []

    result = handlers[(context.operation, context.action)](
        context,
        {"records": [{"fields": dict(_FIELDS)}]},
    )

    assert result["committed"] is True
    assert events == ["marker", "port"]


def test_read_handler_never_marks_a_write_boundary() -> None:
    handlers = build_daily_send_handler_map(_ports(), cursor_secret=_SECRET)
    context = replace(
        _context("browser.invoke", "ronghui.send_order.read_page", "account_id"),
        mark_write_started=lambda: pytest.fail("read action marked a write boundary"),
    )

    result = handlers[(context.operation, context.action)](
        context,
        {"target_date": "2026-05-12", "page_index": 0, "page_size": 100},
    )

    assert result["total"] == 1


def test_lease_handlers_mark_only_after_their_final_state_validation() -> None:
    events: list[str] = []
    handlers = build_daily_send_handler_map(_ports(), cursor_secret=_SECRET)
    acquire_context = replace(
        _context("ledger.invoke", "sync_daily_send_orders.lock.acquire", "account_id"),
        mark_write_started=lambda: events.append("marker"),
    )
    release_context = replace(
        _context("ledger.invoke", "sync_daily_send_orders.lock.release", "account_id"),
        mark_write_started=lambda: events.append("marker"),
    )
    acquire = handlers[(acquire_context.operation, acquire_context.action)]
    release = handlers[(release_context.operation, release_context.action)]
    acquired = acquire(acquire_context, {})

    with pytest.raises(PluginExecutionError) as conflict:
        acquire(acquire_context, {})
    assert conflict.value.code == "BROKER_CONCURRENCY_BLOCKED"

    with pytest.raises(PluginExecutionError) as forged:
        release(release_context, {"lease_ref": "forged"})
    assert forged.value.code == "BROKER_CURSOR_INVALID"
    assert events == ["marker"]

    release(release_context, {"lease_ref": acquired["lease_ref"]})
    assert events == ["marker", "marker"]
