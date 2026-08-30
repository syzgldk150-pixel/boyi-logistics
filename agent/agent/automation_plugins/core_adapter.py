"""Production-safe dispatch adapter for the local plugin capability broker.

Only core-owned, explicitly registered high-level operations are callable. The
adapter revalidates the exact account/resource binding for every call and never
returns a credential-bearing object to plugin code.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from agent.automation_plugins.broker import BrokerGrant, _assert_redacted
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.host_capability_registry import (
    HOST_CAPABILITY_API_VERSION,
    HostCapabilityDescriptor,
    default_host_capability_registry,
)
from agent.automation_plugins.service_v2_contract import SYSTEM_CAPABILITY_ROLE
from agent.execution_boundary import execution_capability_scope
from agent.tms_runtime.account_manager import AutomationAccountManager, get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tool_registry import validate_schema_instance


def _binding_account_ids(grant: BrokerGrant) -> frozenset[str]:
    values: set[str] = set()
    for binding in grant.account_bindings.values():
        raw_values = binding if isinstance(binding, (tuple, list)) else (binding,)
        for item in raw_values:
            normalized = str(item or "").strip()
            if normalized:
                values.add(normalized)
    return frozenset(values)


def _assert_no_account_id_values(value: Any, account_ids: frozenset[str]) -> None:
    if isinstance(value, Mapping):
        for nested in value.values():
            _assert_no_account_id_values(nested, account_ids)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_no_account_id_values(nested, account_ids)
    elif isinstance(value, str) and any(account_id in value for account_id in account_ids):
        raise PluginExecutionError("core broker adapter returned sensitive data")


def _assert_public_result_safe(value: Mapping[str, Any], grant: BrokerGrant) -> None:
    _assert_redacted(value)
    _assert_no_account_id_values(value, _binding_account_ids(grant))


@dataclass(frozen=True)
class CoreBrokerInvocationContext:
    automation_id: str
    plugin_version: str
    tool_name: str
    operation: str
    action: str
    role: str
    account_ids: tuple[str, ...] = ()
    resource_id: str | None = None
    account_bindings: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    resource_bindings: Mapping[str, str] = field(default_factory=dict)
    # Host-private ancestry for nested Service v2 calls.  It is carried only
    # inside the opaque Broker grant and is never exposed as a credential or
    # accepted from subprocess arguments.
    service_call_chain: tuple[str, ...] = ()
    signed_effect: str = ""
    signed_broker_effect: str = ""
    dynamic_effect: bool = False
    service_effect_ceiling: str = ""
    # This is supplied only for a signed write call.  The handler must invoke
    # it exactly once, immediately before its first mutating port call.  It is
    # intentionally optional so closed handler unit tests can exercise their
    # validation branches without manufacturing write-attempt state.
    mark_write_started: Callable[[], None] | None = None


CoreBrokerHandler = Callable[
    [CoreBrokerInvocationContext, Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


class ResourceBindingResolverPort(Protocol):
    def require_active(
        self,
        *,
        resource_id: str,
        allowed_kinds: Sequence[str],
    ) -> Mapping[str, str]:
        """Return a safe descriptor or raise; never return resource config."""


class FailClosedResourceBindingResolver:
    def require_active(
        self,
        *,
        resource_id: str,
        allowed_kinds: Sequence[str],
    ) -> Mapping[str, str]:
        del resource_id, allowed_kinds
        raise PluginExecutionError(
            "core resource binding resolver is not configured",
            code="BROKER_RESOURCE_UNAVAILABLE",
        )


class AccountManagerSessionResolver:
    """Resolve an exact active account from the local registry only.

    Generic broker admission must not validate an external session. The target
    handler owns the one capability-specific authentication check, if required.
    """

    def __init__(self, manager: AutomationAccountManager | None = None) -> None:
        self._manager = manager

    @property
    def manager(self) -> AutomationAccountManager:
        if self._manager is None:
            self._manager = get_account_manager()
        return self._manager

    def require_active_binding_descriptor(
        self,
        *,
        account_id: str,
        allowed_systems: Sequence[str],
    ) -> Mapping[str, str]:
        try:
            descriptor = self.manager.require_active_binding_descriptor(account_id)
        except TMSAuthStateError as exc:
            raise PluginExecutionError(
                "the exact bound account is unavailable",
                code="BROKER_ACCOUNT_UNAVAILABLE",
            ) from exc
        system = str(descriptor.get("system") or "")
        if system not in set(allowed_systems):
            raise PluginExecutionError(
                "the exact bound account does not match the signed role",
                code="BROKER_ACCOUNT_SYSTEM_MISMATCH",
            )
        return {
            "account_id": str(descriptor["account_id"]),
            "system": system,
            "account_purpose": str(descriptor.get("account_purpose") or "general"),
        }


class RegisteredCoreAutomationBrokerAdapter:
    """Dispatch signed operation/action pairs to closed core-owned handlers."""

    def __init__(
        self,
        *,
        handlers: Mapping[tuple[str, str], CoreBrokerHandler],
        account_resolver: AccountManagerSessionResolver | None = None,
        resource_resolver: ResourceBindingResolverPort | None = None,
    ) -> None:
        normalized: dict[tuple[str, str], CoreBrokerHandler] = {}
        for raw_key, handler in handlers.items():
            if (
                not isinstance(raw_key, tuple)
                or len(raw_key) != 2
                or not all(isinstance(item, str) and item for item in raw_key)
                or not callable(handler)
            ):
                raise ValueError("core broker handlers require (operation, action) callable entries")
            normalized[(raw_key[0], raw_key[1])] = handler
        self._handlers = normalized
        self._accounts = account_resolver or AccountManagerSessionResolver()
        self._resources = resource_resolver or FailClosedResourceBindingResolver()

    @staticmethod
    def _service_v2_descriptor(
        signed_contract: Mapping[str, object],
        *,
        operation: str,
        action: str,
        handler_key: str,
        arguments: Mapping[str, Any],
        grant: BrokerGrant,
        signed_roles: tuple[str, ...],
    ) -> HostCapabilityDescriptor | None:
        v2_fields = {
            "operation",
            "action",
            "roles",
            "effect",
            "broker_effect",
            "governance",
            "dynamic_effect",
        }
        if set(signed_contract) != v2_fields or signed_contract.get(
            "dynamic_effect"
        ) is True:
            return None
        try:
            descriptor = default_host_capability_registry().resolve(
                api_version=HOST_CAPABILITY_API_VERSION,
                capability=operation,
                action=action,
            )
        except PluginExecutionError as exc:
            raise PluginExecutionError(
                "Host capability is unavailable",
                code="CAPABILITY_UNAVAILABLE",
            ) from exc
        if descriptor.handler_key != handler_key:
            raise PluginExecutionError(
                "Host capability handler drifted from its registry descriptor",
                code="CAPABILITY_UNAVAILABLE",
            )
        account_declarations = tuple(
            role
            for role in signed_roles
            if RegisteredCoreAutomationBrokerAdapter._role_declaration(
                grant.account_roles,
                role,
            )
            is not None
        )
        resource_declarations = tuple(
            role
            for role in signed_roles
            if RegisteredCoreAutomationBrokerAdapter._role_declaration(
                grant.resource_roles,
                role,
            )
            is not None
        )
        if descriptor.requires_account_role:
            roles_valid = (
                len(signed_roles) == 1
                and account_declarations == signed_roles
                and not resource_declarations
            )
        elif descriptor.requires_resource_role:
            roles_valid = (
                len(signed_roles) == 1
                and resource_declarations == signed_roles
                and not account_declarations
            )
        else:
            roles_valid = (
                signed_roles == (SYSTEM_CAPABILITY_ROLE,)
                and not account_declarations
                and not resource_declarations
            )
        if not roles_valid:
            raise PluginExecutionError(
                "Host capability role binding drifted from its registry descriptor",
                code="BROKER_CONTRACT_INVALID",
            )
        try:
            validate_schema_instance(
                f"{operation}.{action} Host input",
                dict(arguments),
                descriptor.input_schema,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PluginExecutionError(
                "Host capability arguments do not match its registry schema",
                code="CAPABILITY_ARGUMENT_INVALID",
            ) from exc
        return descriptor

    @staticmethod
    def _role_declaration(
        declarations: Sequence[Mapping[str, object]],
        role: str,
    ) -> Mapping[str, object] | None:
        matches = [item for item in declarations if item.get("role") == role]
        if len(matches) > 1:
            raise PluginExecutionError("broker role contract is ambiguous", code="BROKER_CONTRACT_INVALID")
        return matches[0] if matches else None

    @staticmethod
    def _signed_action_roles(
        grant: BrokerGrant,
        *,
        operation: str,
        action: str,
    ) -> tuple[str, ...]:
        contract = RegisteredCoreAutomationBrokerAdapter._signed_action_contract(
            grant,
            operation=operation,
            action=action,
        )
        raw_roles = contract.get("roles")
        if not isinstance(raw_roles, list):
            raise PluginExecutionError(
                "signed broker action roles are invalid",
                code="BROKER_CONTRACT_INVALID",
            )
        roles = tuple(str(item or "").strip() for item in raw_roles)
        if not roles or any(not item for item in roles) or len(roles) != len(set(roles)):
            raise PluginExecutionError(
                "signed broker action roles are invalid",
                code="BROKER_CONTRACT_INVALID",
            )
        return roles

    @staticmethod
    def _signed_action_contract(
        grant: BrokerGrant,
        *,
        operation: str,
        action: str,
    ) -> Mapping[str, object]:
        raw_contracts = grant.runtime_permissions.get("broker_operations")
        if not isinstance(raw_contracts, list):
            raise PluginExecutionError(
                "signed broker contract is invalid",
                code="BROKER_CONTRACT_INVALID",
            )
        matches = [
            item
            for item in raw_contracts
            if isinstance(item, Mapping)
            and item.get("operation") == operation
            and item.get("action") == action
        ]
        if len(matches) != 1:
            raise PluginExecutionError(
                "signed broker action contract is ambiguous",
                code="BROKER_CONTRACT_INVALID",
            )
        return matches[0]

    @staticmethod
    def _normalize_account_binding(binding: object) -> tuple[str, ...]:
        raw_ids = binding if isinstance(binding, (tuple, list)) else (binding,)
        normalized = tuple(str(item or "").strip() for item in raw_ids)
        if not normalized or any(not item for item in normalized):
            raise PluginExecutionError(
                "bound account role is invalid",
                code="BROKER_ROLE_UNBOUND",
            )
        return normalized

    def _resolve_and_invoke_sync(
        self,
        *,
        handler: CoreBrokerHandler,
        grant: BrokerGrant,
        operation: str,
        action: str,
        role: str,
        binding: object,
        arguments: Mapping[str, Any],
        signed_roles: tuple[str, ...],
        signed_contract: Mapping[str, object],
        mark_write_started: Callable[[], None] | None,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Resolve blocking bindings and enter a synchronous handler off-loop."""

        resolved_accounts: dict[str, tuple[str, ...]] = {}
        resolved_resources: dict[str, str] = {}
        if SYSTEM_CAPABILITY_ROLE in signed_roles and signed_roles != (
            SYSTEM_CAPABILITY_ROLE,
        ):
            raise PluginExecutionError(
                "the internal broker role must be the only signed role",
                code="BROKER_CONTRACT_INVALID",
            )
        for signed_role in signed_roles:
            account_role = self._role_declaration(grant.account_roles, signed_role)
            resource_role = self._role_declaration(grant.resource_roles, signed_role)
            if signed_role == SYSTEM_CAPABILITY_ROLE:
                if (
                    account_role is not None
                    or resource_role is not None
                    or signed_role in grant.account_bindings
                    or signed_role in grant.resource_bindings
                ):
                    raise PluginExecutionError(
                        "the internal broker role cannot carry a binding",
                        code="BROKER_CONTRACT_INVALID",
                    )
                continue
            if account_role is not None and resource_role is not None:
                raise PluginExecutionError(
                    "broker role is ambiguous",
                    code="BROKER_CONTRACT_INVALID",
                )
            if account_role is not None:
                if signed_role not in grant.account_bindings:
                    if account_role.get("required") is not True:
                        continue
                    raise PluginExecutionError(
                        "account role is unbound",
                        code="BROKER_ROLE_UNBOUND",
                    )
                normalized_ids = self._normalize_account_binding(
                    grant.account_bindings[signed_role]
                )
                allowed_systems = account_role.get("allowed_systems")
                if not isinstance(allowed_systems, list):
                    raise PluginExecutionError(
                        "account role contract is invalid",
                        code="BROKER_CONTRACT_INVALID",
                    )
                descriptors = [
                    self._accounts.require_active_binding_descriptor(
                        account_id=account_id,
                        allowed_systems=[str(item) for item in allowed_systems],
                    )
                    for account_id in normalized_ids
                ]
                resolved_ids = tuple(str(item["account_id"]) for item in descriptors)
                if resolved_ids != normalized_ids:
                    raise PluginExecutionError(
                        "account resolver changed the exact binding",
                        code="BROKER_ACCOUNT_MISMATCH",
                    )
                resolved_accounts[signed_role] = resolved_ids
                continue
            if resource_role is not None:
                resource_id = str(grant.resource_bindings.get(signed_role) or "").strip()
                allowed_kinds = resource_role.get("allowed_kinds")
                if not resource_id and resource_role.get("required") is not True:
                    continue
                if not resource_id or not isinstance(allowed_kinds, list):
                    raise PluginExecutionError(
                        "resource role contract is invalid",
                        code="BROKER_CONTRACT_INVALID",
                    )
                safe_resource = self._resources.require_active(
                    resource_id=resource_id,
                    allowed_kinds=[str(item) for item in allowed_kinds],
                )
                if str(safe_resource.get("resource_id") or "") != resource_id:
                    raise PluginExecutionError(
                        "resource resolver changed the exact binding",
                        code="BROKER_RESOURCE_MISMATCH",
                    )
                resolved_resources[signed_role] = resource_id
                continue
            raise PluginExecutionError(
                "broker role is undeclared",
                code="BROKER_ROLE_UNBOUND",
            )

        account_ids: tuple[str, ...] = ()
        resource_id: str | None = None
        if role in resolved_accounts:
            account_ids = resolved_accounts[role]
            if self._normalize_account_binding(binding) != account_ids:
                raise PluginExecutionError(
                    "selected account binding does not match the signed grant",
                    code="BROKER_ACCOUNT_MISMATCH",
                )
        elif role in resolved_resources:
            resource_id = resolved_resources[role]
            if str(binding or "").strip() != resource_id:
                raise PluginExecutionError(
                    "selected resource binding does not match the signed grant",
                    code="BROKER_RESOURCE_MISMATCH",
                )
        elif role == SYSTEM_CAPABILITY_ROLE:
            if binding is not None:
                raise PluginExecutionError(
                    "the internal broker role cannot carry a binding",
                    code="BROKER_CONTRACT_INVALID",
                )
        context = CoreBrokerInvocationContext(
            automation_id=grant.automation_id,
            plugin_version=grant.plugin_version,
            tool_name=grant.tool_name,
            operation=operation,
            action=action,
            role=role,
            account_ids=account_ids,
            resource_id=resource_id,
            account_bindings=resolved_accounts,
            resource_bindings=resolved_resources,
            service_call_chain=self._service_call_chain(grant),
            signed_effect=str(signed_contract.get("effect") or ""),
            signed_broker_effect=str(
                signed_contract.get("broker_effect") or ""
            ),
            dynamic_effect=signed_contract.get("dynamic_effect") is True,
            service_effect_ceiling=str(
                grant.runtime_permissions.get("_service_effect_ceiling") or ""
            ),
            mark_write_started=mark_write_started,
        )
        try:
            return handler(context, dict(arguments))
        except TMSAuthStateError as exc:
            raise PluginExecutionError(
                "the exact target session requires login",
                code="BLOCKED_LOGIN",
            ) from exc

    @staticmethod
    def _service_call_chain(grant: BrokerGrant) -> tuple[str, ...]:
        raw = grant.runtime_permissions.get("_service_call_chain")
        if raw is None:
            return ()
        if (
            not isinstance(raw, list)
            or len(raw) > 8
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                or len(item) > 191
                for item in raw
            )
            or len(raw) != len(set(raw))
        ):
            raise PluginExecutionError(
                "service invocation ancestry is invalid",
                code="SERVICE_CALL_CHAIN_INVALID",
            )
        return tuple(raw)

    async def invoke(
        self,
        *,
        grant: BrokerGrant,
        operation: str,
        action: str,
        role: str,
        binding: object,
        arguments: Mapping[str, Any],
        mark_write_started: Callable[[], None] | None = None,
    ) -> Mapping[str, Any]:
        handler = self._handlers.get((operation, action))
        handler_key = f"{operation}:{action}"
        if handler is None:
            handler = self._handlers.get((operation, "*"))
            handler_key = f"{operation}:*"
        if handler is None:
            raise PluginExecutionError(
                "core broker action is not registered",
                code="BROKER_ACTION_UNAVAILABLE",
            )
        signed_contract = self._signed_action_contract(
            grant,
            operation=operation,
            action=action,
        )
        signed_roles = self._signed_action_roles(
            grant,
            operation=operation,
            action=action,
        )
        descriptor = self._service_v2_descriptor(
            signed_contract,
            operation=operation,
            action=action,
            handler_key=handler_key,
            arguments=arguments,
            grant=grant,
            signed_roles=signed_roles,
        )
        if role not in signed_roles:
            raise PluginExecutionError(
                "broker role is not signed for this action",
                code="BROKER_ROLE_DENIED",
            )
        capability_ttl = max(
            1.0,
            (grant.expires_at - datetime.now(timezone.utc)).total_seconds(),
        )
        # Broker handlers execute in the broker server task, not in the
        # orchestration task that launched the signed subprocess. Recreate the
        # exact tool-scoped capability here so reviewed core adapters may make
        # their own local TMS calls without granting the plugin process a
        # reusable service credential.
        with execution_capability_scope(
            grant.tool_name,
            ttl_seconds=capability_ttl,
        ):
            # Binding resolution can perform account/session and resource I/O.
            # Keep it in the same worker as the synchronous handler so neither
            # stage can stall the Agent event loop.
            async def invoke_handler() -> Mapping[str, Any]:
                resolved = await asyncio.to_thread(
                    self._resolve_and_invoke_sync,
                    handler=handler,
                    grant=grant,
                    operation=operation,
                    action=action,
                    role=role,
                    binding=binding,
                    arguments=arguments,
                    signed_roles=signed_roles,
                    signed_contract=signed_contract,
                    mark_write_started=mark_write_started,
                )
                if inspect.isawaitable(resolved):
                    try:
                        resolved = await resolved
                    except TMSAuthStateError as exc:
                        raise PluginExecutionError(
                            "the exact target session requires login",
                            code="BLOCKED_LOGIN",
                        ) from exc
                return resolved

            started_at = asyncio.get_running_loop().time()
            result = await invoke_handler()
            if (
                descriptor is not None
                and asyncio.get_running_loop().time() - started_at
                > descriptor.timeout_seconds
            ):
                # Synchronous Host handlers run in a worker thread and cannot
                # be safely pre-empted. Reject an over-time result only after
                # the handler has stopped, so no orphan thread can mutate
                # after the Broker observes/finalizes the lease boundary.
                raise PluginExecutionError(
                    "Host capability timed out",
                    code="CAPABILITY_TIMEOUT",
                )
        if not isinstance(result, Mapping):
            raise PluginExecutionError("core broker handler returned a non-object result")
        public_result = dict(result)
        await asyncio.to_thread(_assert_public_result_safe, public_result, grant)
        if descriptor is not None:
            try:
                validate_schema_instance(
                    f"{operation}.{action} Host output",
                    public_result,
                    descriptor.output_schema,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PluginExecutionError(
                    "Host capability result does not match its registry schema",
                    code="BROKER_SOURCE_INVALID",
                ) from exc
        return public_result


__all__ = [
    "AccountManagerSessionResolver",
    "CoreBrokerHandler",
    "CoreBrokerInvocationContext",
    "FailClosedResourceBindingResolver",
    "RegisteredCoreAutomationBrokerAdapter",
    "ResourceBindingResolverPort",
]
