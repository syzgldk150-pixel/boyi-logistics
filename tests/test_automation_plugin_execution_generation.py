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

from agent.automation_plugins import sandbox as sandbox_module
from agent.automation_plugins.errors import PluginConflictError, PluginExecutionError
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    governance_for_effect,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import (
    GenerationBoundResult,
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeGenerationLease,
    RuntimeGenerationSnapshot,
    RuntimeLeaseOutcome,
)
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox, SandboxCanaryResult
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.orchestration.models import OperationType, PlanStep, RiskLevel, sha256_json
from agent.orchestration.result_verifier import ResultVerifier


def _digest(value: object) -> str:
    payload = canonical_json_bytes(value) if isinstance(value, (dict, list)) else str(value).encode()
    return hashlib.sha256(payload).hexdigest()


def _service_v2_runtime_permissions() -> dict[str, Any]:
    # V2 router fixtures must use the same Host declaration as real packages,
    # rather than inheriting the V1 browser.invoke fixture below.
    source = (
        Path(__file__).resolve().parents[1]
        / "agent/service_v2_plugins/self_pickup_problem_upload_v2/manifest.json"
    )
    manifest = AutomationPluginManifestV2.from_mapping(
        json.loads(source.read_text(encoding="utf-8"))
    )
    return copy.deepcopy(dict(ServiceV2ProjectContract.from_manifest(manifest).runtime_permissions))


def _project_invocation(
    capability: Mapping[str, Any],
    *,
    contract_id: str = "console",
) -> dict[str, Any]:
    metadata = capability["_plugin_runtime"]
    return {
        "schema_version": 1,
        "automation_id": metadata["automation_id"],
        "automation_generation": metadata["generation"],
        "entrypoint": "console",
        "contract_id": contract_id,
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
    contract_id: str = "console",
) -> dict[str, Any]:
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "step_id": step_id or str(uuid.uuid4()),
        "_automation_project_invocation": _project_invocation(
            capability,
            contract_id=contract_id,
        ),
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
            "trust_source": PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
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


def _mixed_effect_service_v2_capability(tmp_path: Path) -> dict[str, Any]:
    capability = _capability(tmp_path, automation_id="mixed-service")
    service = "plugin.mixed_service.runner@1"
    read_governance = governance_for_effect("read").to_mapping()
    write_governance = governance_for_effect("external_write").to_mapping()
    capability.update(write_governance)
    capability["version"] = "1.1.0"
    capability["output_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string"},
            "data": {"type": "object", "additionalProperties": True},
            "meta": {"type": "object", "additionalProperties": True},
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
    metadata = capability["_plugin_runtime"]
    metadata.update(
        {
            "plugin_id": "mixed_service",
            "runtime_permissions": _service_v2_runtime_permissions(),
            "version": "1.1.0",
            "trust_source": PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
            "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
            "plugin_api": "2.0.0",
            "service_contracts": {
                "provides": [
                    {
                        "service": service,
                        "operations": [
                            {"name": "inspect", "effect": "read"},
                            {"name": "apply", "effect": "external_write"},
                        ],
                    }
                ],
                "requires": [],
            },
            "compiled_invocations": {
                "console": {
                    "arguments": {},
                    "dynamic_resolvers": {},
                    "target": {
                        "service": service,
                        "operation": "inspect",
                        "contribution_id": "console",
                        "contribution_kind": "console",
                    },
                    "governance": read_governance,
                },
                "apply": {
                    "arguments": {},
                    "dynamic_resolvers": {},
                    "target": {
                        "service": service,
                        "operation": "apply",
                        "contribution_id": "apply",
                        "contribution_kind": "console",
                    },
                    "governance": write_governance,
                },
            },
        }
    )
    return capability


