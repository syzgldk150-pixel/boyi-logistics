from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from service_v2_plugins._shared.build_zip import build_plugin_zip
from tests.first_party_action_payload_support import load_first_party_action


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent" / "service_v2_plugins" / "split_pending_problem_upload_v2"
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "service_v2"
    / "split_pending_problem_upload_v2"
    / "split_pending_case.json"
)
SERVICE_NAME = (
    "plugin.split_pending_problem_upload_v2.split_pending_problem_upload@1"
)


class _HostResult(dict[str, object]):
    def __init__(self, value: dict[str, object], evidence_ref: str) -> None:
        super().__init__(value)
        self.host_evidence_ref = evidence_ref


@pytest.fixture(autouse=True)
def _restore_embedded_module_names():
    previous = {
        name: sys.modules.get(name)
        for name in ("boyi_plugin_result", "boyi_plugin_sdk", "plugin", "action")
    }
    yield
    for name, old in previous.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_runtime(payload: Path):
    result = _load_module("split_pending_embedded_result", payload / "boyi_plugin_result.py")
    sdk = _load_module("split_pending_embedded_sdk", payload / "boyi_plugin_sdk.py")
    sys.modules.update({"boyi_plugin_result": result, "boyi_plugin_sdk": sdk})
    plugin = _load_module("split_pending_embedded_plugin", payload / "plugin.py")
    action = _load_module("split_pending_embedded_action", payload / "action.py")
    sys.modules.update({"plugin": plugin, "action": action})
    runtime = _load_module("split_pending_embedded_runtime", payload / "main.py")
    return runtime, plugin


def _build_and_load(tmp_path: Path):
    package_path = tmp_path / "split-pending.zip"
    build_plugin_zip(SOURCE, package_path)
    install_root = tmp_path / "installed"
    install_root.mkdir()
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(install_root)
    return _load_runtime(install_root / "payload")


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _business_response(
    action: str,
    arguments: dict[str, object],
    *,
    stored: list[dict[str, object]],
) -> dict[str, object]:
    bill_code = str(arguments.get("bill_code") or "")
    if action == "split_pending.snapshot.read":
        return {"complete": True, "records": copy.deepcopy(stored)}
    if action == "ronghui.problem.query":
        return {
            "bill_code": bill_code,
            "existing": False,
            "precondition_ref": f"precondition:{bill_code}",
            "ready": True,
        }
    if action == "ronghui.problem.create":
        return {
            "bill_code": bill_code,
            "committed": True,
            "external_id": f"problem:{bill_code}",
        }
    if action == "ronghui.problem.verify":
        return {
            "bill_code": bill_code,
            "confirmed": True,
            "external_id": str(arguments["external_id"]),
            "problem_cause_sha256": str(arguments["problem_cause_sha256"]),
            "problem_owner_type": str(arguments["problem_owner_type"]),
            "problem_type": str(arguments["problem_type"]),
            "registered_at": "2026-08-31T01:02:03Z",
            "registered_site": "测试登记网点",
        }
    if action in {
        "split_pending.snapshot.replace",
        "feishu.sheet.replace_rows",
        "daily_sign.problem_event.upsert",
        "split_pending.result.upsert",
    }:
        return {"committed": True}
    raise AssertionError(action)


