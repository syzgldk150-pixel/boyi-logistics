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
SOURCE = ROOT / "agent" / "service_v2_plugins" / "self_pickup_problem_upload_v2"
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "service_v2"
    / "self_pickup_problem_upload_v2"
    / "self_pickup_case.json"
)
SERVICE_NAME = "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1"


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
    result = _load_module("self_pickup_embedded_result", payload / "boyi_plugin_result.py")
    sdk = _load_module("self_pickup_embedded_sdk", payload / "boyi_plugin_sdk.py")
    sys.modules.update(
        {
            "boyi_plugin_result": result,
            "boyi_plugin_sdk": sdk,
        }
    )
    plugin = _load_module("self_pickup_embedded_plugin", payload / "plugin.py")
    action = _load_module("self_pickup_embedded_action", payload / "action.py")
    sys.modules.update({"plugin": plugin, "action": action})
    runtime = _load_module("self_pickup_embedded_runtime", payload / "main.py")
    return runtime, plugin


def _build_and_load(tmp_path: Path):
    package_path = tmp_path / "self-pickup.zip"
    build_plugin_zip(SOURCE, package_path)
    install_root = tmp_path / "installed"
    install_root.mkdir()
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(install_root)
    return _load_runtime(install_root / "payload")


def _business_response(
    action: str,
    arguments: dict[str, object],
    evidence_ref: str,
) -> dict[str, object]:
    bill_code = str(arguments.get("bill_code") or "")
    if action == "feishu.sheet.read_rows":
        raise AssertionError("source rows are supplied by the test broker")
    if action == "ronghui.problem.query":
        return {
            "bill_code": bill_code,
            "precondition_ref": f"precondition:{bill_code}",
            "ready": True,
        }
    if action == "ronghui.problem.create":
        return {
            "bill_code": bill_code,
            "committed": True,
            "external_id": f"problem:{bill_code}",
            "postpone_updated": False,
        }
    if action == "ronghui.problem.verify":
        return {
            "bill_code": bill_code,
            "confirmed": True,
            "external_id": f"problem:{bill_code}",
            "problem_cause_sha256": str(arguments["problem_cause_sha256"]),
            "problem_owner_type": str(arguments["problem_owner_type"]),
            "problem_type": str(arguments["problem_type"]),
            "registered_at": "2026-08-31T01:02:03Z",
        }
    raise AssertionError(action)


def _recording_brokers(
    rows: list[list[object]],
    plugin,
    *,
    failure_action: str | None = None,
):
    v1_calls: list[dict[str, object]] = []
    v2_calls: list[dict[str, object]] = []

    def v1_broker(operation: str, *, action: str, role: str, arguments: dict[str, object]) -> object:
        index = len(v1_calls) + 1
        call = {
            "operation": operation,
            "action": action,
            "role": role,
            "arguments": copy.deepcopy(arguments),
        }
        v1_calls.append(call)
        if action == "feishu.sheet.read_rows":
            return {
                "complete": True,
                "evidence_ref": f"synthetic-host-evidence:{index}",
                "rows": copy.deepcopy(rows),
            }
        if action == failure_action:
            raise RuntimeError(f"{action.replace('.', '_')}_UNCERTAIN")
        result = _business_response(action, arguments, f"synthetic-host-evidence:{index}")
        result["evidence_ref"] = f"synthetic-host-evidence:{index}"
        return result

    service_to_role = {
        plugin.PRIMARY_CONNECTOR: plugin.PRIMARY_ACCOUNT_ROLE,
        plugin.DAXIANG_CONNECTOR: plugin.DAXIANG_ACCOUNT_ROLE,
    }
    operation_to_action = {
        "read_rows": "feishu.sheet.read_rows",
        "query": "ronghui.problem.query",
        "create": "ronghui.problem.create",
        "verify": "ronghui.problem.verify",
    }

    def v2_host_broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        assert operation == "service.invoke"
        assert action in {"read_rows", "query", "create", "verify"}
        assert role == plugin.SYSTEM_ROLE
        nested_service = str(arguments["service"])
        nested_operation = str(arguments["operation"])
        nested_arguments = copy.deepcopy(arguments["arguments"])
        primitive_action = operation_to_action[nested_operation]
        if nested_service == plugin.SOURCE_CONNECTOR:
            primitive_role = plugin.SOURCE_RESOURCE_ROLE
            assert nested_operation == "read_rows"
        else:
            primitive_role = service_to_role[nested_service]
            assert nested_operation in {"query", "create", "verify"}
        call = {
            "operation": "browser.invoke" if primitive_action.startswith("ronghui") else "network.request",
            "action": primitive_action,
            "role": primitive_role,
            "arguments": nested_arguments,
            "outer_action": action,
            "outer_arguments": copy.deepcopy(arguments),
        }
        v2_calls.append(call)
        if primitive_action == "feishu.sheet.read_rows":
            response = {
                "complete": True,
                "rows": copy.deepcopy(rows),
            }
        elif primitive_action == failure_action:
            raise RuntimeError(f"{primitive_action.replace('.', '_')}_UNCERTAIN")
        else:
            response = _business_response(primitive_action, nested_arguments, "")
        return _HostResult(response, f"synthetic-host-evidence:{len(v2_calls)}")

    return v1_broker, v2_host_broker, v1_calls, v2_calls


