from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    GenerationBoundResult,
    GenerationVerificationContext,
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeActivationPhase,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
    RuntimeLeaseOutcome,
)
from agent.automation_plugins.production import ProductionServiceV2ProviderExecutor
from agent.automation_plugins.service_registry import (
    ResolvedServiceOperation,
    ServiceProviderAmbiguous,
    ServiceRegistryError,
    ServiceRegistry,
)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _provider_snapshot(
    automation_id: str,
    generation: int,
) -> RuntimeGenerationSnapshot:
    project_config: dict[str, object] = {}
    account_bindings: dict[str, object] = {}
    resource_bindings: dict[str, object] = {}
    schedule: dict[str, object] = {"kind": "none", "enabled": False}
    compiled_invocations: dict[str, object] = {}
    runtime_descriptor = {
        "install_metadata": {
            "install_root": "/immutable/base/1.0.0",
            "python_relative": "venv/bin/python",
        },
        "runtime": {
            "kind": "python_subprocess",
            "entrypoint": "payload/main.py",
            "mode": "on_demand",
        },
        "runtime_permissions": {"broker_operations": []},
        "account_roles": [],
        "resource_roles": [],
    }
    action_contract = {"operation_type": "read"}
    governance_anchor = {"name": "service-provider"}
    service_contracts = {
        "provides": [
            {
                "service": "plugin.base.runner@1",
                "operations": [
                    {"name": "get", "effect": "read"},
                    {"name": "run", "effect": "external_write"},
                ],
            }
        ],
        "requires": [],
    }
    metadata = {
        "project_config_version": generation,
        "project_config": project_config,
        "account_bindings": account_bindings,
        "resource_bindings": resource_bindings,
        "device_binding": None,
        "schedule": schedule,
        "compiled_invocations": compiled_invocations,
        "runtime_descriptor": runtime_descriptor,
        "action_contract": action_contract,
        "governance_anchor": governance_anchor,
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "plugin_api": "2.0.0",
        "service_contracts": service_contracts,
        "contributions": {},
        "storage_contract": {},
    }
    return RuntimeGenerationSnapshot(
        automation_id=automation_id,
        generation=generation,
        plugin_id="base",
        plugin_version="1.0.0",
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD,
        project_config_sha256=_digest(project_config),
        account_bindings_sha256=_digest(account_bindings),
        resource_bindings_sha256=_digest(resource_bindings),
        device_binding_sha256=_digest(None),
        schedule_sha256=_digest(schedule),
        core_registry_sha256=_digest(governance_anchor),
        tool_contract_sha256=_digest(action_contract),
        invocation_contracts_sha256=_digest({}),
        compiled_invocations_sha256=_digest(compiled_invocations),
        runtime_descriptor_sha256=_digest(runtime_descriptor),
        governance_anchor_sha256=_digest(governance_anchor),
        policy_contract_sha256=_digest({}),
        enabled_entrypoints=(),
        execution_metadata=metadata,
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )


class _RuntimeGenerations:
    def __init__(self, *snapshots: RuntimeGenerationSnapshot) -> None:
        self._records = {
            (snapshot.automation_id, snapshot.generation): RuntimeGenerationRecord(
                snapshot=snapshot,
                state=RuntimeGenerationState.COMMITTED,
            )
            for snapshot in snapshots
        }

    def get_generation(self, automation_id: str, generation: int):
        return self._records.get((automation_id, generation))


def _registry() -> tuple[ServiceRegistry, Any]:
    registry = ServiceRegistry()
    package_sha = "a" * 64
    provider_id = f"package:{package_sha}"
    registry.register_contract(
        automation_id=provider_id,
        generation=1,
        plugin_id="base",
        plugin_version="1.0.0",
        package_sha256=package_sha,
        manifest_sha256="b" * 64,
        runtime_mode="on_demand",
        provides=(
            {
                "service": "plugin.base.runner@1",
                "operations": [
                    {"name": "get", "effect": "read"},
                    {"name": "run", "effect": "external_write"},
                ],
            },
        ),
        requires=(),
    )
    registry.bind_project_reference(
        provider_automation_id=provider_id,
        automation_id="base-project",
        generation=1,
        package_sha256=package_sha,
        manifest_sha256="b" * 64,
    )
    registry.activate_project_reference(
        automation_id="base-project",
        generation=1,
    )
    return registry, registry.require_operation("plugin.base.runner@1", "run")