def _recording_brokers(
    rows: list[list[object]],
    stored: list[dict[str, object]],
    plugin,
    *,
    failure_action: str | None = None,
):
    v1_calls: list[dict[str, object]] = []
    v2_calls: list[dict[str, object]] = []

    def v1_broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        index = len(v1_calls) + 1
        reference = f"synthetic-host-evidence:{index}"
        call = {
            "operation": operation,
            "action": action,
            "role": role,
            "arguments": copy.deepcopy(arguments),
            "host_evidence_ref": reference,
        }
        v1_calls.append(call)
        if action == "feishu.sheet.read_rows":
            response = {"complete": True, "rows": copy.deepcopy(rows)}
        elif action == failure_action:
            raise RuntimeError(f"{action.replace('.', '_')}_UNCERTAIN")
        else:
            response = _business_response(action, arguments, stored=stored)
        response["evidence_ref"] = reference
        return response

    operation_map = {
        "read_rows": (
            plugin.SOURCE_CONNECTOR,
            "network.request",
            "feishu.sheet.read_rows",
            plugin.SOURCE_RESOURCE_ROLE,
        ),
        "snapshot_read": (
            plugin.PROJECTION_CONNECTOR,
            "projection.invoke",
            "split_pending.snapshot.read",
            plugin.TARGET_RESOURCE_ROLE,
        ),
        "problem_query": (
            plugin.RONGHUI_CONNECTOR,
            "browser.invoke",
            "ronghui.problem.query",
            plugin.ACCOUNT_ROLE,
        ),
        "snapshot_replace": (
            plugin.PROJECTION_CONNECTOR,
            "projection.invoke",
            "split_pending.snapshot.replace",
            plugin.TARGET_RESOURCE_ROLE,
        ),
        "replace_rows": (
            plugin.TARGET_CONNECTOR,
            "network.request",
            "feishu.sheet.replace_rows",
            plugin.TARGET_RESOURCE_ROLE,
        ),
        "problem_create": (
            plugin.RONGHUI_CONNECTOR,
            "browser.invoke",
            "ronghui.problem.create",
            plugin.ACCOUNT_ROLE,
        ),
        "problem_verify": (
            plugin.RONGHUI_CONNECTOR,
            "browser.invoke",
            "ronghui.problem.verify",
            plugin.ACCOUNT_ROLE,
        ),
        "event_upsert": (
            plugin.LEDGER_CONNECTOR,
            "ledger.invoke",
            "daily_sign.problem_event.upsert",
            plugin.ACCOUNT_ROLE,
        ),
        "result_upsert": (
            plugin.PROJECTION_CONNECTOR,
            "projection.invoke",
            "split_pending.result.upsert",
            plugin.TARGET_RESOURCE_ROLE,
        ),
    }

    def v2_host_broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        assert operation == "service.invoke"
        assert action in operation_map
        assert role == plugin.SYSTEM_ROLE
        service, primitive_operation, primitive_action, primitive_role = operation_map[action]
        assert arguments["service"] == service
        assert arguments["operation"] == action
        nested_arguments = copy.deepcopy(arguments["arguments"])
        reference = f"synthetic-host-evidence:{len(v2_calls) + 1}"
        call = {
            "operation": primitive_operation,
            "action": primitive_action,
            "role": primitive_role,
            "arguments": nested_arguments,
            "outer_action": action,
            "outer_arguments": copy.deepcopy(arguments),
            "host_evidence_ref": reference,
        }
        v2_calls.append(call)
        if primitive_action == "feishu.sheet.read_rows":
            response = {"complete": True, "rows": copy.deepcopy(rows)}
        elif primitive_action == failure_action:
            raise RuntimeError(f"{primitive_action.replace('.', '_')}_UNCERTAIN")
        else:
            response = _business_response(
                primitive_action,
                nested_arguments,
                stored=stored,
            )
        return _HostResult(response, reference)

    return v1_broker, v2_host_broker, v1_calls, v2_calls


def _stable_data(result: dict[str, object]) -> dict[str, object]:
    data = copy.deepcopy(result["data"])
    assert isinstance(data, dict)
    evidence = data.get("evidence")
    assert isinstance(evidence, dict)
    evidence["observed_at"] = "OBSERVED_AT"
    return data


def _formal_arguments(
    preview: dict[str, object],
    selected: list[str],
) -> dict[str, object]:
    return {
        "dry_run": False,
        "preview_fingerprint": str(preview["data"]["preview_fingerprint"]),
        "selected_bill_codes": list(selected),
    }


