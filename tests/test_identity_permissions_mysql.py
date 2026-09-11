"""Real shared-role changes across Console and Feishu in disposable MySQL."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.identity_access import authorize_console_request, require_project_access
from agent.orchestration.feishu_approval_service import FeishuApprovalService
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.identity_repository import IdentityRepository
from tests.test_feishu_bindings_mysql import binding_repository as binding_repository, pytestmark, seed_admin
from tests.test_manual_unknown_write_mysql import database as database


def setup_identities(repository):
    identities = IdentityRepository(repository._connection_factory)
    admin_id = seed_admin(repository)
    service = FeishuApprovalService(repository, None, send_text=lambda *_: True, identity_access=identities)
    return identities, admin_id, service


def ordinary_account(repository, identities, role_id):
    account_id = seed_admin(repository)
    with repository.unit_of_work() as uow, uow.feishu_approvals.cursor() as cursor:
        cursor.execute("UPDATE admin_users SET role='admin',control_plane_role='admin' WHERE id=%s", (account_id,))
        uow.commit()
    identities.assign_account(account_id, role_id)
    return account_id


def test_role_changes_reach_account_and_both_feishu_binding_modes(binding_repository):
    identities, admin_id, service = setup_identities(binding_repository)
    role_name = f"查询员-{uuid4().hex}"
    role_sender, account_sender = f"ou-role-{uuid4().hex}", f"ou-account-{uuid4().hex}"
    role_id = identities.save_role(name=role_name, permissions=["business.query", "ai.chat"], active=True)
    account_id = ordinary_account(binding_repository, identities, role_id)
    actor = Actor(ActorType.CONSOLE_ADMIN, str(account_id), roles=("admin",), authenticated_by="mysql_admin_session")
    for sender, target in ((role_sender, {"role_id": role_id}), (account_sender, {"account_id": account_id})):
        challenge = service.create_binding_challenge(admin_id, label=sender, **target)
        assert "绑定成功" in service.handle_binding_text(sender, "test-chat", challenge["command"])
        assert service.resolve_actor(sender).roles == ("admin",)
    assert identities.allows(actor, "business.query")
    assert not identities.allows(actor, "plugins.execute")
    assert not identities.allows(actor, "finance.read")
    identities.save_role(role_id=role_id, name=role_name, permissions=["plugins.execute", "finance.read"], active=True)
    for subject in (actor, service.resolve_actor(role_sender), service.resolve_actor(account_sender)):
        assert identities.allows(subject, "plugins.execute")
        assert identities.allows(subject, "finance.read")
        assert not identities.allows(subject, "business.query")
        assert not identities.allows(subject, "super_admin")
    identities.save_role(role_id=role_id, name=role_name, permissions=[], active=False)
    assert not identities.for_actor(actor).active
    assert service.resolve_actor(role_sender).roles == ()
    assert service.resolve_actor(account_sender).roles == ()
    assert identities.account(admin_id).allows("super_admin")
    assert identities.account(admin_id).allows("future.function")
    with pytest.raises(ValueError, match="内置全部权限"):
        identities.assign_account(admin_id, identities.save_role(name=f"新身份-{uuid4().hex}", permissions=[], active=True))


def test_unbind_rebind_and_all_bindings_are_visible_without_raw_ids(binding_repository):
    identities, admin_id, service = setup_identities(binding_repository)
    existing_ids = {row["binding_id"] for row in identities.list_bindings()}
    role_id = identities.save_role(name=f"业务管理员-{uuid4().hex}", permissions=["business.query"], active=True)
    sender = f"ou-private-{uuid4().hex}"
    challenge = service.create_binding_challenge(admin_id, role_id=role_id, label="仓库")
    assert "绑定成功" in service.handle_binding_text(sender, "test-chat", challenge["command"])
    rows = [row for row in identities.list_bindings() if row["binding_id"] not in existing_ids]
    assert len(rows) == 1 and rows[0]["effective"]
    assert sender not in str(rows)
    assert rows[0]["identity_label"] == "仓库"
    another = service.create_binding_challenge(admin_id, account_id=admin_id)
    assert "已有绑定" in service.handle_binding_text(sender, "test-chat", another["command"])
    identities.update_binding(rows[0]["binding_id"], revoke=True)
    assert service.resolve_actor(sender).roles == ()
    assert "绑定成功" in service.handle_binding_text(sender, "test-chat", another["command"])
    assert service.resolve_actor(sender).roles == ("admin", "super_admin")
    history = [row for row in identities.list_bindings() if row["binding_id"] not in existing_ids]
    assert len(history) == 2 and sum(row["active"] for row in history) == 1
    # An unrelated binding by the same administrator stays active.
    other = service.create_binding_challenge(admin_id, role_id=role_id, label="其他")
    other_sender = f"ou-other-{uuid4().hex}"
    assert "绑定成功" in service.handle_binding_text(other_sender, "test-chat", other["command"])
    latest = next(row for row in history if row["active"])
    identities.update_binding(latest["binding_id"], role_id=role_id, label="仓库新身份")
    assert service.resolve_actor(sender).roles == ("admin",)
    assert service.resolve_actor(other_sender).roles == ("admin",)
    identities.update_binding(latest["binding_id"], revoke=True)
    assert service.resolve_actor(other_sender).roles == ("admin",)


def test_backend_plugin_and_finance_permissions_are_not_menu_only(binding_repository):
    identities, _, _ = setup_identities(binding_repository)
    role_name = f"插件操作员-{uuid4().hex}"
    role_id = identities.save_role(name=role_name, permissions=["plugins.execute"], active=True)
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
    identities.save_role(role_id=role_id, name=role_name, permissions=["finance.read", "finance.manage"], active=True)
    require_project_access(policy, actor, "finance")
    with pytest.raises(OrchestrationError, match="权限"):
        require_project_access(policy, actor, "scan")
