from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from agent.automation_plugins.errors import PluginConflictError, PluginExecutionError
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    GenerationBoundResult,
    PluginTrustSource,
    RuntimeGenerationLease,
    RuntimeGenerationSnapshot,
    RuntimeLeaseOutcome,
)
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox, SandboxCanaryResult
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import OperationType, PlanStep, RiskLevel, sha256_json
from agent.orchestration.result_verifier import ResultVerifier


def _digest(value: object) -> str:
    payload = canonical_json_bytes(value) if isinstance(value, (dict, list)) else str(value).encode()
    return hashlib.sha256(payload).hexdigest()


def _project_invocation(capability: Mapping[str, Any]) -> dict[str, Any]:
    metadata = capability["_plugin_runtime"]
    return {
        "schema_version": 1,
        "automation_id": metadata["automation_id"],
        "automation_generation": metadata["generation"],
        "entrypoint": "console",
        "contract_id": "console",
        "contract_hash": "a" * 64,
        "policy_version": 1,
        "project_configuration_version": 1,
        "request_id": str(uuid.uuid4()),
    }


def _trusted_binding(
    capability: Mapping[str, Any],
    *,
    run_id: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "step_id": step_id or str(uuid.uuid4()),
        "_automation_project_invocation": _project_invocation(capability),
    }


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["SUCCESS", "FAILED"]},
            "data": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            # Deliberately closed: generation/account proof must remain in the
            # Python side channel and cannot be injected into signed JSON.
            "meta": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_system": {"type": "string"},
                    "observed_at": {"type": "string"},
                    "record_count": {"type": "integer"},
                    "pagination_complete": {"type": "boolean"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "postconditions": {"type": "object", "additionalProperties": True},
                    "postcondition_evidence": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": [
                    "source_system",
                    "observed_at",
                    "record_count",
                    "pagination_complete",
                    "evidence_refs",
                    "postconditions",
                    "postcondition_evidence",
                ],
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
            "error": {
                "oneOf": [
                    {"type": "object", "additionalProperties": True},
                    {"type": "null"},
                ]
            },
        },
        "required": ["status", "data", "meta", "warnings", "error"],
    }


def _plugin_result(tool_name: str) -> dict[str, Any]:
    data = {"ok": True}
    evidence_ref = f"tool-result:{tool_name}:{sha256_json(data)}"
    observed_at = "2026-08-15T00:00:00Z"
    return {
        "status": "SUCCESS",
        "data": data,
        "meta": {
            "source_system": "ronghui",
            "observed_at": observed_at,
            "record_count": 1,
            "pagination_complete": True,
            "evidence_refs": [evidence_ref],
            "postconditions": {"0": True},
            "postcondition_evidence": {
                "0": {
                    "condition": "executor_reported_success",
                    "verified": True,
                    "observed_at": observed_at,
                    "evidence_ref": evidence_ref,
                    "details": {"result_sha256": sha256_json(data)},
                }
            },
        },
        "warnings": [],
        "error": None,
    }


def _capability(tmp_path: Path, *, automation_id: str = "project-a") -> dict[str, Any]:
    root = tmp_path / automation_id
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "package" / "payload").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("python", encoding="utf-8")
    (root / "package" / "payload" / "main.py").write_text("# signed", encoding="utf-8")
    account_bindings = {"source": "account-1"}
    action_id = f"automation.{automation_id}.run"
    return {
        "name": action_id,
        "version": "1.0.0",
        "operation_type": "internal_projection_write",
        "timeout": 5,
        "evidence": [{"required": True, "pagination_complete": False}],
        "postconditions": [{"name": "executor_reported_success"}],
        "output_schema": _output_schema(),
        "_plugin_runtime": {
            "automation_id": automation_id,
            "plugin_id": "customer_action",
            "version": "1.0.0",
            "generation": 1,
            "package_sha256": _digest("package"),
            "manifest_sha256": _digest("manifest"),
            "trust_source": PluginTrustSource.ED25519_FIRST_PARTY.value,
            "install_root": str(root),
            "runtime": {"kind": "python_subprocess", "entrypoint": "payload/main.py"},
            "install_metadata": {"python_relative": "venv/bin/python"},
            "runtime_permissions": {
                "network": False,
                "browser": True,
                "office": False,
                "file_roles": [],
                "broker_operations": [
                    {
                        "operation": "browser.invoke",
                        "action": "customer_problem.list_page",
                        "roles": ["source"],
                        "effect": "read",
                    }
                ],
                "max_broker_calls": 20,
            },
            "account_roles": [
                {
                    "role": "source",
                    "allowed_systems": ["ronghui"],
                    "required": True,
                    "argument_field": None,
                    "collection": False,
                }
            ],
            "resource_roles": [],
            "account_bindings": account_bindings,
            "resource_bindings": {},
        },
    }


