from __future__ import annotations

from agent.orchestration.models import Actor, ActorType
from agent.orchestration.policy_engine import PolicyEngine


def _engine() -> PolicyEngine:
    return PolicyEngine(catalog=object())


def test_console_admin_needs_a_mysql_session_and_the_role() -> None:
    admin = Actor(ActorType.CONSOLE_ADMIN, "1", roles=("admin",), authenticated_by="mysql_admin_session")
    basic = Actor(ActorType.CONSOLE_ADMIN, "0", roles=("admin",), authenticated_by="legacy_basic_auth")

    assert _engine().can_decide(admin, required_role="admin", source="console")
    assert not _engine().can_decide(admin, required_role="super_admin", source="console")
    assert not _engine().can_decide(basic, required_role="admin", source="console")
    assert not _engine().can_decide(admin, required_role="admin", source="feishu")


def test_super_admin_inherits_admin() -> None:
    root = Actor(ActorType.CONSOLE_ADMIN, "1", roles=("super_admin",), authenticated_by="mysql_admin_session")

    assert _engine().can_decide(root, required_role="admin", source="console")
    assert _engine().can_decide(root, required_role="super_admin", source="console")


def test_feishu_needs_a_bound_identity() -> None:
    bound = Actor(ActorType.FEISHU_USER, "ou-1", roles=("admin",), authenticated_by="feishu_admin_binding")
    unbound = Actor(ActorType.FEISHU_USER, "ou-2", roles=("admin",), authenticated_by="feishu_verified_event")

    assert _engine().can_decide(bound, required_role="admin", source="feishu")
    assert not _engine().can_decide(unbound, required_role="admin", source="feishu")
    assert not _engine().can_decide(bound, required_role="unknown", source="feishu")
