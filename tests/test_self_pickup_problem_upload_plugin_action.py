from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agent.tms_runtime.scripts import self_pickup_problem_upload as legacy_action


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "agent" / "first_party_automation_plugins" / "self_pickup_problem_upload"
ACTION_SOURCE = PLUGIN_ROOT / "payload" / "action.py"
RESULT_SOURCE = ROOT / "agent" / "first_party_automation_plugins" / "_runtime" / "result.py"
_FORBIDDEN_LOCATION_KEYS = {
    "account_id",
    "account_ids",
    "app_token",
    "base_token",
    "resource_id",
    "session_profile",
    "sheet_id",
    "spreadsheet_token",
    "table_id",
    "tenant_access_token",
    "token",
}


def _load_action():
    result_spec = importlib.util.spec_from_file_location("boyi_plugin_result", RESULT_SOURCE)
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    action_spec = importlib.util.spec_from_file_location(
        "self_pickup_problem_upload_plugin_action",
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


def _source_rows() -> list[list[object]]:
    return [
        ["0601运单编号", "货物名称", "派送方式", "件数", "目的站点", "累计到货件数"],
        ["R_SELF", "配件", "派送", "3", "邵阳自提部", "3件"],
        ["R_PARTIAL", "配件", "派送", "3", "邵阳自提部", "2"],
        ["R_DX_PICK", "配件", "自提", "1", "邵阳大祥S站", "1.0"],
        ["R_DX_SEND", "配件", "派送", "1", "邵阳大祥S站", "1"],
        ["R_OTHER", "配件", "自提", "1", "其他站点", "1"],
        ["R_SELF", "配件", "派送", "3.0", "邵阳自提部", "3"],
    ]


def _source_response(rows: list[list[object]], index: int = 1) -> dict[str, object]:
    return {
        "complete": True,
        "evidence_ref": f"broker-evidence:source-{index}",
        "rows": rows,
    }


def _legacy_source_params() -> dict[str, str]:
    return {
        "account_id": "bound-self",
        "session_profile": "bound-self-profile",
        "daxiang_s_account_id": "bound-daxiang",
        "daxiang_s_session_profile": "bound-daxiang-profile",
    }


def _preview(action, rows: list[list[object]] | None = None):
    calls: list[dict[str, object]] = []

    def broker(operation, *, action, role, arguments):
        calls.append(
            {
                "action": action,
                "arguments": arguments,
                "operation": operation,
                "role": role,
            }
        )
        assert operation == "network.request"
        assert action == "feishu.sheet.read_rows"
        assert role == "self_pickup_source_sheet"
        assert arguments == {"end_column": "S", "max_rows": 2_000}
        _assert_no_resource_locations(arguments)
        return _source_response(rows or _source_rows())

    result = action.run_action({}, broker)
    return result, calls


def test_preview_owns_exact_source_filtering_deduplication_and_fingerprint():
    action, result_module = _load_action()

    result, calls = _preview(action)

    assert len(calls) == 1
    assert result_module.validate_result(result) == result
    assert result["status"] == "SUCCESS"
    assert result["meta"]["source_system"] == "feishu"
    assert result["meta"]["record_count"] == 2
    data = result["data"]
    assert data["dry_run"] is True
    assert data["candidate_count"] == 2
    assert data["duplicate_source_rows"] == 1
    assert [candidate["bill_code"] for candidate in data["candidates"]] == [
        "R_SELF",
        "R_DX_PICK",
    ]
    assert data["candidates"][0]["arrival_count"] == "3"
    assert data["candidates"][0]["goods_count"] == "3"
    assert data["candidates"][1]["arrival_count"] == "1"
    assert data["source_summaries"] == [
        {
            "candidate_count": 1,
            "source_id": "self_pickup_department",
            "source_name": "邵阳自提部",
        },
        {
            "candidate_count": 1,
            "source_id": "daxiang_s_self_pickup",
            "source_name": "邵阳大祥S站自提",
        },
    ]
    assert len(data["preview_fingerprint"]) == 64
    assert data["evidence"]["execution_result"] == "preview_only"
    assert result["meta"]["postconditions"] == {"0": True}
    proof = result["meta"]["postcondition_evidence"]["0"]
    assert proof["condition"] == "third_party_self_pickup_problem_confirmed"
    assert proof["details"]["write_attempted"] is False
    _assert_no_resource_locations(result)


def test_legacy_read_only_preview_fingerprint_matches_signed_candidate_material():
    action, _ = _load_action()
    rows = _source_rows()
    legacy_records = legacy_action._collect_waybills_from_values(
        rows,
        source_rules=legacy_action._source_rules(_legacy_source_params()),
        source_sheet_id="sheet-1",
        source_sheet_title="每日到货表",
    )
    signed_candidates, _duplicates = action._collect_candidates(
        rows,
        include_daxiang=True,
        limit=None,
    )

    assert legacy_action._preview_fingerprint(
        legacy_records
    ) == action._preview_fingerprint(signed_candidates)


def test_preview_fingerprint_is_stable_when_source_rows_are_reordered():
    action, _ = _load_action()
    original_rows = _source_rows()
    reordered_rows = _source_rows()
    reordered_rows[1], reordered_rows[3] = reordered_rows[3], reordered_rows[1]

    original_candidates, _ = action._collect_candidates(
        original_rows,
        include_daxiang=True,
        limit=None,
    )
    reordered_candidates, _ = action._collect_candidates(
        reordered_rows,
        include_daxiang=True,
        limit=None,
    )
    original_legacy = legacy_action._collect_waybills_from_values(
        original_rows,
        source_rules=legacy_action._source_rules(_legacy_source_params()),
        source_sheet_id="sheet-1",
        source_sheet_title="每日到货表",
    )
    reordered_legacy = legacy_action._collect_waybills_from_values(
        reordered_rows,
        source_rules=legacy_action._source_rules(_legacy_source_params()),
        source_sheet_id="sheet-1",
        source_sheet_title="每日到货表",
    )

    assert action._preview_fingerprint(original_candidates) == action._preview_fingerprint(
        reordered_candidates
    )
    assert legacy_action._preview_fingerprint(
        original_legacy
    ) == legacy_action._preview_fingerprint(reordered_legacy)

    original_limited, _ = action._collect_candidates(
        original_rows,
        include_daxiang=True,
        limit=1,
    )
    reordered_limited, _ = action._collect_candidates(
        reordered_rows,
        include_daxiang=True,
        limit=1,
    )
    original_legacy_limited = legacy_action._limited_preview_records(
        original_legacy,
        1,
    )
    reordered_legacy_limited = legacy_action._limited_preview_records(
        reordered_legacy,
        1,
    )
    assert [item["bill_code"] for item in original_limited] == [
        item["bill_code"] for item in reordered_limited
    ]
    assert action._preview_fingerprint(original_limited) == action._preview_fingerprint(
        reordered_limited
    )
    assert [item["bill_code"] for item in original_legacy_limited] == [
        item["bill_code"] for item in reordered_legacy_limited
    ]
    assert legacy_action._preview_fingerprint(
        original_legacy_limited
    ) == legacy_action._preview_fingerprint(reordered_legacy_limited)


def test_waybill_outer_whitespace_is_trimmed_in_preview_and_formal_selection():
    action, _ = _load_action()
    rows = _source_rows()
    rows[1][0] = "\t R_SELF \n"

    result, _calls = _preview(action, rows)
    assert result["data"]["candidates"][0]["bill_code"] == "R_SELF"
    assert action._selected_bill_codes(
        {
            "preview_fingerprint": "a" * 64,
            "selected_bill_codes": ["\t R_SELF \n"],
        },
        dry_run=False,
    ) == ["R_SELF"]
    with pytest.raises(
        ValueError,
        match="selected_bill_codes item contains internal whitespace",
    ):
        action._selected_bill_codes(
            {
                "preview_fingerprint": "a" * 64,
                "selected_bill_codes": ["R SELF"],
            },
            dry_run=False,
        )

    legacy_records = legacy_action._collect_waybills_from_values(
        rows,
        source_rules=legacy_action._source_rules(_legacy_source_params()),
        source_sheet_id="sheet-1",
        source_sheet_title="每日到货表",
    )
    assert legacy_records[0]["bill_code"] == "R_SELF"


def test_internal_whitespace_is_ignored_for_unrelated_rows_but_rejected_for_target_rows():
    action, _ = _load_action()
    unrelated = _source_rows() + [
        ["R BAD", "配件", "自提", "1", "其他站点", "1"],
    ]
    result, _calls = _preview(action, unrelated)
    assert result["data"]["candidate_count"] == 2
    legacy_records = legacy_action._collect_waybills_from_values(
        unrelated,
        source_rules=legacy_action._source_rules(_legacy_source_params()),
        source_sheet_id="sheet-1",
        source_sheet_title="每日到货表",
    )
    assert len(legacy_records) == 2

    target = _source_rows() + [
        ["R BAD", "配件", "派送", "1", "邵阳自提部", "1"],
    ]
    with pytest.raises(ValueError, match="row 8 waybill contains internal whitespace"):
        action._collect_candidates(target, include_daxiang=True, limit=None)
    with pytest.raises(ValueError, match="第 8 行运单号包含内部空白"):
        legacy_action._collect_waybills_from_values(
            target,
            source_rules=legacy_action._source_rules(_legacy_source_params()),
            source_sheet_id="sheet-1",
            source_sheet_title="每日到货表",
        )


def test_formal_selection_preflights_every_target_before_create_and_verifies_each_write():
    action, result_module = _load_action()
    preview, _ = _preview(action)
    fingerprint = preview["data"]["preview_fingerprint"]
    calls: list[dict[str, object]] = []
    created_arguments: list[tuple[str, dict[str, object]]] = []

    def broker(operation, *, action, role, arguments):
        _assert_no_resource_locations(arguments)
        calls.append(
            {
                "action": action,
                "arguments": arguments,
                "operation": operation,
                "role": role,
            }
        )
        evidence_ref = f"broker-evidence:{len(calls)}"
        if action == "feishu.sheet.read_rows":
            assert operation == "network.request"
            assert role == "self_pickup_source_sheet"
            return {**_source_response(_source_rows()), "evidence_ref": evidence_ref}
        if action == "ronghui.problem.query":
            assert operation == "browser.invoke"
            expected_role = "daxiang_s_account_id" if arguments["bill_code"] == "R_DX_PICK" else "account_id"
            assert role == expected_role
            return {
                "bill_code": arguments["bill_code"],
                "evidence_ref": evidence_ref,
                "precondition_ref": f"opaque-precondition:{arguments['bill_code']}",
                "ready": True,
            }
        if action == "ronghui.problem.create":
            assert operation == "browser.invoke"
            created_arguments.append((role, dict(arguments)))
            assert arguments["problem_type"] == "开单为自提件"
            assert arguments["problem_owner_type"] == "特殊时效"
            assert arguments["update_postpone_days"] is True
            return {
                "bill_code": arguments["bill_code"],
                "committed": True,
                "evidence_ref": evidence_ref,
                "external_id": f"problem:{arguments['bill_code']}",
                "postpone_updated": False,
            }
        if action == "ronghui.problem.verify":
            assert operation == "browser.invoke"
            return {
                "bill_code": arguments["bill_code"],
                "confirmed": True,
                "evidence_ref": evidence_ref,
                "external_id": arguments["external_id"],
                "problem_cause_sha256": arguments["problem_cause_sha256"],
                "problem_owner_type": arguments["problem_owner_type"],
                "problem_type": arguments["problem_type"],
                "registered_at": "2026-08-15T01:02:03Z",
            }
        raise AssertionError(action)

    result = action.run_action(
        {
            "dry_run": False,
            "preview_fingerprint": fingerprint,
            "selected_bill_codes": ["R_DX_PICK", "R_SELF"],
        },
        broker,
    )

    assert result_module.validate_result(result) == result
    assert [call["action"] for call in calls] == [
        "feishu.sheet.read_rows",
        "ronghui.problem.query",
        "ronghui.problem.query",
        "ronghui.problem.create",
        "ronghui.problem.verify",
        "ronghui.problem.create",
        "ronghui.problem.verify",
    ]
    assert [call["role"] for call in calls[1:]] == [
        "daxiang_s_account_id",
        "account_id",
        "daxiang_s_account_id",
        "daxiang_s_account_id",
        "account_id",
        "account_id",
    ]
    assert created_arguments[0][1]["problem_cause"] == action._DAXIANG_CAUSE
    assert created_arguments[1][1]["problem_cause"] == action._PRIMARY_CAUSE
    assert result["meta"]["record_count"] == 2
    assert result["data"]["selected_bill_codes"] == ["R_DX_PICK", "R_SELF"]
    assert all(row["verified"] is True for row in result["data"]["results"])
    proof = result["meta"]["postcondition_evidence"]["0"]
    assert proof["condition"] == "third_party_self_pickup_problem_confirmed"
    assert proof["details"]["confirmed_count"] == 2
    assert proof["details"]["verification_evidence_refs"] == [
        "broker-evidence:5",
        "broker-evidence:7",
    ]
    assert proof["evidence_ref"] == "broker-evidence:7"
    _assert_no_resource_locations(result)
    serialized = json.dumps(result, ensure_ascii=False)
    assert "opaque-precondition" not in serialized


def test_preview_drift_blocks_before_any_ronghui_call():
    action, _ = _load_action()
    preview, _ = _preview(action)
    changed = _source_rows()
    changed[1][-1] = "2"
    changed[6][-1] = "2"
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "feishu.sheet.read_rows":
            return _source_response(changed)
        raise AssertionError("Ronghui must not run after source drift")

    with pytest.raises(RuntimeError, match="SELECTION_PREVIEW_EXPIRED"):
        action.run_action(
            {
                "dry_run": False,
                "preview_fingerprint": preview["data"]["preview_fingerprint"],
                "selected_bill_codes": ["R_SELF"],
            },
            broker,
        )

    assert calls == ["feishu.sheet.read_rows"]


def test_unavailable_selection_blocks_before_any_ronghui_call():
    action, _ = _load_action()
    preview, _ = _preview(action)
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "feishu.sheet.read_rows":
            return _source_response(_source_rows())
        raise AssertionError("Ronghui must not run for an unavailable selection")

    with pytest.raises(ValueError, match="are unavailable"):
        action.run_action(
            {
                "dry_run": False,
                "preview_fingerprint": preview["data"]["preview_fingerprint"],
                "selected_bill_codes": ["R_UNKNOWN"],
            },
            broker,
        )

    assert calls == ["feishu.sheet.read_rows"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"account_id": "forbidden"},
        {"daxiang_s_account_id": "forbidden"},
        {"spreadsheet_token": "forbidden", "sheet_id": "forbidden"},
        {"problem_cause": "forbidden override"},
        {"screenshot_path": "forbidden"},
        {"screenshot_dir": "forbidden"},
        {"screenshot_map": {"R1": "forbidden"}},
        {"upload_screenshot": True},
        {"session_profile": "forbidden"},
    ],
)
def test_payload_rejects_account_resource_location_and_attachment_arguments(arguments):
    action, _ = _load_action()

    with pytest.raises(ValueError, match="undeclared fields"):
        action.run_action(arguments, lambda *_args, **_kwargs: None)


