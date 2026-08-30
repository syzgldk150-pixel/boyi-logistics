from __future__ import annotations

import pytest

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    HOST_CAPABILITY_API_VERSION,
    HostCapabilityDescriptor,
    HostCapabilityRegistry,
    default_host_capability_registry,
    governance_for_effect,
)
from agent.tool_registry import validate_schema_instance


def _descriptor(
    *,
    action: str = "run",
    effect: CapabilityEffect = CapabilityEffect.READ,
    enabled: bool = True,
) -> HostCapabilityDescriptor:
    return HostCapabilityDescriptor(
        api_version=HOST_CAPABILITY_API_VERSION,
        capability="test.capability",
        action=action,
        governance=governance_for_effect(effect),
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": True},
        handler_key="test.capability:run",
        requires_account_role=False,
        requires_resource_role=False,
        scheduler_allowed=True,
        per_call_limit=1,
        timeout_seconds=1,
        enabled=enabled,
    )


def test_effect_governance_is_unique_complete_and_ordered() -> None:
    expected = {
        CapabilityEffect.READ: ("read", "low", "none", "read", True),
        CapabilityEffect.COMPUTE: ("compute", "low", "none", "read", True),
        CapabilityEffect.INTERNAL_WRITE: (
            "internal_projection_write",
            "medium",
            "project",
            "write",
            True,
        ),
        CapabilityEffect.EXTERNAL_WRITE: (
            "external_write",
            "high",
            "external_target",
            "write",
            False,
        ),
        CapabilityEffect.DESTRUCTIVE: (
            "destructive",
            "extreme",
            "destructive_target",
            "write",
            False,
        ),
    }

    for effect, values in expected.items():
        governance = governance_for_effect(effect).to_mapping()
        assert (
            governance["operation_type"],
            governance["risk_level"],
            governance["lock_class"],
            governance["broker_effect"],
            governance["harness_allowed"],
        ) == values
        assert governance["effect"] == effect.value
        assert set(governance) == {
            "effect",
            "operation_type",
            "risk_level",
            "lock_class",
            "evidence",
            "postconditions",
            "retry",
            "harness_allowed",
            "broker_effect",
            "approval",
            "idempotency",
            "project_full_auto_allowed",
        }


def test_default_registry_resolves_exact_known_host_action() -> None:
    descriptor = default_host_capability_registry().resolve(
        api_version=HOST_CAPABILITY_API_VERSION,
        capability="storage.collection",
        action="upsert",
    )

    assert descriptor.governance.effect is CapabilityEffect.INTERNAL_WRITE
    assert descriptor.to_mapping()["broker_effect"] == "write"


@pytest.mark.parametrize(
    ("capability", "action", "required_fields"),
    [
        ("storage.kv", "get", {"key"}),
        ("storage.kv", "put", {"key", "value", "expected_version"}),
        ("storage.collection", "get", {"collection", "document_key"}),
        (
            "storage.collection",
            "query",
            {"collection", "index_name", "values", "limit"},
        ),
        (
            "storage.collection",
            "put",
            {"collection", "document_key", "document", "expected_version"},
        ),
        (
            "storage.collection",
            "upsert",
            {"collection", "document_key", "document", "expected_version"},
        ),
        ("browser.session", "ronghui.clock.precheck", {"site", "clock_types"}),
        ("browser.session", "ronghui.clock.submit", {"site", "clock_type"}),
        (
            "browser.session",
            "ronghui.clock.verify",
            {"site", "clock_type", "operation_id"},
        ),
    ],
)
def test_default_descriptor_schemas_match_the_real_closed_handler_arguments(
    capability: str,
    action: str,
    required_fields: set[str],
) -> None:
    descriptor = (
        default_host_capability_registry()
        .resolve(
            api_version=HOST_CAPABILITY_API_VERSION,
            capability=capability,
            action=action,
        )
        .to_mapping()
    )
    schema = descriptor["input_schema"]

    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == required_fields
    assert set(schema["properties"]) == required_fields
    assert descriptor["handler_key"]
    assert descriptor["availability"] == "enabled"


def test_managed_kv_schema_accepts_json_values_without_oneof_overlap() -> None:
    descriptor = default_host_capability_registry().resolve(
        api_version=HOST_CAPABILITY_API_VERSION,
        capability="storage.kv",
        action="put",
    )

    for value in (1, 1.5, True, "value", None, ["value"], {"key": "value"}):
        validate_schema_instance(
            "storage.kv.put input",
            {"key": "checkpoint", "value": value, "expected_version": 0},
            descriptor.input_schema,
        )


@pytest.mark.parametrize(
    "registry",
    [
        HostCapabilityRegistry(),
        HostCapabilityRegistry([_descriptor(enabled=False)]),
        HostCapabilityRegistry([_descriptor(), _descriptor()]),
    ],
)
def test_unknown_disabled_and_duplicate_descriptors_are_capability_unavailable(
    registry: HostCapabilityRegistry,
) -> None:
    with pytest.raises(PluginExecutionError) as rejected:
        registry.resolve(
            api_version=HOST_CAPABILITY_API_VERSION,
            capability="test.capability",
            action="run",
        )
    assert rejected.value.code == "CAPABILITY_UNAVAILABLE"


def test_dynamic_descriptor_must_match_its_exact_static_baseline() -> None:
    static = _descriptor()
    drifted = _descriptor(effect=CapabilityEffect.EXTERNAL_WRITE)
    registry = HostCapabilityRegistry(
        [static],
        dynamic_query=lambda *_key: drifted,
    )

    with pytest.raises(PluginExecutionError) as rejected:
        registry.resolve(
            api_version=HOST_CAPABILITY_API_VERSION,
            capability="test.capability",
            action="run",
        )
    assert rejected.value.code == "CAPABILITY_UNAVAILABLE"


def test_dynamic_descriptor_is_rejected_when_it_changes_its_lookup_identity() -> None:
    registry = HostCapabilityRegistry(
        dynamic_query=lambda *_key: _descriptor(action="other"),
    )

    with pytest.raises(PluginExecutionError) as rejected:
        registry.resolve(
            api_version=HOST_CAPABILITY_API_VERSION,
            capability="test.capability",
            action="run",
        )
    assert rejected.value.code == "CAPABILITY_UNAVAILABLE"


def test_dynamic_lookup_failure_is_capability_unavailable() -> None:
    def unavailable(*_key: str) -> HostCapabilityDescriptor | None:
        raise RuntimeError("host registry storage unavailable")

    registry = HostCapabilityRegistry(dynamic_query=unavailable)
    with pytest.raises(PluginExecutionError) as rejected:
        registry.resolve(
            api_version=HOST_CAPABILITY_API_VERSION,
            capability="test.capability",
            action="run",
        )
    assert rejected.value.code == "CAPABILITY_UNAVAILABLE"
