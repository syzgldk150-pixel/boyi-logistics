"""Shared HTTP actions resolve only the exact role selected for this call."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.automation_plugins.broker import BrokerGrant
from agent.automation_plugins.core_adapter import AccountManagerSessionResolver, RegisteredCoreAutomationBrokerAdapter
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from tests.test_account_http import manifest


def grant(action="read_json"):
    raw = manifest()
    raw["account_roles"] = [
        {"role": system + "_quote", "allowed_systems": [system], "required": True}
        for system in ("ronghui", "yunda", "best")
    ]
    raw["capabilities"] = [
        {"name": "http.request", "operations": [action], "account_role": role["role"], "resource_role": None}
        for role in raw["account_roles"]
    ]
    raw["http_requests"] = [
        {"name": role["role"], "account_role": role["role"], "action": action, "method": "POST", "path": "/records"}
        for role in raw["account_roles"]
    ]
    contract = ServiceV2ProjectContract.from_manifest(AutomationPluginManifestV2.from_mapping(raw))
    return BrokerGrant(
        automation_id="instance", plugin_version="1.0.0", tool_name="service.quote",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        runtime_permissions=contract.runtime_permissions, account_roles=tuple(raw["account_roles"]),
        resource_roles=(), account_bindings={role["role"]: "selected-" + role["role"] for role in raw["account_roles"]},
        resource_bindings={},
    )


def adapter(*, expected="yunda_quote", wrong_system=False, action="read_json"):
    resolved, contexts = [], []

    def binding(account_id):
        resolved.append(account_id)
        assert account_id == "selected-" + expected, "unrelated provider must not be resolved"
        return {"account_id": account_id, "system": "best" if wrong_system else expected.removesuffix("_quote")}

    def handler(context, arguments):
        contexts.append(context)
        return {"status_code": 200, "response": "page" if action == "read_text" else {"ok": True}}

    return RegisteredCoreAutomationBrokerAdapter(
        handlers={("http.request", "*"): handler},
        account_resolver=AccountManagerSessionResolver(SimpleNamespace(require_active_binding_descriptor=binding)),
    ), resolved, contexts


def invoke(current, current_grant, *, role="yunda_quote", binding=None, action="read_json"):
    return asyncio.run(current.invoke(
        grant=current_grant, operation="http.request", action=action, role=role,
        binding="selected-" + role if binding is None else binding,
        arguments={"request": role, "body": {}},
    ))


@pytest.mark.parametrize("action", ["read_json", "read_text", "write_json"])
@pytest.mark.parametrize("role", ["ronghui_quote", "yunda_quote", "best_quote"])
def test_shared_http_action_resolves_only_selected_role(action, role):
    current, resolved, contexts = adapter(expected=role, action=action)
    result = invoke(current, grant(action), role=role, action=action)
    assert result["status_code"] == 200
    assert resolved == ["selected-" + role]
    assert contexts[0].account_ids == ("selected-" + role,)
    assert contexts[0].account_bindings == {role: ("selected-" + role,)}


def test_unavailable_unrelated_binding_does_not_block_selected_provider():
    current_grant = grant()
    current_grant = replace(current_grant, account_bindings={"yunda_quote": "selected-yunda_quote"})
    current, resolved, contexts = adapter()
    assert invoke(current, current_grant)["status_code"] == 200
    assert resolved == ["selected-yunda_quote"] and len(contexts) == 1


def test_selected_wrong_system_is_rejected_before_transport():
    current, resolved, contexts = adapter(wrong_system=True)
    with pytest.raises(PluginExecutionError) as error:
        invoke(current, grant())
    assert error.value.code == "BROKER_ACCOUNT_SYSTEM_MISMATCH"
    assert resolved == ["selected-yunda_quote"] and not contexts


def test_selected_account_cannot_be_replaced_by_another_binding():
    current, _, contexts = adapter()
    with pytest.raises(PluginExecutionError) as error:
        invoke(current, grant(), binding="selected-best_quote")
    assert error.value.code == "BROKER_ACCOUNT_MISMATCH" and not contexts


def test_unsigned_role_does_not_resolve_accounts():
    current, resolved, contexts = adapter()
    with pytest.raises(PluginExecutionError) as error:
        invoke(current, grant(), role="unsigned")
    assert error.value.code == "BROKER_ROLE_DENIED" and not resolved and not contexts


def test_mixed_resource_declaration_is_still_rejected():
    current, resolved, contexts = adapter()
    current_grant = replace(grant(), resource_roles=({"role": "best_quote", "required": True},))
    with pytest.raises(PluginExecutionError) as error:
        invoke(current, current_grant)
    assert error.value.code == "BROKER_CONTRACT_INVALID" and not resolved and not contexts
