"""Closed Connector dependency contracts shared by every runtime projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agent.automation_plugins.connector_registry import (
    ConnectorDescriptor,
    ConnectorRegistry,
    ConnectorRegistryError,
    validate_connector_account_role,
    validate_connector_service_name,
    validate_connector_system,
)


@dataclass(frozen=True)
class ConnectorRequirementContract:
    """Credential-free manifest requirement retained in durable runtime state."""

    service: str
    account_role: str
    allowed_systems: tuple[str, ...]
    required: bool

    def __post_init__(self) -> None:
        validate_connector_service_name(self.service)
        validate_connector_account_role(self.account_role)
        if (
            not isinstance(self.allowed_systems, tuple)
            or not self.allowed_systems
            or len(self.allowed_systems) != len(set(self.allowed_systems))
        ):
            raise ValueError("Connector requirement allowed systems are invalid")
        for system in self.allowed_systems:
            validate_connector_system(system)
        if not isinstance(self.required, bool):
            raise ValueError("Connector requirement required flag is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "service": self.service,
            "account_role": self.account_role,
            "allowed_systems": list(self.allowed_systems),
            "required": self.required,
        }


@dataclass(frozen=True)
class ConnectorCompatibility:
    ready: bool
    reason_code: str | None
    reason: str | None
    descriptor: ConnectorDescriptor | None = None


def _requirement_mappings(
    requirements: Iterable[Mapping[str, Any]],
    account_roles: Iterable[Mapping[str, Any]],
) -> tuple[ConnectorRequirementContract, ...]:
    roles: dict[str, Mapping[str, Any]] = {}
    for declaration in account_roles:
        role = declaration.get("role")
        if not isinstance(role, str) or not role or role in roles:
            raise ValueError("Connector account role declaration is invalid")
        roles[role] = declaration

    contracts: list[ConnectorRequirementContract] = []
    seen: set[str] = set()
    for requirement in requirements:
        service = requirement.get("service")
        if not isinstance(service, str):
            raise ValueError("Connector requirement service is invalid")
        if not service.startswith("connector."):
            continue
        if service in seen or set(requirement) != {"service", "account_role"}:
            raise ValueError("Connector requirement declaration is invalid")
        account_role = requirement.get("account_role")
        if not isinstance(account_role, str):
            raise ValueError("Connector requirement account role is invalid")
        declaration = roles.get(account_role)
        if declaration is None:
            raise ValueError("Connector requirement account role is undeclared")
        allowed_systems = declaration.get("allowed_systems")
        if not isinstance(allowed_systems, (list, tuple)):
            raise ValueError("Connector requirement allowed systems are invalid")
        contracts.append(
            ConnectorRequirementContract(
                service=service,
                account_role=account_role,
                allowed_systems=tuple(allowed_systems),
                required=declaration.get("required"),
            )
        )
        seen.add(service)
    return tuple(contracts)


def connector_requirements_from_manifest(
    manifest: Any,
) -> tuple[ConnectorRequirementContract, ...]:
    """Extract exact credential-free Connector requirements from a parsed manifest."""

    return _requirement_mappings(
        manifest.connector_requirements,
        manifest.account_roles,
    )


def connector_requirements_from_contracts(
    *,
    requirements: Iterable[Mapping[str, Any]],
    account_roles: Iterable[Mapping[str, Any]],
) -> tuple[ConnectorRequirementContract, ...]:
    """Extract requirements from immutable catalog/generation contract mappings."""

    return _requirement_mappings(requirements, account_roles)


def connector_requirement_from_mapping(
    raw: Mapping[str, Any],
) -> ConnectorRequirementContract:
    if set(raw) != {"service", "account_role", "allowed_systems", "required"}:
        raise ValueError("Persisted Connector requirement is invalid")
    allowed_systems = raw.get("allowed_systems")
    if not isinstance(allowed_systems, (list, tuple)):
        raise ValueError("Persisted Connector requirement systems are invalid")
    return ConnectorRequirementContract(
        service=raw.get("service"),
        account_role=raw.get("account_role"),
        allowed_systems=tuple(allowed_systems),
        required=raw.get("required"),
    )


def connector_requirement_for_service(
    requirements: Iterable[ConnectorRequirementContract],
    service: str,
) -> ConnectorRequirementContract | None:
    matches = tuple(item for item in requirements if item.service == service)
    return matches[0] if len(matches) == 1 else None


def evaluate_connector_requirement(
    connector_registry: ConnectorRegistry,
    requirement: ConnectorRequirementContract | None,
    *,
    service: str,
) -> ConnectorCompatibility:
    """Evaluate exact role/system compatibility without resolving an account."""

    if requirement is None or requirement.service != service:
        return ConnectorCompatibility(
            ready=False,
            reason_code="CONNECTOR_REQUIREMENT_CONTRACT_MISSING",
            reason="Connector dependency has no exact persisted role contract",
        )
    try:
        descriptor = connector_registry.resolve(service)
    except ConnectorRegistryError:
        return ConnectorCompatibility(
            ready=False,
            reason_code="MISSING_CONNECTOR",
            reason="Host Connector is unavailable",
        )
    if requirement.account_role != descriptor.account_role:
        return ConnectorCompatibility(
            ready=False,
            reason_code="CONNECTOR_ACCOUNT_ROLE_MISMATCH",
            reason="Connector account role does not match the Host contract",
            descriptor=descriptor,
        )
    if requirement.required is not True:
        return ConnectorCompatibility(
            ready=False,
            reason_code="CONNECTOR_ACCOUNT_ROLE_NOT_REQUIRED",
            reason="Connector account role must be required",
            descriptor=descriptor,
        )
    if frozenset(requirement.allowed_systems) != frozenset(
        descriptor.allowed_systems
    ):
        return ConnectorCompatibility(
            ready=False,
            reason_code="CONNECTOR_ALLOWED_SYSTEMS_MISMATCH",
            reason="Connector allowed systems do not match the Host contract",
            descriptor=descriptor,
        )
    return ConnectorCompatibility(
        ready=True,
        reason_code=None,
        reason=None,
        descriptor=descriptor,
    )


__all__ = [
    "ConnectorCompatibility",
    "ConnectorRequirementContract",
    "connector_requirement_for_service",
    "connector_requirement_from_mapping",
    "connector_requirements_from_contracts",
    "connector_requirements_from_manifest",
    "evaluate_connector_requirement",
]
