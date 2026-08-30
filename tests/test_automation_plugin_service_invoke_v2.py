from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.models import (
    GenerationBoundResult,
    GenerationVerificationContext,
    PluginRuntimeModel,
    RuntimeLeaseOutcome,
)
from agent.automation_plugins.production import ProductionServiceV2ProviderExecutor
from agent.automation_plugins.service_registry import ServiceRegistry


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
            {"service": "plugin.base.runner@1", "operations": ["get", "run"]},
        ),
        requires=(),
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
                        "operations": ["get", "run"],
                    }
                ],
                "requires": [],
            },
        },
    }


class _Catalog:
    def __init__(self, capabilities: Mapping[str, Mapping[str, Any]]) -> None:
        self.capabilities = capabilities

    def get_project_capability(self, automation_id: str) -> Mapping[str, Any]:
        capability = self.capabilities.get(automation_id)
        if capability is None:
            raise PluginExecutionError("disabled", code="PLUGIN_DISABLED")
        return capability


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
    registry.bind_project_reference(
        provider_automation_id=provider.automation_id,
        automation_id="base-project",
        generation=1,
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    # A prepared replacement reference must not compete with the committed
    # generation exposed by Catalog during an upgrade.
    registry.bind_project_reference(
        provider_automation_id=provider.automation_id,
        automation_id="base-project",
        generation=2,
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    executor = ProductionServiceV2ProviderExecutor(
        service_registry=registry,
        catalog=_Catalog({"base-project": _capability("base-project", 1)}),
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


def test_production_service_executor_rejects_multiple_active_project_routes() -> None:
    registry, provider = _registry()
    for automation_id in ("base-one", "base-two"):
        registry.bind_project_reference(
            provider_automation_id=provider.automation_id,
            automation_id=automation_id,
            generation=1,
            package_sha256="a" * 64,
            manifest_sha256="b" * 64,
        )
    executor = ProductionServiceV2ProviderExecutor(
        service_registry=registry,
        catalog=_Catalog(
            {
                automation_id: _capability(automation_id, 1)
                for automation_id in ("base-one", "base-two")
            }
        ),
    )
    executor.bind_router(_Router())  # type: ignore[arg-type]

    with pytest.raises(PluginExecutionError) as ambiguous:
        asyncio.run(
            executor(
                provider=provider,
                caller_automation_id="consumer-project",
                operation="run",
                arguments={},
                call_chain=("plugin.base.runner@1",),
            )
        )

    assert ambiguous.value.code == "SERVICE_PROVIDER_AMBIGUOUS"


def test_production_service_executor_explicitly_blocks_resident_without_manager() -> None:
    registry = ServiceRegistry()
    package_sha = "d" * 64
    registry.register_contract(
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
                "operations": ["run"],
            },
        ),
        requires=(),
    )
    provider = registry.require_operation(
        "plugin.resident_base.runner@1",
        "run",
    )
    executor = ProductionServiceV2ProviderExecutor(
        service_registry=registry,
        catalog=_Catalog({}),
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
                call_chain=(service,),
            )
        )

    assert unknown.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert repository.finalized[0]["outcome"] is RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