@pytest.mark.parametrize(
    "arguments",
    [
        {"dry_run": False, "preview_fingerprint": "a" * 64},
        {"dry_run": False, "selected_bill_codes": ["R_SELF"]},
        {
            "dry_run": False,
            "preview_fingerprint": "a" * 64,
            "selected_bill_codes": ["R_SELF", "R_SELF"],
        },
        {
            "dry_run": False,
            "preview_fingerprint": "a" * 64,
            "selected_bill_codes": [123],
        },
        {
            "dry_run": False,
            "preview_fingerprint": "a" * 64,
            "selected_bill_codes": [f"R{index:04d}" for index in range(251)],
        },
        {"dry_run": True, "selected_bill_codes": ["R_SELF"]},
    ],
)
def test_invalid_formal_or_preview_selection_is_rejected_before_source_read(arguments):
    action, _ = _load_action()

    with pytest.raises(ValueError):
        action.run_action(
            arguments,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected broker call")),
        )


def test_all_preflights_must_pass_before_the_first_create():
    action, _ = _load_action()
    preview, _ = _preview(action)
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        evidence_ref = f"broker-evidence:{len(calls)}"
        if action == "feishu.sheet.read_rows":
            return {**_source_response(_source_rows()), "evidence_ref": evidence_ref}
        if action == "ronghui.problem.query":
            return {
                "bill_code": arguments["bill_code"],
                "evidence_ref": evidence_ref,
                "precondition_ref": f"precondition:{arguments['bill_code']}",
                "ready": arguments["bill_code"] != "R_DX_PICK",
            }
        raise AssertionError("no create is allowed before all preflights pass")

    with pytest.raises(ValueError, match="did not confirm R_DX_PICK"):
        action.run_action(
            {
                "dry_run": False,
                "preview_fingerprint": preview["data"]["preview_fingerprint"],
                "selected_bill_codes": ["R_SELF", "R_DX_PICK"],
            },
            broker,
        )

    assert calls == [
        "feishu.sheet.read_rows",
        "ronghui.problem.query",
        "ronghui.problem.query",
    ]


