from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from agent.automation_plugins.broker import BrokerGrant
from agent.automation_plugins.capability_proxy_v2 import (
    SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY,
    build_service_v2_capability_handler_map,
)
from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.connector_dependency_projection import (
    project_service_dependencies,
)
from agent.automation_plugins.connector_registry import (
    ConnectorBindingRef,
    ConnectorDescriptor,
    ConnectorOperation,
    ConnectorRegistry,
)
from agent.automation_plugins.core_adapter import (
    CoreBrokerInvocationContext,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.fixture_connectors import (
    FIXTURE_TRACKING_ACCOUNT_ROLE,
    FIXTURE_TRACKING_SERVICE,
    FIXTURE_TRACKING_SYSTEM,
    build_fixture_tracking_registry,
)
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    governance_for_effect,
)
from agent.automation_plugins.models import (
    PluginProjectState,
    PluginRuntimeModel,
    RuntimeReconcileState,
)
from agent.automation_plugins.service_registry import (
    ServiceRegistrationState,
    ServiceRegistry,
)
from agent.automation_plugins.service_v2_contract import SYSTEM_CAPABILITY_ROLE


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "automation_plugins" / "connector_tracking.json"
)


def _manifest(
    *,
    requires: list[dict[str, str]] | None = None,
    account_role: str = FIXTURE_TRACKING_ACCOUNT_ROLE,
    allowed_systems: Sequence[str] = (FIXTURE_TRACKING_SYSTEM,),
) -> dict[str, Any]:
    local_service = "plugin.connector_consumer.runner@1"
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": "connector_consumer",
        "name": "Connector consumer",
        "version": "1.0.0",
        "description": "Offline Connector runtime integration fixture.",
        "host_api": {"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
        "runtime": {
            "kind": "python_subprocess",
            "python": "3.10",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
            "requirements_lock": None,
            "wheelhouse": [],
        },
        "provides": [
            {
                "service": local_service,
                "operations": [{"name": "run", "effect": "read"}],
            }
        ],
        "requires": (
            [
                {
                    "service": FIXTURE_TRACKING_SERVICE,
                    "account_role": account_role,
                }
            ]
            if requires is None
            else requires
        ),
        "capabilities": [
            {
                "name": "service.invoke",
                "operations": ["query"],
                "account_role": None,
                "resource_role": None,
            }
        ],
        "account_roles": [
            {
                "role": account_role,
                "allowed_systems": list(allowed_systems),
                "required": True,
            }
        ],
        "resource_roles": [],
        "contributes": {
            "console": [
                {
                    "id": "run",
                    "title": "Run",
                    "service": local_service,
                    "operation": "run",
                    "default_enabled": True,
                }
            ],
            "scheduler": [],
            "webhook": [],
            "feishu": [],
            "events": [],
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "storage": {"kv": False, "collections": []},
    }


class _Documents:
    def __init__(self, manifest: Mapping[str, Any]) -> None:
        self.manifest = dict(manifest)

    @staticmethod
    def get_project(automation_id: str, *, for_update: bool = False):
        del for_update
        if automation_id != "connector-project":
            return None
        return {"automation_id": automation_id, "plugin_id": "connector_consumer"}

    def get_version(self, plugin_id: str, version: str, *, for_update: bool = False):
        del for_update
        if (plugin_id, version) != ("connector_consumer", "1.0.0"):
            return None
        return {
            "runtime_model": "SERVICE_V2",
            "manifest_json": self.manifest,
        }


class _UnitOfWork:
    def __init__(self, documents: _Documents) -> None:
        self.automation_plugins = documents

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    @staticmethod
    def commit() -> None:
        raise AssertionError("Connector reads must not commit the plugin repository")


class _Orchestration:
    def __init__(self, manifest: Mapping[str, Any]) -> None:
        self.documents = _Documents(manifest)

    def unit_of_work(self):
        return _UnitOfWork(self.documents)


class _AccountResolver:
    def __init__(self, *, system: str = FIXTURE_TRACKING_SYSTEM) -> None:
        self.system = system
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def require_active_binding_descriptor(
        self,
        *,
        account_id: str,
        allowed_systems: Sequence[str],
    ) -> Mapping[str, str]:
        allowed = tuple(str(item) for item in allowed_systems)
        self.calls.append((account_id, allowed))
        if self.system not in allowed:
            raise PluginExecutionError(
                "the exact bound account does not match the signed role",
                code="BROKER_ACCOUNT_SYSTEM_MISMATCH",
            )
        return {
            "account_id": account_id,
            "system": self.system,
            "account_purpose": "test",
        }


def _grant(
    *,
    account_bindings: Mapping[str, object] | None = None,
    allowed_systems: Sequence[str] = (FIXTURE_TRACKING_SYSTEM,),
    account_role: str = FIXTURE_TRACKING_ACCOUNT_ROLE,
    service_call_chain: Sequence[str] = (),
) -> BrokerGrant:
    governance = governance_for_effect(CapabilityEffect.EXTERNAL_WRITE)
    return BrokerGrant(
        automation_id="connector-project",
        plugin_version="1.0.0",
        tool_name="service.connector_consumer",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        runtime_permissions={
            "_service_effect_ceiling": CapabilityEffect.READ.value,
            "_service_call_chain": list(service_call_chain),
            "broker_operations": [
                {
                    "operation": "service.invoke",
                    "action": "query",
                    "roles": [SYSTEM_CAPABILITY_ROLE],
                    "effect": governance.effect.value,
                    "broker_effect": governance.broker_effect,
                    "governance": governance.to_mapping(),
                    "dynamic_effect": True,
                }
            ],
        },
        account_roles=(
            {
                "role": account_role,
                "allowed_systems": list(allowed_systems),
                "required": True,
            },
        ),
        resource_roles=(),
        account_bindings=(
            {account_role: "fixture-account-001"}
            if account_bindings is None
            else dict(account_bindings)
        ),
        resource_bindings={},
    )


def _adapter(
    *,
    manifest: Mapping[str, Any] | None = None,
    connector_registry: ConnectorRegistry | None = None,
    account_resolver: _AccountResolver | None = None,
) -> tuple[RegisteredCoreAutomationBrokerAdapter, _AccountResolver]:
    connectors = (
        connector_registry
        if connector_registry is not None
        else build_fixture_tracking_registry(
            FIXTURE_PATH,
            fixture_root=FIXTURE_PATH.parent,
        )
    )
    accounts = account_resolver or _AccountResolver()
    handlers = build_service_v2_capability_handler_map(
        _Orchestration(manifest or _manifest()),
        connector_registry=connectors,
    )
    return (
        RegisteredCoreAutomationBrokerAdapter(
            handlers=handlers,
            account_resolver=accounts,  # type: ignore[arg-type]
            connector_registry=connectors,
        ),
        accounts,
    )


def _invoke(
    adapter: RegisteredCoreAutomationBrokerAdapter,
    *,
    grant: BrokerGrant | None = None,
    arguments: Mapping[str, Any] | None = None,
    mark_write_started=None,
) -> Mapping[str, Any]:
    return asyncio.run(
        adapter.invoke(
            grant=grant or _grant(),
            operation="service.invoke",
            action="query",
            role=SYSTEM_CAPABILITY_ROLE,
            binding=None,
            arguments={
                "service": FIXTURE_TRACKING_SERVICE,
                "operation": "query",
                "arguments": {"tracking_number": "OFFLINE1001"},
                **dict(arguments or {}),
            },
            mark_write_started=mark_write_started,
        )
    )


def test_connector_service_invoke_resolves_private_account_and_returns_only_closed_data() -> None:
    adapter, accounts = _adapter()
    write_markers: list[str] = []

    result = _invoke(adapter, mark_write_started=lambda: write_markers.append("write"))

    assert result["found"] is True
    assert result["tracking_number"] == "OFFLINE1001"
    assert accounts.calls == [("fixture-account-001", (FIXTURE_TRACKING_SYSTEM,))]
    assert write_markers == []
    public_json = json.dumps(result, sort_keys=True)
    assert "fixture-account-001" not in public_json
    assert "account_id" not in public_json
    assert "endpoint" not in public_json


def test_connector_service_invoke_cannot_bypass_manifest_dependency_or_binding() -> None:
    registered, registered_accounts = _adapter(manifest=_manifest(requires=[]))
    unknown, unknown_accounts = _adapter(
        manifest=_manifest(requires=[]),
        connector_registry=ConnectorRegistry(),
    )
    for adapter in (registered, unknown):
        with pytest.raises(PluginExecutionError) as missing_dependency:
            _invoke(adapter)
        assert missing_dependency.value.code == "SERVICE_DEPENDENCY_UNDECLARED"
    assert registered_accounts.calls == []
    assert unknown_accounts.calls == []

    adapter, _accounts = _adapter()
    with pytest.raises(PluginExecutionError) as missing_binding:
        _invoke(adapter, grant=_grant(account_bindings={}))
    assert missing_binding.value.code == "BROKER_ROLE_UNBOUND"


@pytest.mark.parametrize(
    ("service_call_chain", "code"),
    (
        ((FIXTURE_TRACKING_SERVICE,), "SERVICE_CALL_CYCLE"),
        (
            tuple(f"plugin.depth_{index}.runner@1" for index in range(8)),
            "SERVICE_CALL_DEPTH_EXCEEDED",
        ),
    ),
)
def test_connector_cycle_and_depth_fail_before_account_resolution(
    service_call_chain: tuple[str, ...],
    code: str,
) -> None:
    adapter, accounts = _adapter()

    with pytest.raises(PluginExecutionError) as rejected:
        _invoke(adapter, grant=_grant(service_call_chain=service_call_chain))

    assert rejected.value.code == code
    assert accounts.calls == []


def test_direct_proxy_call_cannot_bypass_host_private_connector_binding() -> None:
    connectors = build_fixture_tracking_registry(
        FIXTURE_PATH,
        fixture_root=FIXTURE_PATH.parent,
    )
    handler = build_service_v2_capability_handler_map(
        _Orchestration(_manifest()),
        connector_registry=connectors,
    )[SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY]
    context = CoreBrokerInvocationContext(
        automation_id="connector-project",
        plugin_version="1.0.0",
        tool_name="service.connector_consumer",
        operation="service.invoke",
        action="query",
        role=SYSTEM_CAPABILITY_ROLE,
        dynamic_effect=True,
        signed_effect=CapabilityEffect.EXTERNAL_WRITE.value,
        signed_broker_effect="write",
        service_effect_ceiling=CapabilityEffect.READ.value,
    )

    with pytest.raises(PluginExecutionError) as rejected:
        asyncio.run(
            handler(
                context,
                {
                    "service": FIXTURE_TRACKING_SERVICE,
                    "operation": "query",
                    "arguments": {"tracking_number": "OFFLINE1001"},
                },
            )
        )

    assert rejected.value.code == "BROKER_ROLE_UNBOUND"


def test_connector_service_invoke_fails_closed_when_host_connector_is_missing() -> None:
    adapter, _accounts = _adapter(connector_registry=ConnectorRegistry())

    with pytest.raises(PluginExecutionError) as missing:
        _invoke(adapter)

    assert missing.value.code == "CONNECTOR_UNAVAILABLE"


def test_connector_service_invoke_rejects_role_and_account_system_drift() -> None:
    adapter, _accounts = _adapter()
    with pytest.raises(PluginExecutionError) as role_drift:
        _invoke(adapter, grant=_grant(allowed_systems=("other",)))
    assert role_drift.value.code == "BROKER_CONTRACT_INVALID"

    system_drift, _accounts = _adapter(
        account_resolver=_AccountResolver(system="other")
    )
    with pytest.raises(PluginExecutionError) as wrong_system:
        _invoke(system_drift)
    assert wrong_system.value.code == "BROKER_ACCOUNT_SYSTEM_MISMATCH"


@pytest.mark.parametrize(
    ("manifest", "grant", "code"),
    (
        (
            _manifest(account_role="alternate_account"),
            _grant(account_role="alternate_account"),
            "CONNECTOR_ACCOUNT_ROLE_MISMATCH",
        ),
        (
            _manifest(allowed_systems=("other",)),
            _grant(allowed_systems=("other",)),
            "CONNECTOR_ALLOWED_SYSTEMS_MISMATCH",
        ),
    ),
)
def test_connector_contract_drift_is_rejected_before_account_resolution(
    manifest: Mapping[str, Any],
    grant: BrokerGrant,
    code: str,
) -> None:
    adapter, accounts = _adapter(manifest=manifest)

    with pytest.raises(PluginExecutionError) as rejected:
        _invoke(adapter, grant=grant)

    assert rejected.value.code == code
    assert accounts.calls == []


def test_connector_system_order_is_not_contract_drift() -> None:
    connectors = _result_registry(
        lambda _binding, _arguments: {"value": "ready"},
        allowed_systems=(FIXTURE_TRACKING_SYSTEM, "other"),
    )
    adapter, accounts = _adapter(
        manifest=_manifest(allowed_systems=("other", FIXTURE_TRACKING_SYSTEM)),
        connector_registry=connectors,
    )

    result = _invoke(
        adapter,
        grant=_grant(allowed_systems=("other", FIXTURE_TRACKING_SYSTEM)),
    )

    assert result == {"value": "ready"}
    assert accounts.calls == [
        ("fixture-account-001", (FIXTURE_TRACKING_SYSTEM, "other"))
    ]


def test_connector_service_invoke_rejects_input_drift_before_fixture_handler() -> None:
    adapter, _accounts = _adapter()

    with pytest.raises(PluginExecutionError) as invalid:
        _invoke(
            adapter,
            arguments={
                "arguments": {
                    "tracking_number": "OFFLINE1001",
                    "unexpected": True,
                }
            },
        )

    assert invalid.value.code == "CONNECTOR_INVOCATION_FAILED"


_VALUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"value": {"type": "string", "minLength": 1, "maxLength": 191}},
    "required": ["value"],
}


