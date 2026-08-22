from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "sync_delivery_status"
    / "payload"
    / "action.py"
)


def _load_action():
    result_path = (
        ROOT
        / "agent"
        / "first_party_automation_plugins"
        / "_runtime"
        / "result.py"
    )
    result_spec = importlib.util.spec_from_file_location("boyi_plugin_result", result_path)
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    result_spec.loader.exec_module(result_module)
    sys.modules["boyi_plugin_result"] = result_module
    spec = importlib.util.spec_from_file_location("delivery_status_plugin_action", ACTION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(operation: str, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if action == "feishu.bitable.list_views":
        return {
            "items": [{"view_id": "view-pending", "view_name": "未签收明细"}],
            "evidence_ref": "evidence:view-list",
        }
    if action == "feishu.bitable.list_records":
        assert arguments["view_id"] == "view-pending"
        return {
            "items": [
                {
                    "record_id": "rec-signed",
                    "fields": {"运单编号": "R001", "签收状态": "未签收"},
                },
                {
                    "record_id": "rec-still",
                    "fields": {"运单编号": "R002", "签收状态": "未签收"},
                },
                {
                    "record_id": "rec-done",
                    "fields": {"运单编号": "R003", "签收状态": "已签收"},
                },
            ],
            "pagination_complete": True,
            "next_cursor": None,
            "evidence_ref": "evidence:record-page",
        }
    if action == "ronghui.delivery_status.read":
        assert arguments["bill_codes"] == ["R001", "R002"]
        return {
            "items": [
                {"bill_code": "R001", "status": "签收"},
                {"bill_code": "R002", "status": "未签收"},
            ],
            "evidence_ref": "evidence:source-status",
        }
    if action == "feishu.bitable.write_records":
        assert arguments == {
            "records": [{"record_id": "rec-signed", "status": "已签收"}]
        }
        return {"committed": True, "written": 1, "evidence_ref": "evidence:write"}
    if action == "waybill.delivery_status.update":
        assert arguments == {"bill_codes": ["R001"], "status": "signed"}
        return {"committed": True, "updated": 1, "evidence_ref": "evidence:projection"}
    raise AssertionError((operation, action, arguments))


def test_pending_view_scan_updates_only_newly_signed_records():
    module = _load_action()
    calls: list[tuple[str, str, str]] = []

    def broker(operation, *, action, role, arguments):
        calls.append((operation, action, role))
        return _result(operation, action, arguments)

    result = module.run_action({}, broker)

    assert result["status"] == "SUCCESS"
    assert result["data"] | {
        "mode": "pending_view",
        "scanned": 3,
        "pending": 2,
        "queried": 2,
        "updated": 1,
        "unchanged": 1,
        "unmatched": 0,
    } == result["data"]
    assert calls == [
        ("network.request", "feishu.bitable.list_views", "delivery_status_bitable"),
        ("network.request", "feishu.bitable.list_records", "delivery_status_bitable"),
        ("browser.invoke", "ronghui.delivery_status.read", "account_id"),
        ("network.request", "feishu.bitable.write_records", "delivery_status_bitable"),
        ("projection.invoke", "waybill.delivery_status.update", "account_id"),
    ]


def test_explicit_webhook_mode_preserves_non_signed_status_but_dry_run_never_writes():
    module = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        assert action == "ronghui.delivery_status.read"
        assert role == "account_id"
        assert arguments == {"bill_codes": ["R001"]}
        return {
            "items": [{"bill_code": "R001", "status": "未签收"}],
            "evidence_ref": "evidence:explicit-query",
        }

    result = module.run_action(
        {"BILL_CODE": "R001", "RECORD_ID": "rec-1", "dry_run": True},
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["mode"] == "explicit"
    assert result["data"]["updated"] == 1
    assert calls == ["ronghui.delivery_status.read"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"account_id": "must-not-cross-process"},
        {"base_token": "must-be-a-resource-binding"},
        {"bill_codes": ["R001"], "record_ids": []},
        {"bill_codes": ["R001", "R002"], "record_ids": ["rec-1"]},
        {"bill_codes": ["R001", "R002"], "record_ids": ["rec-1", "rec-1"]},
        {"list_limit": 201},
        {"query_batch_size": True},
    ],
)
def test_payload_rejects_untrusted_or_ambiguous_inputs(arguments):
    module = _load_action()

    with pytest.raises(ValueError):
        module.run_action(arguments, lambda *args, **kwargs: {})


def test_payload_rejects_ambiguous_pending_view_without_querying_accounts():
    module = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        return {
            "items": [
                {"view_id": "one", "view_name": "未签收明细"},
                {"view_id": "two", "view_name": "未签收明细"},
            ],
            "evidence_ref": "evidence:ambiguous-view",
        }

    with pytest.raises(ValueError, match="missing or ambiguous"):
        module.run_action({}, broker)

    assert calls == ["feishu.bitable.list_views"]
