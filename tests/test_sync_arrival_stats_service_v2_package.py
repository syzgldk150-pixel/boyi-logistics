from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import (
    GenerationBoundResult,
    GenerationVerificationContext,
    RuntimeLeaseOutcome,
)
from agent.automation_plugins.package_v2 import (
    extract_verified_plugin_package_v2,
    verify_unsigned_plugin_zip_v2,
)
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.automation_plugins.host_capability_registry import governance_for_effect
from agent.orchestration.models import OperationType, PlanStep, RiskLevel
from agent.orchestration.result_verifier import ResultVerifier
from agent.tool_registry import validate_schema_instance
from service_v2_plugins._shared.build_zip import build_plugin_zip
from tests.first_party_action_payload_support import load_first_party_action


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent" / "service_v2_plugins" / "sync_arrival_stats_v2"
FIXTURE = ROOT / "tests" / "fixtures" / "service_v2" / "sync_arrival_stats_v2" / "arrival_stats_case.json"


def _build(source: Path, output: Path) -> bytes:
    built = build_plugin_zip(source, output)
    assert built == output.resolve()
    return built.read_bytes()


def _load_plugin():
    path = SOURCE / "payload" / "plugin.py"
    spec = importlib.util.spec_from_file_location("arrival_stats_v2_plugin_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_arrival_runtime():
    """Load the shared runtime with package-local imports isolated for a test."""

    sdk_path = ROOT / "agent" / "service_v2_plugins" / "_shared" / "boyi_plugin_sdk.py"
    plugin_path = SOURCE / "payload" / "plugin.py"
    runtime_path = ROOT / "agent" / "service_v2_plugins" / "_shared" / "arrival_service_main.py"
    previous = {name: sys.modules.get(name) for name in ("boyi_plugin_sdk", "plugin")}
    try:
        for name, path in (("boyi_plugin_sdk", sdk_path), ("plugin", plugin_path)):
            spec = importlib.util.spec_from_file_location(f"{name}_arrival_runtime_test", path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        spec = importlib.util.spec_from_file_location("arrival_service_main_test", runtime_path)
        assert spec is not None and spec.loader is not None
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        return runtime
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_arrival_stats_v2_zip_is_deterministic_and_embeds_authoritative_v1_bytes(
    tmp_path: Path,
) -> None:
    first = _build(SOURCE, tmp_path / "arrival-first.zip")
    second = _build(SOURCE, tmp_path / "arrival-second.zip")
    assert first == second

    with zipfile.ZipFile(tmp_path / "arrival-first.zip") as archive:
        expected_members = {
            "manifest.json",
            "payload/action.py",
            "payload/boyi_plugin_result.py",
            "payload/boyi_plugin_sdk.py",
            "payload/main.py",
            "payload/plugin.py",
            "settings/index.html",
            "settings/settings.css",
            "settings/settings.js",
        }
        assert set(archive.namelist()) == expected_members
        assert archive.read("payload/action.py") == (
            ROOT
            / "agent"
            / "first_party_automation_plugins"
            / "sync_arrival_stats"
            / "payload"
            / "action.py"
        ).read_bytes()
        assert archive.read("payload/boyi_plugin_result.py") == (
            ROOT / "agent" / "first_party_automation_plugins" / "_runtime" / "result.py"
        ).read_bytes()
        for member in sorted(expected_members):
            content = archive.read(member)
            if member.endswith(".py"):
                compile(content, f"arrival_stats_v2.zip/{member}", "exec")

    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    parsed = AutomationPluginManifestV2.from_mapping(manifest)
    assert parsed.plugin_id == "sync_arrival_stats_v2"
    assert parsed.contributes["scheduler"] == (
        {
            "id": "daily_arrival_stats",
            "title": "每日到货统计（默认关闭）",
            "service": "plugin.sync_arrival_stats_v2.arrival_stats@1",
            "operation": "run",
            "default_enabled": False,
        },
    )
    assert parsed.contributes["feishu"] == (
        {
            "id": "arrival_stats_command",
            "service": "plugin.sync_arrival_stats_v2.arrival_stats@1",
            "operation": "run",
            "commands": ("统计到货数据",),
            "default_enabled": False,
        },
    )
    pending = next(
        item for item in parsed.resource_roles if item["role"] == "arrival_stats_pending_sheet"
    )
    assert pending["required"] is False
    with pytest.raises(ValueError, match="pending_sheet_disabled"):
        validate_schema_instance("arrival stats config", {}, parsed.config_schema)
    validate_schema_instance(
        "arrival stats config",
        {"pending_sheet_disabled": True},
        parsed.config_schema,
    )
    validate_schema_instance(
        "arrival stats config",
        {"pending_sheet_disabled": False},
        parsed.config_schema,
    )
    projection = ServiceV2ProjectContract.from_manifest(parsed)
    assert projection.default_entrypoints == ("manual_run", "assistant_preview")
    assert projection.contribution_kinds["arrival_stats_command"] == "feishu"


def test_arrival_stats_v2_payload_has_no_legacy_runtime_import_or_path_mutation() -> None:
    for path in (SOURCE / "payload" / "plugin.py", ROOT / "agent" / "service_v2_plugins" / "_shared" / "arrival_service_main.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        source = path.read_text(encoding="utf-8")
        assert "first_party_automation_plugins" not in source
        assert "sys.path" not in source
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            str(node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert "agent" not in imported_roots


def test_arrival_stats_representative_20k_payloads_fit_only_a_proposed_candidate_cap() -> None:
    action = load_first_party_action("sync_arrival_stats")
    fields = tuple(action._WAYBILL_FIELDS)
    assert len(fields) == 18
    records = [
        {
            **{
                field: ("R12345678901" if field == "tracking_number" else "synthetic-value")
                for field in fields
            },
            "tracking_number": f"R{index:011d}",
        }
        for index in range(20_000)
    ]
    waybill_payload = json.dumps(
        {"records": records, "target_date": "2026-08-24"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    stats_payload = json.dumps(
        {
            "records": [{**record, "arrived_quantity": 1} for record in records],
            "target_date": "2026-08-24",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    proposed_candidate_cap = 16 * 1024 * 1024
    assert len(waybill_payload) == 12_280_040
    assert len(stats_payload) == 12_700_040
    assert max(len(waybill_payload), len(stats_payload)) <= proposed_candidate_cap
    # There is intentionally no production Connector descriptor yet. These are
    # representative fixture measurements, not a formal operation byte cap.


def test_arrival_stats_adapter_uses_exact_connector_roles_and_projects_host_receipt() -> None:
    plugin = _load_plugin()
    calls: list[dict[str, object]] = []

    class HostResult(dict[str, object]):
        host_evidence_ref = "host-evidence:arrival:1"

    def broker(operation: str, *, action: str, role: str, arguments: dict[str, object]) -> object:
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": arguments,
            }
        )
        return HostResult({"committed": True})

    result = plugin.service_invoke_adapter(
        broker,
        "network.request",
        action="feishu.sheet.replace",
        role="arrival_stats_primary_sheet",
        arguments={"resource_slot": "arrival_stats_primary", "records": []},
    )
    assert result == {"committed": True, "evidence_ref": "host-evidence:arrival:1"}
    assert not hasattr(result, "host_evidence_ref")
    assert calls == [
        {
            "operation": "service.invoke",
            "action": "replace",
            "role": "__system__",
            "arguments": {
                "service": "connector.boyi.arrival_stats_primary_sheet@1",
                "operation": "replace",
                "arguments": {"resource_slot": "arrival_stats_primary", "records": []},
            },
        }
    ]

    plugin.service_invoke_adapter(
        broker,
        "network.request",
        action="feishu.sheet.add",
        role="arrival_stats_archive_sheet",
        arguments={"records": []},
    )
    assert calls[-1]["arguments"]["service"] == "connector.boyi.arrival_stats_archive_sheet@1"
    assert calls[-1]["action"] == "add"

    with pytest.raises(ValueError, match="slot"):
        plugin.service_invoke_adapter(
            broker,
            "network.request",
            action="feishu.sheet.replace",
            role="arrival_stats_primary_sheet",
            arguments={"resource_slot": "arrival_stats_secondary", "records": []},
        )
    with pytest.raises(ValueError, match="archive"):
        plugin.service_invoke_adapter(
            broker,
            "network.request",
            action="feishu.sheet.add",
            role="arrival_stats_primary_sheet",
            arguments={"records": []},
        )


def test_arrival_stats_first_connector_call_carries_bounded_preflight_without_extra_broker_calls() -> None:
    plugin = _load_plugin()
    calls: list[dict[str, object]] = []

    def broker(operation: str, *, action: str, role: str, arguments: dict[str, object]) -> object:
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": arguments,
            }
        )
        return {"found": True}

    plugin.service_invoke_adapter(
        broker,
        "browser.invoke",
        action="ronghui.arrive_list.read_page",
        role="account_id",
        arguments={"page": 1},
        preflight_services=(
            plugin.TMS_CONNECTOR,
            plugin.PROJECTION_CONNECTOR,
            plugin.SHEET_CONNECTORS["arrival_stats_primary_sheet"],
        ),
    )

    assert len(calls) == 1
    assert calls[0]["arguments"]["preflight_services"] == [
        plugin.TMS_CONNECTOR,
        plugin.PROJECTION_CONNECTOR,
        plugin.SHEET_CONNECTORS["arrival_stats_primary_sheet"],
    ]
    with pytest.raises(ValueError, match="preflight"):
        plugin.service_invoke_adapter(
            broker,
            "browser.invoke",
            action="ronghui.arrive_list.read_page",
            role="account_id",
            arguments={"page": 1},
            preflight_services=("connector.boyi.undeclared@1",),
        )


def test_arrival_stats_preflight_targets_follow_optional_pending_and_archive_config() -> None:
    runtime = _load_arrival_runtime()

    enabled = runtime._preflight_connector_services(
        {"pending_sheet_disabled": False, "archive_snapshot": True}
    )
    disabled = runtime._preflight_connector_services(
        {"pending_sheet_disabled": True, "archive_snapshot": False}
    )
    dry_run = runtime._preflight_connector_services(
        {"pending_sheet_disabled": False, "archive_snapshot": True, "dry_run": True}
    )

    assert runtime.SHEET_CONNECTORS["arrival_stats_pending_sheet"] in enabled
    assert runtime.SHEET_CONNECTORS["arrival_stats_archive_sheet"] in enabled
    assert runtime.SHEET_CONNECTORS["arrival_stats_pending_sheet"] not in disabled
    assert runtime.SHEET_CONNECTORS["arrival_stats_archive_sheet"] not in disabled
    assert dry_run == (runtime.TMS_CONNECTOR, runtime.PROJECTION_CONNECTOR)


class _ArrivalGenerationLeases:
    def __init__(self) -> None:
        self.outcomes: list[RuntimeLeaseOutcome] = []

    def finalize_generation_write(self, **values: object) -> None:
        self.outcomes.append(values["outcome"])


def test_arrival_stats_service_result_converts_v1_proof_to_host_closed_write_contract() -> None:
    runtime = _load_arrival_runtime()
    observed_at = "2026-08-31T10:00:00+08:00"
    tracker = runtime._ExecutionTracker()
    tracker.host_refs = ["host-evidence:read", "host-evidence:write"]
    tracker.mutating_started = True
    legacy_result = {
        "status": "SUCCESS",
        "data": {
            "records": 1,
            "evidence": {
                "source": "signed_first_party_plugin",
                "observed_at": observed_at,
                "pagination_complete": True,
                "execution_result": "all_required_outputs_committed",
            },
        },
        "meta": {
            "source_system": "ronghui+feishu+mysql",
            "observed_at": observed_at,
            "record_count": 1,
            "pagination_complete": True,
            "evidence_refs": [*tracker.host_refs, "tool-result:sync_arrival_stats:legacy"],
        },
        "warnings": [],
        "error": None,
    }
    result = runtime._service_success_result(legacy_result, tracker)
    assert result["data"]["evidence"] == {
        "source": "signed_first_party_plugin",
        "observed_at": observed_at,
        "pagination_complete": True,
        "execution_result": "all_required_outputs_committed",
        "service": "plugin.sync_arrival_stats_v2.arrival_stats@1",
        "operation": "run",
        "outcome": "WRITE_VERIFIED",
    }
    assert result["meta"]["evidence_refs"] == tracker.host_refs
    assert result["meta"]["postcondition_evidence"]["0"]["condition"] == (
        "plugin_result_contract_valid"
    )

    manifest = AutomationPluginManifestV2.from_mapping(
        json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    )
    projected = ServiceV2ProjectContract.from_manifest(manifest)
    invocation = projected.invocation_contracts["manual_run"]
    capability = {
        **dict(projected.tool_contract),
        "_plugin_runtime": {
            "runtime_model": "SERVICE_V2",
            "plugin_id": manifest.plugin_id,
            "compiled_invocations": {
                "manual_run": {
                    "target": {
                        "service": invocation["service"],
                        "operation": invocation["operation"],
                        "contribution_id": "manual_run",
                        "contribution_kind": "console",
                    }
                }
            },
        },
    }
    step = PlanStep(
        step_key="arrival-stats",
        tool_name=str(capability["name"]),
        tool_version=str(capability["version"]),
        operation_type=OperationType.EXTERNAL_WRITE,
        arguments={},
        account_id="synthetic-account",
        depends_on=(),
        idempotency_key="arrival-stats-write",
        expected_evidence=(dict(capability["evidence"]),),
        postconditions=tuple(capability["postconditions"]),
        risk_level=RiskLevel.HIGH,
    )
    observations = tuple(
        {
            "request_id": f"00000000-0000-4000-8000-{index:012d}",
            "operation": "service.invoke",
            "action": action,
            "role": "__system__",
            "arguments_sha256": "a" * 64,
            "write_started": index == 2,
            "evidence_ref": reference,
            "result": {"committed": index == 2},
        }
        for index, (action, reference) in enumerate(
            (("scan_read", tracker.host_refs[0]), ("replace", tracker.host_refs[1])),
            start=1,
        )
    )
    raw = GenerationBoundResult(
        result,
        verification=GenerationVerificationContext(
            automation_id="arrival-stats-project",
            generation=1,
            lease_id="11111111-1111-4111-8111-111111111111",
            account_ids=("synthetic-account",),
            account_bindings_sha256="b" * 64,
            requires_write_verification=True,
            started_mutating_call_count=1,
            host_call_observations=observations,
        ),
    )
    leases = _ArrivalGenerationLeases()
    outcome = ResultVerifier(leases).verify(step, raw, capability)
    assert outcome.accepted is True
    assert leases.outcomes == [RuntimeLeaseOutcome.WRITE_VERIFIED]


@pytest.mark.parametrize(
    ("entrypoint", "contribution_id", "contribution_kind"),
    (
        ("console", "manual_run", "console"),
        ("scheduler", "daily_arrival_stats", "scheduler"),
        ("feishu", "arrival_stats_command", "feishu"),
    ),
)
def test_arrival_stats_subprocess_fails_closed_without_real_broker(
    tmp_path: Path,
    entrypoint: str,
    contribution_id: str,
    contribution_kind: str,
) -> None:
    package_bytes = _build(SOURCE, tmp_path / f"arrival-{entrypoint}.zip")
    verified = verify_unsigned_plugin_zip_v2(
        package_bytes,
        transport_sha256=hashlib.sha256(package_bytes).hexdigest(),
    )
    install_root = extract_verified_plugin_package_v2(
        verified,
        tmp_path / f"installed-{entrypoint}",
    )
    request = {
        "schema_version": 2,
        "runtime_model": "SERVICE_V2",
        "automation_id": "arrival-stats-offline-test",
        "plugin_id": "sync_arrival_stats_v2",
        "plugin_version": "1.0.0",
        "entrypoint": entrypoint,
        "target": {
            "service": "plugin.sync_arrival_stats_v2.arrival_stats@1",
            "operation": "run",
            "contribution_id": contribution_id,
            "contribution_kind": contribution_kind,
        },
        "governance": governance_for_effect("external_write").to_mapping(),
        "arguments": {
            "target_date": "2026-08-24",
            "dry_run": True,
            "pending_sheet_disabled": True,
        },
    }
    environment = {
        "BOYI_AUTOMATION_ID": request["automation_id"],
        "BOYI_PLUGIN_ID": request["plugin_id"],
        "BOYI_PLUGIN_VERSION": request["plugin_version"],
        "PYTHONIOENCODING": "utf-8",
    }
    completed = subprocess.run(
        [sys.executable, "main.py"],
        cwd=install_root / "payload",
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
        env=environment,
    )
    assert completed.returncode == 0
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "BROKER_CAPABILITY_UNAVAILABLE"
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"
    assert result["data"]["completed_results"] == []


@pytest.mark.parametrize("pending_value", (None, "true", 1))
def test_arrival_stats_subprocess_rejects_missing_or_non_boolean_pending_policy_before_broker(
    tmp_path: Path,
    pending_value: object,
) -> None:
    package_bytes = _build(SOURCE, tmp_path / "arrival-invalid-config.zip")
    verified = verify_unsigned_plugin_zip_v2(
        package_bytes,
        transport_sha256=hashlib.sha256(package_bytes).hexdigest(),
    )
    install_root = extract_verified_plugin_package_v2(
        verified,
        tmp_path / "installed-invalid-config",
    )
    arguments: dict[str, object] = {
        "target_date": "2026-08-24",
        "dry_run": True,
    }
    if pending_value is not None:
        arguments["pending_sheet_disabled"] = pending_value
    request = {
        "schema_version": 2,
        "runtime_model": "SERVICE_V2",
        "automation_id": "arrival-stats-offline-test",
        "plugin_id": "sync_arrival_stats_v2",
        "plugin_version": "1.0.0",
        "entrypoint": "console",
        "target": {
            "service": "plugin.sync_arrival_stats_v2.arrival_stats@1",
            "operation": "run",
            "contribution_id": "manual_run",
            "contribution_kind": "console",
        },
        "governance": governance_for_effect("external_write").to_mapping(),
        "arguments": arguments,
    }
    completed = subprocess.run(
        [sys.executable, "main.py"],
        cwd=install_root / "payload",
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
        env={
            "BOYI_AUTOMATION_ID": request["automation_id"],
            "BOYI_PLUGIN_ID": request["plugin_id"],
            "BOYI_PLUGIN_VERSION": request["plugin_version"],
            "PYTHONIOENCODING": "utf-8",
        },
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "INVALID_CONFIGURATION"
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"