def _result_registry(
    handler,
    *,
    output_schema: Mapping[str, object] = _VALUE_SCHEMA,
    allowed_systems: Sequence[str] = (FIXTURE_TRACKING_SYSTEM,),
) -> ConnectorRegistry:
    return ConnectorRegistry(
        (
            ConnectorDescriptor(
                service=FIXTURE_TRACKING_SERVICE,
                title="Runtime result test",
                account_role=FIXTURE_TRACKING_ACCOUNT_ROLE,
                allowed_systems=tuple(allowed_systems),
                operations=(
                    ConnectorOperation(
                        name="query",
                        effect=CapabilityEffect.READ,
                        input_schema={
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "tracking_number": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 64,
                                }
                            },
                            "required": ["tracking_number"],
                        },
                        output_schema=output_schema,
                        handler=handler,
                    ),
                ),
            ),
        )
    )


def test_connector_contract_revision_tracks_schema_but_not_handler() -> None:
    baseline = _result_registry(
        lambda _binding, _arguments: {"value": "baseline"}
    ).contract_sha256(FIXTURE_TRACKING_SERVICE)
    handler_changed = _result_registry(
        lambda _binding, _arguments: {"value": "different-handler"}
    ).contract_sha256(FIXTURE_TRACKING_SERVICE)
    schema_changed = _result_registry(
        lambda _binding, _arguments: {"value": "baseline"},
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "value": {"type": "string", "minLength": 1, "maxLength": 190}
            },
            "required": ["value"],
        },
    ).contract_sha256(FIXTURE_TRACKING_SERVICE)

    assert handler_changed == baseline
    assert schema_changed != baseline


