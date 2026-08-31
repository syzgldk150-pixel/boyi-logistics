from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent.automation_plugins.connector_registry import (
    ConnectorBindingInvalid,
    ConnectorBindingRef,
    ConnectorConflict,
    ConnectorContractInvalid,
    ConnectorDescriptor,
    ConnectorInvocationError,
    ConnectorOperation,
    ConnectorOperationUnavailable,
    ConnectorRegistry,
    ConnectorSensitiveDataDenied,
    ConnectorUnavailable,
)
from agent.automation_plugins.fixture_connectors import (
    FIXTURE_TRACKING_ACCOUNT_ROLE,
    FIXTURE_TRACKING_SERVICE,
    FIXTURE_TRACKING_SYSTEM,
    build_fixture_tracking_connector,
    build_fixture_tracking_registry,
)
from agent.automation_plugins.host_capability_registry import CapabilityEffect


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "automation_plugins" / "connector_tracking.json"
)
_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"value": {"type": "string", "minLength": 1, "maxLength": 64}},
    "required": ["value"],
}
_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"value": {"type": "string", "minLength": 1, "maxLength": 191}},
    "required": ["value"],
}


def _handler(
    _binding: ConnectorBindingRef,
    arguments: Mapping[str, object],
) -> Mapping[str, object]:
    return {"value": arguments["value"]}


def _operation(
    *,
    handler=_handler,
    name: str = "query",
    effect: CapabilityEffect = CapabilityEffect.READ,
    input_schema: Mapping[str, object] = _INPUT_SCHEMA,
    output_schema: Mapping[str, object] = _OUTPUT_SCHEMA,
) -> ConnectorOperation:
    return ConnectorOperation(
        name=name,
        effect=effect,
        input_schema=input_schema,
        output_schema=output_schema,
        handler=handler,
    )


def _descriptor(
    *,
    service: str = "connector.test.sample@1",
    account_role: str = "sample_account",
    allowed_systems: tuple[str, ...] = ("fixture",),
    operations: tuple[ConnectorOperation, ...] | None = None,
    title: str = "Sample connector",
) -> ConnectorDescriptor:
    return ConnectorDescriptor(
        service=service,
        title=title,
        account_role=account_role,
        allowed_systems=allowed_systems,
        operations=operations or (_operation(),),
    )


def _binding(
    *,
    service: str = "connector.test.sample@1",
    account_role: str = "sample_account",
    account_id: str = "fixture-account-001",
    system: str = "fixture",
) -> ConnectorBindingRef:
    return ConnectorBindingRef(
        service=service,
        account_role=account_role,
        account_id=account_id,
        system=system,
    )


def test_registry_is_construct_once_deterministic_and_has_no_lifecycle_methods() -> None:
    registry = ConnectorRegistry(
        (
            _descriptor(service="connector.zz.second@1"),
            _descriptor(service="connector.aa.first@0"),
        )
    )

    assert [item.service for item in registry.snapshot()] == [
        "connector.aa.first@0",
        "connector.zz.second@1",
    ]
    assert not hasattr(registry, "register")
    assert not hasattr(registry, "unregister")
    assert ConnectorRegistry().snapshot() == ()


def test_registry_rejects_duplicate_services_and_operations() -> None:
    descriptor = _descriptor()
    with pytest.raises(ConnectorConflict, match="service is duplicated") as duplicate:
        ConnectorRegistry((descriptor, descriptor))
    assert duplicate.value.code == "CONNECTOR_CONFLICT"

    with pytest.raises(ConnectorConflict, match="operation is duplicated"):
        _descriptor(operations=(_operation(), _operation()))


@pytest.mark.parametrize(
    "service",
    [
        "connector.a.sample@1",
        "connector.test.sample@01",
        "connector.test.sample",
        "plugin.test.sample@1",
        "connector.Test.sample@1",
        "connector.test.sample@-1",
        "connector.test.sample@1 ",
    ],
)
def test_connector_service_names_match_the_manifest_namespace(service: str) -> None:
    with pytest.raises(ConnectorContractInvalid, match="service is invalid"):
        _descriptor(service=service)

    assert _descriptor(service="connector.owner.a-b.c@0").service == "connector.owner.a-b.c@0"


