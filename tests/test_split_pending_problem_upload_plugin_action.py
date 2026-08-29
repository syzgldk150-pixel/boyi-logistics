from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tools import split_pending_problem_upload_tool as legacy_action


ROOT = Path(__file__).resolve().parents[1]
ACTION_SOURCE = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "split_pending_problem_upload"
    / "payload"
    / "action.py"
)
RESULT_SOURCE = (
    ROOT / "agent" / "first_party_automation_plugins" / "_runtime" / "result.py"
)


def _load_action():
    result_spec = importlib.util.spec_from_file_location("boyi_plugin_result", RESULT_SOURCE)
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    action_spec = importlib.util.spec_from_file_location(
        "split_pending_problem_upload_plugin_action",
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
    return action_module, result_module


def _header() -> list[str]:
    return [
        "运单编号",
        "货物名称",
        "包装类型",
        "派送方式",
        "件数",
        "回单号",
        "实际重量",
        "体积",
        "备注",
        "目的站点",
        "收件人",
        "收件电话",
        "收件地址",
        "结算重量",
        "体积重",
        "运费",
        "支付类型",
        "到付款",
        "累计到货件数",
    ]


def _row(code: str, expected: int, arrived: int) -> list[object]:
    return [
        code,
        "配件",
        "纸箱",
        "派送",
        expected,
        "",
        "1",
        "0.001",
        "",
        "目的站",
        "收件人",
        "",
        "地址",
        "1",
        "0",
        "0",
        "现付",
        "0",
        arrived,
    ]


def _source_rows() -> list[list[object]]:
    return [
        _header(),
        _row("R_SPLIT", 3, 1),
        _row("R_ZERO", 2, 0),
        _row("R_COMPLETE", 1, 1),
    ]


def _preview(action, *, rows=None, stored=None):
    calls: list[dict[str, object]] = []

    def broker(operation, *, action, role, arguments):
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": arguments,
            }
        )
        if action == "feishu.sheet.read_rows":
            assert operation == "network.request"
            assert role == "split_pending_source_sheet"
            return {
                "complete": True,
                "evidence_ref": "broker-evidence:source",
                "rows": rows or _source_rows(),
            }
        if action == "split_pending.snapshot.read":
            assert operation == "projection.invoke"
            assert role == "split_pending_target_sheet"
            return {
                "complete": True,
                "evidence_ref": "broker-evidence:snapshot",
                "records": stored or [],
            }
        raise AssertionError(action)

    return action.run_action({}, broker), calls


def test_preview_owns_classification_state_join_and_fingerprint():
    action, result_module = _load_action()
    stored = [
        {
            "tracking_number": "R_ZERO",
            "problem_type": "有发未到",
            "upload_status": "success",
            "complaint_status": "not_applicable",
        }
    ]

    result, calls = _preview(action, stored=stored)

    assert [call["action"] for call in calls] == [
        "feishu.sheet.read_rows",
        "split_pending.snapshot.read",
    ]
    assert result_module.validate_result(result) == result
    assert result["status"] == "SUCCESS"
    assert result["meta"]["source_system"] == "feishu+mysql"
    assert result["data"]["source_rows"] == 3
    assert result["data"]["snapshot_count"] == 2
    assert result["data"]["complete_count"] == 1
    assert result["data"]["hidden_completed_count"] == 1
    assert [item["bill_code"] for item in result["data"]["candidates"]] == [
        "R_SPLIT"
    ]
    assert result["data"]["candidates"][0]["problem_type"] == "少货/分批"
    classified, _source_rows_count = action._classify(action._normalized_rows(_source_rows()))
    assert classified[0]["problem_cause"] == "应到3件 实际到1件"
    assert len(result["data"]["preview_fingerprint"]) == 64
    assert result["meta"]["postcondition_evidence"]["0"]["details"][
        "write_attempted"
    ] is False