@pytest.mark.parametrize(
    ("handler", "code"),
    (
        (lambda _binding, _arguments: {}, "CONNECTOR_INVOCATION_FAILED"),
        (
            lambda binding, _arguments: {"value": binding.account_id},
            "CONNECTOR_SENSITIVE_DATA_DENIED",
        ),
    ),
)
def test_connector_service_invoke_rejects_output_drift_and_account_leak(handler, code: str) -> None:
    adapter, _accounts = _adapter(connector_registry=_result_registry(handler))

    with pytest.raises(PluginExecutionError) as rejected:
        _invoke(adapter)

    assert rejected.value.code == code


def _connector_requirement(
    *,
    account_role: str = FIXTURE_TRACKING_ACCOUNT_ROLE,
    allowed_systems: Sequence[str] = (FIXTURE_TRACKING_SYSTEM,),
    required: bool = True,
) -> dict[str, object]:
    return {
        "service": FIXTURE_TRACKING_SERVICE,
        "account_role": account_role,
        "allowed_systems": list(allowed_systems),
        "required": required,
    }


def _service_registration(
    registry: ServiceRegistry,
    *,
    connector_requirement: Mapping[str, object] | None = None,
):
    return registry.register_contract(
        automation_id=f"package:{'a' * 64}",
        generation=1,
        plugin_id="connector_consumer",
        plugin_version="1.0.0",
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
        runtime_mode="on_demand",
        provides=(
            {
                "service": "plugin.connector_consumer.runner@1",
                "operations": [{"name": "run", "effect": "read"}],
            },
        ),
        requires=(FIXTURE_TRACKING_SERVICE,),
        connector_requirements=(
            dict(connector_requirement or _connector_requirement()),
        ),
    )