def _selection_service_v2_capability(tmp_path: Path) -> dict[str, Any]:
    capability = _mixed_effect_service_v2_capability(tmp_path)
    service = "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1"
    write_governance = governance_for_effect("external_write").to_mapping()
    capability["name"] = "automation.self_pickup_problem_upload.run"
    metadata = capability["_plugin_runtime"]
    metadata.update(
        {
            "automation_id": "self_pickup_problem_upload",
            "plugin_id": "self_pickup_problem_upload_v2",
            "contributions": {
                "console": [
                    {
                        "id": "execute_console",
                        "service": service,
                        "operation": "execute",
                        "selection_preview_operation": "preview",
                        "default_enabled": False,
                    }
                ],
                "scheduler": [],
                "webhook": [],
                "feishu": [],
                "events": [],
            },
            "compiled_invocations": {
                "execute_console": {
                    "arguments": {},
                    "dynamic_resolvers": {},
                    "target": {
                        "service": service,
                        "operation": "execute",
                        "contribution_id": "execute_console",
                        "contribution_kind": "console",
                    },
                    "governance": write_governance,
                }
            },
            "service_contracts": {
                "provides": [
                    {
                        "service": service,
                        "operations": [
                            {"name": "preview", "effect": "read"},
                            {"name": "execute", "effect": "external_write"},
                        ],
                    }
                ],
                "requires": [],
            },
        }
    )
    return capability


def _selection_service_result(
    *,
    service: str,
    operation: str,
    evidence_ref: str,
) -> dict[str, Any]:
    observed_at = "2026-08-30T10:00:00Z"
    outcome = "READ_ONLY" if operation == "preview" else "WRITE_VERIFIED"
    data = {
        "ok": True,
        "evidence": {
            "service": service,
            "operation": operation,
            "outcome": outcome,
        },
    }
    return {
        "status": "SUCCESS",
        "data": data,
        "meta": {
            "source_system": "host-test",
            "observed_at": observed_at,
            "record_count": 1,
            "pagination_complete": True,
            "evidence_refs": [evidence_ref],
            "postconditions": {"0": True},
            "postcondition_evidence": {
                "0": {
                    "condition": "plugin_result_contract_valid",
                    "verified": True,
                    "observed_at": observed_at,
                    "evidence_ref": evidence_ref,
                    "details": {"result_summary": copy.deepcopy(data)},
                }
            },
            **(
                {"write_outcome": "WRITE_VERIFIED"}
                if operation == "execute"
                else {}
            ),
        },
        "warnings": [],
        "error": None,
    }


