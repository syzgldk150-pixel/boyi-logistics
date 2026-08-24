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
    / "sync_scan_codes"
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
    spec = importlib.util.spec_from_file_location("scan_codes_plugin_action", ACTION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_action_owns_classification_batching_verification_and_commit_order():
    module = _load_action()
    calls: list[tuple[str, str, str, dict[str, Any]]] = []
    child_one = "R123456789010001"
    child_two = "R123456789010002"
    source = [
        {
            "bill_code": "R12345678901",
            "destination": "总站",
            "scan_type": "到货",
            "scan_time": "2026-08-15 08:00:00",
            "scan_site": "测试网点",
        },
        {
            "bill_code": child_one,
            "destination": "A站",
            "scan_type": "到货",
            "scan_time": "2026-08-15 08:01:00",
            "scan_site": "测试网点",
        },
        {
            "bill_code": child_two,
            "destination": "B站",
            "scan_type": "到货",
            "scan_time": "2026-08-15 08:02:00",
            "scan_site": "测试网点",
        },
        {
            "bill_code": "H0001",
            "destination": "回单",
            "scan_type": "到货",
            "scan_time": "2026-08-15 08:03:00",
            "scan_site": "测试网点",
        },
    ]

    def broker(operation, *, action, role, arguments):
        calls.append((operation, action, role, arguments))
        if action == "ronghui.scan.read_page":
            return {
                "items": source,
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "evidence:scan-source",
            }
        if action == "scan.snapshot.replace":
            assert arguments["records"] == [
                {
                    "raw_code": "R12345678901",
                    "destination": "总站",
                    "code_type": "main",
                    "main_tracking": "R12345678901",
                },
                {
                    "raw_code": child_one,
                    "destination": "A站",
                    "code_type": "child",
                    "main_tracking": "R12345678901",
                },
                {
                    "raw_code": child_two,
                    "destination": "B站",
                    "code_type": "child",
                    "main_tracking": "R12345678901",
                },
            ]
            return {"committed": True, "record_count": 3, "evidence_ref": "evidence:snapshot"}
        if action == "ronghui.scan_next.submit":
            items = arguments["items"]
            digest = module._canonical_sha256(items)
            skipped = [child_two] if items[0]["bill_code"] == child_two else []
            return {
                "operation_id": f"opaque:{digest}",
                "items_sha256": digest,
                "submitted": len(items),
                "scanned": len(items) - len(skipped),
                "skipped_signed_codes": skipped,
                "evidence_ref": f"evidence:submit:{digest}",
            }
        if action == "ronghui.scan_next.verify":
            return {
                "verified": True,
                "items_sha256": arguments["items_sha256"],
                "submitted": arguments["submitted"],
                "scanned": arguments["scanned"],
                "skipped_signed_codes": arguments["skipped_signed_codes"],
                "postcondition": "server_ledger_verified",
                "readback_count": arguments["scanned"],
                "evidence_ref": f"evidence:verify:{arguments['items_sha256']}",
            }
        raise AssertionError((operation, action, role, arguments))

    result = module.run_action(
        {
            "target_date": "2026-08-15",
            "batch_size": 1,
        },
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"] | {
        "fetched": 4,
        "normalized": 3,
        "candidate_items": 2,
        "scheduled_items": 2,
        "scanned": 1,
        "skipped_signed_count": 1,
        "skipped_signed_codes": [child_two],
    } == result["data"]
    assert [call[1] for call in calls] == [
        "ronghui.scan.read_page",
        "scan.snapshot.replace",
        "ronghui.scan_next.submit",
        "ronghui.scan_next.verify",
        "ronghui.scan_next.submit",
        "ronghui.scan_next.verify",
    ]


def test_scan_dry_run_reads_and_plans_but_never_writes():
    module = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        assert action == "ronghui.scan.read_page"
        return {
            "items": [],
            "pagination_complete": True,
            "next_cursor": None,
            "evidence_ref": "evidence:source",
        }

    result = module.run_action(
        {"target_date": "2026-08-15", "dry_run": True},
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["dry_run"] is True
    assert calls == ["ronghui.scan.read_page"]


def test_empty_scan_source_commits_empty_snapshot_without_scan_batches():
    module = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        del operation, role
        calls.append(action)
        if action == "ronghui.scan.read_page":
            return {
                "items": [],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "evidence:empty-source",
            }
        assert action == "scan.snapshot.replace"
        assert arguments == {"records": [], "target_date": "2026-08-24"}
        return {
            "committed": True,
            "record_count": 0,
            "evidence_ref": "evidence:empty-snapshot",
        }

    result = module.run_action({"target_date": "2026-08-24"}, broker)

    assert result["status"] == "SUCCESS"
    assert result["meta"]["record_count"] == 0
    assert result["data"]["evidence"]["execution_result"] == "no_data_cleared"
    assert calls == ["ronghui.scan.read_page", "scan.snapshot.replace"]


def test_scan_collapses_duplicate_events_with_the_same_business_destination():
    module = _load_action()
    source = [
        {
            "bill_code": "R12345678901",
            "destination": "总站",
            "scan_type": "到货",
            "scan_time": "2026-08-24 08:00:00",
            "scan_site": "网点一",
        },
        {
            "bill_code": "R12345678901",
            "destination": "总站",
            "scan_type": "到货",
            "scan_time": "2026-08-24 08:05:00",
            "scan_site": "网点二",
        },
    ]

    def broker(_operation, *, action, role, arguments):
        del role
        if action == "ronghui.scan.read_page":
            return {
                "items": source,
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "evidence:duplicate-source",
            }
        assert action == "scan.snapshot.replace"
        assert arguments["records"] == [
            {
                "raw_code": "R12345678901",
                "destination": "总站",
                "code_type": "main",
                "main_tracking": "R12345678901",
            }
        ]
        return {
            "committed": True,
            "record_count": 1,
            "evidence_ref": "evidence:snapshot",
        }

    result = module.run_action({"target_date": "2026-08-24"}, broker)

    assert result["status"] == "SUCCESS"
    assert result["data"]["fetched"] == 1


def test_scan_rejects_duplicate_bill_with_conflicting_destination():
    module = _load_action()

    def broker(_operation, *, action, role, arguments):
        del role, arguments
        assert action == "ronghui.scan.read_page"
        return {
            "items": [
                {
                    "bill_code": "R12345678901",
                    "destination": "A站",
                    "scan_type": "到货",
                    "scan_time": "2026-08-24 08:00:00",
                    "scan_site": "网点一",
                },
                {
                    "bill_code": "R12345678901",
                    "destination": "B站",
                    "scan_type": "到货",
                    "scan_time": "2026-08-24 08:05:00",
                    "scan_site": "网点二",
                },
            ],
            "pagination_complete": True,
            "next_cursor": None,
            "evidence_ref": "evidence:conflicting-source",
        }

    with pytest.raises(ValueError, match="conflicting duplicate destinations"):
        module.run_action({"target_date": "2026-08-24"}, broker)


def test_scan_stops_before_next_batch_when_postcondition_proof_changes():
    module = _load_action()
    child = "R123456789010001"
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "ronghui.scan.read_page":
            return {
                "items": [
                    {
                        "bill_code": child,
                        "destination": "A站",
                        "scan_type": "到货",
                        "scan_time": "2026-08-15 08:00:00",
                        "scan_site": "测试网点",
                    }
                ],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "evidence:source",
            }
        if action == "scan.snapshot.replace":
            return {"committed": True, "evidence_ref": "evidence:snapshot"}
        if action == "ronghui.scan_next.submit":
            digest = module._canonical_sha256(arguments["items"])
            return {
                "operation_id": "opaque-operation",
                "items_sha256": digest,
                "submitted": 1,
                "scanned": 1,
                "skipped_signed_codes": [],
                "evidence_ref": "evidence:submit",
            }
        if action == "ronghui.scan_next.verify":
            return {
                "verified": True,
                "items_sha256": "0" * 64,
                "submitted": 1,
                "scanned": 1,
                "skipped_signed_codes": [],
                "postcondition": "uploaded_and_table_cleared",
                "evidence_ref": "evidence:verify",
            }
        raise AssertionError(action)

    with pytest.raises(ValueError, match="postcondition"):
        module.run_action({"target_date": "2026-08-15"}, broker)

    assert calls == [
        "ronghui.scan.read_page",
        "scan.snapshot.replace",
        "ronghui.scan_next.submit",
        "ronghui.scan_next.verify",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {"account_id": "must-not-cross-process"},
        {"request_body": {}},
        {"scan_next_request_body": {}},
        {"flow_payload": {}},
        {"batch_size": 0},
        {"max_batches": True},
        {"child_item_limit": -1},
        {"skip_bill_codes": ["one", "one"]},
        {"trigger_flow": "yes"},
        {"target_date": "2026/08/15"},
    ],
)
def test_scan_rejects_untrusted_or_ambiguous_arguments(arguments):
    module = _load_action()

    with pytest.raises(ValueError):
        module.run_action(arguments, lambda *args, **kwargs: {})
