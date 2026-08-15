"""Production-safe dispatch adapter for the local plugin capability broker.

Only core-owned, explicitly registered high-level operations are callable. The
adapter revalidates the exact account/resource binding for every call and never
returns a credential-bearing object to plugin code.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from agent.automation_plugins.broker import BrokerGrant
from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.account_manager import AutomationAccountManager, get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError


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
    """Resolve only an explicitly bound active account and current session."""

    def __init__(self, manager: AutomationAccountManager | None = None) -> None:
        self._manager = manager

    @property
    def manager(self) -> AutomationAccountManager:
        if self._manager is None:
            self._manager = get_account_manager()
        return self._manager

    def require_authenticated(
        self,
        *,
        account_id: str,
        allowed_systems: Sequence[str],
    ) -> Mapping[str, str]:
        try:
            descriptor = self.manager.require_authenticated_binding(account_id)
        except TMSAuthStateError as exc:
            raise PluginExecutionError(
                "the exact bound account has no valid authenticated session",
                code="BLOCKED_LOGIN",
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
        raw_roles = matches[0].get("roles")
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
    def _normalize_account_binding(binding: object) -> tuple[str, ...]:
        raw_ids = binding if isinstance(binding, (tuple, list)) else (binding,)
        normalized = tuple(str(item or "").strip() for item in raw_ids)
        if not normalized or any(not item for item in normalized):
            raise PluginExecutionError(
                "bound account role is invalid",
                code="BROKER_ROLE_UNBOUND",
            )
        return normalized

    async def invoke(
        self,
        *,
        grant: BrokerGrant,
        operation: str,
        action: str,
        role: str,
        binding: object,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        handler = self._handlers.get((operation, action))
        if handler is None:
            raise PluginExecutionError(
                "core broker action is not registered",
                code="BROKER_ACTION_UNAVAILABLE",
            )
        signed_roles = self._signed_action_roles(
            grant,
            operation=operation,
            action=action,
        )
        if role not in signed_roles:
            raise PluginExecutionError(
                "broker role is not signed for this action",
                code="BROKER_ROLE_DENIED",
            )
        resolved_accounts: dict[str, tuple[str, ...]] = {}
        resolved_resources: dict[str, str] = {}
        for signed_role in signed_roles:
            account_role = self._role_declaration(grant.account_roles, signed_role)
            resource_role = self._role_declaration(grant.resource_roles, signed_role)
            if account_role is not None and resource_role is not None:
                raise PluginExecutionError(
                    "broker role is ambiguous",
                    code="BROKER_CONTRACT_INVALID",
                )
            if account_role is not None:
                if signed_role not in grant.account_bindings:
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
                    self._accounts.require_authenticated(
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
        )
        result = handler(context, dict(arguments))
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise PluginExecutionError("core broker handler returned a non-object result")
        return dict(result)


__all__ = [
    "AccountManagerSessionResolver",
    "CoreBrokerHandler",
    "CoreBrokerInvocationContext",
    "FailClosedResourceBindingResolver",
    "RegisteredCoreAutomationBrokerAdapter",
    "ResourceBindingResolverPort",
]
