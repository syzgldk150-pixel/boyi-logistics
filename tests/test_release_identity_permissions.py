"""Release identities stay operational and cannot acquire business permissions."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.identity_access import authorize_console_request
from agent.orchestration.models import ActorType, OrchestrationError
from shared.identity_repository import IdentityRepository
from shared.release_identity import RELEASE_OPERATIONS, release_principal


@contextmanager
def no_database():
    raise AssertionError("non-account release principal must not query admin_users")
    yield


@pytest.mark.parametrize("operation", RELEASE_OPERATIONS)
def test_signed_release_operation_is_not_a_database_account(operation):
    method, path, _, _ = RELEASE_OPERATIONS[operation]
    authorize_console_request(IdentityRepository(no_database), None,
                              release_principal(operation), method, path)


@pytest.mark.parametrize("operation", RELEASE_OPERATIONS)
@pytest.mark.parametrize("method,path", [
    ("POST", "/internal/v1/health"),
    ("GET", "/internal/v1/admin/scheduler/activate-after-release"),
    ("GET", "/internal/v1/admin/finance/summary"),
    ("POST", "/internal/v1/admin/accounts/clear"),
    ("POST", "/internal/v1/automation/migrations"),
    ("GET", "/internal/v1/harness/sessions"),
])
def test_release_principal_cannot_authorize_business_or_other_methods(operation, method, path):
    with pytest.raises(OrchestrationError, match="当前身份没有此功能权限"):
        authorize_console_request(IdentityRepository(no_database), None,
                                  release_principal(operation), method, path)


@pytest.mark.parametrize("operation", RELEASE_OPERATIONS)
def test_release_principal_cannot_invoke_plugin(operation):
    access = IdentityRepository(no_database)
    entry = SimpleNamespace(plugin_id="sync_scan_codes", management={
        "purpose": "action", "module": "automation", "dataset": "", "version": ""})
    policy = SimpleNamespace(identity_access=access,
                             _plugin_catalog=SimpleNamespace(require=lambda _: entry))
    with pytest.raises(OrchestrationError, match="当前身份没有执行或查看此插件"):
        authorize_console_request(access, policy, release_principal(operation),
                                  "POST", "/internal/v1/automation-projects/scan_codes/invoke")


@pytest.mark.parametrize("operation", RELEASE_OPERATIONS)
def test_regular_account_still_needs_live_permission_for_operational_request(operation):
    method, path, _, _ = RELEASE_OPERATIONS[operation]
    principal = {**release_principal(operation), "actor_id": "123"}
    calls = []
    def denied(actor, permission):
        calls.append((actor.actor_id, permission))
        return False
    with pytest.raises(OrchestrationError):
        authorize_console_request(SimpleNamespace(allows=denied), None, principal, method, path)
    assert calls == [("123", "super_admin")]


@pytest.mark.parametrize("actor_id", ["release-identity-probe", "bad-account-id", None])
def test_non_numeric_business_identity_is_denied_instead_of_500(actor_id):
    actor = SimpleNamespace(actor_type=ActorType.CONSOLE_ADMIN, actor_id=actor_id,
                            roles=("super_admin",), authenticated_by="mysql_admin_session")
    assert not IdentityRepository(no_database).for_actor(actor).active


@pytest.mark.parametrize("operation", RELEASE_OPERATIONS)
def test_actual_http_middleware_verifies_release_signature_and_token(operation):
    import main
    from tests.test_service_identity_boundary import _run_agent_auth_middleware
    method, path, _, _ = RELEASE_OPERATIONS[operation]
    principal = release_principal(operation)
    policy = SimpleNamespace(identity_access=IdentityRepository(no_database))
    with patch.object(main, "automation_project_policy_service", policy):
        status, payload, reached = _run_agent_auth_middleware(
            path, method=method, signed_principal=principal)
        assert (status, payload, reached) == (200, {"passed": True}, [principal])
        status, _, reached = _run_agent_auth_middleware(
            path, method=method, signed_principal=principal, internal_token="wrong-token")
        assert status == 401 and not reached
        status, _, reached = _run_agent_auth_middleware(
            "/internal/v1/admin/finance/summary", signed_principal=principal)
        assert status == 403 and not reached
