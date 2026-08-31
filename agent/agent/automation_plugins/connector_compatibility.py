"""Closed Connector dependency contracts shared by every runtime projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agent.automation_plugins.connector_registry import (
    ConnectorBindingKind,
    ConnectorDescriptor,
    ConnectorRegistry,
    ConnectorRegistryError,
    validate_connector_account_role,
    validate_connector_resource_kind,
    validate_connector_resource_role,
    validate_connector_service_name,
    validate_connector_system,
)


@dataclass(frozen=True)
class ConnectorRequirementContract:
    """Credential-free, exact Connector binding retained in durable state.

    The account form deliberately keeps EXT011's persisted shape. Resource
    and Host-internal forms are explicit so a resource can never be passed off
    as an account role.
    """

    service: str
    account_role: str | None = None
    allowed_systems: tuple[str, ...] = ()
    required: bool = True
    binding_kind: ConnectorBindingKind | str = ConnectorBindingKind.ACCOUNT
    resource_role: str | None = None
    allowed_resource_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_connector_service_name(self.service)
        try:
            kind = ConnectorBindingKind(self.binding_kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("Connector requirement binding kind is invalid") from exc
        if not isinstance(self.required, bool):
            raise ValueError("Connector requirement required flag is invalid")
        if not isinstance(self.allowed_systems, tuple) or not isinstance(
            self.allowed_resource_kinds, tuple
        ):
            raise ValueError("Connector requirement binding contract is invalid")
        systems = tuple(validate_connector_system(item) for item in self.allowed_systems)
        resource_kinds = tuple(
            validate_connector_resource_kind(item)
            for item in self.allowed_resource_kinds
        )
        if len(systems) != len(set(systems)) or len(resource_kinds) != len(
            set(resource_kinds)
        ):
            raise ValueError("Connector requirement binding contract is invalid")
        if kind is ConnectorBindingKind.ACCOUNT:
            if (
                self.account_role is None
                or not systems
                or self.resource_role is not None
                or resource_kinds
            ):
                raise ValueError("Connector account requirement is invalid")
            validate_connector_account_role(self.account_role)
        elif kind is ConnectorBindingKind.RESOURCE:
            if (
                self.account_role is not None
                or systems
                or self.resource_role is None
                or not resource_kinds
            ):
                raise ValueError("Connector resource requirement is invalid")
            validate_connector_resource_role(self.resource_role)
        else:
            if (
                self.account_role is not None
                or systems
                or self.resource_role is not None
                or resource_kinds
            ):
                raise ValueError("Connector host_internal requirement is invalid")
        object.__setattr__(self, "binding_kind", kind)
        object.__setattr__(self, "allowed_systems", systems)
        object.__setattr__(self, "allowed_resource_kinds", resource_kinds)

    def to_mapping(self) -> dict[str, object]:
        if self.binding_kind is ConnectorBindingKind.ACCOUNT:
            return {
                "service": self.service,
                "account_role": self.account_role,
                "allowed_systems": list(self.allowed_systems),
                "required": self.required,
            }
        if self.binding_kind is ConnectorBindingKind.RESOURCE:
            return {
                "service": self.service,
                "binding_kind": self.binding_kind.value,
                "resource_role": self.resource_role,
                "allowed_resource_kinds": list(self.allowed_resource_kinds),
                "required": self.required,
            }
        return {
            "service": self.service,
            "binding_kind": self.binding_kind.value,
            "required": self.required,
        }


@dataclass(frozen=True)
class ConnectorCompatibility:
    ready: bool
    reason_code: str | None
    reason: str | None
    descriptor: ConnectorDescriptor | None = None


def _role_declarations(
    declarations: Iterable[Mapping[str, Any]], *, kind: str
) -> dict[str, Mapping[str, Any]]:
    roles: dict[str, Mapping[str, Any]] = {}
    for declaration in declarations:
        role = declaration.get("role")
        if not isinstance(role, str) or not role or role in roles:
            raise ValueError(f"Connector {kind} role declaration is invalid")
        roles[role] = declaration
    return roles


def _tuple_field(declaration: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = declaration.get(key)
    if not isinstance(value, (list, tuple)):
        raise ValueError("Connector requirement binding contract is invalid")
    return tuple(value)


def _requirement_mappings(
    requirements: Iterable[Mapping[str, Any]],
    account_roles: Iterable[Mapping[str, Any]],
    resource_roles: Iterable[Mapping[str, Any]] = (),
) -> tuple[ConnectorRequirementContract, ...]:
    accounts = _role_declarations(account_roles, kind="account")
    resources = _role_declarations(resource_roles, kind="resource")
    contracts: list[ConnectorRequirementContract] = []
    seen: set[str] = set()
    for requirement in requirements:
        service = requirement.get("service")
        if not isinstance(service, str):
            raise ValueError("Connector requirement service is invalid")
        if not service.startswith("connector."):
            continue
        if service in seen:
            raise ValueError("Connector requirement declaration is invalid")
        fields = set(requirement)
        if fields == {"service", "account_role"} or (
            fields == {"service", "binding_kind", "account_role"}
            and requirement.get("binding_kind") == ConnectorBindingKind.ACCOUNT.value
        ):
            account_role = requirement.get("account_role")
            declaration = accounts.get(account_role) if isinstance(account_role, str) else None
            if declaration is None:
                raise ValueError("Connector requirement account role is undeclared")
            contract = ConnectorRequirementContract(
                service=service,
                account_role=account_role,
                allowed_systems=_tuple_field(declaration, "allowed_systems"),
                required=declaration.get("required"),
            )
        elif fields == {"service", "binding_kind", "resource_role"} and requirement.get(
            "binding_kind"
        ) == ConnectorBindingKind.RESOURCE.value:
            resource_role = requirement.get("resource_role")
            declaration = resources.get(resource_role) if isinstance(resource_role, str) else None
            if declaration is None:
                raise ValueError("Connector requirement resource role is undeclared")
            contract = ConnectorRequirementContract(
                service=service,
                binding_kind=ConnectorBindingKind.RESOURCE,
                resource_role=resource_role,
                allowed_resource_kinds=_tuple_field(declaration, "allowed_kinds"),
                required=declaration.get("required"),
            )
        elif fields == {"service", "binding_kind"} and requirement.get(
            "binding_kind"
        ) == ConnectorBindingKind.HOST_INTERNAL.value:
            contract = ConnectorRequirementContract(
                service=service,
                binding_kind=ConnectorBindingKind.HOST_INTERNAL,
                required=True,
            )
        else:
            raise ValueError("Connector requirement declaration is invalid")
        if (
            contract.binding_kind is not ConnectorBindingKind.RESOURCE
            and contract.required is not True
        ):
            raise ValueError("Connector requirement binding must be required")
        contracts.append(contract)
        seen.add(service)
    return tuple(contracts)


def connector_requirements_from_manifest(
    manifest: Any,
) -> tuple[ConnectorRequirementContract, ...]:
    """Extract exact credential-free Connector requirements from a parsed manifest."""

    return _requirement_mappings(
        manifest.connector_requirements,
        manifest.account_roles,
        manifest.resource_roles,
    )


def connector_requirements_from_contracts(
    *,
    requirements: Iterable[Mapping[str, Any]],
    account_roles: Iterable[Mapping[str, Any]],
    resource_roles: Iterable[Mapping[str, Any]] = (),
) -> tuple[ConnectorRequirementContract, ...]:
    """Extract requirements from immutable catalog/generation contract mappings."""

    return _requirement_mappings(requirements, account_roles, resource_roles)


def connector_requirement_from_mapping(
    raw: Mapping[str, Any],
) -> ConnectorRequirementContract:
    fields = set(raw)
    if fields == {"service", "account_role", "allowed_systems", "required"}:
        allowed_systems = raw.get("allowed_systems")
        if not isinstance(allowed_systems, (list, tuple)):
            raise ValueError("Persisted Connector requirement systems are invalid")
        return ConnectorRequirementContract(
            service=raw.get("service"),
            account_role=raw.get("account_role"),
            allowed_systems=tuple(allowed_systems),
            required=raw.get("required"),
        )
    if fields == {
        "service",
        "binding_kind",
        "resource_role",
        "allowed_resource_kinds",
        "required",
    }:
        kinds = raw.get("allowed_resource_kinds")
        if not isinstance(kinds, (list, tuple)):
            raise ValueError("Persisted Connector requirement resource kinds are invalid")
        return ConnectorRequirementContract(
            service=raw.get("service"),
            binding_kind=raw.get("binding_kind"),
            resource_role=raw.get("resource_role"),
            allowed_resource_kinds=tuple(kinds),
            required=raw.get("required"),
        )
    if fields == {"service", "binding_kind", "required"}:
        return ConnectorRequirementContract(
            service=raw.get("service"),
            binding_kind=raw.get("binding_kind"),
            required=raw.get("required"),
        )
    raise ValueError("Persisted Connector requirement is invalid")


def connector_requirement_for_service(
    requirements: Iterable[ConnectorRequirementContract], service: str
) -> ConnectorRequirementContract | None:
    matches = tuple(item for item in requirements if item.service == service)
    return matches[0] if len(matches) == 1 else None


def evaluate_connector_requirement(
    connector_registry: ConnectorRegistry,
    requirement: ConnectorRequirementContract | None,
    *,
    service: str,
) -> ConnectorCompatibility:
    """Evaluate exact binding compatibility without resolving an identity."""

    if requirement is None or requirement.service != service:
        return ConnectorCompatibility(
            False,
            "CONNECTOR_REQUIREMENT_CONTRACT_MISSING",
            "Connector dependency has no exact persisted binding contract",
        )
    try:
        descriptor = connector_registry.resolve(service)
    except ConnectorRegistryError:
        return ConnectorCompatibility(False, "MISSING_CONNECTOR", "Host Connector is unavailable")
    if requirement.binding_kind is not descriptor.binding_kind:
        return ConnectorCompatibility(False, "CONNECTOR_BINDING_KIND_MISMATCH", "Connector binding kind does not match the Host contract", descriptor)
    if (
        requirement.binding_kind is not ConnectorBindingKind.RESOURCE
        and requirement.required is not True
    ):
        return ConnectorCompatibility(False, "CONNECTOR_BINDING_NOT_REQUIRED", "Connector binding must be required", descriptor)
    if requirement.binding_kind is ConnectorBindingKind.ACCOUNT:
        if requirement.account_role != descriptor.account_role:
            return ConnectorCompatibility(False, "CONNECTOR_ACCOUNT_ROLE_MISMATCH", "Connector account role does not match the Host contract", descriptor)
        if frozenset(requirement.allowed_systems) != frozenset(descriptor.allowed_systems):
            return ConnectorCompatibility(False, "CONNECTOR_ALLOWED_SYSTEMS_MISMATCH", "Connector allowed systems do not match the Host contract", descriptor)
    elif requirement.binding_kind is ConnectorBindingKind.RESOURCE:
        if requirement.resource_role != descriptor.resource_role:
            return ConnectorCompatibility(False, "CONNECTOR_RESOURCE_ROLE_MISMATCH", "Connector resource role does not match the Host contract", descriptor)
        if frozenset(requirement.allowed_resource_kinds) != frozenset(descriptor.allowed_resource_kinds):
            return ConnectorCompatibility(False, "CONNECTOR_ALLOWED_RESOURCE_KINDS_MISMATCH", "Connector allowed resource kinds do not match the Host contract", descriptor)
    return ConnectorCompatibility(True, None, None, descriptor)


__all__ = [
    "ConnectorCompatibility",
    "ConnectorRequirementContract",
    "connector_requirement_for_service",
    "connector_requirement_from_mapping",
    "connector_requirements_from_contracts",
    "connector_requirements_from_manifest",
    "evaluate_connector_requirement",
]
