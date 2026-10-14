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
PLUGIN_ROOT = ROOT / "agent" / "service_v2_plugins" / "sync_daily_send_orders_v2"
ACTION_SOURCE = PLUGIN_ROOT / "payload" / "action.py"
RESULT_SOURCE = ROOT / "agent" / "service_v2_plugins" / "_shared" / "result.py"
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


def test_final_snapshot_ignores_only_known_other_business_dates():
    action = _load_action()
    old = {"fields": {"发件日期": "2026-01-24", "运单编号": None}}
    action._verify_bitable_snapshot([old], [], target_date="2026-09-12")
    for record_date in ("2026-09-12", "invalid", None):
        with pytest.raises(ValueError):
            action._verify_bitable_snapshot(
                [{"fields": {"发件日期": record_date, "运单编号": None}}],
                [], target_date="2026-09-12")


def test_date_only_bitable_readback_keeps_business_date_and_all_other_fields():
    action = _load_action()
    fields, _ = action._normalize_source_row(
        {"BILL_CODE": "R001", "REGISTER_DATE": "2026-09-12 20:26:08", "PIECE_NUMBER": "2", "GUEST_FREIGHT": "12.50"},
        target_date="2026-09-12",
    )
    # Production Bitable returns the date-only column at UTC midnight;
    # REGISTER_DATE includes the original time of day.
    actual = dict(fields, **{"发件日期": 1789142400000})
    action._verify_bitable_snapshot(
        [{"fields": actual}], [{"fields": fields}], target_date="2026-09-12")
    for change in ({"发件日期": "2026-09-11"}, {"发件日期": None},
                   {"发件日期": "invalid"}, {"件数": 3}, {"运单编号": "R002"}):
        with pytest.raises(ValueError):
            action._verify_bitable_snapshot(
                [{"fields": dict(actual, **change)}], [{"fields": fields}], target_date="2026-09-12")


