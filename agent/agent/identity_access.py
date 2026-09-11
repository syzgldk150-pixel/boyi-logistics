"""Production authorization projections for authenticated human entrypoints."""
from __future__ import annotations

import re

from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.identity_permissions import plugin_permission
from shared.identity_routes import agent_permission


def require_project_access(policy, actor, automation_id, *, entrypoint=""):
    access = getattr(policy, "identity_access", None)
    if access is None:  # Offline policy compositions supply their own actors.
        return
    if actor.actor_type not in {ActorType.CONSOLE_ADMIN, ActorType.FEISHU_USER}:
        return  # Existing scheduler/webhook contracts own these non-human sources.
    entry = policy._plugin_catalog.require(automation_id)
    if not access.allows(actor, plugin_permission(entry.plugin_id, management=entry.management, entrypoint=entrypoint)):
        raise OrchestrationError("IDENTITY_PERMISSION_DENIED", "当前身份没有执行或查看此插件的权限。")


def authorize_console_request(access, policy, principal, method, path):
    actor = Actor(ActorType.CONSOLE_ADMIN, principal["actor_id"],
                  roles=tuple(principal["roles"]), authenticated_by=principal["authenticated_by"])
    if path.startswith("/internal/v1/automation-projects/") and "/module-slots/" not in path:
        match = re.fullmatch(r"/internal/v1/automation-projects/([^/]+)/(invoke|invocations|scan-previews/[^/]+|selection-previews(?:/[^/]+(?:/confirm)?)?)", path)
        if match:
            require_project_access(policy, actor, match[1], entrypoint="console")
            return
    if path.startswith("/internal/v1/automation-invocations/"):
        match = re.fullmatch(r"/internal/v1/automation-invocations/([^/]+)(?:/cancel)?", path)
        if match:
            row = policy.direct_invocations.repository.get(match[1])
            if row:
                require_project_access(policy, actor, row["automation_id"], entrypoint="console")
                return
    permission = agent_permission(method, path)
    allowed = access.allows(actor, permission)
    if path in {"/internal/v1/automation/plugins/catalog", "/internal/v1/automation-project-policies"}:
        allowed = any(access.allows(actor, p) for p in ("plugins.execute", "finance.read", "customer_service"))
    if not allowed:
        raise OrchestrationError("IDENTITY_PERMISSION_DENIED", "当前身份没有此功能权限。")
