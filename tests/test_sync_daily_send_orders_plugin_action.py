from __future__ import annotations

import ast
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "agent" / "first_party_automation_plugins" / "sync_daily_send_orders"
ACTION_SOURCE = PLUGIN_ROOT / "payload" / "action.py"
RESULT_SOURCE = ROOT / "agent" / "first_party_automation_plugins" / "_runtime" / "result.py"
_FORBIDDEN_LOCATION_KEYS = {
    "account_id",
    "account_ids",
    "base_token",
    "app_token",
    "table_id",
    "resource_id",
    "session_profile",
}


def _load_action():
    result_spec = importlib.util.spec_from_file_location(
        "boyi_plugin_result",
        RESULT_SOURCE,
    )
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    action_spec = importlib.util.spec_from_file_location(
        "sync_daily_send_orders_plugin_action",
        ACTION_SOURCE,
    )
    assert action_spec is not None and action_spec.loader is not None
    action_module = importlib.util.module_from_spec(action_spec)
    try:
        action_spec.loader.exec_module(action_module)
    finally:
        if previous is None:
            sys.modules.pop("boyi_plugin_result", None)
        else:
            sys.modules["boyi_plugin_result"] = previous
    return action_module


def _walk_keys(value: object):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _assert_no_resource_locations(value: object) -> None:
    assert not (_FORBIDDEN_LOCATION_KEYS & set(_walk_keys(value)))


def _evidence(index: int) -> str:
    return f"test-evidence:{index}"


