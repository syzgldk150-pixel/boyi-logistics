"""Permissions are checked at query execution and signed browser boundaries."""
import asyncio
import io
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.direct_readers import invoke_registered_reader
from agent.harness.errors import HarnessError
from agent.harness_application import TrustedHarnessInvocationAdapter, build_fixed_harness_tools, FIXED_HARNESS_TOOL_IDS
from agent.orchestration.models import Actor, ActorType
from agent.tool_registry import ToolRegistry
from console.app import LocalDocFlowApp
from shared.identity_permissions import (
    IdentityAccess,
    validate_agent_permissions,
    validate_console_permissions,
)
from shared.identity_routes import console_permission, agent_permission
from tests.identity_support import granted


class MutableAuthority:
    """Same channel rule as IdentityRepository.for_actor."""

    def __init__(self, *permissions):
        self.state = granted(*permissions)

    def for_actor(self, actor, **_kwargs):
        channel = getattr(actor, "channel", "") or ("console" if actor.actor_type is ActorType.CONSOLE_ADMIN else "agent")
        return self.state.for_channel(channel)

    def allows(self, actor, permission, *, read=False, **_kwargs):
        return self.for_actor(actor).allows(permission, read=read)


def test_harness_permissions_change_between_catalog_and_execution():
    access = MutableAuthority("business.query", "ai.chat")
    calls = []
    adapter = TrustedHarnessInvocationAdapter(
        policy_service=SimpleNamespace(identity_access=access),
        actor=Actor(ActorType.CONSOLE_ADMIN, "123", roles=("admin",), authenticated_by="mysql_admin_session"),
        base_request_id=str(uuid4()), fixed_handlers={name: lambda args: calls.append(args) or {"ok": True} for name in FIXED_HARNESS_TOOL_IDS},
    )
    tools = {tool.descriptor.tool_id: tool for tool in build_fixed_harness_tools()}
    visible = adapter.visible_tool_filter()
    assert visible(tools["tracking.lookup"].opaque_handle)
    assert not visible(tools["finance.summary"].opaque_handle)
    assert not visible(tools["runs.get_summary"].opaque_handle)
    adapter.invoke(handle=tools["tracking.lookup"].opaque_handle, arguments={"tracking_number": "R00000000001"})
    assert len(calls) == 1
    access.state = granted("ai.chat")
    with pytest.raises(HarnessError, match="权限"):
        adapter.invoke(handle=tools["tracking.lookup"].opaque_handle, arguments={"tracking_number": "R00000000001"})
    assert len(calls) == 1


def test_feishu_custom_identity_reader_is_allowed_and_then_revoked():
    access = MutableAuthority("business.query")
    calls = []
    actor = Actor(ActorType.FEISHU_USER, "ou-test", roles=("admin",), authenticated_by="feishu_admin_binding")
    def invoke():
        return asyncio.run(invoke_registered_reader(
            catalog=ToolRegistry(), name="track_waybill", arguments={"tracking_number": "R00000000001"},
            handler=lambda args: calls.append(args) or {"ok": True, "route_rows": []}, actor=actor, source="feishu", identity_access=access))
    assert invoke()["success"]
    access.state = granted("plugins.execute")
    assert invoke()["error_code"] == "PERMISSION_DENIED"
    assert len(calls) == 1


@pytest.mark.parametrize("path,method,permission", [
    ("/waybills", "GET", "business.query:view"), ("/tracking/query", "POST", "business.query:view"),
    ("/receipts/data", "GET", "business.query:view"), ("/waybills/manual", "POST", "waybill.write:operate"),
    ("/waybills/source-refresh", "POST", "business.query:view"), ("/waybills/7/status", "POST", "waybill.write:operate"),
    ("/original-pages/ronghui/launch", "GET", "waybill.write:view"), ("/ocr/upload", "POST", "waybill.write:operate"),
    ("/finance/summary", "GET", "finance:view"), ("/finance/sync", "POST", "finance:operate"),
    ("/customer-service/problems/live-query", "POST", "customer_service:view"),
    ("/customer-service/problem-settings", "POST", "customer_service:operate"),
    ("/automations", "GET", "plugins.execute:view"), ("/automations/tasks/run-now", "POST", "plugins.execute:operate"),
    ("/automations/tasks/output", "POST", "plugins.execute:view"),
    ("/harness", "GET", "ai_assistant:view"), ("/harness/messages", "POST", "ai_assistant:view"),
    ("/warehouse-monitor", "GET", "warehouse.monitor:view"), ("/warehouse-monitor/lookup", "GET", "warehouse.monitor:view"),
    ("/settings/accounts/identities/save", "POST", "super_admin"), ("/settings/accounts/persons/save", "POST", "super_admin"),
    ("/extensions/install", "POST", "super_admin"), ("/unknown-new-endpoint", "GET", "super_admin"),
])
def test_console_routes_use_explicit_function_permission(path, method, permission):
    assert console_permission(method, path) == permission