def _mixed_service_write_result(
    *,
    service: str = "plugin.mixed_service.runner@1",
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    operation = "apply"
    evidence_ref = "evidence:mixed-service:apply"
    observed_at = "2026-08-30T10:00:00+08:00"
    data = {
        "evidence": {
            "service": service,
            "operation": operation,
            "outcome": "WRITE_VERIFIED",
        },
        "changed": True,
    }
    result = {
        "status": "SUCCESS",
        "data": data,
        "meta": {
            "source_system": "host-test",
            "observed_at": observed_at,
            "record_count": 1,
            "pagination_complete": True,
            "evidence_refs": [evidence_ref],
            "write_outcome": "WRITE_VERIFIED",
            "postconditions": {"0": True},
            "postcondition_evidence": {
                "0": {
                    "condition": "plugin_result_contract_valid",
                    "verified": True,
                    "observed_at": observed_at,
                    "evidence_ref": evidence_ref,
                    "details": {
                        "evidence_refs": [evidence_ref],
                        "result_summary": copy.deepcopy(data),
                    },
                }
            },
        },
        "warnings": [],
        "error": None,
    }
    observations = (
        {
            "request_id": "33333333-3333-4333-8333-333333333333",
            "operation": "service.invoke",
            "action": "apply",
            "role": "__system__",
            "arguments_sha256": "e" * 64,
            "write_started": True,
            "evidence_ref": evidence_ref,
            "result": {
                "status": "SUCCESS",
                "data": {"provider_write": "verified"},
                "meta": {"evidence_refs": ["provider:evidence:apply"]},
                "warnings": [],
                "error": None,
            },
        },
    )
    return result, observations


def _mixed_service_write_step(capability: Mapping[str, Any]) -> PlanStep:
    return PlanStep(
        step_key="mixed-write",
        tool_name=str(capability["name"]),
        tool_version=str(capability["version"]),
        operation_type=OperationType.EXTERNAL_WRITE,
        arguments={},
        account_id="account-1",
        depends_on=(),
        idempotency_key="mixed-service-write",
        expected_evidence=(dict(capability["evidence"]),),
        postconditions=tuple(capability["postconditions"]),
        risk_level=RiskLevel.HIGH,
        requires_approval=True,
    )


def _mixed_service_effective_write_capability(
    capability: Mapping[str, Any],
) -> dict[str, Any]:
    effective = copy.deepcopy(dict(capability))
    governance = governance_for_effect("external_write").to_mapping()
    effective.update(governance)
    effective["service"] = "plugin.mixed_service.runner@1"
    effective["operation"] = "apply"
    return effective


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
        runtime_model=PluginRuntimeModel(
            str(metadata.get("runtime_model") or PluginRuntimeModel.SERVICE_V2.value)
        ),
        plugin_api=str(metadata.get("plugin_api") or "2.0.0"),
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
        orchestration_run_id: str | None,
        invocation_id: str | None = None,
        provider_call: bool = False,
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
            invocation_id=invocation_id,
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

    def __init__(
        self,
        *,
        started_mutating_calls: int = 0,
        host_call_observations: tuple[Mapping[str, Any], ...] = (),
    ) -> None:
        self._started_mutating_calls = started_mutating_calls
        self._host_call_observations = host_call_observations
        self.last_issue: dict[str, object] | None = None

    def issue(self, **values: object) -> str:
        self.last_issue = dict(values)
        return "test-capability"

    def revoke(self, capability: str) -> None:
        assert capability == "test-capability"

    def started_mutating_call_count(self, capability: str) -> int:
        assert capability == "test-capability"
        return self._started_mutating_calls

    def broker_call_observations(
        self,
        capability: str,
    ) -> tuple[Mapping[str, Any], ...]:
        assert capability == "test-capability"
        return self._host_call_observations


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

    async def cancel_bound_run(
        self,
        *,
        tool_name: str,
        run_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        return {
            "ok": True,
            "source": "core",
            "tool_name": tool_name,
            "run_id": run_id,
            "step_id": step_id,
        }


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


class _PayloadCaptureSandbox:
    def __init__(self, output: bytes, payload_path: Path) -> None:
        self.output = output
        self.payload_path = payload_path

    async def launch(self, **_: object) -> asyncio.subprocess.Process:
        source = (
            "import pathlib,sys; "
            f"pathlib.Path({str(self.payload_path)!r}).write_bytes(sys.stdin.buffer.read()); "
            f"sys.stdout.buffer.write({self.output!r})"
        )
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


def test_router_forwards_execution_identity_unchanged_to_core() -> None:
    captured: dict[str, object] = {}

    class CapturingCore(_Core):
        async def execute(
            self,
            capability: Mapping[str, Any],
            params: Mapping[str, Any],
            *,
            execution_identity: Mapping[str, object] | None = None,
        ) -> Mapping[str, Any]:
            captured.update(
                capability=capability,
                params=params,
                execution_identity=execution_identity,
            )
            return {"success": True}

    router = PluginExecutionRouter(
        core_executor=CapturingCore(),
        capability_issuer=_Issuer(),
    )
    identity = {"schema_version": 1, "lock_key": "exact-resource"}

    result = asyncio.run(
        router.execute(
            {"name": "core-tool"},
            {"query": "x"},
            execution_identity=identity,
        )
    )

    assert result == {"success": True}
    assert captured["execution_identity"] is identity


def test_internal_service_invocation_uses_normal_generation_lease_and_opaque_chain(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path, automation_id="base-service-project")
    service = "plugin.base_service.runner@1"
    metadata = capability["_plugin_runtime"]
    metadata.update(
        {
            "plugin_id": "base_service",
            "runtime_permissions": _service_v2_runtime_permissions(),
            "trust_source": PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
            "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
            "plugin_api": "2.0.0",
            "service_contracts": {
                "provides": [
                    {
                        "service": service,
                        "operations": [{"name": "get", "effect": "read"}],
                    }
                ],
                "requires": [],
            },
            "compiled_invocations": {},
        }
    )
    capability["operation_type"] = "read"
    capability["output_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string"},
            "data": {"type": "object", "additionalProperties": True},
            "meta": {"type": "object", "additionalProperties": True},
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
    provider_result = {
        "status": "SUCCESS",
        "data": {
            "evidence": {
                "service": service,
                "operation": "get",
                "outcome": "READ_VERIFIED",
            }
        },
        "meta": {
            "observed_at": "2026-08-30T00:00:00Z",
            "evidence_refs": ["evidence:base:get"],
        },
        "warnings": [],
        "error": None,
    }
    leases = _LeaseRepository({"base-service-project": capability})
    issuer = _Issuer(started_mutating_calls=0)
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=issuer,
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(canonical_json_bytes(provider_result)),
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )
    identity = "11111111-1111-4111-8111-111111111111"
    class DirectAdmission:
        async def reserve_provider(self, invocation_id, capability):
            assert invocation_id == identity
            assert capability["operation_type"] == "read"
            return (), {}
    router.direct_invocations = DirectAdmission()

    result = asyncio.run(
        router.execute_service_operation(
            capability,
            {"query": "safe"},
            service=service,
            operation="get",
            effect=CapabilityEffect.READ,
            call_chain=(service,),
            invocation_id=identity,
        )
    )

    assert result == provider_result
    assert leases.released[0][1] is RuntimeLeaseOutcome.SUCCEEDED
    assert issuer.last_issue is not None
    assert issuer.last_issue["runtime_permissions"]["_service_call_chain"] == [service]
    assert issuer.last_issue["write_attempt_context"]["invocation_id"] == identity
    assert "orchestration_run_id" not in issuer.last_issue["write_attempt_context"]


def test_direct_service_v2_read_contribution_does_not_inherit_summary_write_lease(
    tmp_path: Path,
) -> None:
    capability = _mixed_effect_service_v2_capability(tmp_path)
    leases = _LeaseRepository({"mixed-service": capability})
    issuer = _Issuer(started_mutating_calls=0)
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=issuer,
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(
            canonical_json_bytes(_plugin_result("mixed_service"))
        ),
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

    assert isinstance(result, GenerationBoundResult)
    assert result.generation_verification.requires_write_verification is False
    assert result.generation_verification.started_mutating_call_count == 0
    assert leases.released[0][1] is RuntimeLeaseOutcome.SUCCEEDED
    assert issuer.last_issue is not None
    assert issuer.last_issue["runtime_permissions"]["_service_effect_ceiling"] == "read"


@pytest.mark.parametrize(
    ("arguments", "expected_operation", "expected_effect", "started_mutating_calls"),
    (
        (
            {
                "dry_run": True,
                "selected_bill_codes": [],
                "preview_fingerprint": "",
            },
            "preview",
            CapabilityEffect.READ,
            0,
        ),
        (
            {
                "dry_run": False,
                "selected_bill_codes": ["R_SELF"],
                "preview_fingerprint": "a" * 64,
            },
            "execute",
            CapabilityEffect.EXTERNAL_WRITE,
            1,
        ),
    ),
)
def test_service_v2_selection_payload_target_matches_signed_phase(
    tmp_path: Path,
    arguments: dict[str, Any],
    expected_operation: str,
    expected_effect: CapabilityEffect,
    started_mutating_calls: int,
) -> None:
    capability = _selection_service_v2_capability(tmp_path)
    service = "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1"
    evidence_ref = f"evidence:self-pickup:{expected_operation}"
    payload_path = tmp_path / f"{expected_operation}-payload.json"
    output = _selection_service_result(
        service=service,
        operation=expected_operation,
        evidence_ref=evidence_ref,
    )
    leases = _LeaseRepository({"self_pickup_problem_upload": capability})
    observations = (
        {
            "request_id": "44444444-4444-4444-8444-444444444444",
            "operation": "service.invoke",
            "action": "execute",
            "role": "__system__",
            "arguments_sha256": "f" * 64,
            "write_started": True,
            "evidence_ref": evidence_ref,
            "result": {"status": "SUCCESS"},
        },
    ) if expected_effect is CapabilityEffect.EXTERNAL_WRITE else ()
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(
            started_mutating_calls=started_mutating_calls,
            host_call_observations=observations,
        ),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_PayloadCaptureSandbox(
            json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            payload_path,
        ),
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )

    raw = asyncio.run(
        router.execute(
            capability,
            arguments,
            trusted_invocation_context=_trusted_binding(
                capability,
                contract_id="execute_console",
            ),
        )
    )

    assert raw["status"] == "SUCCESS"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["arguments"] == arguments
    assert payload["target"] == {
        "service": service,
        "operation": expected_operation,
        "contribution_id": "execute_console",
        "contribution_kind": "console",
    }
    assert payload["governance"] == governance_for_effect(expected_effect.value).to_mapping()