def test_descriptor_is_read_only_and_immutable() -> None:
    with pytest.raises(ConnectorContractInvalid, match="read-only"):
        _operation(effect=CapabilityEffect.COMPUTE)

    operation = _operation()
    with pytest.raises(TypeError):
        operation.input_schema["type"] = "string"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        operation.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "field",
    [
        "account_id",
        "source_account_ids",
        "endpoint",
        "api_endpoint",
        "database",
        "db_connection",
        "file_path",
        "path",
        "password",
        "session_state",
        "access_token",
    ],
)
def test_descriptor_rejects_sensitive_schema_fields(field: str) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {field: {"type": "string"}},
        "required": [field],
    }
    with pytest.raises(ConnectorSensitiveDataDenied, match="sensitive field"):
        _operation(input_schema=schema)


def test_descriptor_rejects_open_nested_schema_and_sensitive_metadata() -> None:
    open_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "result": {
                "type": "object",
                "additionalProperties": True,
                "properties": {},
                "required": [],
            }
        },
        "required": ["result"],
    }
    with pytest.raises(ConnectorContractInvalid, match="additionalProperties"):
        _operation(output_schema=open_schema)

    sensitive_metadata = {
        "type": "object",
        "description": "Authorization: Bearer sample-secret-value-1234567890",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }
    with pytest.raises(ConnectorSensitiveDataDenied, match="metadata"):
        _operation(input_schema=sensitive_metadata)

    with pytest.raises(ConnectorSensitiveDataDenied, match="title"):
        _descriptor(title="See https://internal.invalid")


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "file:///tmp/fixture.json",
        "ssh://host.invalid",
        "mysql://db.invalid/schema",
        "postgresql://db.invalid/schema",
        "urn:fixture:tracking",
        "/etc/connector.json",
        "prefix,/var/tmp/result.json",
        "see[/etc/passwd",
        r"C:\connector\fixture.json",
        r"\\server\share\fixture.json",
        "127.0.0.1:8080/api",
        "[::1]:8080/api",
    ],
)
def test_title_and_schema_metadata_share_strict_target_detection(unsafe_text: str) -> None:
    with pytest.raises(ConnectorSensitiveDataDenied):
        _descriptor(title=unsafe_text)
    schema = {
        "type": "object",
        "description": unsafe_text,
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }
    with pytest.raises(ConnectorSensitiveDataDenied):
        _operation(input_schema=schema)


@pytest.mark.parametrize("enum", [[{"value": "safe"}], {"safe", "other"}])
def test_descriptor_rejects_all_enum_forms(enum) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
        "enum": enum,
    }
    with pytest.raises(ConnectorContractInvalid, match="unsupported fields"):
        _operation(input_schema=schema)


@pytest.mark.parametrize(
    "property_schema",
    [
        {"type": "string", "minLength": -1},
        {"type": "string", "minLength": 3, "maxLength": 2},
        {"type": "array", "items": {"type": "string"}, "uniqueItems": "yes"},
        {"type": "number", "minimum": float("nan")},
    ],
)
def test_descriptor_rejects_invalid_schema_bounds(property_schema) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"value": property_schema},
        "required": ["value"],
    }
    with pytest.raises(ConnectorContractInvalid):
        _operation(input_schema=schema)


def test_resolution_and_safe_projection_expose_no_handler_schema_or_account_id() -> None:
    registry = ConnectorRegistry((_descriptor(),))
    descriptor = registry.resolve("connector.test.sample@1")
    resolved = registry.require_operation("connector.test.sample@1", "query")
    projection = registry.safe_projection()

    assert descriptor.account_role == "sample_account"
    assert resolved.effect is CapabilityEffect.READ
    assert projection == (
        {
            "service": "connector.test.sample@1",
            "title": "Sample connector",
            "account_role": "sample_account",
            "allowed_systems": ["fixture"],
            "operations": [{"name": "query", "effect": "read"}],
        },
    )
    assert "handler" not in json.dumps(projection)
    assert "schema" not in json.dumps(projection)
    assert "account_id" not in json.dumps(projection)
    assert "endpoint" not in json.dumps(projection)
    assert "contract_sha256" not in json.dumps(projection)


