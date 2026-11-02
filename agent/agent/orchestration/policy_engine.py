"""Role decisions for direct readers and project policy evaluation results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.orchestration.models import Actor, ActorType


@dataclass(frozen=True)
class ProjectPolicyEvaluation:
    """Project-specific decision for one trusted entrypoint."""

    allowed: bool
    requires_approval: bool | None
    code: str
    reason: str


class PolicyEngine:
    def __init__(self, catalog: Any) -> None:
        self._catalog = catalog

    def can_decide(self, actor: Actor, *, required_role: str, source: str) -> bool:
        console_admin = (
            actor.actor_type is ActorType.CONSOLE_ADMIN
            and source == "console"
            and actor.authenticated_by == "mysql_admin_session"
        )
        feishu_admin = (
            actor.actor_type is ActorType.FEISHU_USER
            and source == "feishu"
            and actor.authenticated_by == "feishu_admin_binding"
        )
        if not (console_admin or feishu_admin):
            return False
        if required_role == "super_admin":
            return "super_admin" in actor.roles
        if required_role == "admin":
            return bool({"admin", "super_admin"}.intersection(actor.roles))
        return False