@pytest.mark.parametrize("path", [
    "/internal/v1/automation-projects/example/configuration",
    "/internal/v1/automation-projects/example/approval-policy",
    "/internal/v1/automation/plugins/example/install",
    "/internal/v1/admin/identities",
])
def test_plugin_execution_permission_does_not_grant_administrative_routes(path):
    from shared.identity_permissions import SUPER_ADMIN
    assert agent_permission("POST", path) == SUPER_ADMIN


def test_console_session_enforces_live_role_permissions():
    session = {"user_id": 1, "username": "ordinary", "role": "admin", "control_plane_role": "admin",
               "is_active": 1, "expires_at": datetime.now() + timedelta(hours=1),
               "access_role_id": str(uuid4()), "role_active": 1, "role_name": "查询员",
               "console_permissions_json": '{"business.query": "view"}', "agent_permissions_json": '{"finance.read": "direct"}'}
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app.settings = SimpleNamespace(basic_auth_user="", basic_auth_password="", session_secret="synthetic", session_cookie_secure=False)
    app.repository = SimpleNamespace(get_admin_session=lambda _: session, touch_admin_session=lambda _: None)
    app._session_secret = "synthetic"
    handler = SimpleNamespace(path="/finance/summary", command="GET", headers={"Cookie": "docflow_admin_session=" + app._encode_session_cookie("test")}, wfile=io.BytesIO())
    errors = []
    app._send_json = lambda _handler, status, data: errors.append((status, data))
    assert not app._ensure_authorized(handler)
    assert errors[-1][0] == 403
    # The agent set never opens Console pages.
    session["console_permissions_json"] = '{"finance": "view"}'
    assert app._ensure_authorized(handler)
    handler.command, handler.path = "POST", "/finance/sync"
    assert not app._ensure_authorized(handler)  # view is not operate
    session["console_permissions_json"] = '{"finance": "operate"}'
    assert app._ensure_authorized(handler)
    session["role_active"] = 0
    assert not app._ensure_authorized(handler)
    session.update(role="super_admin", control_plane_role="super_admin")
    assert app._ensure_authorized(handler)


def test_role_definition_rejects_unknown_permissions_and_finance_write_without_read():
    with pytest.raises(ValueError):
        validate_console_permissions({"super_admin": "view"})
    with pytest.raises(ValueError, match="只有可见"):
        validate_console_permissions({"business.query": "operate"})
    with pytest.raises(ValueError):
        validate_agent_permissions({"super_admin": "direct"})
    with pytest.raises(ValueError, match="无需确认"):
        validate_agent_permissions({"business.query": "confirm"})
    with pytest.raises(ValueError, match="财务数据"):
        validate_agent_permissions({"finance.manage": "confirm"})
    assert validate_agent_permissions({"finance.manage": "direct", "finance.read": "direct"}) == {
        "finance.manage": "direct", "finance.read": "direct"}


def test_console_and_agent_sets_are_independent():
    access = IdentityAccess(True, console={"finance": "view"}, agent={"business.query": "direct", "plugins.execute": "confirm"})
    console, agent = access.for_channel("console"), access.for_channel("agent")
    assert console.allows("finance.read") and console.allows("finance:view")
    assert not console.allows("finance.manage") and not console.allows("finance:operate")
    assert console.allows("finance.manage", read=True)  # viewing a collector's runs
    assert not console.allows("business.query") and not console.allows("plugins.execute")
    assert agent.allows("business.query") and agent.allows("plugins.execute")
    assert not agent.allows("finance.read") and not agent.allows("finance:view")
    assert agent.agent_level("plugins.execute") == "confirm" and agent.agent_level("finance.read") is None
    assert console.public()["access_permissions"] == ["finance:view"]
    assert console.public()["access_agent_permissions"] == {"business.query": "direct", "plugins.execute": "confirm"}
    boss = IdentityAccess(True, super_admin=True).for_channel("agent")
    assert boss.allows("finance.manage") and boss.agent_level("plugins.execute") == "direct"


def test_console_ai_assistant_uses_the_agent_set():
    """A Console admin chatting with the assistant is checked like Feishu."""
    access = MutableAuthority()
    access.state = IdentityAccess(True, console={"business.query": "view", "finance": "view"},
                                  agent={"ai.chat": "direct", "finance.read": "direct"})
    adapter = TrustedHarnessInvocationAdapter(
        policy_service=SimpleNamespace(identity_access=access),
        actor=Actor(ActorType.CONSOLE_ADMIN, "123", roles=("admin",), authenticated_by="mysql_admin_session"),
        base_request_id=str(uuid4()), fixed_handlers={name: lambda args: {"ok": True} for name in FIXED_HARNESS_TOOL_IDS},
    )
    tools = {tool.descriptor.tool_id: tool for tool in build_fixed_harness_tools()}
    visible = adapter.visible_tool_filter()
    assert visible(tools["finance.summary"].opaque_handle)
    assert not visible(tools["tracking.lookup"].opaque_handle)  # a Console page right only