def test_contract_digest_tracks_schema_but_not_handler_implementation() -> None:
    first = ConnectorRegistry((_descriptor(),))
    other_handler = ConnectorRegistry(
        (_descriptor(operations=(_operation(handler=lambda _binding, args: dict(args)),)),)
    )
    changed_output = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"value": {"type": "string", "minLength": 1, "maxLength": 190}},
        "required": ["value"],
    }
    changed_schema = ConnectorRegistry(
        (_descriptor(operations=(_operation(output_schema=changed_output),)),)
    )

    first_digest = first.contract_sha256("connector.test.sample@1")
    assert other_handler.contract_sha256("connector.test.sample@1") == first_digest
    assert changed_schema.contract_sha256("connector.test.sample@1") != first_digest
    assert len(first_digest) == 64


def test_missing_service_and_operation_fail_closed() -> None:
    registry = ConnectorRegistry((_descriptor(),))
    with pytest.raises(ConnectorUnavailable) as missing_service:
        registry.resolve("connector.test.missing@1")
    assert missing_service.value.code == "CONNECTOR_UNAVAILABLE"
    with pytest.raises(ConnectorOperationUnavailable) as missing_operation:
        registry.require_operation("connector.test.sample@1", "missing")
    assert missing_operation.value.code == "CONNECTOR_OPERATION_UNDECLARED"


def test_invoke_validates_binding_input_and_output() -> None:
    registry = ConnectorRegistry((_descriptor(),))
    resolved = registry.require_operation("connector.test.sample@1", "query")

    assert asyncio.run(
        registry.invoke(resolved=resolved, binding=_binding(), arguments={"value": "ok"})
    ) == {"value": "ok"}
    with pytest.raises(ConnectorInvocationError, match="closed schema"):
        asyncio.run(
            registry.invoke(
                resolved=resolved,
                binding=_binding(),
                arguments={"value": "ok", "extra": "denied"},
            )
        )
    for binding in (
        _binding(service="connector.test.other@1"),
        _binding(account_role="other_account"),
        _binding(system="other"),
    ):
        with pytest.raises(ConnectorBindingInvalid, match="does not match"):
            asyncio.run(
                registry.invoke(resolved=resolved, binding=binding, arguments={"value": "ok"})
            )


def test_invoke_rechecks_current_contract_and_does_not_accept_forged_resolution() -> None:
    registry = ConnectorRegistry((_descriptor(),))
    resolved = registry.require_operation("connector.test.sample@1", "query")
    forged = type(resolved)(
        service=resolved.service,
        title=resolved.title,
        account_role=resolved.account_role,
        allowed_systems=resolved.allowed_systems,
        operation=resolved.operation,
        effect=resolved.effect,
        input_schema=resolved.input_schema,
        output_schema=resolved.output_schema,
        handler=lambda _binding, _arguments: {"value": "forged"},
    )
    with pytest.raises(ConnectorOperationUnavailable, match="drifted"):
        asyncio.run(
            registry.invoke(resolved=forged, binding=_binding(), arguments={"value": "ok"})
        )


@pytest.mark.parametrize(
    "handler",
    [
        lambda _binding, _arguments: {"value": "fixture-account-001"},
        lambda _binding, _arguments: {"value": "https://internal.invalid/query"},
        lambda _binding, _arguments: {"value": "file:///tmp/result.json"},
        lambda _binding, _arguments: {"value": "ssh://host.invalid"},
        lambda _binding, _arguments: {"value": "mysql://db.invalid/schema"},
        lambda _binding, _arguments: {"value": "postgres://db.invalid/schema"},
        lambda _binding, _arguments: {"value": "/var/tmp/result.json"},
        lambda _binding, _arguments: {"value": "prefix,/var/tmp/result.json"},
        lambda _binding, _arguments: {"value": "see[/etc/passwd"},
        lambda _binding, _arguments: {"value": r"C:\result\data.json"},
        lambda _binding, _arguments: {"value": r"\\server\share\data.json"},
        lambda _binding, _arguments: {"value": "127.0.0.1:8080/api"},
        lambda _binding, _arguments: {"value": "[::1]:8080/api"},
        lambda _binding, _arguments: {"account_id": "hidden"},
    ],
)
def test_invoke_rejects_account_ids_endpoints_and_sensitive_result_fields(handler) -> None:
    registry = ConnectorRegistry((_descriptor(operations=(_operation(handler=handler),)),))
    resolved = registry.require_operation("connector.test.sample@1", "query")
    with pytest.raises(ConnectorSensitiveDataDenied):
        asyncio.run(
            registry.invoke(resolved=resolved, binding=_binding(), arguments={"value": "ok"})
        )