def test_payload_owns_full_replace_pagination_normalization_and_commit_order():
    action_module = _load_action()
    action_module._BITABLE_PAGE_SIZE = 2
    calls: list[dict[str, object]] = []
    list_call = 0
    delete_call = 0
    write_arguments: dict[str, object] = {}
    projection_arguments: dict[str, object] = {}

    source_rows = [
        {
            "BILL_CODE": " = 'R001' ",
            "INSERT_DATE": "2026-05-12 08:00:00",
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
        },
        {
            "BILL_CODE": "H001",
            "INSERT_DATE": "2026-05-12 08:30:00",
        },
        {
            "BILL_CODE": "R001",
            "INSERT_DATE": "2026-05-12 08:00:00",
        },
        {
            "BILL_CODE": "R002",
            "INSERT_DATE": "2026-05-12 09:00:00",
            "PIECE_NUMBER": "1",
        },
    ]

    def broker(operation, *, action, role, arguments):
        nonlocal list_call, delete_call
        _assert_no_resource_locations(arguments)
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": arguments,
            }
        )
        evidence_ref = _evidence(len(calls))
        if action == "sync_daily_send_orders.lock.acquire":
            assert operation == "ledger.invoke"
            assert role == "account_id"
            assert arguments == {}
            return {
                "acquired": True,
                "lease_ref": "opaque-lease",
                "evidence_ref": evidence_ref,
            }
        if action == "ronghui.send_order.read_page":
            assert operation == "browser.invoke"
            assert role == "account_id"
            index = arguments["page_index"]
            return {
                "items": source_rows[index * 2 : index * 2 + 2],
                "total": 4,
                "evidence_ref": evidence_ref,
            }
        if action == "feishu.bitable.list_records":
            assert operation == "network.request"
            assert role == "send_order_bitable"
            assert arguments["page_size"] == 2
            if list_call < 2:
                assert arguments["fields"] == ["运单编号", "发件日期"]
                pages = (
                    [
                        {
                            "record_ref": "opaque-old-1",
                            "fields": {"运单编号": "R001", "发件日期": "2026-05-12"},
                        },
                        {
                            "record_ref": "opaque-other-day",
                            "fields": {"运单编号": "R999", "发件日期": "2026-05-11"},
                        },
                    ],
                    [
                        {
                            "record_ref": "opaque-old-2",
                            "fields": {"运单编号": "R002", "发件日期": "2026-05-12"},
                        }
                    ],
                )
                items = pages[list_call]
            else:
                assert arguments["fields"] == list(action_module._BITABLE_FIELDS)
                written_records = list(write_arguments["records"])
                first = dict(written_records[0]["fields"])
                second = dict(written_records[1]["fields"])
                pages = (
                    [
                        {"record_ref": "opaque-new-1", "fields": first},
                        {"record_ref": "opaque-new-dup", "fields": first},
                    ],
                    [{"record_ref": "opaque-new-2", "fields": second}],
                    [
                        {"record_ref": "opaque-new-1", "fields": first},
                        {"record_ref": "opaque-new-2", "fields": second},
                    ],
                    [],
                )
                items = pages[list_call - 2]
            expected_offsets = (0, 2, 0, 2, 0, 2)
            assert arguments["offset"] == expected_offsets[list_call]
            list_call += 1
            return {"items": items, "evidence_ref": evidence_ref}
        if action == "feishu.bitable.delete_records":
            assert operation == "network.request"
            assert role == "send_order_bitable"
            expected_refs = (
                ["opaque-old-1", "opaque-old-2"],
                ["opaque-new-dup"],
            )
            assert arguments["record_refs"] == expected_refs[delete_call]
            delete_call += 1
            return {
                "committed": True,
                "deleted": len(arguments["record_refs"]),
                "evidence_ref": evidence_ref,
            }
        if action == "feishu.bitable.write_records":
            assert operation == "network.request"
            assert role == "send_order_bitable"
            write_arguments.update(arguments)
            return {
                "committed": True,
                "written": len(arguments["records"]),
                "evidence_ref": evidence_ref,
            }
        if action == "waybill.ronghui.replace_date":
            assert operation == "projection.invoke"
            assert role == "account_id"
            projection_arguments.update(arguments)
            return {
                "committed": True,
                "upserted": 2,
                "updates": 1,
                "creates": 1,
                "deleted_stale": 1,
                "evidence_ref": evidence_ref,
            }
        if action == "sync_daily_send_orders.lock.release":
            assert operation == "ledger.invoke"
            assert role == "account_id"
            assert arguments == {"lease_ref": "opaque-lease"}
            return {
                "committed": True,
                "released": True,
                "evidence_ref": evidence_ref,
            }
        raise AssertionError((operation, action, role, arguments))

    result = action_module.run_action(
        {
            "target_date": "2026-05-12",
            "page_size": 2,
        },
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["meta"]["record_count"] == 2
    assert result["meta"]["pagination_complete"] is True
    assert len(result["meta"]["evidence_refs"]) == len(set(result["meta"]["evidence_refs"]))
    data = result["data"]
    assert data["raw_fetched"] == 4
    assert data["fetched"] == 2
    assert data["skipped_receipt_like"] == 1
    assert data["source_duplicates"] == 1
    assert data["written"] == 2
    assert data["deleted"] == 2
    assert data["dedup_deleted"] == 1
    assert data["sql_upserted"] == 2
    assert data["sql_deleted_stale"] == 1
    assert data["evidence"]["broker_call_count"] == len(calls)

    records = write_arguments["records"]
    assert [record["fields"]["运单编号"] for record in records] == ["R001", "R002"]
    first_fields = records[0]["fields"]
    assert set(first_fields) == {
        "运单编号",
        "发件日期",
        "签收状态",
        "目的网点",
        "收件区/县",
        "收件地址",
        "寄件人",
        "寄件手机",
        "收货人",
        "收货电话",
        "货物名称",
        "包装类型",
        "派送方式",
        "件数",
        "实际重量",
        "录单金额",
        "回单号",
        "备注",
        "支付类型",
        "体积重量",
        "体积",
        "结算重量",
        "到付款",
    }
    assert first_fields["件数"] == 2
    assert first_fields["实际重量"] == "10.50"
    assert first_fields["录单金额"] == "12.50"
    assert first_fields["到付款"] == "3.40"
    assert first_fields["发件日期"] == int(
        datetime(2026, 5, 12, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )
    projections = projection_arguments["records"]
    assert projections[0]["waybill_no"] == "R001"
    assert projections[0]["freight_fee"] == "12.50"
    assert projections[0]["cod_amount"] == "3.40"
    assert projections[0]["status"] == "signed"
    assert projections[0]["scan_status"] == "签收扫描"
    assert projection_arguments["target_date"] == "2026-05-12"
    assert [call["action"] for call in calls] == [
        "sync_daily_send_orders.lock.acquire",
        "ronghui.send_order.read_page",
        "ronghui.send_order.read_page",
        "feishu.bitable.list_records",
        "feishu.bitable.list_records",
        "feishu.bitable.delete_records",
        "feishu.bitable.write_records",
        "feishu.bitable.list_records",
        "feishu.bitable.list_records",
        "feishu.bitable.delete_records",
        "feishu.bitable.list_records",
        "feishu.bitable.list_records",
        "waybill.ronghui.replace_date",
        "sync_daily_send_orders.lock.release",
    ]
    _assert_no_resource_locations(result)
    assert "record_ref" not in set(_walk_keys(result))
    assert "lease_ref" not in set(_walk_keys(result))
    serialized_result = json.dumps(result, ensure_ascii=False)
    assert "测试地址" not in serialized_result
    assert "13000000000" not in serialized_result


@pytest.mark.parametrize(
    "arguments",
    [
        {"account_id": "forbidden"},
        {"base_token": "forbidden", "table_id": "forbidden"},
        {"session_profile": "forbidden"},
        {"request_body": {}},
        {"extra_filters": {}},
        {"list_limit": 2},
        {"list_max_pages": 2},
    ],
)
def test_payload_rejects_account_resource_and_runtime_location_arguments(arguments):
    action = _load_action()

    with pytest.raises(ValueError, match="undeclared fields"):
        action.run_action(arguments, lambda *_args, **_kwargs: None)


def test_payload_rejects_conflicting_single_date_and_range():
    action = _load_action()

    with pytest.raises(ValueError, match="cannot be combined"):
        action.run_action(
            {"target_date": "2026-05-12", "start_date": "2026-05-11"},
            lambda *_args, **_kwargs: None,
        )


def test_sql_only_uses_the_account_bound_projection_and_never_feishu():
    action = _load_action()
    calls: list[tuple[str, str, str, dict[str, object]]] = []

    def broker(operation, *, action, role, arguments):
        calls.append((operation, action, role, arguments))
        evidence_ref = _evidence(len(calls))
        if action == "sync_daily_send_orders.lock.acquire":
            return {
                "acquired": True,
                "lease_ref": "lease-sql",
                "evidence_ref": evidence_ref,
            }
        if action == "ronghui.send_order.read_page":
            return {
                "items": [
                    {
                        "BILL_CODE": "R001",
                        "INSERT_DATE": "2026-05-12",
                        "GUEST_FREIGHT": "12.50",
                    }
                ],
                "total": 1,
                "evidence_ref": evidence_ref,
            }
        if action == "waybill.ronghui.replace_date":
            assert arguments["records"][0]["freight_fee"] == "12.50"
            return {
                "committed": True,
                "upserted": 1,
                "updates": 0,
                "creates": 1,
                "deleted_stale": 2,
                "evidence_ref": evidence_ref,
            }
        if action == "sync_daily_send_orders.lock.release":
            return {
                "committed": True,
                "released": True,
                "evidence_ref": evidence_ref,
            }
        raise AssertionError(action)

    result = action.run_action(
        {"target_date": "2026-05-12", "sql_only": True},
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["sql_only"] is True
    assert result["data"]["sql_upserted"] == 1
    assert result["data"]["sql_deleted_stale"] == 2
    assert not any(role == "send_order_bitable" for _, _, role, _ in calls)


def test_empty_source_clears_the_target_date_in_bitable_and_projection():
    action = _load_action()
    calls: list[dict[str, object]] = []
    list_call = 0

    def broker(operation, *, action, role, arguments):
        nonlocal list_call
        calls.append({"action": action, "arguments": arguments})
        evidence_ref = _evidence(len(calls))
        if action == "sync_daily_send_orders.lock.acquire":
            return {
                "acquired": True,
                "lease_ref": "lease-empty",
                "evidence_ref": evidence_ref,
            }
        if action == "ronghui.send_order.read_page":
            return {"items": [], "total": 0, "evidence_ref": evidence_ref}
        if action == "feishu.bitable.list_records":
            list_call += 1
            items = (
                [
                    {
                        "record_ref": "opaque-old",
                        "fields": {"运单编号": "R001", "发件日期": "2026-05-12"},
                    }
                ]
                if list_call == 1
                else []
            )
            return {"items": items, "evidence_ref": evidence_ref}
        if action == "feishu.bitable.delete_records":
            assert arguments["record_refs"] == ["opaque-old"]
            return {"committed": True, "deleted": 1, "evidence_ref": evidence_ref}
        if action == "waybill.ronghui.replace_date":
            assert arguments["records"] == []
            return {
                "committed": True,
                "upserted": 0,
                "updates": 0,
                "creates": 0,
                "deleted_stale": 1,
                "evidence_ref": evidence_ref,
            }
        if action == "sync_daily_send_orders.lock.release":
            return {
                "committed": True,
                "released": True,
                "evidence_ref": evidence_ref,
            }
        raise AssertionError(action)

    result = action.run_action({"target_date": "2026-05-12"}, broker)

    assert result["status"] == "SUCCESS"
    assert result["data"]["fetched"] == 0
    assert result["data"]["written"] == 0
    assert result["data"]["deleted"] == 1
    assert result["data"]["sql_deleted_stale"] == 1
    assert not any(call["action"] == "feishu.bitable.write_records" for call in calls)


def test_dry_run_reads_the_existing_snapshot_but_never_mutates_any_sink():
    action = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        evidence_ref = _evidence(len(calls))
        if action == "sync_daily_send_orders.lock.acquire":
            return {
                "acquired": True,
                "lease_ref": "lease-dry",
                "evidence_ref": evidence_ref,
            }
        if action == "ronghui.send_order.read_page":
            return {
                "items": [{"BILL_CODE": "R001", "INSERT_DATE": "2026-05-12"}],
                "total": 1,
                "evidence_ref": evidence_ref,
            }
        if action == "feishu.bitable.list_records":
            return {
                "items": [
                    {
                        "record_ref": "opaque-old",
                        "fields": {"运单编号": "R002", "发件日期": "2026-05-12"},
                    }
                ],
                "evidence_ref": evidence_ref,
            }
        if action == "sync_daily_send_orders.lock.release":
            return {
                "committed": True,
                "released": True,
                "evidence_ref": evidence_ref,
            }
        raise AssertionError(action)

    result = action.run_action(
        {"target_date": "2026-05-12", "dry_run": True},
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["planned_creates"] == 1
    assert result["data"]["planned_deletes"] == 1
    assert result["data"]["planned_sql_upserts"] == 1
    assert calls == [
        "sync_daily_send_orders.lock.acquire",
        "ronghui.send_order.read_page",
        "feishu.bitable.list_records",
        "sync_daily_send_orders.lock.release",
    ]


def test_incomplete_source_pagination_fails_closed_and_releases_the_lock():
    action = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "sync_daily_send_orders.lock.acquire":
            return {
                "acquired": True,
                "lease_ref": "lease-page",
                "evidence_ref": _evidence(len(calls)),
            }
        if action == "ronghui.send_order.read_page":
            return {
                "items": [],
                "total": 1,
                "evidence_ref": _evidence(len(calls)),
            }
        if action == "sync_daily_send_orders.lock.release":
            return {
                "committed": True,
                "released": True,
                "evidence_ref": _evidence(len(calls)),
            }
        raise AssertionError(action)

    with pytest.raises(ValueError, match="ended before total"):
        action.run_action({"target_date": "2026-05-12"}, broker)

    assert calls[-1] == "sync_daily_send_orders.lock.release"


def test_invalid_lock_evidence_still_releases_an_acquired_lease():
    action = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "sync_daily_send_orders.lock.acquire":
            return {"acquired": True, "lease_ref": "lease-no-evidence"}
        if action == "sync_daily_send_orders.lock.release":
            assert arguments == {"lease_ref": "lease-no-evidence"}
            return {
                "committed": True,
                "released": True,
                "evidence_ref": _evidence(len(calls)),
            }
        raise AssertionError(action)

    with pytest.raises(ValueError, match="evidence reference"):
        action.run_action({"target_date": "2026-05-12"}, broker)

    assert calls == [
        "sync_daily_send_orders.lock.acquire",
        "sync_daily_send_orders.lock.release",
    ]


def test_invalid_financial_value_fails_before_any_external_write_and_releases_lock():
    action = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "sync_daily_send_orders.lock.acquire":
            return {
                "acquired": True,
                "lease_ref": "lease-money",
                "evidence_ref": _evidence(len(calls)),
            }
        if action == "ronghui.send_order.read_page":
            return {
                "items": [
                    {
                        "BILL_CODE": "R001",
                        "INSERT_DATE": "2026-05-12",
                        "GUEST_FREIGHT": "not-an-amount",
                    }
                ],
                "total": 1,
                "evidence_ref": _evidence(len(calls)),
            }
        if action == "sync_daily_send_orders.lock.release":
            return {
                "committed": True,
                "released": True,
                "evidence_ref": _evidence(len(calls)),
            }
        raise AssertionError(action)

    with pytest.raises(ValueError, match="numeric field"):
        action.run_action({"target_date": "2026-05-12"}, broker)

    assert calls == [
        "sync_daily_send_orders.lock.acquire",
        "ronghui.send_order.read_page",
        "sync_daily_send_orders.lock.release",
    ]


def test_source_row_outside_requested_date_fails_before_sink_access():
    action = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "sync_daily_send_orders.lock.acquire":
            return {
                "acquired": True,
                "lease_ref": "lease-date",
                "evidence_ref": _evidence(len(calls)),
            }
        if action == "ronghui.send_order.read_page":
            return {
                "items": [{"BILL_CODE": "R001", "INSERT_DATE": "2026-05-11 23:59:59"}],
                "total": 1,
                "evidence_ref": _evidence(len(calls)),
            }
        if action == "sync_daily_send_orders.lock.release":
            return {
                "committed": True,
                "released": True,
                "evidence_ref": _evidence(len(calls)),
            }
        raise AssertionError(action)

    with pytest.raises(ValueError, match="outside the requested date"):
        action.run_action({"target_date": "2026-05-12"}, broker)

    assert calls == [
        "sync_daily_send_orders.lock.acquire",
        "ronghui.send_order.read_page",
        "sync_daily_send_orders.lock.release",
    ]


def test_payload_is_standalone_and_has_no_whole_tool_fallback():
    source = ACTION_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(
        module == "agent" or module.startswith("agent.") or module == "tools" or module.startswith("tools.")
        for module in imported_modules
    )
    assert "send_order_sync_tool" not in source
    assert "run_once" not in source
    assert '"/send_order"' not in source
    assert "call_http_service" not in source
    assert json.loads(json.dumps({"source": source}))["source"] == source