def _capability(automation_id: str, generation: int) -> dict[str, Any]:
    return {
        "operation_type": "read",
        "_plugin_runtime": {
            "automation_id": automation_id,
            "generation": generation,
            "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
            "plugin_id": "base",
            "version": "1.0.0",
            "package_sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "service_contracts": {
                "provides": [
                    {
                        "service": "plugin.base.runner@1",
                        "operations": [
                            {"name": "get", "effect": "read"},
                            {"name": "run", "effect": "external_write"},
                        ],
                    }
                ],
                "requires": [],
            },
        },
    }


class _Router:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_service_operation(self, capability, arguments, **values):
        self.calls.append(
            {"capability": capability, "arguments": dict(arguments), **values}
        )
        return {"status": "SUCCESS"}


def test_production_service_executor_routes_only_one_committed_project_reference() -> None:
    registry, provider = _registry()
    # A prepared replacement reference must not compete with the committed
    # exact route during an upgrade.
    registry.bind_project_reference(
        provider_automation_id=provider.provider_registration_id,
        automation_id="base-project",
        generation=2,
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    executor = ProductionServiceV2ProviderExecutor(
        service_registry=registry,
        generation_repository=_RuntimeGenerations(
            _provider_snapshot("base-project", 1),
        ),
    )
    router = _Router()
    executor.bind_router(router)  # type: ignore[arg-type]

    result = asyncio.run(
        executor(
            provider=provider,
            caller_automation_id="consumer-project",
            operation="run",
            arguments={"batch": "safe"},
            call_chain=("plugin.base.runner@1",),
        )
    )

    assert result == {"status": "SUCCESS"}
    assert router.calls[0]["service"] == "plugin.base.runner@1"
    assert router.calls[0]["operation"] == "run"
    assert router.calls[0]["call_chain"] == ("plugin.base.runner@1",)


@pytest.mark.parametrize(
    "activation_phase",
    [RuntimeActivationPhase.PENDING_PROJECTION, RuntimeActivationPhase.BLOCKED],
)
def test_production_service_executor_blocks_unacknowledged_generation(
    activation_phase: RuntimeActivationPhase,
) -> None:
    registry, provider = _registry()
    snapshot = _provider_snapshot("base-project", 1)
    generations = _RuntimeGenerations(snapshot)
    key = (snapshot.automation_id, snapshot.generation)
    generations._records[key] = replace(
        generations._records[key],
        activation_transition_token="00000000-0000-4000-8000-000000000001",
        activation_phase=activation_phase,
    )
    executor = ProductionServiceV2ProviderExecutor(
        service_registry=registry,
        generation_repository=generations,
    )
    router = _Router()
    executor.bind_router(router)  # type: ignore[arg-type]

    with pytest.raises(PluginExecutionError) as blocked:
        asyncio.run(
            executor(
                provider=provider,
                caller_automation_id="consumer-project",
                operation="run",
                arguments={},
                call_chain=("plugin.base.runner@1",),
            )
        )

    assert blocked.value.code == "SERVICE_PROVIDER_BLOCKED"
    assert router.calls == []


def test_service_resolution_rejects_multiple_active_project_routes() -> None:
    registry, provider = _registry()
    for automation_id in ("base-one", "base-two"):
        registry.bind_project_reference(
            provider_automation_id=provider.provider_registration_id,
            automation_id=automation_id,
            generation=1,
            package_sha256="a" * 64,
            manifest_sha256="b" * 64,
        )
        registry.activate_project_reference(
            automation_id=automation_id,
            generation=1,
        )

    with pytest.raises(ServiceProviderAmbiguous):
        registry.require_operation("plugin.base.runner@1", "run")


def test_resolved_service_operation_rejects_directly_forged_provider_or_service_identity() -> None:
    _registry_instance, resolved = _registry()
    assert isinstance(resolved, ResolvedServiceOperation)

    with pytest.raises(ServiceRegistryError, match="registration identity"):
        replace(
            resolved,
            provider_registration_id=f"package:{'c' * 64}",
        )
    with pytest.raises(ServiceRegistryError, match="resolved service is invalid"):
        replace(resolved, service="plugin.other.runner@1")
    with pytest.raises(ServiceRegistryError, match="operation effect"):
        replace(resolved, effect="read")  # type: ignore[arg-type]


def test_resolved_old_route_cannot_obtain_execution_after_new_generation_switch() -> None:
    registry, old_route = _registry()
    new_package_sha = "c" * 64
    new_manifest_sha = "d" * 64
    new_provider_id = f"package:{new_package_sha}"
    registry.register_contract(
        automation_id=new_provider_id,
        generation=1,
        plugin_id="base",
        plugin_version="2.0.0",
        package_sha256=new_package_sha,
        manifest_sha256=new_manifest_sha,
        runtime_mode="on_demand",
        provides=(
            {
                "service": "plugin.base.runner@1",
                "operations": [
                    {"name": "get", "effect": "read"},
                    {"name": "run", "effect": "external_write"},
                ],
            },
        ),
        requires=(),
    )
    registry.bind_project_reference(
        provider_automation_id=new_provider_id,
        automation_id="base-project",
        generation=2,
        package_sha256=new_package_sha,
        manifest_sha256=new_manifest_sha,
    )
    registry.activate_project_reference(automation_id="base-project", generation=2)
    new_route = registry.require_operation("plugin.base.runner@1", "run")
    assert new_route.project_generation == 2
    assert old_route.project_generation == 1

    old_snapshot = _provider_snapshot("base-project", 1)
    new_snapshot = replace(
        _provider_snapshot("base-project", 2),
        plugin_version="2.0.0",
        package_sha256=new_package_sha,
        manifest_sha256=new_manifest_sha,
    )
    executor = ProductionServiceV2ProviderExecutor(
        service_registry=registry,
        generation_repository=_RuntimeGenerations(old_snapshot, new_snapshot),
    )
    router = _Router()
    executor.bind_router(router)  # type: ignore[arg-type]

    with pytest.raises(PluginExecutionError) as stale:
        asyncio.run(
            executor(
                provider=old_route,
                caller_automation_id="consumer-project",
                operation="run",
                arguments={"batch": "stale"},
                call_chain=("plugin.base.runner@1",),
            )
        )
    assert stale.value.code == "SERVICE_PROVIDER_BLOCKED"
    assert router.calls == []

    assert asyncio.run(
        executor(
            provider=new_route,
            caller_automation_id="consumer-project",
            operation="run",
            arguments={"batch": "current"},
            call_chain=("plugin.base.runner@1",),
        )
    ) == {"status": "SUCCESS"}
    assert router.calls[0]["capability"]["_plugin_runtime"]["generation"] == 2


def test_production_service_executor_explicitly_blocks_resident_without_manager() -> None:
    registry = ServiceRegistry()
    package_sha = "d" * 64
    registration = registry.register_contract(
        automation_id=f"package:{package_sha}",
        generation=1,
        plugin_id="resident_base",
        plugin_version="1.0.0",
        package_sha256=package_sha,
        manifest_sha256="e" * 64,
        runtime_mode="resident",
        provides=(
            {
                "service": "plugin.resident_base.runner@1",
                "operations": [{"name": "run", "effect": "read"}],
            },
        ),
        requires=(),
    )
    registry.bind_project_reference(
        provider_automation_id=registration.automation_id,
        automation_id="resident-project",
        generation=1,
        package_sha256=package_sha,
        manifest_sha256="e" * 64,
    )
    registry.activate_project_reference(
        automation_id="resident-project",
        generation=1,
    )
    provider = registry.require_operation(
        "plugin.resident_base.runner@1",
        "run",
    )
    executor = ProductionServiceV2ProviderExecutor(
        service_registry=registry,
        generation_repository=_RuntimeGenerations(),
    )
    executor.bind_router(_Router())  # type: ignore[arg-type]

    with pytest.raises(PluginExecutionError) as blocked:
        asyncio.run(
            executor(
                provider=provider,
                caller_automation_id="consumer-project",
                operation="run",
                arguments={},
                call_chain=("plugin.resident_base.runner@1",),
            )
        )

    assert blocked.value.code == "RESIDENT_RUNTIME_UNAVAILABLE"


class _Issuer:
    broker_endpoint = "unix:///tmp/test.sock"
    broker_socket_path = None


class _GenerationRepository:
    def __init__(self) -> None:
        self.finalized: list[dict[str, Any]] = []

    def finalize_generation_write(self, **values) -> None:
        self.finalized.append(values)


def _provider_result(*, service: str, operation: str) -> dict[str, Any]:
    observed_at = datetime(2026, 8, 30, tzinfo=timezone.utc).isoformat()
    return {
        "status": "SUCCESS",
        "data": {
            "evidence": {
                "service": service,
                "operation": operation,
                "outcome": "WRITE_VERIFIED",
            }
        },
        "meta": {
            "observed_at": observed_at,
            "evidence_refs": ["evidence:provider:write"],
            "write_outcome": "WRITE_VERIFIED",
        },
        "warnings": [],
        "error": None,
    }


def _provider_host_observations(
    result: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    meta = result.get("meta")
    assert isinstance(meta, Mapping)
    refs = meta.get("evidence_refs")
    assert isinstance(refs, list) and len(refs) == 1
    return (
        {
            "request_id": "22222222-2222-4222-8222-222222222222",
            "operation": "browser.session",
            "action": "provider.write",
            "role": "operator",
            "arguments_sha256": "d" * 64,
            "write_started": True,
            "evidence_ref": refs[0],
            "result": {"provider_write": "verified"},
        },
    )


def test_internal_service_write_is_finalized_only_after_provider_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _GenerationRepository()
    router = PluginExecutionRouter(
        core_executor=object(),
        capability_issuer=_Issuer(),  # type: ignore[arg-type]
        generation_leases=repository,  # type: ignore[arg-type]
        release_hold_provider=lambda: False,
    )
    service = "plugin.base.runner@1"
    operation = "run"
    result = _provider_result(service=service, operation=operation)
    verification = GenerationVerificationContext(
        automation_id="base-project",
        generation=3,
        lease_id="11111111-1111-4111-8111-111111111111",
        account_ids=(),
        account_bindings_sha256="c" * 64,
        requires_write_verification=True,
        started_mutating_call_count=1,
        orchestration_run_id="service-run",
        host_call_observations=_provider_host_observations(result),
    )

    async def execute(*_args, **_kwargs):
        return GenerationBoundResult(result, verification=verification)

    monkeypatch.setattr(router, "execute", execute)
    public = asyncio.run(
        router.execute_service_operation(
            _capability("base-project", 3),
            {},
            service=service,
            operation=operation,
            effect=CapabilityEffect.EXTERNAL_WRITE,
            call_chain=(service,),
        )
    )

    assert public == result
    assert repository.finalized[0]["outcome"] is RuntimeLeaseOutcome.WRITE_VERIFIED
    assert repository.finalized[0]["automation_id"] == "base-project"


def test_internal_service_write_with_incomplete_evidence_becomes_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _GenerationRepository()
    router = PluginExecutionRouter(
        core_executor=object(),
        capability_issuer=_Issuer(),  # type: ignore[arg-type]
        generation_leases=repository,  # type: ignore[arg-type]
        release_hold_provider=lambda: False,
    )
    service = "plugin.base.runner@1"
    result = _provider_result(service=service, operation="run")
    result["data"]["evidence"]["outcome"] = "UNCONFIRMED"
    verification = GenerationVerificationContext(
        automation_id="base-project",
        generation=3,
        lease_id="11111111-1111-4111-8111-111111111111",
        account_ids=(),
        account_bindings_sha256="c" * 64,
        requires_write_verification=True,
        started_mutating_call_count=1,
        orchestration_run_id="service-run",
        host_call_observations=_provider_host_observations(result),
    )

    async def execute(*_args, **_kwargs):
        return GenerationBoundResult(result, verification=verification)

    monkeypatch.setattr(router, "execute", execute)
    with pytest.raises(PluginExecutionError) as unknown:
        asyncio.run(
            router.execute_service_operation(
                _capability("base-project", 3),
                {},
                service=service,
                operation="run",
                effect=CapabilityEffect.EXTERNAL_WRITE,
                call_chain=(service,),
            )
        )

    assert unknown.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert repository.finalized[0]["outcome"] is RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