def test_inconclusive_readback_fails_closed_after_the_external_write():
    action, _ = _load_action()
    preview, _ = _preview(action)
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        evidence_ref = f"broker-evidence:{len(calls)}"
        if action == "feishu.sheet.read_rows":
            return {**_source_response(_source_rows()), "evidence_ref": evidence_ref}
        if action == "ronghui.problem.query":
            return {
                "bill_code": arguments["bill_code"],
                "evidence_ref": evidence_ref,
                "precondition_ref": "precondition:R_SELF",
                "ready": True,
            }
        if action == "ronghui.problem.create":
            return {
                "bill_code": arguments["bill_code"],
                "committed": True,
                "evidence_ref": evidence_ref,
                "external_id": "problem:R_SELF",
                "postpone_updated": True,
            }
        if action == "ronghui.problem.verify":
            return {
                "bill_code": arguments["bill_code"],
                "confirmed": False,
                "evidence_ref": evidence_ref,
                "external_id": arguments["external_id"],
            }
        raise AssertionError(action)

    with pytest.raises(ValueError, match="read-back did not confirm"):
        action.run_action(
            {
                "dry_run": False,
                "preview_fingerprint": preview["data"]["preview_fingerprint"],
                "selected_bill_codes": ["R_SELF"],
            },
            broker,
        )

    assert calls == [
        "feishu.sheet.read_rows",
        "ronghui.problem.query",
        "ronghui.problem.create",
        "ronghui.problem.verify",
    ]


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        (["运单编号", "目的站点", "派送方式", "件数"], "arrival-count column"),
        (
            ["运单编号", "单号", "目的站点", "派送方式", "件数", "累计到货件数"],
            "ambiguous waybill column",
        ),
    ],
)
def test_missing_or_ambiguous_headers_fail_closed(headers, error):
    action, _ = _load_action()

    def broker(operation, *, action, role, arguments):
        return _source_response([headers])

    with pytest.raises(ValueError, match=error):
        action.run_action({}, broker)


def test_invalid_target_count_fails_instead_of_silently_dropping_the_waybill():
    action, _ = _load_action()
    rows = _source_rows()
    rows[1][-1] = "三件"

    def broker(operation, *, action, role, arguments):
        return _source_response(rows)

    with pytest.raises(ValueError, match="R_SELF arrival count"):
        action.run_action({}, broker)


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
    assert "self_pickup_problem_upload_tool" not in source
    assert "run_once" not in source
    assert '"/self_pickup_problem_upload"' not in source
    assert "call_http_service" not in source
    assert json.loads(json.dumps({"source": source}))["source"] == source
