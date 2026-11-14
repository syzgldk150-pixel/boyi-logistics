"""Real shared-role changes across Console and Feishu in disposable MySQL."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.identity_access import authorize_console_request, require_project_access
from agent.orchestration.feishu_binding_service import FeishuBindingService
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.identity_repository import IdentityRepository
from tests.identity_support import role_sets
from tests.test_feishu_bindings_mysql import binding_repository as binding_repository, person_of, pytestmark, seed_admin
from tests.mysql_migrated_database import database as database


def setup_identities(repository):
    identities = IdentityRepository(repository._connection_factory)
    admin_id = seed_admin(repository)
    service = FeishuBindingService(repository, identity_access=identities)
    return identities, admin_id, service


def ordinary_account(repository, identities, role_id):
    account_id = seed_admin(repository)
    with repository.unit_of_work() as uow, uow.feishu_bindings.cursor() as cursor:
        cursor.execute("UPDATE admin_users SET role='admin',control_plane_role='admin' WHERE id=%s", (account_id,))
        uow.commit()
    identities.assign_account(account_id, role_id)
    return account_id


def test_role_changes_reach_account_and_both_kinds_of_person(binding_repository):
    identities, admin_id, service = setup_identities(binding_repository)
    role_name = f"查询员-{uuid4().hex}"
    role_sender, account_sender = f"ou-role-{uuid4().hex}", f"ou-account-{uuid4().hex}"
    role_id = identities.save_role(name=role_name, **role_sets("business.query", "ai.chat"), active=True)
    account_id = ordinary_account(binding_repository, identities, role_id)
    actor = Actor(ActorType.CONSOLE_ADMIN, str(account_id), roles=("admin",), authenticated_by="mysql_admin_session")
    chat_only = identities.save_person(name="只用飞书的人", role_id=role_id)
    for sender, person_id in ((role_sender, chat_only), (account_sender, person_of(binding_repository, account_id))):
        challenge = service.create_binding_challenge(admin_id, label=sender, person_id=person_id)
        assert "绑定成功" in service.handle_binding_text(sender, "test-chat", challenge["command"])
        assert service.resolve_actor(sender).roles == ("admin",)
    assert identities.allows(actor, "business.query")
    assert not identities.allows(actor, "plugins.execute")
    assert not identities.allows(actor, "finance.read")
    identities.save_role(role_id=role_id, name=role_name, **role_sets("plugins.execute", "finance.read"), active=True)
    for subject in (actor, service.resolve_actor(role_sender), service.resolve_actor(account_sender)):
        assert identities.allows(subject, "plugins.execute")
        assert identities.allows(subject, "finance.read")
        assert not identities.allows(subject, "business.query")
        assert not identities.allows(subject, "super_admin")
    identities.save_role(role_id=role_id, name=role_name, **role_sets(), active=False)
    assert not identities.for_actor(actor).active
    assert service.resolve_actor(role_sender).roles == ()
    assert service.resolve_actor(account_sender).roles == ()
    assert identities.account(admin_id).allows("super_admin")
    assert identities.account(admin_id).allows("future.function")
    with pytest.raises(ValueError, match="内置全部权限"):
        identities.assign_account(admin_id, identities.save_role(name=f"新身份-{uuid4().hex}", **role_sets(), active=True))


def test_unbind_rebind_and_all_bindings_are_visible_without_raw_ids(binding_repository):
    identities, admin_id, service = setup_identities(binding_repository)
    existing_ids = {row["binding_id"] for row in identities.list_bindings()}
    role_id = identities.save_role(name=f"业务管理员-{uuid4().hex}", **role_sets("business.query"), active=True)
    sender = f"ou-private-{uuid4().hex}"
    warehouse = identities.save_person(name="仓库", role_id=role_id)
    challenge = service.create_binding_challenge(admin_id, person_id=warehouse, label="仓库")
    assert "绑定成功" in service.handle_binding_text(sender, "test-chat", challenge["command"])
    rows = [row for row in identities.list_bindings() if row["binding_id"] not in existing_ids]
    assert len(rows) == 1 and rows[0]["effective"]
    assert sender not in str(rows)
    assert rows[0]["identity_label"] == "仓库"
    another = service.create_binding_challenge(admin_id, person_id=person_of(binding_repository, admin_id))
    assert "已有绑定" in service.handle_binding_text(sender, "test-chat", another["command"])
    identities.update_binding(rows[0]["binding_id"], revoke=True)
    assert service.resolve_actor(sender).roles == ()
    assert "绑定成功" in service.handle_binding_text(sender, "test-chat", another["command"])
    assert service.resolve_actor(sender).roles == ("admin", "super_admin")
    history = [row for row in identities.list_bindings() if row["binding_id"] not in existing_ids]
    assert len(history) == 2 and sum(row["active"] for row in history) == 1
    # An unrelated binding by the same administrator stays active.
    other = service.create_binding_challenge(admin_id, person_id=identities.save_person(name="其他", role_id=role_id), label="其他")
    other_sender = f"ou-other-{uuid4().hex}"
    assert "绑定成功" in service.handle_binding_text(other_sender, "test-chat", other["command"])
    latest = next(row for row in history if row["active"])
    identities.update_binding(latest["binding_id"], person_id=warehouse, label="仓库新身份")
    assert service.resolve_actor(sender).roles == ("admin",)
    assert service.resolve_actor(other_sender).roles == ("admin",)
    identities.update_binding(latest["binding_id"], revoke=True)
    assert service.resolve_actor(other_sender).roles == ("admin",)


def test_backend_plugin_and_finance_permissions_are_not_menu_only(binding_repository):
    identities, _, _ = setup_identities(binding_repository)
    role_name = f"插件操作员-{uuid4().hex}"
    role_id = identities.save_role(name=role_name, **role_sets("plugins.execute"), active=True)
    account_id = ordinary_account(binding_repository, identities, role_id)
    actor = Actor(ActorType.CONSOLE_ADMIN, str(account_id), roles=("admin",), authenticated_by="mysql_admin_session")
    principal = {"actor_id": actor.actor_id, "roles": ["admin"], "authenticated_by": actor.authenticated_by}
    entries = {"scan": SimpleNamespace(plugin_id="scan_test", management={"purpose": "action", "module": "automation", "dataset": "", "version": ""}),
               "finance": SimpleNamespace(plugin_id="finance_test", management={"purpose": "collector", "module": "finance", "dataset": "finance.transactions", "version": "1"})}
    policy = SimpleNamespace(identity_access=identities, _plugin_catalog=SimpleNamespace(require=entries.__getitem__))
    require_project_access(policy, actor, "scan")
    with pytest.raises(OrchestrationError, match="权限"):
        require_project_access(policy, actor, "finance")
    with pytest.raises(OrchestrationError, match="权限"):
        authorize_console_request(identities, policy, principal, "GET", "/internal/v1/admin/finance/summary")
    with pytest.raises(OrchestrationError, match="权限"):
        authorize_console_request(identities, policy, principal, "POST", "/internal/v1/admin/feishu-approval-binding/challenge")
    identities.save_role(role_id=role_id, name=role_name, **role_sets("finance.read", "finance.manage"), active=True)
    require_project_access(policy, actor, "finance")
    with pytest.raises(OrchestrationError, match="权限"):
        require_project_access(policy, actor, "scan")


def test_person_status_and_account_status_reach_every_channel(binding_repository):
    identities, admin_id, service = setup_identities(binding_repository)
    role_id = identities.save_role(name=f"司机-{uuid4().hex}", **role_sets("business.query"), active=True)
    chat_only = identities.save_person(name="司机老王", role_id=role_id)
    account_id = ordinary_account(binding_repository, identities, role_id)
    account_person = person_of(binding_repository, account_id)
    senders = {chat_only: f"ou-driver-{uuid4().hex}", account_person: f"ou-clerk-{uuid4().hex}"}
    for person_id, sender in senders.items():
        challenge = service.create_binding_challenge(admin_id, person_id=person_id, label="x")
        assert "绑定成功" in service.handle_binding_text(sender, "test-chat", challenge["command"])
        assert identities.feishu(sender).person_id == person_id
    # Disabling a chat-only person stops its bindings; it can be re-enabled.
    identities.save_person(person_id=chat_only, name="司机老王", role_id=role_id, active=False)
    assert not identities.feishu(senders[chat_only]).active
    with pytest.raises(OrchestrationError, match="已停用"):
        service.create_binding_challenge(admin_id, person_id=chat_only)
    identities.save_person(person_id=chat_only, name="司机老王", role_id=role_id, active=True)
    assert identities.feishu(senders[chat_only]).allows("business.query")
    # A person with a login follows that account's status, not a separate switch.
    with pytest.raises(ValueError, match="后台账号"):
        identities.save_person(person_id=account_person, name="x", role_id=role_id, active=False)
    with binding_repository.unit_of_work() as uow, uow.feishu_bindings.cursor() as cursor:
        cursor.execute("UPDATE admin_users SET is_active=0 WHERE id=%s", (account_id,))
        uow.commit()
    assert not identities.feishu(senders[account_person]).active
    assert identities.feishu(senders[chat_only]).active
    # Changing the person's role through the account reaches its Feishu binding.
    with binding_repository.unit_of_work() as uow, uow.feishu_bindings.cursor() as cursor:
        cursor.execute("UPDATE admin_users SET is_active=1 WHERE id=%s", (account_id,))
        uow.commit()
    other_role = identities.save_role(name=f"财务-{uuid4().hex}", **role_sets("finance.read"), active=True)
    identities.assign_account(account_id, other_role)
    assert identities.feishu(senders[account_person]).allows("finance.read")
    assert not identities.feishu(senders[account_person]).allows("business.query")
    listed = {row["person_id"]: row for row in identities.list_persons()}
    assert listed[account_person]["account_id"] == account_id and listed[account_person]["feishu_count"] == 1
    assert listed[chat_only]["account_id"] is None and listed[chat_only]["effective"]


def test_one_person_gets_console_rights_on_pages_and_agent_rights_in_chat(binding_repository):
    """The same person: Console set on pages, agent set in Feishu and the AI assistant."""
    from dataclasses import replace
    identities, admin_id, service = setup_identities(binding_repository)
    role_id = identities.save_role(name=f"内勤-{uuid4().hex}", console={"business.query": "view", "finance": "view"},
                                   agent={"ai.chat": "direct", "finance.read": "direct", "plugins.execute": "confirm"},
                                   active=True)
    account_id = ordinary_account(binding_repository, identities, role_id)
    sender = f"ou-clerk-{uuid4().hex}"
    challenge = service.create_binding_challenge(admin_id, person_id=person_of(binding_repository, account_id))
    assert "绑定成功" in service.handle_binding_text(sender, "test-chat", challenge["command"])
    page = Actor(ActorType.CONSOLE_ADMIN, str(account_id), roles=("admin",), authenticated_by="mysql_admin_session")
    assistant = replace(page, channel="agent")
    feishu = service.resolve_actor(sender)
    # Console pages: modules only, view is not operate.
    assert identities.allows(page, "business.query") and identities.allows(page, "finance:view")
    assert not identities.allows(page, "finance:operate") and not identities.allows(page, "plugins.execute")
    # Feishu and the Console AI assistant: the agent set only.
    for actor in (assistant, feishu):
        access = identities.for_actor(actor)
        assert access.channel == "agent" and access.allows("finance.read") and access.allows("ai.chat")
        assert not access.allows("business.query")
        assert access.agent_level("plugins.execute") == "confirm"
    # Console page routes on the Agent use the Console set.
    principal = {"actor_id": page.actor_id, "roles": ["admin"], "authenticated_by": page.authenticated_by}
    policy = SimpleNamespace(identity_access=identities)
    authorize_console_request(identities, policy, principal, "GET", "/internal/v1/admin/finance/summary")
    with pytest.raises(OrchestrationError, match="权限"):
        authorize_console_request(identities, policy, principal, "POST", "/internal/v1/admin/finance/sync")
    with pytest.raises(OrchestrationError, match="权限"):
        authorize_console_request(identities, policy, principal, "POST", "/internal/v1/harness/sessions")
    identities.save_role(role_id=role_id, name=f"内勤-{uuid4().hex}", console={"ai_assistant": "view"},
                         agent={"ai.chat": "direct"}, active=True)
    authorize_console_request(identities, policy, principal, "POST", "/internal/v1/harness/sessions")
    assert not identities.for_actor(feishu).allows("finance.read")
