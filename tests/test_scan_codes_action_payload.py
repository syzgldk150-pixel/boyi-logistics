from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any
import uuid

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


def _formal_arguments(module, arguments, source_rows):
    values = {**arguments, "dry_run": False}
    target_date = module._target_date(values)
    unique_source_rows = {}
    for item in source_rows:
        normalized = module._normalize_source_row(item)
        previous = unique_source_rows.get(normalized["bill_code"])
        if previous is not None and previous["destination"] != normalized["destination"]:
            raise ValueError("test preview source contains conflicting destinations")
        unique_source_rows.setdefault(normalized["bill_code"], normalized)
    snapshot = module._normalize_snapshot(
        [unique_source_rows[key] for key in sorted(unique_source_rows)]
    )
    candidates = module._candidate_items(
        snapshot,
        skipped=module._skip_codes(values.get("skip_bill_codes")),
    )
    batches, _omitted = module._batch_plan(candidates, values)
    planned_items = [item for batch in batches for item in batch]
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    binding = {
        "contract_version": 1,
        "plugin_id": "sync_scan_codes",
        "preview_run_id": str(uuid.uuid4()),
        "preview_step_id": str(uuid.uuid4()),
        "preview_result_sha256": "1" * 64,
        "project_instance_id": "scan_codes",
        "generation": 1,
        "contract_digest": "2" * 64,
        "configuration_version": 1,
        "target_date": target_date,
        "observed_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(minutes=15)).isoformat(),
        "source_page_count": 1,
        "normalized_record_count": len(snapshot),
        "source_snapshot_sha256": module._canonical_sha256(snapshot),
        "source_evidence_count": 1,
        "source_evidence_refs_sha256": module._canonical_sha256(["evidence:preview"]),
        "selection_count": len(planned_items),
        "selection_sha256": module._canonical_sha256(planned_items),
        "batch_count": len(batches),
        "batch_plan_sha256": module._canonical_sha256(batches),
        "formal_arguments_sha256": module._canonical_sha256(values),
    }
    binding["context_sha256"] = module._canonical_sha256(binding)
    values["_scan_preview_binding"] = binding
    return values


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
        _formal_arguments(
            module,
            {
                "target_date": "2026-08-15",
                "batch_size": 1,
            },
            source,
        ),
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
    assert "preview_evidence" not in result["data"]
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


def test_scan_dry_run_returns_exact_stable_preview_evidence():
    module = _load_action()
    child_one = "R123456789010001"
    child_two = "R123456789010002"
    rows = [
        {
            "bill_code": child_two,
            "destination": "B站",
            "scan_type": "到货",
            "scan_time": "2026-08-15 08:02:00",
            "scan_site": "测试网点",
        },
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
    ]

    def run(source):
        def broker(_operation, *, action, role, arguments):
            del role, arguments
            assert action == "ronghui.scan.read_page"
            return {
                "items": source,
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "evidence:stable-source",
            }

        return module.run_action(
            {
                "target_date": "2026-08-15",
                "batch_size": 1,
                "max_batches": 2,
                "dry_run": True,
            },
            broker,
        )["data"]["preview_evidence"]

    first = run(rows)
    second = run(list(reversed(rows)))
    expected_items = [
        {"bill_code": child_one, "station_name": "A站"},
        {"bill_code": child_two, "station_name": "B站"},
    ]
    assert first["contract_version"] == 1
    assert first["target_date"] == "2026-08-15"
    assert first["pagination_complete"] is True
    assert first["source_page_count"] == 1
    assert first["normalized_record_count"] == 3
    assert first["source_evidence_refs"] == ["evidence:stable-source"]
    assert first["selection_count"] == 2
    assert first["selection_sha256"] == module._canonical_sha256(expected_items)
    assert first["batch_count"] == 2
    assert first["batch_plan_sha256"] == module._canonical_sha256(
        [[expected_items[0]], [expected_items[1]]]
    )
    assert first["items"] == expected_items
    assert len(first["source_snapshot_sha256"]) == 64
    assert {
        key: value for key, value in first.items() if key != "observed_at"
    } == {
        key: value for key, value in second.items() if key != "observed_at"
    }


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

    result = module.run_action(
        _formal_arguments(module, {"target_date": "2026-08-24"}, []),
        broker,
    )

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

    result = module.run_action(
        _formal_arguments(module, {"target_date": "2026-08-24"}, source),
        broker,
    )

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
        module.run_action(
            {"target_date": "2026-08-24", "dry_run": True},
            broker,
        )


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
        module.run_action(
            _formal_arguments(
                module,
                {"target_date": "2026-08-15"},
                [
                    {
                        "bill_code": child,
                        "destination": "A站",
                        "scan_type": "到货",
                        "scan_time": "2026-08-15 08:00:00",
                        "scan_site": "测试网点",
                    }
                ],
            ),
            broker,
        )

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