def test_legacy_read_only_preview_fingerprint_matches_signed_candidate_material():
    action, _ = _load_action()
    rows = _source_rows()
    stored = [
        {
            "tracking_number": "R_SPLIT",
            "problem_type": "少货/分批",
            "upload_status": "failed",
            "complaint_status": "failed",
        },
        {
            "tracking_number": "R_ZERO",
            "problem_type": "有发未到",
            "upload_status": "pending",
            "complaint_status": "not_applicable",
        },
    ]

    signed_result, _ = _preview(action, rows=rows, stored=stored)
    legacy_candidates, _ = legacy_action.classify_sheet_values(rows)
    legacy_eligible, legacy_hidden = legacy_action._stateful_candidates(
        legacy_candidates,
        stored,
    )

    assert legacy_hidden == signed_result["data"]["hidden_completed_count"]
    assert [item["bill_code"] for item in legacy_eligible] == [
        item["bill_code"] for item in signed_result["data"]["candidates"]
    ]
    assert legacy_action._preview_fingerprint(legacy_eligible) == signed_result["data"][
        "preview_fingerprint"
    ]


def test_formal_selection_preflights_all_before_internal_or_external_writes():
    action, result_module = _load_action()
    preview, _ = _preview(action)
    fingerprint = preview["data"]["preview_fingerprint"]
    calls: list[dict[str, object]] = []

    def broker(operation, *, action, role, arguments):
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": arguments,
            }
        )
        evidence_ref = f"broker-evidence:{len(calls)}"
        if action == "feishu.sheet.read_rows":
            return {"complete": True, "evidence_ref": evidence_ref, "rows": _source_rows()}
        if action == "split_pending.snapshot.read":
            return {"complete": True, "evidence_ref": evidence_ref, "records": []}
        if action == "ronghui.problem.query":
            return {
                "bill_code": arguments["bill_code"],
                "evidence_ref": evidence_ref,
                "existing": False,
                "precondition_ref": f"problem-precondition:{arguments['bill_code']}",
                "ready": True,
            }
        if action in {"split_pending.snapshot.replace", "feishu.sheet.replace_rows"}:
            return {"committed": True, "evidence_ref": evidence_ref}
        if action == "ronghui.problem.create":
            if arguments["bill_code"] == "R_SPLIT":
                assert arguments["problem_cause"] == "应到3件 实际到1件"
            return {
                "committed": True,
                "evidence_ref": evidence_ref,
                "external_id": f"problem:{arguments['bill_code']}",
            }
        if action == "ronghui.problem.verify":
            return {
                "bill_code": arguments["bill_code"],
                "confirmed": True,
                "evidence_ref": evidence_ref,
                "external_id": arguments["external_id"],
                "problem_cause_sha256": arguments["problem_cause_sha256"],
                "problem_owner_type": arguments["problem_owner_type"],
                "problem_type": arguments["problem_type"],
                "registered_at": "2026-08-15T01:02:03Z",
                "registered_site": "登记网点",
            }
        if action in {
            "split_pending.result.upsert",
            "daily_sign.problem_event.upsert",
        }:
            return {"committed": True, "evidence_ref": evidence_ref}
        raise AssertionError(action)

    result = action.run_action(
        {
            "dry_run": False,
            "preview_fingerprint": fingerprint,
            "selected_bill_codes": ["R_SPLIT", "R_ZERO"],
        },
        broker,
    )

    actions = [call["action"] for call in calls]
    assert actions[:4] == [
        "feishu.sheet.read_rows",
        "split_pending.snapshot.read",
        "ronghui.problem.query",
        "ronghui.problem.query",
    ]
    assert actions[4:6] == [
        "split_pending.snapshot.replace",
        "feishu.sheet.replace_rows",
    ]
    assert not any(action_name.startswith("ronghui.complaint.") for action_name in actions)
    assert result_module.validate_result(result) == result
    assert result["meta"]["record_count"] == 2
    assert result["data"]["selected_bill_codes"] == ["R_SPLIT", "R_ZERO"]
    assert all(item["verified"] is True for item in result["data"]["results"])
    assert result["meta"]["postcondition_evidence"]["0"]["details"][
        "confirmed_count"
    ] == 2