def test_service_registry_treats_connector_as_external_dependency_without_provider_owner() -> None:
    connectors = build_fixture_tracking_registry(
        FIXTURE_PATH,
        fixture_root=FIXTURE_PATH.parent,
    )
    ready_registry = ServiceRegistry(connector_registry=connectors)
    ready = _service_registration(ready_registry)

    assert ready.state is ServiceRegistrationState.ACTIVE
    assert ready.blocking_reasons == ()
    assert ready_registry.claimed_provider_for(FIXTURE_TRACKING_SERVICE) is None

    missing = _service_registration(ServiceRegistry())
    assert missing.state is ServiceRegistrationState.BLOCKED_DEPENDENCY
    assert [reason.code for reason in missing.blocking_reasons] == ["MISSING_CONNECTOR"]


def test_connector_dependency_projection_is_ready_or_explicitly_missing() -> None:
    connectors = build_fixture_tracking_registry(
        FIXTURE_PATH,
        fixture_root=FIXTURE_PATH.parent,
    )
    ready_services = ServiceRegistry(connector_registry=connectors)

    ready = project_service_dependencies(
        (FIXTURE_TRACKING_SERVICE,),
        connector_requirements=(_connector_requirement(),),
        connector_registry=connectors,
        service_registry=ready_services,
    )
    missing = project_service_dependencies(
        (FIXTURE_TRACKING_SERVICE,),
        connector_requirements=(_connector_requirement(),),
        connector_registry=ConnectorRegistry(),
        service_registry=ServiceRegistry(),
    )

    assert ready[0][0:2] == (FIXTURE_TRACKING_SERVICE, True)
    assert ready[0][2]["dependency_status"] == "READY"
    assert ready[0][2]["connector"]["service"] == FIXTURE_TRACKING_SERVICE
    assert len(ready[0][2]["connector_contract_sha256"]) == 64
    assert ready[0][2]["provider"] is None
    assert missing == (
        (
            FIXTURE_TRACKING_SERVICE,
            False,
            {
                "service": FIXTURE_TRACKING_SERVICE,
                "dependency_status": "MISSING_CONNECTOR",
                "dependency_reason": "Host Connector is unavailable",
                "connector": None,
                "connector_contract_sha256": None,
                "provider": None,
            },
        ),
    )