def test_v1_and_v2_preview_preserve_19_column_classification_and_conservation(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    rows = copy.deepcopy(fixture["rows"])
    stored = copy.deepcopy(fixture["stored"])
    expected = fixture["expected"]
    assert all(len(row) == 19 for row in rows)

    v1_action = load_first_party_action("split_pending_problem_upload")
    runtime, plugin = _build_and_load(tmp_path)
    v1_broker, v2_broker, v1_calls, v2_calls = _recording_brokers(
        rows,
        stored,
        plugin,
    )
    arguments = copy.deepcopy(fixture["arguments"])

    v1_result = v1_action.run_action(arguments, v1_broker)
    v2_result = runtime.run_split_pending_action_offline(
        "preview",
        arguments,
        v2_broker,
    )

    assert [call["action"] for call in v1_calls] == expected["preview_primitive_actions"]
    assert [call["action"] for call in v2_calls] == expected["preview_primitive_actions"]
    assert [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v1_calls
    ] == [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v2_calls
    ]
    assert _stable_data(v1_result) == _stable_data(v2_result)

    data = v2_result["data"]
    assert data["source_rows"] == expected["source_rows"]
    assert data["snapshot_count"] == expected["snapshot_count"]
    assert data["complete_count"] == expected["complete_count"]
    assert data["candidate_count"] == expected["candidate_count"]
    assert data["hidden_completed_count"] == expected["hidden_completed_count"]
    assert [item["bill_code"] for item in data["candidates"]] == expected[
        "candidate_bill_codes"
    ]
    assert data["type_counts"] == expected["type_counts"]
    assert data["source_rows"] == data["complete_count"] + data["snapshot_count"]
    assert data["snapshot_count"] == (
        data["candidate_count"] + data["hidden_completed_count"]
    )
    assert all(
        item["expected_quantity"]
        == item["arrived_quantity"] + item["pending_quantity"]
        for item in data["candidates"]
    )

    _unused_v1, repeat_broker, _unused_calls, repeat_calls = _recording_brokers(
        rows,
        stored,
        plugin,
    )
    repeat = runtime.run_split_pending_action_offline(
        "preview",
        copy.deepcopy(arguments),
        repeat_broker,
    )
    assert repeat["data"]["candidates"] == data["candidates"]
    assert repeat["data"]["preview_fingerprint"] == data["preview_fingerprint"]
    assert [call["action"] for call in repeat_calls] == expected[
        "preview_primitive_actions"
    ]
    assert v2_calls[0]["outer_arguments"]["preflight_services"] == list(
        plugin.PREVIEW_PREFLIGHT_SERVICES
    )
    assert "preflight_services" not in v2_calls[1]["outer_arguments"]


@pytest.mark.parametrize("drift", ("header", "too_many_columns", "quantity"))
def test_invalid_19_column_source_fails_before_state_or_write(
    tmp_path: Path,
    drift: str,
) -> None:
    fixture = _fixture()
    rows = copy.deepcopy(fixture["rows"])
    if drift == "header":
        rows[0][7] = "漂移体积列"
    elif drift == "too_many_columns":
        rows[1].append("未声明列")
    else:
        rows[1][18] = rows[1][4] + 1

    runtime, plugin = _build_and_load(tmp_path)
    _v1_broker, host_broker, _v1_calls, v2_calls = _recording_brokers(
        rows,
        copy.deepcopy(fixture["stored"]),
        plugin,
    )
    with pytest.raises(ValueError):
        runtime.run_split_pending_action_offline(
            "preview",
            copy.deepcopy(fixture["arguments"]),
            host_broker,
        )
    assert [call["action"] for call in v2_calls] == ["feishu.sheet.read_rows"]


def test_v1_and_v2_formal_subset_preserve_projection_order_and_ticket_evidence(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    rows = copy.deepcopy(fixture["rows"])
    stored = copy.deepcopy(fixture["stored"])
    expected = fixture["expected"]
    v1_action = load_first_party_action("split_pending_problem_upload")
    runtime, plugin = _build_and_load(tmp_path)

    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        rows,
        stored,
        plugin,
    )
    preview = v1_action.run_action(copy.deepcopy(fixture["arguments"]), preview_broker)
    arguments = _formal_arguments(preview, expected["selected_bill_codes"])
    v1_broker, v2_broker, v1_calls, v2_calls = _recording_brokers(
        rows,
        stored,
        plugin,
    )

    v1_result = v1_action.run_action(copy.deepcopy(arguments), v1_broker)
    v2_result = runtime.run_split_pending_action_offline(
        "execute",
        copy.deepcopy(arguments),
        v2_broker,
    )

    assert [call["action"] for call in v1_calls] == expected["formal_primitive_actions"]
    assert [call["action"] for call in v2_calls] == expected["formal_primitive_actions"]
    assert [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v1_calls
    ] == [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v2_calls
    ]
    assert _stable_data(v1_result) == _stable_data(v2_result)
    assert v2_calls[0]["outer_arguments"]["preflight_services"] == list(
        plugin.EXECUTE_PREFLIGHT_SERVICES
    )
    assert all(
        "preflight_services" not in call["outer_arguments"] for call in v2_calls[1:]
    )

    data = v2_result["data"]
    selected = expected["selected_bill_codes"]
    assert data["selected_bill_codes"] == selected
    assert [item["bill_code"] for item in data["results"]] == selected
    assert v2_result["meta"]["record_count"] == len(selected) == len(data["results"])

    snapshot_call = next(
        call for call in v2_calls if call["action"] == "split_pending.snapshot.replace"
    )
    snapshot_codes = [
        item["bill_code"] for item in snapshot_call["arguments"]["records"]
    ]
    assert snapshot_codes == ["R_SPLIT_A", "R_ZERO", "R_SPLIT_B", "R_HIDDEN"]
    sheet_call = next(
        call for call in v2_calls if call["action"] == "feishu.sheet.replace_rows"
    )
    sheet_rows = sheet_call["arguments"]["rows"]
    assert len(sheet_rows[0]) == 19
    assert [row[0] for row in sheet_rows[1:]] == snapshot_codes
    assert all(len(row) == 19 for row in sheet_rows)

    per_ticket_actions = {
        code: [
            call["action"]
            for call in v2_calls
            if call["arguments"].get("bill_code") == code
        ]
        for code in selected
    }
    assert per_ticket_actions == {
        code: [
            "ronghui.problem.query",
            "ronghui.problem.create",
            "ronghui.problem.verify",
            "daily_sign.problem_event.upsert",
            "split_pending.result.upsert",
        ]
        for code in selected
    }
    assert not any(
        call["arguments"].get("bill_code") in {"R_SPLIT_A", "R_HIDDEN"}
        for call in v2_calls
        if call["action"].startswith("ronghui.problem")
    )

    all_refs = [call["host_evidence_ref"] for call in v2_calls]
    assert len(all_refs) == len(set(all_refs))
    assert v2_result["meta"]["evidence_refs"] == all_refs
    verification_calls = [
        call for call in v2_calls if call["action"] == "ronghui.problem.verify"
    ]
    assert [call["arguments"]["bill_code"] for call in verification_calls] == selected
    verification_refs = [call["host_evidence_ref"] for call in verification_calls]
    assert v2_result["meta"]["postcondition_evidence"]["0"]["details"][
        "verification_evidence_refs"
    ] == verification_refs


def test_source_drift_expires_formal_selection_before_problem_preflight(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    original_rows = copy.deepcopy(fixture["rows"])
    stored = copy.deepcopy(fixture["stored"])
    v1_action = load_first_party_action("split_pending_problem_upload")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        original_rows,
        stored,
        plugin,
    )
    preview = v1_action.run_action(copy.deepcopy(fixture["arguments"]), preview_broker)
    changed_rows = copy.deepcopy(original_rows)
    changed_rows[1][18] = 3
    _v1, changed_broker, _v1_calls, changed_calls = _recording_brokers(
        changed_rows,
        stored,
        plugin,
    )

    with pytest.raises(ValueError, match="preview expired"):
        runtime.run_split_pending_action_offline(
            "execute",
            _formal_arguments(preview, ["R_SPLIT_A"]),
            changed_broker,
        )
    assert [call["action"] for call in changed_calls] == [
        "feishu.sheet.read_rows",
        "split_pending.snapshot.read",
    ]


def test_execute_preflights_every_connector_before_any_primitive_or_write(
    tmp_path: Path,
) -> None:
    runtime, plugin = _build_and_load(tmp_path)
    calls: list[dict[str, object]] = []

    def host_broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": copy.deepcopy(arguments),
            }
        )
        raise RuntimeError("BROKER_ROLE_UNBOUND")

    tracker = runtime._ExecutionTracker()
    old_broker = runtime.broker_call
    runtime.broker_call = host_broker
    try:
        with pytest.raises(RuntimeError, match="BROKER_ROLE_UNBOUND"):
            runtime.run_split_pending_service(
                "execute",
                {
                    "dry_run": False,
                    "preview_fingerprint": "a" * 64,
                    "selected_bill_codes": ["R_ZERO"],
                },
                tracker=tracker,
            )
    finally:
        runtime.broker_call = old_broker

    assert len(calls) == 1
    assert calls[0]["operation"] == "service.invoke"
    assert calls[0]["action"] == "read_rows"
    assert calls[0]["role"] == plugin.SYSTEM_ROLE
    assert calls[0]["arguments"]["preflight_services"] == list(
        plugin.EXECUTE_PREFLIGHT_SERVICES
    )
    assert tracker.mutating_started is False
    assert tracker.host_refs == []


