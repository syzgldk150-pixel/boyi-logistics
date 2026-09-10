from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.orchestration.automation_project_entrypoints import AutomationProjectEntrypoints
from agent.orchestration.automation_project_policy_service import AutomationProjectPolicyService
from agent.orchestration.models import Actor, ActorType, OrchestrationError


INVOCATION_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
CONTEXT = {"invocation_id": INVOCATION_ID, "automation_id": "scan_codes", "event_id": "event-one", "sender_id": "sender-one", "chat_id": "chat-one"}


def _reader(*, row_changes=None, actor=None, result_changes=None):
    row = {
        "invocation_id": INVOCATION_ID, "automation_id": "scan_codes", "source": "feishu",
        "actor_id": "sender-one", "request_id": "event-one", "preview_invocation_id": None,
        **(row_changes or {}),
    }
    result = {"invocation_id": INVOCATION_ID, "automation_id": "scan_codes", "status": "COMPLETED", "output": {"dry_run": True}, **(result_changes or {})}
    policy = object.__new__(AutomationProjectPolicyService)
    policy.direct_invocations = SimpleNamespace(repository=SimpleNamespace(get=Mock(return_value=row)), wait=AsyncMock(return_value=result))
    policy._load_catalog_entry = Mock(return_value=SimpleNamespace(automation_id="scan_codes", plugin_id="sync_scan_codes", trust_source="ed25519_first_party"))
    policy.get_scan_preview_projection = Mock(return_value={"preview_invocation_id": INVOCATION_ID, "can_confirm": True})
    resolver = Mock(return_value=actor or Actor(ActorType.FEISHU_USER, "sender-one", authenticated_by="feishu_verified_event"))
    entrypoints = AutomationProjectEntrypoints(policy, route_resolver=None, feishu_actor_resolver=resolver)
    return entrypoints, policy


def test_exact_result_read_uses_original_event_and_preserves_late_preview_projection():
    reader, policy = _reader()
    result = asyncio.run(reader.wait_feishu_invocation(**CONTEXT, timeout_seconds=0.05))
    assert result["scan_preview"] == {"preview_invocation_id": INVOCATION_ID, "can_confirm": True}
    policy.direct_invocations.repository.get.assert_called_once_with(INVOCATION_ID)
    policy.direct_invocations.wait.assert_awaited_once_with(INVOCATION_ID, timeout_seconds=0.05)
    policy.get_scan_preview_projection.assert_called_once_with("scan_codes", preview_invocation_id=INVOCATION_ID)


def test_formal_call_does_not_recreate_confirmation_from_a_late_result():
    reader, policy = _reader(row_changes={"preview_invocation_id": OTHER_ID})
    result = asyncio.run(reader.wait_feishu_invocation(**CONTEXT))
    assert "scan_preview" not in result
    policy._load_catalog_entry.assert_not_called()
    policy.get_scan_preview_projection.assert_not_called()


@pytest.mark.parametrize("wrong", [{"source": "console"}, {"actor_id": "another-user"}, {"request_id": "another-event"}, {"automation_id": "another-project"}])
def test_result_read_refuses_another_source_sender_event_or_project(wrong):
    reader, policy = _reader(row_changes=wrong)
    with pytest.raises(OrchestrationError) as caught:
        asyncio.run(reader.wait_feishu_invocation(**CONTEXT))
    assert caught.value.code == "ACTOR_NOT_AUTHORIZED"
    policy.direct_invocations.wait.assert_not_awaited()


@pytest.mark.parametrize("actor", [Actor(ActorType.FEISHU_USER, "other-user", authenticated_by="feishu_verified_event"), Actor(ActorType.CONSOLE_ADMIN, "sender-one", roles=("admin",))])
def test_result_read_rechecks_resolved_actor_identity_and_trust(actor):
    reader, policy = _reader(actor=actor)
    with pytest.raises(OrchestrationError) as caught:
        asyncio.run(reader.wait_feishu_invocation(**CONTEXT))
    assert caught.value.code in {"ACTOR_NOT_AUTHORIZED", "TRUSTED_ENTRYPOINT_REQUIRED"}
    policy.direct_invocations.repository.get.assert_not_called()


@pytest.mark.parametrize("wrong", [{"invocation_id": OTHER_ID}, {"automation_id": "another-project"}])
def test_result_read_refuses_mismatched_wait_result(wrong):
    reader, policy = _reader(result_changes=wrong)
    with pytest.raises(OrchestrationError) as caught:
        asyncio.run(reader.wait_feishu_invocation(**CONTEXT))
    assert caught.value.code == "INVOCATION_IDENTITY_INVALID"
    policy.get_scan_preview_projection.assert_not_called()


def test_missing_result_is_explicit_and_never_resubmitted():
    reader, policy = _reader()
    policy.direct_invocations.repository.get.return_value = None
    with pytest.raises(OrchestrationError) as caught:
        asyncio.run(reader.wait_feishu_invocation(**CONTEXT))
    assert caught.value.code == "INVOCATION_NOT_FOUND"
    policy.direct_invocations.wait.assert_not_awaited()