@pytest.mark.parametrize(
    ("account_id", "leaked_value"),
    [("123", 123), ("123", 123.0), ("123.0", 123), ("-123", -123.0)],
)
def test_invoke_rejects_numeric_forms_of_a_numeric_account_id(
    account_id: str,
    leaked_value,
) -> None:
    registry = ConnectorRegistry(
        (
            _descriptor(
                operations=(
                    _operation(
                        handler=lambda _binding, _arguments: {"value": leaked_value}
                    ),
                )
            ),
        )
    )
    resolved = registry.require_operation("connector.test.sample@1", "query")
    with pytest.raises(ConnectorSensitiveDataDenied):
        asyncio.run(
            registry.invoke(
                resolved=resolved,
                binding=_binding(account_id=account_id),
                arguments={"value": "ok"},
            )
        )


def test_string_account_id_matching_uses_identifier_boundaries() -> None:
    registry = ConnectorRegistry(
        (
            _descriptor(
                operations=(
                    _operation(
                        handler=lambda _binding, _arguments: {"value": "OFFLINE1001"}
                    ),
                )
            ),
        )
    )
    resolved = registry.require_operation("connector.test.sample@1", "query")
    assert asyncio.run(
        registry.invoke(
            resolved=resolved,
            binding=_binding(account_id="1"),
            arguments={"value": "ok"},
        )
    ) == {"value": "OFFLINE1001"}

    boundary_registry = ConnectorRegistry(
        (
            _descriptor(
                operations=(
                    _operation(
                        handler=lambda _binding, _arguments: {
                            "value": "account=fixture-account-001"
                        }
                    ),
                )
            ),
        )
    )
    with pytest.raises(ConnectorSensitiveDataDenied):
        asyncio.run(
            boundary_registry.invoke(
                resolved=boundary_registry.require_operation(
                    "connector.test.sample@1",
                    "query",
                ),
                binding=_binding(),
                arguments={"value": "ok"},
            )
        )


@pytest.mark.parametrize("public_value", ["read/write", "read / write"])
def test_public_text_allows_non_absolute_business_slashes(public_value: str) -> None:
    registry = ConnectorRegistry(
        (
            _descriptor(
                operations=(
                    _operation(
                        handler=lambda _binding, _arguments: {"value": public_value}
                    ),
                )
            ),
        )
    )
    resolved = registry.require_operation("connector.test.sample@1", "query")
    assert asyncio.run(
        registry.invoke(
            resolved=resolved,
            binding=_binding(),
            arguments={"value": "ok"},
        )
    ) == {"value": public_value}


def test_invoke_wraps_handler_failure_without_exposing_details() -> None:
    def failing(_binding, _arguments):
        raise RuntimeError("Authorization: Bearer must-not-escape")

    registry = ConnectorRegistry((_descriptor(operations=(_operation(handler=failing),)),))
    resolved = registry.require_operation("connector.test.sample@1", "query")
    with pytest.raises(ConnectorInvocationError, match="handler failed") as error:
        asyncio.run(
            registry.invoke(resolved=resolved, binding=_binding(), arguments={"value": "ok"})
        )
    assert "must-not-escape" not in str(error.value)


def test_fixture_connector_returns_closed_found_and_not_found_results() -> None:
    registry = build_fixture_tracking_registry(
        FIXTURE_PATH,
        fixture_root=FIXTURE_PATH.parent,
    )
    resolved = registry.require_operation(FIXTURE_TRACKING_SERVICE, "query")
    binding = ConnectorBindingRef(
        service=FIXTURE_TRACKING_SERVICE,
        account_role=FIXTURE_TRACKING_ACCOUNT_ROLE,
        account_id="offline-fixture-account",
        system=FIXTURE_TRACKING_SYSTEM,
    )

    found = asyncio.run(
        registry.invoke(
            resolved=resolved,
            binding=binding,
            arguments={"tracking_number": "OFFLINE1001"},
        )
    )
    missing = asyncio.run(
        registry.invoke(
            resolved=resolved,
            binding=binding,
            arguments={"tracking_number": "OFFLINE9999"},
        )
    )

    assert found == {
        "found": True,
        "tracking_number": "OFFLINE1001",
        "status": "IN_TRANSIT",
        "observed_at": "2026-08-31T03:15:00Z",
        "events": [
            {
                "occurred_at": "2026-08-31T01:20:00Z",
                "status": "ACCEPTED",
                "description": "Offline fixture accepted the parcel",
            },
            {
                "occurred_at": "2026-08-31T03:15:00Z",
                "status": "IN_TRANSIT",
                "description": "Offline fixture recorded a transit event",
            },
        ],
    }
    assert missing == {
        "found": False,
        "tracking_number": "OFFLINE9999",
        "status": "NOT_FOUND",
        "observed_at": "2026-08-31T03:15:00Z",
        "events": [],
    }