def test_preview_service_is_read_only_and_retains_exact_host_evidence(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    runtime, plugin = _build_and_load(tmp_path)
    _v1, host_broker, _v1_calls, v2_calls = _recording_brokers(
        copy.deepcopy(fixture["rows"]),
        copy.deepcopy(fixture["stored"]),
        plugin,
    )
    tracker = runtime._ExecutionTracker()
    old_broker = runtime.broker_call
    runtime.broker_call = host_broker
    try:
        result = runtime.run_split_pending_service(
            "preview",
            copy.deepcopy(fixture["arguments"]),
            tracker=tracker,
        )
    finally:
        runtime.broker_call = old_broker

    refs = [call["host_evidence_ref"] for call in v2_calls]
    assert result["status"] == "SUCCESS"
    assert result["data"]["evidence"]["service"] == SERVICE_NAME
    assert result["data"]["evidence"]["operation"] == "preview"
    assert result["data"]["evidence"]["outcome"] == "READ_ONLY"
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"
    assert result["meta"]["evidence_refs"] == refs == tracker.host_refs
    assert tracker.mutating_started is False


def test_execute_service_projects_verified_results_and_all_host_evidence(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    rows = copy.deepcopy(fixture["rows"])
    stored = copy.deepcopy(fixture["stored"])
    selected = list(fixture["expected"]["selected_bill_codes"])
    runtime, plugin = _build_and_load(tmp_path)
    v1_action = load_first_party_action("split_pending_problem_upload")
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        rows,
        stored,
        plugin,
    )
    preview = v1_action.run_action(copy.deepcopy(fixture["arguments"]), preview_broker)
    _v1, host_broker, _v1_calls, v2_calls = _recording_brokers(
        rows,
        stored,
        plugin,
    )
    tracker = runtime._ExecutionTracker()
    old_broker = runtime.broker_call
    runtime.broker_call = host_broker
    try:
        result = runtime.run_split_pending_service(
            "execute",
            _formal_arguments(preview, selected),
            tracker=tracker,
        )
    finally:
        runtime.broker_call = old_broker

    refs = [call["host_evidence_ref"] for call in v2_calls]
    verification_refs = [
        call["host_evidence_ref"]
        for call in v2_calls
        if call["action"] == "ronghui.problem.verify"
    ]
    assert result["status"] == "SUCCESS"
    assert result["data"]["selected_bill_codes"] == selected
    assert all(item["verified"] is True for item in result["data"]["results"])
    assert result["data"]["evidence"]["service"] == SERVICE_NAME
    assert result["data"]["evidence"]["operation"] == "execute"
    assert result["data"]["evidence"]["outcome"] == "WRITE_VERIFIED"
    assert result["meta"]["write_outcome"] == "WRITE_VERIFIED"
    assert result["meta"]["evidence_refs"] == refs == tracker.host_refs
    assert result["meta"]["postcondition_evidence"]["0"]["details"][
        "evidence_refs"
    ] == refs
    retained_action_proof = result["meta"]["postcondition_evidence"]["0"][
        "details"
    ]["action_postcondition_evidence"]["0"]
    assert retained_action_proof["details"]["selected_bill_codes"] == selected
    assert retained_action_proof["details"][
        "verification_evidence_refs"
    ] == verification_refs
    assert retained_action_proof["evidence_ref"] == verification_refs[-1]
    assert tracker.mutating_started is True


def test_signed_90_ticket_limit_rejects_the_91st_before_any_connector_call(
    tmp_path: Path,
) -> None:
    runtime, _plugin = _build_and_load(tmp_path)
    calls: list[object] = []

    def host_broker(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("Connector must not run after selection validation fails")

    with pytest.raises(ValueError, match="signed limit"):
        runtime.run_split_pending_action_offline(
            "execute",
            {
                "dry_run": False,
                "preview_fingerprint": "a" * 64,
                "selected_bill_codes": [f"R{index:03d}" for index in range(91)],
            },
            host_broker,
        )
    assert calls == []


def _service_request(plugin, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "runtime_model": "SERVICE_V2",
        "automation_id": "split-pending-test",
        "plugin_id": plugin.PLUGIN_ID,
        "plugin_version": "1.0.0",
        "entrypoint": "service",
        "target": {
            "service": SERVICE_NAME,
            "operation": "execute",
            "contribution_id": "host.service.invoke",
            "contribution_kind": "service",
        },
        "governance": {
            "effect": "external_write",
            "operation_type": "external_write",
            "broker_effect": "write",
            "harness_allowed": False,
        },
        "arguments": arguments,
    }


@pytest.mark.parametrize(
    ("failure_action", "expected_outcome"),
    (
        ("ronghui.problem.query", "NOT_APPLIED"),
        ("split_pending.snapshot.replace", "WRITE_OUTCOME_UNKNOWN"),
        ("feishu.sheet.replace_rows", "WRITE_OUTCOME_UNKNOWN"),
        ("ronghui.problem.create", "WRITE_OUTCOME_UNKNOWN"),
        ("ronghui.problem.verify", "WRITE_OUTCOME_UNKNOWN"),
        ("daily_sign.problem_event.upsert", "WRITE_OUTCOME_UNKNOWN"),
        ("split_pending.result.upsert", "WRITE_OUTCOME_UNKNOWN"),
    ),
)
def test_runtime_failure_respects_first_mutation_write_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_action: str,
    expected_outcome: str,
) -> None:
    fixture = _fixture()
    rows = copy.deepcopy(fixture["rows"])
    stored = copy.deepcopy(fixture["stored"])
    runtime, plugin = _build_and_load(tmp_path)
    v1_action = load_first_party_action("split_pending_problem_upload")
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        rows,
        stored,
        plugin,
    )
    preview = v1_action.run_action(copy.deepcopy(fixture["arguments"]), preview_broker)
    arguments = _formal_arguments(preview, ["R_ZERO"])
    _v1, host_broker, _v1_calls, _v2_calls = _recording_brokers(
        rows,
        stored,
        plugin,
        failure_action=failure_action,
    )

    monkeypatch.setenv("BOYI_AUTOMATION_ID", "split-pending-test")
    monkeypatch.setenv("BOYI_PLUGIN_ID", plugin.PLUGIN_ID)
    monkeypatch.setenv("BOYI_PLUGIN_VERSION", "1.0.0")
    old_broker = runtime.broker_call
    old_stdin, old_stdout = runtime.sys.stdin, runtime.sys.stdout
    runtime.broker_call = host_broker
    runtime.sys.stdin = io.StringIO(
        json.dumps(_service_request(plugin, arguments), ensure_ascii=False)
    )
    runtime.sys.stdout = io.StringIO()
    try:
        assert runtime.main() == 0
        output = runtime.sys.stdout.getvalue()
    finally:
        runtime.broker_call = old_broker
        runtime.sys.stdin, runtime.sys.stdout = old_stdin, old_stdout

    result = json.loads(output)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == failure_action.replace(".", "_").upper() + "_UNCERTAIN"
    assert result["meta"]["write_outcome"] == expected_outcome
    assert result["error"]["retryable"] is False
