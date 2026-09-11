"""Observe dependencies for committed plugin execution generations."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence
from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.connector_dependency_projection import project_service_dependencies
from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import PluginRuntimeModel, RuntimeCoeffectKind, RuntimeCoeffectSnapshot, RuntimeGenerationSnapshot
from agent.automation_plugins.service_registry import ServiceRegistry
from agent.automation_plugins.service_v2_projection import _MANAGED_CONTRIBUTION_KINDS, _closed_service_v2_contributions, _contribution_backend, _service_registration_material


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _required_sha(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PluginConflictError(f"persisted {field} is not a SHA-256 digest")
    return text


class ProductionRuntimeCoeffectProvider:
    """Observe structural account, resource and closed-adapter revisions.

    Authenticated sessions are deliberately not generation coeffects.  They
    are transient execution dependencies and the core Broker revalidates the
    exact bound account immediately before every account-backed invocation.
    """

    def __init__(
        self,
        *,
        core_catalog: Any,
        broker_handler_keys: Sequence[tuple[str, str]],
        account_manager: Any,
        binding_resolver: ProductionProjectBindingResolver | None = None,
        service_registry: ServiceRegistry | None = None,
        connector_registry: ConnectorRegistry | None = None,
    ) -> None:
        self._core_catalog = core_catalog
        self._handler_keys = frozenset((str(a), str(b)) for a, b in broker_handler_keys)
        self._account_manager = account_manager
        self._bindings = binding_resolver
        self._connectors = connector_registry or ConnectorRegistry()
        self._services = service_registry or ServiceRegistry(
            connector_registry=self._connectors
        )

    @staticmethod
    def _record(
        kind: RuntimeCoeffectKind,
        key: str,
        value: Any,
        *,
        ready: bool,
        reason_code: str | None = None,
    ) -> RuntimeCoeffectSnapshot:
        return RuntimeCoeffectSnapshot(
            kind=kind,
            key=key,
            revision=_digest(value),
            ready=ready,
            reason_code=reason_code,
        )

    def _service_coeffects(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> tuple[RuntimeCoeffectSnapshot, ...]:
        material = _service_registration_material(snapshot)
        dependencies = project_service_dependencies(
            material["requires"],
            connector_requirements=material.get("connector_requirements", ()),
            connector_registry=self._connectors,
            service_registry=self._services,
        )
        return tuple(
            self._record(
                RuntimeCoeffectKind.SERVICE,
                service,
                revision,
                ready=ready,
                reason_code=None if ready else "BLOCKED_DEPENDENCY",
            )
            for service, ready, revision in dependencies
        )

    def _contribution_coeffects(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> tuple[RuntimeCoeffectSnapshot, ...]:
        """Observe the host backend for every enabled managed contribution."""

        contributions = _closed_service_v2_contributions(snapshot)
        enabled = set(snapshot.enabled_entrypoints)
        project_schedule = snapshot.execution_metadata.get("schedule")
        if not isinstance(project_schedule, Mapping):
            raise PluginConflictError("generation project schedule is invalid")
        results: list[RuntimeCoeffectSnapshot] = []
        for kind in _MANAGED_CONTRIBUTION_KINDS:
            for declaration in contributions[kind]:
                contribution_id = str(declaration.get("id") or "")
                if contribution_id not in enabled:
                    continue
                backend, status, reason_code, reason_detail = _contribution_backend(
                    contribution_kind=kind,
                    declaration=declaration,
                    project_schedule=project_schedule,
                )
                # DISABLED is emitted only for an intentionally closed project
                # schedule.  It is audited but is not a missing host capability.
                ready = status in {"READY", "DISABLED"}
                results.append(
                    self._record(
                        RuntimeCoeffectKind.CORE_ADAPTER,
                        f"contribution:{kind}:{contribution_id}",
                        {
                            "backend": backend,
                            "backend_status": status,
                            "reason_code": reason_code,
                            "reason_detail": reason_detail,
                        },
                        ready=ready,
                        reason_code=None if ready else "CAPABILITY_UNAVAILABLE",
                    )
                )
        return tuple(results)

    def observe(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> Sequence[RuntimeCoeffectSnapshot]:
        metadata = snapshot.execution_metadata
        descriptor = metadata.get("runtime_descriptor")
        if not isinstance(descriptor, Mapping):
            raise PluginConflictError("runtime descriptor is absent from generation")
        permissions = descriptor.get("runtime_permissions")
        account_roles = descriptor.get("account_roles")
        bindings = metadata.get("account_bindings")
        anchor = metadata.get("governance_anchor")
        if (
            not isinstance(permissions, Mapping)
            or not isinstance(account_roles, list)
            or not isinstance(bindings, Mapping)
            or not isinstance(anchor, Mapping)
        ):
            raise PluginConflictError("runtime coeffect material is invalid")
        operations = permissions.get("broker_operations")
        if not isinstance(operations, list):
            raise PluginConflictError("signed broker operation contract is invalid")
        required_pairs: set[tuple[str, str]] = set()
        for operation in operations:
            if not isinstance(operation, Mapping):
                raise PluginConflictError("signed broker operation contract is invalid")
            required_pairs.add((str(operation.get("operation") or ""), str(operation.get("action") or "")))
        is_v2 = snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2
        runtime_contract = descriptor.get("runtime")
        service_runtime_ready = (
            not is_v2 or isinstance(runtime_contract, Mapping) and runtime_contract.get("mode") == "on_demand"
        )
        core_capability = None if is_v2 else self._core_catalog.get_capability(str(anchor.get("name") or ""))
        core_ready = (is_v2 and service_runtime_ready) or (
            not is_v2
            and isinstance(core_capability, Mapping)
            and all(
                key in core_capability and canonical_json_bytes(core_capability[key]) == canonical_json_bytes(value)
                for key, value in anchor.items()
            )
        )
        adapters_ready = (
            all(pair in self._handler_keys or (pair[0], "*") in self._handler_keys for pair in required_pairs)
            if is_v2
            else bool(required_pairs) and required_pairs <= self._handler_keys
        )
        results: list[RuntimeCoeffectSnapshot] = [
            self._record(
                RuntimeCoeffectKind.CORE_ADAPTER,
                "governance-and-broker",
                {
                    "governance_anchor_sha256": snapshot.governance_anchor_sha256,
                    "required_broker_operations": sorted(required_pairs),
                    "registered_broker_operations": sorted(
                        pair
                        for pair in required_pairs
                        if pair in self._handler_keys or (pair[0], "*") in self._handler_keys
                    ),
                },
                ready=core_ready and adapters_ready,
                reason_code=(
                    None
                    if core_ready and adapters_ready
                    else (
                        "CORE_REGISTRY_MISMATCH"
                        if not core_ready and service_runtime_ready
                        else "RESIDENT_RUNTIME_UNAVAILABLE"
                        if not service_runtime_ready
                        else "CORE_ADAPTER_ACTION_UNAVAILABLE"
                    )
                ),
            )
        ]
        if is_v2:
            results.extend(self._service_coeffects(snapshot))
            results.extend(self._contribution_coeffects(snapshot))
        public_accounts = {
            str(item.get("account_id") or ""): item
            for item in self._account_manager.list_accounts(
                include_status=False,
                validate=False,
            )
            if isinstance(item, Mapping) and str(item.get("account_id") or "")
        }
        declared = {str(item.get("role") or ""): item for item in account_roles}
        if "" in declared or set(bindings) != {
            role for role, value in declared.items() if value.get("required") is True
        } | (set(bindings) - {""}):
            # The exact set is validated below; this guard primarily rejects
            # malformed duplicate/empty declarations before any session read.
            if "" in declared or not set(bindings) <= set(declared):
                raise PluginConflictError("generation account role contract is invalid")
        for role_name, declaration in declared.items():
            raw_binding = bindings.get(role_name)
            if raw_binding is None and declaration.get("required") is not True:
                results.append(
                    self._record(
                        RuntimeCoeffectKind.ACCOUNT,
                        role_name,
                        None,
                        ready=True,
                    )
                )
                continue
            values = raw_binding if isinstance(raw_binding, (list, tuple)) else (raw_binding,)
            account_ids = tuple(str(value or "").strip() for value in values)
            allowed_systems = declaration.get("allowed_systems")
            if (
                not account_ids
                or any(not account_id for account_id in account_ids)
                or not isinstance(allowed_systems, list)
            ):
                raise PluginConflictError("generation account binding is invalid")
            descriptors = [public_accounts.get(account_id) for account_id in account_ids]
            accounts_ready = all(
                descriptor is not None
                and descriptor.get("is_active") is True
                and str(descriptor.get("system") or "") in set(allowed_systems)
                for descriptor in descriptors
            )
            account_revision = [
                {
                    "binding_sha256": _digest(account_id),
                    "system": str((descriptor or {}).get("system") or ""),
                    "active": bool((descriptor or {}).get("is_active") is True),
                }
                for account_id, descriptor in zip(account_ids, descriptors, strict=True)
            ]
            results.append(
                self._record(
                    RuntimeCoeffectKind.ACCOUNT,
                    role_name,
                    account_revision,
                    ready=accounts_ready,
                    reason_code=None if accounts_ready else "BLOCKED_CONFIG",
                )
            )
        resource_roles = descriptor.get("resource_roles")
        resource_bindings = metadata.get("resource_bindings")
        if not isinstance(resource_roles, list) or not isinstance(
            resource_bindings,
            Mapping,
        ):
            raise PluginConflictError("generation resource role contract is invalid")
        declared_resource_roles = {
            str(item.get("role") or ""): item for item in resource_roles if isinstance(item, Mapping)
        }
        if (
            len(declared_resource_roles) != len(resource_roles)
            or "" in declared_resource_roles
            or not set(resource_bindings) <= set(declared_resource_roles)
        ):
            raise PluginConflictError("generation resource role contract is invalid")
        for raw_role in resource_roles:
            if not isinstance(raw_role, Mapping) or not str(raw_role.get("role") or ""):
                raise PluginConflictError("generation resource role contract is invalid")
            role_name = str(raw_role["role"])
            resource_id = resource_bindings.get(role_name)
            if resource_id is None:
                if raw_role.get("required") is True:
                    raise PluginConflictError("generation resource binding is missing")
                results.append(
                    self._record(
                        RuntimeCoeffectKind.RESOURCE,
                        role_name,
                        None,
                        ready=True,
                    )
                )
                continue
            descriptor_value: Mapping[str, Any] | None = None
            reason_code: str | None = None
            if self._bindings is None:
                reason_code = "RESOURCE_BINDING_UNAVAILABLE"
            else:
                try:
                    descriptor_value = self._bindings.describe_resource_binding(
                        automation_id=snapshot.automation_id,
                        role=raw_role,
                        resource_id=str(resource_id),
                    )
                except PluginConflictError as exc:
                    reason_code = exc.code
            results.append(
                self._record(
                    RuntimeCoeffectKind.RESOURCE,
                    role_name,
                    descriptor_value or {"binding_sha256": _digest(resource_id)},
                    ready=descriptor_value is not None,
                    reason_code=reason_code,
                )
            )
        if metadata.get("device_binding") is not None:
            results.append(
                self._record(
                    RuntimeCoeffectKind.DEVICE,
                    "named-worker",
                    {"binding": "unresolved"},
                    ready=False,
                    reason_code="WORKER_DEVICE_UNAVAILABLE",
                )
            )
        return tuple(results)