def test_fixture_requires_explicit_safe_regular_json_and_rejects_duplicate_identity(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConnectorContractInvalid, match="unavailable"):
        build_fixture_tracking_connector(
            tmp_path / "missing.json",
            fixture_root=tmp_path,
        )

    sensitive_name = tmp_path / "credentials.json"
    sensitive_name.write_text("{}", encoding="utf-8")
    with pytest.raises(ConnectorContractInvalid, match="not trusted"):
        build_fixture_tracking_connector(sensitive_name, fixture_root=tmp_path)

    duplicate = tmp_path / "tracking.json"
    record = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["records"][0]
    duplicate.write_text(
        json.dumps(
            {
                "observed_at": "2026-08-31T03:15:00Z",
                "records": [record, record],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConnectorContractInvalid, match="duplicated"):
        build_fixture_tracking_connector(duplicate, fixture_root=tmp_path)


def test_fixture_rejects_malformed_unqueried_record_at_construction(tmp_path: Path) -> None:
    source = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    source["records"].append(
        {
            "tracking_number": "UNQUERIED1002",
            "status": 7,
            "observed_at": "2026-08-31T03:15:00Z",
            "events": [],
        }
    )
    malformed = tmp_path / "malformed-tracking.json"
    malformed.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ConnectorContractInvalid, match="closed output contract"):
        build_fixture_tracking_connector(malformed, fixture_root=tmp_path)


def test_fixture_requires_explicit_trusted_root_and_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="fixture_root"):
        build_fixture_tracking_connector(FIXTURE_PATH)  # type: ignore[call-arg]

    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConnectorContractInvalid, match="outside"):
        build_fixture_tracking_connector(outside, fixture_root=trusted)


@pytest.mark.parametrize("sensitive_component", ["credentials", ".ssh", ".aws", ".gnupg", ".kube"])
def test_fixture_rejects_sensitive_path_components(
    tmp_path: Path,
    sensitive_component: str,
) -> None:
    trusted = tmp_path / "trusted"
    sensitive = trusted / sensitive_component
    sensitive.mkdir(parents=True)
    sensitive_fixture = sensitive / "tracking.json"
    sensitive_fixture.write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConnectorContractInvalid, match="not trusted"):
        build_fixture_tracking_connector(sensitive_fixture, fixture_root=trusted)


def test_fixture_rejects_symlinked_path_components(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    actual = trusted / "actual"
    actual.mkdir(parents=True)
    fixture = actual / "tracking.json"
    fixture.write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    linked = trusted / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ConnectorContractInvalid, match="symlink"):
        build_fixture_tracking_connector(linked / "tracking.json", fixture_root=trusted)


def test_fixture_enforces_pre_read_and_stream_read_size_limit(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ConnectorContractInvalid, match="read limit"):
        build_fixture_tracking_connector(oversized, fixture_root=tmp_path)


def test_binding_ref_is_frozen_and_validated() -> None:
    binding = _binding()
    with pytest.raises(FrozenInstanceError):
        binding.account_id = "changed"  # type: ignore[misc]
    with pytest.raises(ConnectorBindingInvalid, match="account_id"):
        _binding(account_id=" spaced ")


def test_role_and_system_identifiers_match_manifest_boundaries() -> None:
    role_64 = "a" + ("b" * 63)
    system_128 = "a" + ("b" * 127)
    descriptor = _descriptor(
        account_role=role_64,
        allowed_systems=(system_128, "fixture.test-system"),
    )
    assert descriptor.account_role == role_64
    assert descriptor.allowed_systems == (system_128, "fixture.test-system")
    assert _binding(account_role=role_64, system=system_128).system == system_128

    with pytest.raises(ConnectorContractInvalid, match="account_role"):
        _descriptor(account_role="a" + ("b" * 64))
    with pytest.raises(ConnectorContractInvalid, match="allowed system"):
        _descriptor(allowed_systems=("a" + ("b" * 128),))