def test_direct_service_v2_write_success_without_started_receipt_is_not_verifying(
    tmp_path: Path,
) -> None:
    capability = _mixed_effect_service_v2_capability(tmp_path)
    leases = _LeaseRepository({"mixed-service": capability})
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(started_mutating_calls=0),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(
            canonical_json_bytes(_plugin_result("mixed_service"))
        ),
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )

    result = asyncio.run(
        router.execute(
            capability,
            {},
            trusted_invocation_context=_trusted_binding(
                capability,
                contract_id="apply",
            ),
        )
    )

    assert not isinstance(result, GenerationBoundResult)
    assert leases.released[0][1] is RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
    assert not leases.verifying


def test_mixed_service_write_closes_router_and_verifier_host_evidence(
    tmp_path: Path,
) -> None:
    capability = _mixed_effect_service_v2_capability(tmp_path)
    result, observations = _mixed_service_write_result()
    leases = _LeaseRepository({"mixed-service": capability})
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(
            started_mutating_calls=1,
            host_call_observations=observations,
        ),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(canonical_json_bytes(result)),
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )

    raw = asyncio.run(
        router.execute(
            capability,
            {},
            trusted_invocation_context=_trusted_binding(
                capability,
                contract_id="apply",
            ),
        )
    )

    assert isinstance(raw, GenerationBoundResult)
    assert raw.generation_verification.requires_write_verification is True
    assert raw.generation_verification.host_call_observations == observations
    effective = _mixed_service_effective_write_capability(capability)
    verified = ResultVerifier(leases).verify(
        _mixed_service_write_step(effective),
        raw,
        effective,
    )
    assert verified.accepted is True
    assert leases.finalized[0][1] is RuntimeLeaseOutcome.WRITE_VERIFIED