def test_payload_updates_unique_existing_ids_and_preserves_unmatched_rows():
    action_module = _load_action()
    action_module._BITABLE_PAGE_SIZE = 2
    calls = []
    source_rows = [
        {"BILL_CODE": " = 'R001' ", "REGISTER_DATE": "2026-05-12 08:00:00",
         "PIECE_NUMBER": "2", "BILL_WEIGHT": "7.25", "FEE_WEIGHT": "10.50",
         "GUEST_FREIGHT": "12.50", "FREIGHT": "8.25", "GOODS_PAYMENT": "7.50",
         "TOPAYMENT": "3.40", "INSURANCE": "100", "BL_INDENTURE": "1",
         "R_BILLCODE": "HR001", "SEND_MAN_ADDRESS": "测试寄件地址", "SCAN_TYPE": "签收扫描",
         "BL_SIGNS_MARKING_TEXT": "已签收"},
        {"BILL_CODE": "H001", "REGISTER_DATE": "2026-05-12 08:30:00"},
        {"BILL_CODE": "R002", "REGISTER_DATE": "2026-05-12 09:00:00", "PIECE_NUMBER": "1", "GUEST_FREIGHT": 0},
    ]
    existing = [
        {"record_ref": "keep-r001", "fields": {"运单编号": "R001", "发件日期": "2026-05-12",
                                              "人工备注": "保留", "附件": ["synthetic-attachment"]}},
        {"record_ref": "unmatched", "fields": {"运单编号": "R-OLD", "发件日期": "2026-05-12"}},
        {"record_ref": "other-day", "fields": {"运单编号": "R-OTHER", "发件日期": "2026-05-11"}},
    ]
    projections = []

    def broker(operation, *, action, role, arguments):
        _assert_no_resource_locations(arguments)
        calls.append(action)
        evidence = _evidence(len(calls))
        if action == "sync_daily_send_orders.lock.acquire":
            return {"acquired": True, "lease_ref": "synthetic-lease", "evidence_ref": evidence}
        if action == "ronghui.send_order.read_page":
            start = arguments["page_index"] * arguments["page_size"]
            return {"items": source_rows[start:start + arguments["page_size"]],
                    "total": len(source_rows), "evidence_ref": evidence}
        if action == "feishu.bitable.list_records":
            offset, size = arguments["offset"], arguments["page_size"]
            return {"items": [
                {"record_ref": row["record_ref"],
                 "fields": {field: row["fields"].get(field) for field in arguments["fields"]}}
                for row in existing[offset:offset + size]], "evidence_ref": evidence}
        if action == "feishu.bitable.write_records":
            for index, record in enumerate(arguments["records"]):
                if "record_ref" in record:
                    assert record["record_ref"] == "keep-r001"
                    next(row for row in existing if row["record_ref"] == record["record_ref"])["fields"].update(record["fields"])
                else:
                    existing.append({"record_ref": f"new-{index}", "fields": dict(record["fields"])})
            return {"committed": True, "written": len(arguments["records"]), "evidence_ref": evidence}
        if action == "waybill.ronghui.replace_date":
            projections.extend(arguments["records"])
            return {"committed": True, "upserted": 2, "updates": 1, "creates": 1,
                    "deleted_stale": 1, "evidence_ref": evidence}
        if action == "sync_daily_send_orders.lock.release":
            return {"committed": True, "released": True, "evidence_ref": evidence}
        raise AssertionError(action)

    result = action_module.run_action({"target_date": "2026-05-12", "page_size": 2}, broker)
    assert result["status"] == "SUCCESS"
    data = result["data"]
    assert data["raw_fetched"] == 3 and data["fetched"] == 2
    assert data["skipped_receipt_like"] == 1 and data["source_duplicates"] == 0
    assert data["updates"] == data["creates"] == 1 and data["written"] == 2
    assert data["deleted"] == data["dedup_deleted"] == 0
    assert data["unmatched_existing"] == 1 and data["requires_reconciliation"] is True
    assert data["per_date"][0]["unmatched_waybill_numbers"] == ["R-OLD"]
    saved = next(row for row in existing if row["record_ref"] == "keep-r001")
    assert saved["fields"]["人工备注"] == "保留"
    assert saved["fields"]["附件"] == ["synthetic-attachment"]
    assert saved["fields"]["录单运费"] == "12.50"
    assert saved["fields"]["运费"] == "8.25"
    assert saved["fields"]["实际重量"] == "7.25"
    assert saved["fields"]["计费重量"] == "10.50"
    assert saved["fields"]["需要回单"] == "是"
    assert {row["record_ref"] for row in existing} >= {"keep-r001", "unmatched", "other-day"}
    assert projections[0]["recorded_cost"] == "12.50"
    assert projections[0]["freight_fee"] == "8.25"
    assert projections[0]["cod_amount"] == "7.50"
    assert projections[0]["to_pay_amount"] == "3.40"
    assert projections[0]["receipt_required"] == "1"
    assert projections[0]["receipt_number"] == "HR001"
    assert calls[-1] == "sync_daily_send_orders.lock.release"
    assert "feishu.bitable.delete_records" not in calls
    assert len(result["meta"]["evidence_refs"]) == len(set(result["meta"]["evidence_refs"]))


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
                        "REGISTER_DATE": "2026-05-12",
                        "GUEST_FREIGHT": "12.50",
                    }
                ],
                "total": 1,
                "evidence_ref": evidence_ref,
            }
        if action == "waybill.ronghui.replace_date":
            assert arguments["records"][0]["recorded_cost"] == "12.50"
            assert arguments["records"][0]["freight_fee"] == ""
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


def test_empty_source_preserves_unmatched_bitable_rows_and_reports_them():
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
    assert result["data"]["deleted"] == 0
    assert result["data"]["unmatched_existing"] == 1
    assert result["data"]["requires_reconciliation"] is True
    assert result["data"]["sql_deleted_stale"] == 1
    assert result["data"]["evidence"]["execution_result"] == "no_data_preserved_unmatched"
    assert not any(call["action"] == "feishu.bitable.write_records" for call in calls)
    assert not any(call["action"] == "feishu.bitable.delete_records" for call in calls)


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
                "items": [{"BILL_CODE": "R001", "REGISTER_DATE": "2026-05-12", "GUEST_FREIGHT": "12.50"}],
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
    assert result["data"]["planned_deletes"] == 0
    assert result["data"]["unmatched_existing"] == 1
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
                        "REGISTER_DATE": "2026-05-12",
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


@pytest.mark.parametrize(
    ("source_dates", "expected_error"),
    [
        (
            {"REGISTER_DATE": "2026-05-11 23:59:59", "INSERT_DATE": "2026-05-12 08:00:00"},
            "outside the requested date",
        ),
        ({"INSERT_DATE": "2026-05-12 08:00:00"}, "invalid send date"),
        ({"REGISTER_DATE": "", "INSERT_DATE": "2026-05-12 08:00:00"}, "invalid send date"),
    ],
)
def test_source_row_outside_requested_date_fails_before_sink_access(source_dates, expected_error):
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
                "items": [{"BILL_CODE": "R001", **source_dates}],
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

    with pytest.raises(ValueError, match=expected_error):
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