def _stable_data(result: dict[str, object]) -> dict[str, object]:
    data = copy.deepcopy(result["data"])
    assert isinstance(data, dict)
    evidence = data.get("evidence")
    assert isinstance(evidence, dict)
    evidence["observed_at"] = "OBSERVED_AT"
    return data


def test_self_pickup_v1_and_v2_preview_have_same_projection_and_primitive_order(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = copy.deepcopy(fixture["rows"])
    v1_action = load_first_party_action("self_pickup_problem_upload")
    runtime, plugin = _build_and_load(tmp_path)
    v1_broker, v2_broker, v1_calls, v2_calls = _recording_brokers(rows, plugin)
    arguments = copy.deepcopy(fixture["arguments"])

    v1_result = v1_action.run_action(arguments, v1_broker)
    v2_result = runtime.run_self_pickup_action_offline("preview", arguments, v2_broker)

    assert [call["action"] for call in v1_calls] == ["feishu.sheet.read_rows"]
    assert [call["action"] for call in v2_calls] == ["feishu.sheet.read_rows"]
    assert [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v1_calls
    ] == [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v2_calls
    ]
    assert _stable_data(v1_result) == _stable_data(v2_result)
    assert v2_calls[0]["outer_arguments"]["service"] == plugin.SOURCE_CONNECTOR
    assert v2_calls[0]["outer_arguments"]["operation"] == "read_rows"
    assert v2_calls[0]["outer_arguments"]["preflight_services"] == list(
        plugin.PREVIEW_PREFLIGHT_SERVICES
    )
    assert v2_result["data"]["evidence"]["execution_result"] == "preview_only"


def test_preview_service_projects_read_only_evidence_and_never_marks_a_write(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = copy.deepcopy(fixture["rows"])
    runtime, plugin = _build_and_load(tmp_path)
    _v1_broker, host_broker, _v1_calls, _v2_calls = _recording_brokers(rows, plugin)
    tracker = runtime._ExecutionTracker()
    old_broker = runtime.broker_call
    runtime.broker_call = host_broker
    try:
        result = runtime.run_self_pickup_service(
            "preview",
            copy.deepcopy(fixture["arguments"]),
            tracker=tracker,
        )
    finally:
        runtime.broker_call = old_broker
    assert result["status"] == "SUCCESS"
    assert result["data"]["evidence"]["service"] == SERVICE_NAME
    assert result["data"]["evidence"]["operation"] == "preview"
    assert result["data"]["evidence"]["outcome"] == "READ_ONLY"
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"
    assert tracker.mutating_started is False


def test_console_preview_request_accepts_only_the_signed_read_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, plugin = _build_and_load(tmp_path)
    request = {
        "schema_version": 2,
        "runtime_model": "SERVICE_V2",
        "automation_id": "self-pickup-test",
        "plugin_id": plugin.PLUGIN_ID,
        "plugin_version": "1.0.0",
        "entrypoint": "console",
        "target": {
            "service": SERVICE_NAME,
            "operation": "preview",
            "contribution_id": "execute_console",
            "contribution_kind": "console",
        },
        "governance": {
            "effect": "read",
            "operation_type": "read",
            "broker_effect": "read",
            "harness_allowed": True,
        },
        "arguments": {
            "dry_run": True,
            "selected_bill_codes": [],
            "preview_fingerprint": "",
        },
    }
    monkeypatch.setenv("BOYI_AUTOMATION_ID", "self-pickup-test")
    monkeypatch.setenv("BOYI_PLUGIN_ID", plugin.PLUGIN_ID)
    monkeypatch.setenv("BOYI_PLUGIN_VERSION", "1.0.0")
    monkeypatch.setattr(runtime.sys, "stdin", io.StringIO(json.dumps(request)))

    operation, arguments = runtime._read_request()

    assert operation == "preview"
    assert arguments == request["arguments"]

    wrong_operation = copy.deepcopy(request)
    wrong_operation["target"]["operation"] = "inspect"
    monkeypatch.setattr(
        runtime.sys,
        "stdin",
        io.StringIO(json.dumps(wrong_operation)),
    )
    with pytest.raises(ValueError, match="target"):
        runtime._read_request()

    wrong_governance = copy.deepcopy(request)
    wrong_governance["governance"]["effect"] = "external_write"
    monkeypatch.setattr(
        runtime.sys,
        "stdin",
        io.StringIO(json.dumps(wrong_governance)),
    )
    with pytest.raises(ValueError, match="governance"):
        runtime._read_request()


def test_source_reordering_keeps_fingerprint_but_source_drift_expires_execution(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = copy.deepcopy(fixture["rows"])
    runtime, plugin = _build_and_load(tmp_path)
    embedded_action = sys.modules["action"]
    original_candidates, _ = embedded_action._collect_candidates(
        rows,
        include_daxiang=True,
        limit=None,
    )
    reordered = copy.deepcopy(rows)
    reordered[1], reordered[3] = reordered[3], reordered[1]
    reordered_candidates, _ = embedded_action._collect_candidates(
        reordered,
        include_daxiang=True,
        limit=None,
    )
    assert embedded_action._preview_fingerprint(original_candidates) == embedded_action._preview_fingerprint(
        reordered_candidates
    )

    changed = copy.deepcopy(rows)
    changed[1][-1] = "2"
    changed[6][-1] = "2"
    _v1_broker, changed_host_broker, _v1_calls, v2_calls = _recording_brokers(
        changed,
        plugin,
    )
    with pytest.raises(RuntimeError, match="SELECTION_PREVIEW_EXPIRED"):
        runtime.run_self_pickup_action_offline(
            "execute",
            {
                "dry_run": False,
                "include_daxiang_s_self_pickup": True,
                "preview_fingerprint": embedded_action._preview_fingerprint(original_candidates),
                "selected_bill_codes": ["R_SELF"],
            },
            changed_host_broker,
        )
    assert [call["action"] for call in v2_calls] == ["feishu.sheet.read_rows"]


@pytest.mark.parametrize(
    ("operation", "arguments"),
    (
        (
            "preview",
            {"dry_run": False, "selected_bill_codes": [], "preview_fingerprint": ""},
        ),
        (
            "preview",
            {"dry_run": True, "selected_bill_codes": ["R_SELF"]},
        ),
        (
            "execute",
            {"dry_run": True, "selected_bill_codes": ["R_SELF"], "preview_fingerprint": "a" * 64},
        ),
        (
            "execute",
            {"dry_run": False, "selected_bill_codes": ["R_SELF"]},
        ),
    ),
)
def test_operation_boundary_forces_preview_and_execute_arguments(
    tmp_path: Path,
    operation: str,
    arguments: dict[str, object],
) -> None:
    runtime, _plugin = _build_and_load(tmp_path)
    with pytest.raises(ValueError):
        runtime.run_self_pickup_action_offline(operation, arguments, lambda *args, **kwargs: None)


def test_self_pickup_v1_and_v2_execute_preserve_order_and_three_target_preflight(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = copy.deepcopy(fixture["rows"])
    v1_action = load_first_party_action("self_pickup_problem_upload")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused_host, _preview_calls, _unused_calls = _recording_brokers(rows, plugin)
    preview = v1_action.run_action(copy.deepcopy(fixture["arguments"]), preview_broker)
    fingerprint = str(preview["data"]["preview_fingerprint"])
    arguments = {
        "dry_run": False,
        "include_daxiang_s_self_pickup": True,
        "preview_fingerprint": fingerprint,
        "selected_bill_codes": ["R_DX_PICK", "R_SELF"],
    }
    v1_broker, v2_broker, v1_calls, v2_calls = _recording_brokers(rows, plugin)

    v1_result = v1_action.run_action(copy.deepcopy(arguments), v1_broker)
    v2_result = runtime.run_self_pickup_action_offline(
        "execute", copy.deepcopy(arguments), v2_broker
    )

    expected_actions = [
        "feishu.sheet.read_rows",
        "ronghui.problem.query",
        "ronghui.problem.query",
        "ronghui.problem.create",
        "ronghui.problem.verify",
        "ronghui.problem.create",
        "ronghui.problem.verify",
    ]
    assert [call["action"] for call in v1_calls] == expected_actions
    assert [call["action"] for call in v2_calls] == expected_actions
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
    assert all("preflight_services" not in call["outer_arguments"] for call in v2_calls[1:])
    assert v2_result["data"]["evidence"]["execution_result"] == "all_selected_confirmed"
    assert v2_result["meta"]["evidence_refs"] == [
        f"synthetic-host-evidence:{index}" for index in range(1, 8)
    ]
    assert [item["verified"] for item in v2_result["data"]["results"]] == [True, True]


def test_execute_all_target_preflight_failure_is_before_any_query_or_create(
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
        calls.append({"operation": operation, "action": action, "arguments": arguments})
        assert arguments["preflight_services"] == list(plugin.EXECUTE_PREFLIGHT_SERVICES)
        raise RuntimeError("BROKER_ROLE_UNBOUND")

    tracker = runtime._ExecutionTracker()
    old_broker = runtime.broker_call
    runtime.broker_call = host_broker
    with pytest.raises(RuntimeError, match="BROKER_ROLE_UNBOUND"):
        try:
            runtime.run_self_pickup_service(
                "execute",
                {
                    "dry_run": False,
                    "include_daxiang_s_self_pickup": True,
                    "preview_fingerprint": "a" * 64,
                    "selected_bill_codes": ["R_SELF"],
                },
                tracker=tracker,
            )
        finally:
            runtime.broker_call = old_broker
    assert len(calls) == 1
    assert calls[0]["action"] == "read_rows"
    assert tracker.mutating_started is False


@pytest.mark.parametrize("failure_action", ("ronghui.problem.create", "ronghui.problem.verify"))
def test_create_or_verify_uncertainty_is_unknown_after_create_marker(
    tmp_path: Path,
    failure_action: str,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = copy.deepcopy(fixture["rows"])
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _host, _calls, _host_calls = _recording_brokers(rows, plugin)
    v1_action = load_first_party_action("self_pickup_problem_upload")
    preview = v1_action.run_action(copy.deepcopy(fixture["arguments"]), preview_broker)
    arguments = {
        "dry_run": False,
        "include_daxiang_s_self_pickup": True,
        "preview_fingerprint": str(preview["data"]["preview_fingerprint"]),
        "selected_bill_codes": ["R_SELF"],
    }
    _v1_broker, host_broker, _v1_calls, _v2_calls = _recording_brokers(
        rows,
        plugin,
        failure_action=failure_action,
    )
    request = {
        "schema_version": 2,
        "runtime_model": "SERVICE_V2",
        "automation_id": "self-pickup-test",
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
    old_broker = runtime.broker_call
    old_stdin, old_stdout = runtime.sys.stdin, runtime.sys.stdout
    runtime.broker_call = host_broker
    old_environment = {
        key: runtime.os.environ.get(key)
        for key in ("BOYI_AUTOMATION_ID", "BOYI_PLUGIN_ID", "BOYI_PLUGIN_VERSION")
    }
    runtime.os.environ.update(
        {
            "BOYI_AUTOMATION_ID": "self-pickup-test",
            "BOYI_PLUGIN_ID": plugin.PLUGIN_ID,
            "BOYI_PLUGIN_VERSION": "1.0.0",
        }
    )
    runtime.sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False))
    runtime.sys.stdout = io.StringIO()
    try:
        assert runtime.main() == 0
        output = runtime.sys.stdout.getvalue()
    finally:
        runtime.broker_call = old_broker
        for key, value in old_environment.items():
            if value is None:
                runtime.os.environ.pop(key, None)
            else:
                runtime.os.environ[key] = value
        runtime.sys.stdin, runtime.sys.stdout = old_stdin, old_stdout
    result = json.loads(output)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == failure_action.replace(".", "_").upper() + "_UNCERTAIN"
    assert result["meta"]["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert result["meta"]["evidence_refs"]