def _snapshot(capability: Mapping[str, Any]) -> RuntimeGenerationSnapshot:
    metadata = capability["_plugin_runtime"]
    return RuntimeGenerationSnapshot(
        automation_id=str(metadata["automation_id"]),
        generation=int(metadata["generation"]),
        plugin_id=str(metadata["plugin_id"]),
        plugin_version=str(metadata["version"]),
        package_sha256=str(metadata["package_sha256"]),
        manifest_sha256=str(metadata["manifest_sha256"]),
        trust_source=PluginTrustSource(str(metadata["trust_source"])),
        project_config_sha256=_digest({}),
        account_bindings_sha256=_digest(metadata["account_bindings"]),
        resource_bindings_sha256=_digest({}),
        device_binding_sha256=_digest(None),
        schedule_sha256=_digest({"kind": "none"}),
        core_registry_sha256=_digest("registry"),
        tool_contract_sha256=_digest("tool"),
        invocation_contracts_sha256=_digest("invocation"),
        compiled_invocations_sha256=_digest("compiled-invocation"),
        runtime_descriptor_sha256=_digest("runtime-descriptor"),
        governance_anchor_sha256=_digest("governance-anchor"),
        policy_contract_sha256=_digest("policy"),
        enabled_entrypoints=("console",),
        execution_metadata=copy.deepcopy(dict(capability)),
    )


class _LeaseRepository:
    def __init__(self, capabilities: Mapping[str, Mapping[str, Any]]) -> None:
        self.capabilities = {key: dict(value) for key, value in capabilities.items()}
        self.released: list[tuple[str, RuntimeLeaseOutcome]] = []
        self.finalized: list[tuple[str, RuntimeLeaseOutcome]] = []
        self.verifying: set[str] = set()

    def acquire_committed_generation(
        self,
        automation_id: str,
        *,
        expected_generation: int,
        expected_manifest_sha256: str,
        lease_id: str,
        orchestration_run_id: str,
        expires_at: datetime,
    ) -> RuntimeGenerationLease:
        capability = self.capabilities[automation_id]
        snapshot = _snapshot(capability)
        if (
            snapshot.generation != expected_generation
            or snapshot.manifest_sha256 != expected_manifest_sha256
        ):
            raise PluginConflictError("approved generation changed")
        return RuntimeGenerationLease(
            lease_id=lease_id,
            automation_id=automation_id,
            generation=snapshot.generation,
            snapshot=snapshot,
            runtime_metadata=capability,
            acquired_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            orchestration_run_id=orchestration_run_id,
        )

    def release_generation(
        self,
        lease: RuntimeGenerationLease,
        *,
        outcome: RuntimeLeaseOutcome,
    ) -> None:
        self.released.append((lease.lease_id, outcome))
        if outcome == RuntimeLeaseOutcome.VERIFYING:
            self.verifying.add(lease.lease_id)

    def finalize_generation_write(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        outcome: RuntimeLeaseOutcome,
        evidence_sha256: str,
    ) -> None:
        assert automation_id in self.capabilities
        assert generation == 1
        assert lease_id in self.verifying
        assert len(evidence_sha256) == 64
        self.verifying.remove(lease_id)
        self.finalized.append((lease_id, outcome))


class _Issuer:
    broker_endpoint = "unix:///tmp/fake-plugin-broker.sock"
    broker_socket_path = None

    def __init__(self, *, started_mutating_calls: int = 0) -> None:
        self._started_mutating_calls = started_mutating_calls

    def issue(self, **_: object) -> str:
        return "test-capability"

    def revoke(self, capability: str) -> None:
        assert capability == "test-capability"

    def started_mutating_call_count(self, capability: str) -> int:
        assert capability == "test-capability"
        return self._started_mutating_calls