def test_mixed_service_write_rejects_cross_contribution_target_evidence(
    tmp_path: Path,
) -> None:
    capability = _mixed_effect_service_v2_capability(tmp_path)
    result, observations = _mixed_service_write_result(
        service="plugin.other.runner@1"
    )
    leases = _LeaseRepository({"mixed-service": capability})
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(
            started_mutating_calls=1,
            host_call_observations=observations,
        ),
        integrity_verifier=_Integrity(),
        sandbox_launcher=_OutputSandbox(canonical_json_bytes(result)),
        generation_leases=leases,
        release_hold_provider=lambda: False,
    )
    raw = asyncio.run(
        router.execute(
            capability,
            {},
            trusted_invocation_context=_trusted_binding(
                capability,
                contract_id="apply",
            ),
        )
    )
    effective = _mixed_service_effective_write_capability(capability)

    verified = ResultVerifier(leases).verify(
        _mixed_service_write_step(effective),
        raw,
        effective,
    )

    assert verified.accepted is False
    assert verified.code == "POSTCONDITION_UNVERIFIED"
    assert leases.finalized[0][1] is RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN


def test_router_cancel_bound_run_falls_through_to_core_when_no_plugin_matches() -> None:
    router = PluginExecutionRouter(
        core_executor=_Core(),
        capability_issuer=_Issuer(),
    )

    result = asyncio.run(
        router.cancel_bound_run(
            tool_name="legacy-running",
            run_id="run-1",
            step_id="step-1",
        )
    )

    assert result == {
        "ok": True,
        "source": "core",
        "tool_name": "legacy-running",
        "run_id": "run-1",
        "step_id": "step-1",
    }


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
    sandbox = _OutputSandbox(
        canonical_json_bytes(
            _plugin_result(str(capability["_plugin_runtime"]["plugin_id"]))
        )
    )
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
    sandbox = _OutputSandbox(
        canonical_json_bytes(
            _plugin_result(str(capability["_plugin_runtime"]["plugin_id"]))
        )
    )
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
    limiter = tmp_path / "prlimit"
    limiter.write_text("binary", encoding="utf-8")
    install_root = tmp_path / "install"
    (install_root / "venv" / "bin").mkdir(parents=True)
    (install_root / "venv" / "bin" / "python").write_text("python", encoding="utf-8")
    (install_root / "venv" / "pyvenv.cfg").write_text(
        f"home = {sys.prefix}/bin\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    baseline_calls = 0
    sentinel = object()

    def _task_count() -> int:
        nonlocal baseline_calls
        baseline_calls += 1
        return 137

    async def _spawn(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("agent.automation_plugins.sandbox.asyncio.create_subprocess_exec", _spawn)
    monkeypatch.setattr("agent.automation_plugins.sandbox._current_real_uid_task_count", _task_count)
    result = asyncio.run(
        BubblewrapPluginSandbox(executable, prlimit_path=limiter).launch(
            install_root=install_root,
            python_relative="venv/bin/python",
            entrypoint_relative="payload/main.py",
            environment={},
            broker_socket_path=None,
        )
    )
    assert result is sentinel
    assert baseline_calls == 1
    assert captured["start_new_session"] is True
    command = tuple(str(item) for item in captured["args"])
    assert command[:7] == (
        str(limiter),
        "--as=1073741824:1073741824",
        "--nproc=201:201",
        "--cpu=300:300",
        "--fsize=16777216:16777216",
        "--nofile=128:128",
        "--",
    )
    assert command[7] == str(executable)
    assert "--clearenv" not in command
    readonly_binds = [
        (Path(command[index + 1]), Path(command[index + 2]))
        for index, item in enumerate(command)
        if item == "--ro-bind"
    ]
    base_prefix = Path(sys.base_prefix)
    assert any(
        source == destination
        and (source == base_prefix or source in base_prefix.parents)
        for source, destination in readonly_binds
    ), "the trusted Python base must be available through an identity read-only bind"
    permitted_identity_roots = {Path(root) for root in ("/usr", "/bin", "/lib", "/lib64")}
    permitted_identity_roots.add(base_prefix)
    assert all(
        (source == destination and source in permitted_identity_roots)
        or (source == install_root and destination == Path("/plugin"))
        for source, destination in readonly_binds
    ), "the interpreter bind must not expose the Agent venv or a broader host directory"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="Linux /proc contract")
def test_bubblewrap_nproc_baseline_counts_real_uid_tasks_and_fails_closed(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    current_uid = os.getuid()
    current_pid = os.getpid()

    def _write_status(pid: int, *, uid: int, threads: str) -> None:
        process_root = proc_root / str(pid)
        process_root.mkdir(parents=True)
        (process_root / "status").write_bytes(
            f"Name:\ttest\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nThreads:\t{threads}\n".encode()
        )

    _write_status(current_pid, uid=current_uid, threads="3")
    _write_status(current_pid + 1, uid=current_uid, threads="5")
    _write_status(current_pid + 2, uid=current_uid + 1, threads="100")
    (proc_root / str(current_pid + 3)).mkdir()
    assert sandbox_module._current_real_uid_task_count(proc_root) == 8  # noqa: SLF001

    malformed_root = tmp_path / "malformed-proc"
    malformed_process = malformed_root / str(current_pid)
    malformed_process.mkdir(parents=True)
    (malformed_process / "status").write_text(
        f"Uid:\t{current_uid}\t{current_uid}\t{current_uid}\t{current_uid}\nThreads:\tnot-a-number\n",
        encoding="utf-8",
    )
    with pytest.raises(PluginExecutionError) as malformed:
        sandbox_module._current_real_uid_task_count(malformed_root)  # noqa: SLF001
    assert malformed.value.code == "PLUGIN_SANDBOX_NPROC_BASELINE_UNAVAILABLE"

    unreadable_root = tmp_path / "unreadable-proc"
    (unreadable_root / str(current_pid)).mkdir(parents=True)
    with pytest.raises(PluginExecutionError) as unreadable:
        sandbox_module._current_real_uid_task_count(unreadable_root)  # noqa: SLF001
    assert unreadable.value.code == "PLUGIN_SANDBOX_NPROC_BASELINE_UNAVAILABLE"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="Linux /proc contract")
@pytest.mark.parametrize("round_index", range(20))
@pytest.mark.parametrize("error_type", [FileNotFoundError, ProcessLookupError, PermissionError])
def test_sandbox_process_exit_during_count_does_not_reject_new_launch(
    tmp_path, monkeypatch, round_index, error_type,
):
    import errno
    proc_root = tmp_path / "proc"
    current = proc_root / str(os.getpid())
    current.mkdir(parents=True)
    uid = os.getuid()
    (current / "status").write_text(
        f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nThreads:\t3\n", encoding="utf-8")
    exited = proc_root / str(os.getpid() + round_index + 1)
    exited.mkdir()
    original_read = Path.read_bytes
    def read_status(path):
        if path == exited / "status":
            code = {FileNotFoundError: errno.ENOENT, ProcessLookupError: errno.ESRCH,
                    PermissionError: errno.EACCES}[error_type]
            raise error_type(code, "isolated status read")
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", read_status)
    if error_type is PermissionError:
        with pytest.raises(PluginExecutionError) as denied:
            sandbox_module._current_real_uid_task_count(proc_root)
        assert denied.value.code == "PLUGIN_SANDBOX_NPROC_BASELINE_UNAVAILABLE"
    else:
        assert sandbox_module._current_real_uid_task_count(proc_root) == 3


def test_bubblewrap_fails_closed_without_a_regular_absolute_prlimit(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "bwrap"
    executable.write_text("binary", encoding="utf-8")
    missing = tmp_path / "missing-prlimit"

    with pytest.raises(ValueError, match="prlimit executable"):
        BubblewrapPluginSandbox(executable, prlimit_path=missing)

    relative = Path("prlimit")
    with pytest.raises(ValueError, match="prlimit executable"):
        BubblewrapPluginSandbox(executable, prlimit_path=relative)


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
    limiter = tmp_path / "prlimit"
    limiter.write_text("binary", encoding="utf-8")
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    (base / "bin" / "python").write_text("binary", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    captured: dict[str, object] = {}
    baseline_calls = 0

    def _task_count() -> int:
        nonlocal baseline_calls
        baseline_calls += 1
        return 219

    class _CanaryProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b'{"ok": true}\n', b""

    async def _spawn(*args: object, **kwargs: object) -> _CanaryProcess:
        captured["args"] = args
        captured.update(kwargs)
        return _CanaryProcess()

    monkeypatch.setattr("agent.automation_plugins.sandbox.asyncio.create_subprocess_exec", _spawn)
    monkeypatch.setattr("agent.automation_plugins.sandbox._current_real_uid_task_count", _task_count)
    sandbox = BubblewrapPluginSandbox(
        executable,
        trusted_base_prefix=base,
        trusted_runtime_prefix=runtime,
        prlimit_path=limiter,
    )
    first = asyncio.run(sandbox.startup_canary())
    second = asyncio.run(sandbox.startup_canary())

    assert first.healthy is True
    assert second == first
    assert baseline_calls == 1
    command = tuple(str(item) for item in captured["args"])
    assert command[:8] == (
        str(limiter),
        "--as=1073741824:1073741824",
        "--nproc=283:283",
        "--cpu=300:300",
        "--fsize=16777216:16777216",
        "--nofile=128:128",
        "--",
        str(executable),
    )
    assert "--clearenv" not in command
    assert str(base) in command
    assert "BOYI_PLUGIN_EXECUTION_CAPABILITY" not in command