def test_plugin_dependency_projection_preserves_legacy_revision_material() -> None:
    service = "plugin.provider_plugin.runner@1"

    projection = project_service_dependencies(
        (service,),
        connector_registry=ConnectorRegistry(),
        service_registry=ServiceRegistry(),
    )

    assert projection == (
        (
            service,
            False,
            {
                "service": service,
                "dependency_status": "MISSING_PROVIDER",
                "provider": None,
            },
        ),
    )


class _EmptyCatalogRepository:
    @staticmethod
    def list_instance_ids() -> tuple[str, ...]:
        return ()

    @staticmethod
    def list_instances():
        return []

    @staticmethod
    def get_instance(_automation_id: str):
        return None


def _connector_catalog_entry(
    *,
    account_role: str = FIXTURE_TRACKING_ACCOUNT_ROLE,
    allowed_systems: Sequence[str] = (FIXTURE_TRACKING_SYSTEM,),
    required: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        provided_services=("plugin.connector_consumer.runner@1",),
        required_services=(FIXTURE_TRACKING_SERVICE,),
        plugin_id="connector_consumer",
        installed_version="1.0.0",
        manifest_sha256="b" * 64,
        enabled=True,
        configured=True,
        state=PluginProjectState.ENABLED.value,
        committed_snapshot=SimpleNamespace(generation=1, plugin_version="1.0.0"),
        committed_generation=1,
        target_generation=1,
        reconcile_state=RuntimeReconcileState.STABLE,
        account_roles=(
            {
                "role": account_role,
                "allowed_systems": list(allowed_systems),
                "required": required,
            },
        ),
        account_bindings={},
        resource_roles=(),
        resource_bindings={},
        worker_requirement={"required": False},
        device_binding=None,
        automation_id="connector-project",
        service_contracts={
            "requires": [
                {
                    "service": FIXTURE_TRACKING_SERVICE,
                    "account_role": account_role,
                }
            ]
        },
    )