class _Integrity:
    def verify_install_root(self, runtime_metadata: Mapping[str, object]) -> None:
        assert runtime_metadata["install_root"]


class _Core:
    def __init__(self) -> None:
        self.last_info = {
            "tool": "legacy-completed",
            "time": "2026-08-15 00:00:00",
            "success": True,
            "duration_s": 1,
        }
        self.heavy_held = False

    async def execute(self, *_: object, **__: object) -> Mapping[str, Any]:
        raise AssertionError("plugin action must not fall back to core ToolExecutor")

    def get_running_output(
        self,
        tool_name: str,
        offset: int = 0,
        started_at: str = "",
    ) -> dict[str, Any]:
        return {
            "lines": [tool_name],
            "running": tool_name == "legacy-running",
            "offset": offset,
            "total": 1,
            "started_at": started_at,
            "cancel_requested": False,
        }

    def is_tool_running(self, tool_name: str) -> bool:
        return tool_name == "legacy-running"

    def running_tool_info(self, tool_name: str) -> dict[str, Any]:
        return {"running": tool_name == "legacy-running", "tool": tool_name}

    def running_tools(self) -> list[str]:
        return ["legacy-running"]

    def last_tool_info(self) -> dict[str, Any]:
        return self.last_info

    def heavy_lock_held(self) -> bool:
        return self.heavy_held

    async def cancel_tool(self, tool_name: str, started_at: str = "") -> Mapping[str, Any]:
        return {"ok": False, "code": "NOT_RUNNING", "tool": tool_name, "started_at": started_at}


class _OutputSandbox:
    def __init__(self, output: bytes) -> None:
        self.output = output
        self.launch_count = 0

    async def launch(self, **_: object) -> asyncio.subprocess.Process:
        self.launch_count += 1
        source = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(" + repr(self.output) + ")"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            source,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )


class _SleepSandbox:
    async def launch(self, **_: object) -> asyncio.subprocess.Process:
        source = "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            source,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )


class _RunningProcess:
    returncode = None


class _FinishedProcess:
    returncode = 0


class _Catalog:
    def __init__(self, capability: Mapping[str, Any]) -> None:
        self.capability = capability

    def get_capability(self, tool_name: str) -> Mapping[str, Any] | None:
        return self.capability if tool_name == self.capability["name"] else None


def _step(tool_name: str) -> PlanStep:
    return PlanStep(
        step_key="write",
        tool_name=tool_name,
        tool_version="1.0.0",
        operation_type=OperationType.INTERNAL_PROJECTION_WRITE,
        arguments={},
        account_id="account-1",
        depends_on=(),
        idempotency_key="write-1",
        expected_evidence=(),
        postconditions=({"name": "executor_reported_success"},),
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
    )


def test_router_adapter_verifier_keeps_schema_clean_and_verifies_write(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    leases = _LeaseRepository({"project-a": capability})
    sandbox = _OutputSandbox(canonical_json_bytes(_plugin_result(str(capability["name"]))))
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        integrity_verifier=_Integrity(),
        sandbox_launcher=sandbox,
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )
    adapter = RegisteredToolExecutionAdapter(catalog=_Catalog(capability), executor=router)
    step = _step(str(capability["name"]))

    raw = asyncio.run(
        adapter.execute_step(
            step,
            run_id=str(uuid.uuid4()),
            step_id=str(uuid.uuid4()),
            execution_context={
                "source": "console",
                "_automation_project_invocation": _project_invocation(capability),
            },
        )
    )

    assert isinstance(raw, GenerationBoundResult)
    assert "account_id" not in _plugin_result(str(capability["name"]))["meta"]
    assert "account_id" not in raw["meta"]
    assert leases.released[0][1] == RuntimeLeaseOutcome.VERIFYING
    assert leases.verifying

    verified = ResultVerifier(leases).verify(step, raw, capability)

    assert verified.accepted is True
    assert verified.result is not None
    assert verified.result.meta["account_id"] == (
        "binding-set:" + _digest(capability["_plugin_runtime"]["account_bindings"])
    )
    assert leases.finalized[0][1] == RuntimeLeaseOutcome.WRITE_VERIFIED
    assert leases.verifying == set()


