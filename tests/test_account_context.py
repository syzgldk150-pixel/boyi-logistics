"""Exact account-bound, credential-free context projection and Broker evidence."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import requests

from agent.automation_plugins import account_context
from agent.automation_plugins.account_context import account_context_read
from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer, LocalCoreAutomationBroker
from agent.automation_plugins.capability_proxy_v2 import ServiceV2CapabilityProxy, build_service_v2_capability_handler_map
from agent.automation_plugins.core_adapter import (
    AccountManagerSessionResolver,
    CoreBrokerInvocationContext,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.errors import PluginExecutionError, PluginManifestError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.tms_runtime.errors import TMSAuthStateError
from service_v2_plugins._shared.boyi_plugin_sdk import broker_call
from tests.test_automation_plugin_capability_proxy_v2 import _manifest


PUBLIC = {"loginEmpName": "测试员工", "loginEmpCode": "E001", "loginSiteName": "测试网点", "loginSiteCode": "S001"}


def manifest():
    raw = _manifest()
    raw["capabilities"] = [{
        "name": "account.context", "operations": ["read"], "account_role": "source", "resource_role": None,
    }]
    raw["account_roles"] = [{"role": "source", "required": True, "allowed_systems": ["ronghui"]}]
    return AutomationPluginManifestV2.from_mapping(raw)


def context():
    return CoreBrokerInvocationContext(
        "instance-v2", "1.0.0", "service.sample_plugin", "account.context", "read", "source",
        account_ids=("chosen",),
    )


def add_cookie(session, payload=None, *, domain="tms.ronghuiwl.com", path="/", expires=None):
    value = quote(json.dumps(PUBLIC if payload is None else payload))
    session.cookies.set("userInfo", value, domain=domain, path=path, expires=expires)


def setup_account(monkeypatch, session=None, *, system="ronghui", error=None):
    session = session or requests.Session()
    closed = []
    monkeypatch.setattr(session, "close", lambda: closed.append(True))

    def binding(account_id):
        assert account_id == "chosen"
        return {"account_id": "chosen", "system": system, "session_profile": "exact-profile"}

    manager = SimpleNamespace(require_active_binding_descriptor=binding)

    def saved_session(*, validate):
        assert validate is False
        if error:
            raise error
        return session

    def broker(profile):
        assert profile == "exact-profile"
        return SimpleNamespace(build_requests_session=saved_session)

    monkeypatch.setattr(account_context, "get_account_manager", lambda: manager)
    monkeypatch.setattr(account_context, "get_session_broker", broker)
    return session, closed, manager


def test_projects_only_four_fields_and_reuses_encoded_cookie_parser(monkeypatch):
    session, closed, _ = setup_account(monkeypatch)
    add_cookie(session, {**PUBLIC, "password": "fixture-secret", "token": "fixture-secret", "loginUserAccount": "private"})
    assert account_context_read(context(), {}, manifest()) == PUBLIC
    assert closed == [True]


@pytest.mark.parametrize("case", ["missing", "partial", "blank", "malformed", "foreign", "expired", "narrow_path", "ambiguous"])
def test_missing_or_ambiguous_context_fails_without_guessing(monkeypatch, case):
    session, closed, _ = setup_account(monkeypatch)
    if case == "partial":
        add_cookie(session, {key: value for key, value in PUBLIC.items() if key != "loginEmpCode"})
    elif case == "blank":
        add_cookie(session, {**PUBLIC, "loginEmpCode": " "})
    elif case == "malformed":
        session.cookies.set("userInfo", "not-json", domain="tms.ronghuiwl.com", path="/")
    elif case == "foreign":
        add_cookie(session, domain="other.example")
    elif case == "expired":
        add_cookie(session, expires=int(time.time()) - 30)
    elif case == "narrow_path":
        add_cookie(session, path="/other")
    elif case == "ambiguous":
        add_cookie(session)
        add_cookie(session, {**PUBLIC, "loginEmpCode": "E002"}, domain=".ronghuiwl.com")
    with pytest.raises(PluginExecutionError) as failure:
        account_context_read(context(), {}, manifest())
    assert failure.value.code == ("CONTEXT_DATA_AMBIGUOUS" if case == "ambiguous" else "CONTEXT_DATA_MISSING")
    assert closed == [True]
    assert "E002" not in str(failure.value)


@pytest.mark.parametrize("change", ["role", "accounts", "action", "arguments", "system"])
def test_context_cannot_override_exact_account_binding(monkeypatch, change):
    session, _, _ = setup_account(monkeypatch, system="yunda" if change == "system" else "ronghui")
    add_cookie(session)
    current = context()
    if change == "role":
        current = replace(current, role="other")
    elif change == "accounts":
        current = replace(current, account_ids=("chosen", "another"))
    elif change == "action":
        current = replace(current, action="write")
    with pytest.raises(PluginExecutionError) as failure:
        account_context_read(current, {"fields": ["password"]} if change == "arguments" else {}, manifest())
    assert failure.value.code == "CONTEXT_BINDING_MISMATCH"


def test_missing_session_is_one_provider_login_failure(monkeypatch):
    setup_account(monkeypatch, error=TMSAuthStateError("AUTH_REQUIRED", "fixture login absent"))
    with pytest.raises(PluginExecutionError) as failure:
        account_context_read(context(), {}, manifest())
    assert failure.value.code == "BLOCKED_LOGIN"


def test_context_manifest_requires_account_role_and_compiles_read_only():
    contract = ServiceV2ProjectContract.from_manifest(manifest())
    assert contract.runtime_permissions["network"] is False
    operation = contract.runtime_permissions["broker_operations"][0]
    assert operation["operation"] == "account.context"
    assert operation["effect"] == "read" and operation["roles"] == ["source"]
    raw = manifest().to_mapping()
    raw["capabilities"][0]["account_role"] = None
    with pytest.raises(PluginManifestError):
        ServiceV2ProjectContract.from_manifest(AutomationPluginManifestV2.from_mapping(raw))


def test_real_broker_returns_context_and_out_of_band_host_evidence(monkeypatch, tmp_path):
    session, _, manager = setup_account(monkeypatch)
    add_cookie(session)
    parsed = manifest()
    contract = ServiceV2ProjectContract.from_manifest(parsed)
    monkeypatch.setattr(ServiceV2CapabilityProxy, "_invocation_manifest", lambda *_: parsed)
    handlers = build_service_v2_capability_handler_map(SimpleNamespace(unit_of_work=lambda: None))
    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers=handlers, account_resolver=AccountManagerSessionResolver(manager),
    )

    async def invoke():
        endpoint = tmp_path / "context.sock"
        issuer = LocalBrokerCapabilityIssuer(endpoint)
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=adapter)
        await broker.start()
        try:
            capability = issuer.issue(
                automation_id="instance-v2", plugin_version="1.0.0", tool_name="service.sample_plugin",
                ttl_seconds=60, runtime_permissions=contract.runtime_permissions,
                account_roles=tuple(parsed.to_mapping()["account_roles"]), resource_roles=(),
                account_bindings={"source": "chosen"}, resource_bindings={},
            )
            monkeypatch.setenv("BOYI_PLUGIN_BROKER_ENDPOINT", f"unix://{endpoint}")
            monkeypatch.setenv("BOYI_PLUGIN_EXECUTION_CAPABILITY", capability)
            monkeypatch.setenv("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "30")
            result = await asyncio.to_thread(broker_call, "account.context", action="read", role="source", arguments={})
            return result, issuer.broker_call_observations(capability)
        finally:
            await broker.stop()

    result, observations = asyncio.run(invoke())
    assert result == PUBLIC
    assert result.host_evidence_ref.startswith("host-call:")
    assert observations[0]["evidence_ref"] == result.host_evidence_ref
    assert observations[0]["result"] == PUBLIC
    assert observations[0]["write_started"] is False
