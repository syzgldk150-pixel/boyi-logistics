"""Host-private execution context for reviewed infrastructure primitives."""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import Mapping

from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import (
    ConnectorBindingInvalid, ConnectorBindingRef, ConnectorHostInternalBindingRef,
    ConnectorInvocationError, ConnectorResourceBindingRef,
)
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.errors import TMSAuthStateError


def reviewed_connector_handler(reviewed, *, tool, operation, action, role,
                               account: ConnectorRequirementContract | None = None,
                               resource: ConnectorRequirementContract | None = None,
                               account_alias: str | None = None,
                               additional_accounts=(),
                               account_collection_role: str | None = None,
                               decode_arguments=None, encode_result=None):
    """Bind a specific primitive; there is no script-name or dynamic dispatch input."""
    primitive = reviewed.get((operation, action))
    if not callable(primitive):
        raise ValueError(f"Connector primitive is unavailable: {operation}/{action}")

    async def invoke(binding, arguments):
        parent = binding.invocation_context
        if not isinstance(parent, CoreBrokerInvocationContext) or parent.connector_binding_resolver is None:
            raise ConnectorBindingInvalid("Connector requires the current Host invocation and binding resolver")
        try:
            resolver = parent.connector_binding_resolver
            accounts = {}
            resources = {}
            account_ids = ()
            resource_id = None
            resolved_account = None
            resolved_resource = None
            if account_collection_role is not None:
                # Only a declared collection in the current Host grant can
                # resolve identities; validate all accounts before platform IO.
                if parent.account_collection_resolver is None:
                    raise ConnectorBindingInvalid("Connector account collection resolver is unavailable")
                ids = await asyncio.to_thread(parent.account_collection_resolver, account_collection_role)
                if (not isinstance(ids, (tuple, list)) or not ids
                        or any(not isinstance(item, str) or not item for item in ids)
                        or len(ids) != len(set(ids))):
                    raise ConnectorBindingInvalid("Connector account collection is unbound or invalid")
                account_ids = tuple(ids)
                accounts[account_collection_role] = account_ids
            if account is not None:
                resolved_account = await asyncio.to_thread(resolver, account)
                if not isinstance(resolved_account, ConnectorBindingRef):
                    raise ConnectorBindingInvalid("Connector requires its exact account role")
                account_ids = (resolved_account.account_id,)
                accounts[account.account_role] = account_ids
                if account_alias:
                    accounts[account_alias] = account_ids
            if resource is not None:
                resolved_resource = await asyncio.to_thread(resolver, resource)
                if not isinstance(resolved_resource, ConnectorResourceBindingRef):
                    raise ConnectorBindingInvalid("Connector requires its exact resource role")
                resource_id = resolved_resource.resource_id
                resources[resource.resource_role] = resource_id
            for requirement in additional_accounts:
                extra = await asyncio.to_thread(resolver, requirement)
                if not isinstance(extra, ConnectorBindingRef):
                    raise ConnectorBindingInvalid("Connector requires all declared account roles")
                accounts[requirement.account_role] = (extra.account_id,)
            if isinstance(binding, ConnectorBindingRef):
                if binding != resolved_account:
                    raise ConnectorBindingInvalid("Connector account binding changed")
            elif isinstance(binding, ConnectorResourceBindingRef):
                if binding != resolved_resource:
                    raise ConnectorBindingInvalid("Connector resource binding changed")
            elif not isinstance(binding, ConnectorHostInternalBindingRef):
                raise ConnectorBindingInvalid("Connector binding is invalid")
            context = replace(parent, tool_name=tool, operation=operation, action=action, role=role,
                              account_ids=account_ids, resource_id=resource_id,
                              account_bindings=accounts, resource_bindings=resources,
                              # Service.invoke recorded this exact operation's
                              # write boundary before entering the Connector.
                              # A primitive must not start the same receipt twice.
                              mark_write_started=None)
            values = decode_arguments(arguments) if decode_arguments else arguments
            result = await asyncio.to_thread(primitive, context, values)
            if inspect.isawaitable(result):
                result = await result
        except PluginExecutionError as exc:
            raise ConnectorInvocationError("Infrastructure operation failed", code=exc.code) from exc
        except TMSAuthStateError as exc:
            raise ConnectorInvocationError("The bound platform account requires login", code="BLOCKED_LOGIN") from exc
        if not isinstance(result, Mapping) or not result.get("evidence_ref"):
            raise ConnectorInvocationError("Primitive returned no verified evidence")
        # The outer broker creates the service receipt. Internal primitive
        # evidence remains host-private rather than impersonating that receipt.
        public = {key: value for key, value in result.items() if key != "evidence_ref"}
        return encode_result(public) if encode_result else public

    return invoke