def test_formal_scan_requires_preview_binding_before_any_write():
    module = _load_action()
    calls: list[str] = []

    def broker(_operation, *, action, role, arguments):
        del role, arguments
        calls.append(action)
        assert action == "ronghui.scan.read_page"
        return {
            "items": [],
            "pagination_complete": True,
            "next_cursor": None,
            "evidence_ref": "evidence:source",
        }

    with pytest.raises(ValueError, match="requires a preview binding"):
        module.run_action({"target_date": "2026-08-15"}, broker)

    assert calls == []


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("source_snapshot_sha256", "3" * 64, "source_snapshot_sha256"),
        ("selection_sha256", "4" * 64, "selection_sha256"),
        ("batch_plan_sha256", "5" * 64, "batch_plan_sha256"),
    ],
)
def test_formal_scan_revalidates_exact_preview_hashes_before_any_write(
    field,
    replacement,
    message,
):
    module = _load_action()
    child = "R123456789010001"
    source = [
        {
            "bill_code": child,
            "destination": "A站",
            "scan_type": "到货",
            "scan_time": "2026-08-15 08:00:00",
            "scan_site": "测试网点",
        }
    ]
    arguments = _formal_arguments(
        module,
        {"target_date": "2026-08-15"},
        source,
    )
    binding = arguments["_scan_preview_binding"]
    binding[field] = replacement
    binding["context_sha256"] = module._canonical_sha256(
        {key: value for key, value in binding.items() if key != "context_sha256"}
    )
    calls: list[str] = []

    def broker(_operation, *, action, role, arguments):
        del role, arguments
        calls.append(action)
        assert action == "ronghui.scan.read_page"
        return {
            "items": source,
            "pagination_complete": True,
            "next_cursor": None,
            "evidence_ref": "evidence:fresh-source",
        }

    with pytest.raises(ValueError, match=message):
        module.run_action(arguments, broker)

    assert calls == ["ronghui.scan.read_page"]


def test_formal_scan_rejects_expired_preview_before_any_write():
    module = _load_action()
    arguments = _formal_arguments(
        module,
        {"target_date": "2026-08-15"},
        [],
    )
    binding = arguments["_scan_preview_binding"]
    expired_observation = datetime.now(timezone.utc) - timedelta(minutes=20)
    binding["observed_at"] = expired_observation.isoformat()
    binding["expires_at"] = (expired_observation + timedelta(minutes=15)).isoformat()
    binding["context_sha256"] = module._canonical_sha256(
        {key: value for key, value in binding.items() if key != "context_sha256"}
    )
    calls: list[str] = []

    def broker(_operation, *, action, role, arguments):
        del role, arguments
        calls.append(action)
        assert action == "ronghui.scan.read_page"
        return {
            "items": [],
            "pagination_complete": True,
            "next_cursor": None,
            "evidence_ref": "evidence:fresh-source",
        }

    with pytest.raises(ValueError, match="expired"):
        module.run_action(arguments, broker)

    assert calls == ["ronghui.scan.read_page"]