def test_event_failure_does_not_mark_problem_result_complete():
    action, _ = _load_action()
    preview, _ = _preview(action)
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        evidence_ref = f"broker-evidence:{len(calls)}"
        if action == "feishu.sheet.read_rows":
            return {"complete": True, "evidence_ref": evidence_ref, "rows": _source_rows()}
        if action == "split_pending.snapshot.read":
            return {"complete": True, "evidence_ref": evidence_ref, "records": []}
        if action == "ronghui.problem.query":
            return {
                "bill_code": arguments["bill_code"],
                "evidence_ref": evidence_ref,
                "existing": False,
                "precondition_ref": "problem-precondition",
                "ready": True,
            }
        if action in {"split_pending.snapshot.replace", "feishu.sheet.replace_rows"}:
            return {"committed": True, "evidence_ref": evidence_ref}
        if action == "ronghui.problem.create":
            return {
                "committed": True,
                "evidence_ref": evidence_ref,
                "external_id": "problem:R_SPLIT",
            }
        if action == "ronghui.problem.verify":
            return {
                "bill_code": arguments["bill_code"],
                "confirmed": True,
                "evidence_ref": evidence_ref,
                "external_id": arguments["external_id"],
                "problem_cause_sha256": arguments["problem_cause_sha256"],
                "problem_owner_type": arguments["problem_owner_type"],
                "problem_type": arguments["problem_type"],
                "registered_at": "2026-08-15T01:02:03Z",
                "registered_site": "登记网点",
            }
        if action == "daily_sign.problem_event.upsert":
            raise RuntimeError("event write failed")
        raise AssertionError(action)

    with pytest.raises(RuntimeError, match="event write failed"):
        action.run_action(
            {
                "dry_run": False,
                "preview_fingerprint": preview["data"]["preview_fingerprint"],
                "selected_bill_codes": ["R_SPLIT"],
            },
            broker,
        )
    assert "daily_sign.problem_event.upsert" in calls
    assert "split_pending.result.upsert" not in calls


def test_failed_late_preflight_prevents_every_write():
    action, _ = _load_action()
    preview, _ = _preview(action)
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        evidence_ref = f"broker-evidence:{len(calls)}"
        if action == "feishu.sheet.read_rows":
            return {"complete": True, "evidence_ref": evidence_ref, "rows": _source_rows()}
        if action == "split_pending.snapshot.read":
            return {"complete": True, "evidence_ref": evidence_ref, "records": []}
        if action == "ronghui.problem.query":
            return {
                "bill_code": arguments["bill_code"],
                "evidence_ref": evidence_ref,
                "precondition_ref": "problem-precondition",
                "ready": arguments["bill_code"] != "R_ZERO",
            }
        raise AssertionError("no write is allowed before all preflights pass")

    with pytest.raises(ValueError, match="did not confirm R_ZERO"):
        action.run_action(
            {
                "dry_run": False,
                "preview_fingerprint": preview["data"]["preview_fingerprint"],
                "selected_bill_codes": ["R_SPLIT", "R_ZERO"],
            },
            broker,
        )
    assert calls == [
        "feishu.sheet.read_rows",
        "split_pending.snapshot.read",
        "ronghui.problem.query",
        "ronghui.problem.query",
    ]


def test_source_or_state_drift_blocks_before_preflight():
    action, _ = _load_action()
    preview, _ = _preview(action)
    changed = _source_rows()
    changed[1][18] = 2
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "feishu.sheet.read_rows":
            return {
                "complete": True,
                "evidence_ref": "broker-evidence:source",
                "rows": changed,
            }
        if action == "split_pending.snapshot.read":
            return {
                "complete": True,
                "evidence_ref": "broker-evidence:snapshot",
                "records": [],
            }
        raise AssertionError("Ronghui must not run after preview drift")

    with pytest.raises(ValueError, match="preview expired"):
        action.run_action(
            {
                "dry_run": False,
                "preview_fingerprint": preview["data"]["preview_fingerprint"],
                "selected_bill_codes": ["R_SPLIT"],
            },
            broker,
        )
    assert calls == ["feishu.sheet.read_rows", "split_pending.snapshot.read"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"account_id": "forbidden"},
        {"source_resource_id": "forbidden"},
        {"spreadsheet_token": "forbidden", "sheet_id": "forbidden"},
        {"items": []},
        {"problem_cause": "forbidden"},
    ],
)
def test_payload_rejects_broker_side_channel_and_business_overrides(arguments):
    action, _ = _load_action()
    with pytest.raises(ValueError, match="undeclared fields"):
        action.run_action(arguments, lambda *_args, **_kwargs: None)


def test_payload_is_standalone_and_has_no_whole_tool_fallback():
    source = ACTION_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(
        name == "agent"
        or name.startswith("agent.")
        or name == "tools"
        or name.startswith("tools.")
        for name in imported
    )
    assert "split_pending_problem_upload_tool" not in source
    assert "run_once" not in source
    assert "call_http_service" not in source
    assert json.loads(json.dumps({"source": source}))["source"] == source