def test_generation_change_conflicts_before_subprocess_launch(tmp_path: Path) -> None:
    stale = _capability(tmp_path)
    committed = json.loads(json.dumps(stale))
    committed["_plugin_runtime"]["generation"] = 2
    leases = _LeaseRepository({"project-a": committed})
    sandbox = _OutputSandbox(canonical_json_bytes(_plugin_result(str(stale["name"]))))
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        integrity_verifier=_Integrity(),
        sandbox_launcher=sandbox,
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )

    with pytest.raises(PluginConflictError, match="generation changed"):
        asyncio.run(
            router.execute(
                stale,
                {},
                trusted_invocation_context=_trusted_binding(stale),
            )
        )
    assert sandbox.launch_count == 0


@pytest.mark.parametrize(
    ("binding_mutator", "expected_code"),
    [
        (
            lambda capability: {
                "run_id": str(uuid.uuid4()),
                "step_id": str(uuid.uuid4()),
            },
            "PLUGIN_RUN_BINDING_INVALID",
        ),
        (
            lambda capability: {
                **_trusted_binding(capability),
                "_automation_project_invocation": {
                    **_project_invocation(capability),
                    "automation_generation": 2,
                },
            },
            "PLUGIN_PROJECT_GENERATION_CONFLICT",
        ),
    ],
)
def test_router_requires_exact_server_owned_project_generation(
    tmp_path: Path,
    binding_mutator,
    expected_code: str,
) -> None:
    capability = _capability(tmp_path)
    sandbox = _OutputSandbox(canonical_json_bytes(_plugin_result(str(capability["name"]))))
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        integrity_verifier=_Integrity(),
        sandbox_launcher=sandbox,
        generation_leases=_LeaseRepository({"project-a": capability}),
        release_hold_provider=lambda: False,
    )

    with pytest.raises(PluginExecutionError) as raised:
        asyncio.run(
            router.execute(
                capability,
                {},
                trusted_invocation_context=binding_mutator(capability),
            )
        )

    assert raised.value.code == expected_code
    assert sandbox.launch_count == 0


def test_router_defaults_to_release_hold_before_generation_lease(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    sandbox = _OutputSandbox(canonical_json_bytes(_plugin_result(str(capability["name"]))))
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        integrity_verifier=_Integrity(),
        sandbox_launcher=sandbox,
        generation_leases=_LeaseRepository({"project-a": capability}),
    )

    with pytest.raises(PluginExecutionError) as raised:
        asyncio.run(
            router.execute(
                capability,
                {},
                trusted_invocation_context=_trusted_binding(capability),
            )
        )

    assert raised.value.code == "PLUGIN_RELEASE_HELD"
    assert sandbox.launch_count == 0