def test_catalog_projects_connectors_separately_without_lifecycle_or_private_binding() -> None:
    catalog = PluginCatalog(
        _EmptyCatalogRepository(),  # type: ignore[arg-type]
        connector_registry=build_fixture_tracking_registry(
            FIXTURE_PATH,
            fixture_root=FIXTURE_PATH.parent,
        ),
    )

    projection = catalog.safe_projection()

    assert projection["plugins"] == []
    assert projection["instances"] == []
    assert projection["connectors"] == [
        {
            "service": FIXTURE_TRACKING_SERVICE,
            "title": "Offline tracking fixture",
            "account_role": FIXTURE_TRACKING_ACCOUNT_ROLE,
            "allowed_systems": [FIXTURE_TRACKING_SYSTEM],
            "operations": [{"name": "query", "effect": "read"}],
        }
    ]
    connector_json = json.dumps(projection["connectors"], sort_keys=True)
    assert "contract_sha256" not in connector_json
    assert "account_id" not in connector_json
    assert "install" not in connector_json
    assert "enable" not in connector_json
    assert "disable" not in connector_json


def test_catalog_dependency_solver_accepts_only_host_registered_connector() -> None:
    entry = _connector_catalog_entry()
    ready_catalog = PluginCatalog(
        _EmptyCatalogRepository(),  # type: ignore[arg-type]
        connector_registry=build_fixture_tracking_registry(
            FIXTURE_PATH,
            fixture_root=FIXTURE_PATH.parent,
        ),
    )
    missing_catalog = PluginCatalog(_EmptyCatalogRepository())  # type: ignore[arg-type]

    assert ready_catalog._v2_dependency_statuses([entry]) == {
        "connector-project": ("READY", [])
    }
    assert missing_catalog._v2_dependency_statuses([entry]) == {
        "connector-project": (
            "BLOCKED_DEPENDENCY",
            [
                {
                    "code": "MISSING_CONNECTOR",
                    "service": FIXTURE_TRACKING_SERVICE,
                    "message": "Host Connector is unavailable",
                }
            ],
        )
    }