def test_invalid_output_before_broker_write_is_failed_before_write(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    leases = _LeaseRepository({"project-a": capability})
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(b"not-json"),
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )

    result = asyncio.run(
        router.execute(
            capability,
            {},
            trusted_invocation_context=_trusted_binding(capability),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "PLUGIN_OUTPUT_INVALID"
    assert leases.released[0][1] == RuntimeLeaseOutcome.FAILED_BEFORE_WRITE


def test_invalid_output_after_broker_write_is_outcome_unknown(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    leases = _LeaseRepository({"project-a": capability})
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(started_mutating_calls=1),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(b"not-json"),
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )

    result = asyncio.run(
        router.execute(
            capability,
            {},
            trusted_invocation_context=_trusted_binding(capability),
        )
    )

    assert result["error_code"] == "WRITE_OUTCOME_UNKNOWN"
    assert leases.released[0][1] == RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN


def test_started_write_lease_release_failure_returns_governing_unknown(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    class _ReleaseFailingLeaseRepository(_LeaseRepository):
        def release_generation(
            self,
            lease: RuntimeGenerationLease,
            *,
            outcome: RuntimeLeaseOutcome,
        ) -> None:
            del lease, outcome
            raise RuntimeError("generation lease persistence unavailable")

    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(started_mutating_calls=1),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(canonical_json_bytes(_plugin_result(str(capability["name"])))),
        generation_leases=_ReleaseFailingLeaseRepository({"project-a": capability}),
        release_hold_provider=lambda: False,
    )

    step = _step(str(capability["name"]))
    result = asyncio.run(
        RegisteredToolExecutionAdapter(catalog=_Catalog(capability), executor=router).execute_step(
            step,
            run_id=str(uuid.uuid4()),
            step_id=str(uuid.uuid4()),
            execution_context={
                "source": "console",
                "_automation_project_invocation": _project_invocation(capability),
            },
        )
    )

    assert result["status"] == "FAILED"
    assert not isinstance(result, GenerationBoundResult)
    assert result["error"]["code"] == "WRITE_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert result["error"]["persistence_diagnostic"]["code"] == "RUNTIMEERROR"
    assert result["error"]["persistence_diagnostic"]["message"] == "generation lease persistence unavailable"
    outcome = ResultVerifier().verify(
        step,
        result,
        capability,
    )
    assert outcome.accepted is False
    assert outcome.run_status.value == "BLOCKED_DATA"
    assert outcome.code == "WRITE_OUTCOME_UNKNOWN"


def test_zero_started_writes_keep_lease_release_failure_behavior(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    class _ReleaseFailingLeaseRepository(_LeaseRepository):
        def release_generation(
            self,
            lease: RuntimeGenerationLease,
            *,
            outcome: RuntimeLeaseOutcome,
        ) -> None:
            del lease, outcome
            raise RuntimeError("generation lease persistence unavailable")

    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(started_mutating_calls=0),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(canonical_json_bytes(_plugin_result(str(capability["name"])))),
        generation_leases=_ReleaseFailingLeaseRepository({"project-a": capability}),
        release_hold_provider=lambda: False,
    )

    with pytest.raises(RuntimeError, match="generation lease persistence unavailable"):
        asyncio.run(
            router.execute(
                capability,
                {},
                trusted_invocation_context=_trusted_binding(capability),
            )
        )


def test_lease_run_binding_mismatch_blocks_before_plugin_launch(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    class _MismatchedLeaseRepository(_LeaseRepository):
        def acquire_committed_generation(self, *args: object, **kwargs: object) -> RuntimeGenerationLease:
            lease = super().acquire_committed_generation(*args, **kwargs)
            return RuntimeGenerationLease(
                **{**lease.__dict__, "orchestration_run_id": "another-run"}
            )

    leases = _MismatchedLeaseRepository({"project-a": capability})
    sandbox = _OutputSandbox(canonical_json_bytes(_plugin_result(str(capability["name"]))))
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        integrity_verifier=_Integrity(),
        sandbox_launcher=sandbox,
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )

    with pytest.raises(PluginExecutionError) as raised:
        asyncio.run(
            router.execute(
                capability,
                {},
                trusted_invocation_context=_trusted_binding(capability),
            )
        )

    assert raised.value.code == "PLUGIN_GENERATION_LEASE_RUN_BINDING_CONFLICT"
    assert sandbox.launch_count == 0
    assert leases.released[0][1] == RuntimeLeaseOutcome.FAILED_BEFORE_WRITE


def test_plugin_catalog_never_exposes_project_actions_to_llm() -> None:
    from agent.automation_plugins.catalog import PluginCatalog

    class _Repository:
        def list_instances(self) -> list[object]:
            raise AssertionError("LLM projection must not inspect installed action contracts")

    assert PluginCatalog(_Repository()).list_llm_capabilities() == []


def test_router_observability_bridges_core_and_active_plugin_invocations() -> None:
    core = _Core()
    router = PluginExecutionRouter(
        core_executor=core,
        capability_issuer=_Issuer(),
    )
    router._running["plugin-invocation"] = {  # noqa: SLF001 - observability boundary
        "proc": _RunningProcess(),
        "started_at": "2026-08-15 00:01:00",
        "core_tool_name": "",
        "action_name": "automation.project-a.run",
        "automation_id": "project-a",
        "generation": 1,
        "run_id": "run-1",
        "step_id": "step-1",
    }

    assert router.last_tool_info() == core.last_info
    assert router.heavy_lock_held() is True
    assert router.is_tool_running("automation.project-a.run") is True
    assert router.is_tool_running("legacy-running") is True
    assert router.running_tools() == ["automation.project-a.run", "legacy-running"]
    assert router.get_running_output("automation.project-a.run") == {
        "lines": [],
        "running": True,
        "offset": 0,
        "total": 0,
        "started_at": "invocation:plugin-invocation",
        "invocation_id": "plugin-invocation",
        "run_id": "run-1",
        "step_id": "step-1",
        "cancel_requested": False,
    }
    assert router.get_running_output("legacy-running")["lines"] == ["legacy-running"]


def test_router_observability_excludes_finished_plugins_and_deduplicates_names() -> None:
    core = _Core()
    router = PluginExecutionRouter(
        core_executor=core,
        capability_issuer=_Issuer(),
    )
    router._running.update(  # noqa: SLF001 - observability boundary
        {
            "finished": {
                "proc": _FinishedProcess(),
                "action_name": "finished-action",
            },
            "finished-duplicate": {
                "proc": _FinishedProcess(),
                "action_name": "legacy-running",
            },
            "plugin-a": {
                "proc": _RunningProcess(),
                "action_name": "legacy-running",
            },
        }
    )

    assert router.running_tools() == ["legacy-running"]
    assert router.heavy_lock_held() is True
    assert router.get_running_output("finished-action")["lines"] == ["finished-action"]
    finished = router.get_running_output(
        "finished-action",
        started_at="invocation:finished",
    )
    assert finished["running"] is False
    assert finished["invocation_id"] == "finished"
    mixed = router.get_running_output("legacy-running")
    assert mixed.get("ambiguous") is None
    assert mixed["invocation_id"] == "plugin-a"
    router._running["plugin-b"] = {  # noqa: SLF001 - ambiguity boundary
        "proc": _RunningProcess(),
        "action_name": "legacy-running",
    }
    ambiguous = router.get_running_output("legacy-running")
    assert ambiguous["ambiguous"] is True
    assert ambiguous["active_invocations"] == 2

    router._running = {  # noqa: SLF001 - observability boundary
        "finished": {
            "proc": _FinishedProcess(),
            "action_name": "finished-action",
        }
    }
    assert router.heavy_lock_held() is False
    assert "finished-action" not in router.running_tools()
    core.heavy_held = True
    assert router.heavy_lock_held() is True


def test_router_observability_does_not_swallow_core_failures() -> None:
    class _BrokenCore(_Core):
        def last_tool_info(self) -> dict[str, Any]:
            raise RuntimeError("last tool status unavailable")

        def heavy_lock_held(self) -> bool:
            raise RuntimeError("heavy lock status unavailable")

    router = PluginExecutionRouter(
        core_executor=_BrokenCore(),
        capability_issuer=_Issuer(),
    )
    router._running["plugin-invocation"] = {  # noqa: SLF001 - fail-closed boundary
        "proc": _RunningProcess(),
        "action_name": "automation.project-a.run",
    }

    with pytest.raises(RuntimeError, match="last tool status unavailable"):
        router.last_tool_info()
    with pytest.raises(RuntimeError, match="heavy lock status unavailable"):
        router.heavy_lock_held()


def test_bubblewrap_outer_process_always_starts_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bwrap"
    executable.write_text("binary", encoding="utf-8")
    install_root = tmp_path / "install"
    (install_root / "venv" / "bin").mkdir(parents=True)
    (install_root / "venv" / "bin" / "python").write_text("python", encoding="utf-8")
    (install_root / "venv" / "pyvenv.cfg").write_text(
        f"home = {sys.prefix}/bin\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    sentinel = object()

    async def _spawn(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("agent.automation_plugins.sandbox.asyncio.create_subprocess_exec", _spawn)
    result = asyncio.run(
        BubblewrapPluginSandbox(executable).launch(
            install_root=install_root,
            python_relative="venv/bin/python",
            entrypoint_relative="payload/main.py",
            environment={},
            broker_socket_path=None,
        )
    )
    assert result is sentinel
    assert captured["start_new_session"] is True
    command = tuple(str(item) for item in captured["args"])
    assert "--clearenv" not in command
    assert str(sys.base_prefix) in command


def test_router_reports_cached_sandbox_canary_from_fake_launcher() -> None:
    checked_at = datetime.now(timezone.utc)

    class _CanarySandbox:
        def __init__(self, result: SandboxCanaryResult) -> None:
            self.result = result
            self.calls = 0

        async def startup_canary(self) -> SandboxCanaryResult:
            self.calls += 1
            return self.result

    good = _CanarySandbox(SandboxCanaryResult(True, "OK", checked_at))
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        sandbox_launcher=good,
    )
    assert asyncio.run(router.startup_sandbox_canary()) == good.result
    assert good.calls == 1

    broken = _CanarySandbox(
        SandboxCanaryResult(False, "PLUGIN_SANDBOX_CANARY_FAILED", checked_at)
    )
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        sandbox_launcher=broken,
    )
    assert asyncio.run(router.startup_sandbox_canary()).code == "PLUGIN_SANDBOX_CANARY_FAILED"
    assert broken.calls == 1


def test_bubblewrap_canary_caches_success_and_never_uses_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bwrap"
    executable.write_text("binary", encoding="utf-8")
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    (base / "bin" / "python").write_text("binary", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    captured: dict[str, object] = {}

    class _CanaryProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b'{"ok": true}\n', b""

    async def _spawn(*args: object, **kwargs: object) -> _CanaryProcess:
        captured["args"] = args
        captured.update(kwargs)
        return _CanaryProcess()

    monkeypatch.setattr("agent.automation_plugins.sandbox.asyncio.create_subprocess_exec", _spawn)
    sandbox = BubblewrapPluginSandbox(
        executable,
        trusted_base_prefix=base,
        trusted_runtime_prefix=runtime,
    )
    first = asyncio.run(sandbox.startup_canary())
    second = asyncio.run(sandbox.startup_canary())

    assert first.healthy is True
    assert second == first
    command = tuple(str(item) for item in captured["args"])
    assert "--clearenv" not in command
    assert str(base) in command
    assert "BOYI_PLUGIN_EXECUTION_CAPABILITY" not in command


def test_concurrent_invocation_cancel_is_bound_to_exact_run(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    leases = _LeaseRepository({"project-a": capability})
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_SleepSandbox(),
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )
    tool_name = str(capability["name"])
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    step_a, step_b = str(uuid.uuid4()), str(uuid.uuid4())

    async def _exercise() -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        task_a = asyncio.create_task(
            router.execute(
                capability,
                {},
                trusted_invocation_context=_trusted_binding(
                    capability,
                    run_id=run_a,
                    step_id=step_a,
                ),
            )
        )
        task_b = asyncio.create_task(
            router.execute(
                capability,
                {},
                trusted_invocation_context=_trusted_binding(
                    capability,
                    run_id=run_b,
                    step_id=step_b,
                ),
            )
        )
        for _ in range(100):
            if len(router._running) == 2:  # noqa: SLF001 - concurrency boundary test
                break
            await asyncio.sleep(0.01)
        assert len(router._running) == 2  # noqa: SLF001
        entries = list(router._running.values())  # noqa: SLF001
        if os.name != "nt":
            for entry in entries:
                process = entry["proc"]
                assert os.getpgid(process.pid) == process.pid
                assert process.pid != os.getpgrp()

        cancelled_a = await router.cancel_bound_run(
            tool_name=tool_name,
            run_id=run_a,
            step_id=step_a,
        )
        assert cancelled_a["ok"] is True
        survivors = [
            entry
            for entry in router._running.values()  # noqa: SLF001
            if entry.get("run_id") == run_b
        ]
        assert len(survivors) == 1
        assert survivors[0]["proc"].returncode is None
        cancelled_b = await router.cancel_bound_run(
            tool_name=tool_name,
            run_id=run_b,
            step_id=step_b,
        )
        assert cancelled_b["ok"] is True
        result_a, result_b = await asyncio.gather(task_a, task_b)
        return result_a, result_b

    result_a, result_b = asyncio.run(_exercise())
    assert result_a["error_code"] == "PLUGIN_PROCESS_FAILED"
    assert result_b["error_code"] == "PLUGIN_PROCESS_FAILED"
    assert [outcome for _, outcome in leases.released] == [
        RuntimeLeaseOutcome.FAILED_BEFORE_WRITE,
        RuntimeLeaseOutcome.FAILED_BEFORE_WRITE,
    ]