@pytest.mark.parametrize(
    ("requirement", "code"),
    (
        (
            _connector_requirement(account_role="alternate_account"),
            "CONNECTOR_ACCOUNT_ROLE_MISMATCH",
        ),
        (
            _connector_requirement(allowed_systems=("other",)),
            "CONNECTOR_ALLOWED_SYSTEMS_MISMATCH",
        ),
    ),
)
def test_connector_contract_drift_blocks_registry_catalog_and_coeffects(
    requirement: Mapping[str, object],
    code: str,
) -> None:
    connectors = build_fixture_tracking_registry(
        FIXTURE_PATH,
        fixture_root=FIXTURE_PATH.parent,
    )
    services = ServiceRegistry(connector_registry=connectors)
    registration = _service_registration(
        services,
        connector_requirement=requirement,
    )
    coeffects = project_service_dependencies(
        (FIXTURE_TRACKING_SERVICE,),
        connector_requirements=(requirement,),
        connector_registry=connectors,
        service_registry=services,
    )
    catalog = PluginCatalog(
        _EmptyCatalogRepository(),  # type: ignore[arg-type]
        connector_registry=connectors,
    )
    entry = _connector_catalog_entry(
        account_role=str(requirement["account_role"]),
        allowed_systems=tuple(requirement["allowed_systems"]),
        required=bool(requirement["required"]),
    )

    assert registration.state is ServiceRegistrationState.BLOCKED_DEPENDENCY
    assert [reason.code for reason in registration.blocking_reasons] == [code]
    assert coeffects[0][0:2] == (FIXTURE_TRACKING_SERVICE, False)
    assert coeffects[0][2]["dependency_status"] == code
    catalog_state, catalog_reasons = catalog._v2_dependency_statuses([entry])[
        "connector-project"
    ]
    assert catalog_state == "BLOCKED_DEPENDENCY"
    assert [reason["code"] for reason in catalog_reasons] == [code]


def test_connector_system_order_is_ready_across_all_dependency_projections() -> None:
    connectors = _result_registry(
        lambda _binding, _arguments: {"value": "ready"},
        allowed_systems=(FIXTURE_TRACKING_SYSTEM, "other"),
    )
    requirement = _connector_requirement(
        allowed_systems=("other", FIXTURE_TRACKING_SYSTEM)
    )
    services = ServiceRegistry(connector_registry=connectors)
    registration = _service_registration(
        services,
        connector_requirement=requirement,
    )
    coeffects = project_service_dependencies(
        (FIXTURE_TRACKING_SERVICE,),
        connector_requirements=(requirement,),
        connector_registry=connectors,
        service_registry=services,
    )
    catalog = PluginCatalog(
        _EmptyCatalogRepository(),  # type: ignore[arg-type]
        connector_registry=connectors,
    )

    assert registration.state is ServiceRegistrationState.ACTIVE
    assert coeffects[0][0:2] == (FIXTURE_TRACKING_SERVICE, True)
    assert catalog._v2_dependency_statuses(
        [
            _connector_catalog_entry(
                allowed_systems=("other", FIXTURE_TRACKING_SYSTEM)
            )
        ]
    ) == {"connector-project": ("READY", [])}
