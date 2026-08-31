from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
    _trusted_context,
)
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.automation_project_authorization import AutomationEntrypoint
from shared.automation_project_manifest import TRUSTED_AUTOMATION_ENTRYPOINTS


def _admin_actor(*, authenticated_by: str = "mysql_admin_session") -> Actor:
    return Actor(
        ActorType.CONSOLE_ADMIN,
        "admin-one",
        roles=("admin",),
        authenticated_by=authenticated_by,
    )


def test_harness_is_service_v2_only_and_not_a_legacy_entrypoint() -> None:
    assert AutomationEntrypoint.HARNESS.value == "harness"
    assert "harness" not in TRUSTED_AUTOMATION_ENTRYPOINTS


def test_harness_requires_the_original_signed_console_admin() -> None:
    AutomationProjectPolicyService._require_trusted_entrypoint_actor(
        AutomationEntrypoint.HARNESS,
        _admin_actor(),
    )

    with pytest.raises(OrchestrationError) as exc_info:
        AutomationProjectPolicyService._require_trusted_entrypoint_actor(
            AutomationEntrypoint.HARNESS,
            _admin_actor(authenticated_by="basic_auth"),
        )

    assert exc_info.value.code == "ACTION_FORBIDDEN"


def test_harness_transport_context_is_closed() -> None:
    assert _trusted_context(AutomationEntrypoint.HARNESS, None) == {}

    with pytest.raises(OrchestrationError) as exc_info:
        _trusted_context(
            AutomationEntrypoint.HARNESS,
            {"dynamic_inputs": {"guessed": True}},
        )

    assert exc_info.value.code == "TRUSTED_CONTEXT_INVALID"


def test_harness_wrapper_forwards_only_opaque_managed_identity() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Recorder:
        def invoke_trusted(self, automation_id: str, **kwargs: object) -> str:
            calls.append((automation_id, kwargs))
            return "receipt-one"

    actor = _admin_actor()
    result = AutomationProjectPolicyService.invoke_harness(
        Recorder(),  # type: ignore[arg-type]
        "project-one",
        request_id="00000000-0000-4000-8000-000000000001",
        actor=actor,
        expected_automation_generation=7,
        contribution_id="lookup-one",
    )

    assert result == "receipt-one"
    assert calls == [
        (
            "project-one",
            {
                "entrypoint": AutomationEntrypoint.HARNESS,
                "request_id": "00000000-0000-4000-8000-000000000001",
                "actor": actor,
                "expected_automation_generation": 7,
                "contribution_id": "lookup-one",
            },
        )
    ]


def test_action_v1_cannot_be_invoked_from_harness() -> None:
    service = AutomationProjectPolicyService.__new__(AutomationProjectPolicyService)
    service._require_release_active = lambda: None  # type: ignore[method-assign]
    service._command_gateway = object()
    service._load_contract = lambda _automation_id: (  # type: ignore[method-assign]
        SimpleNamespace(runtime_model="ACTION_V1"),
        object(),
    )

    with pytest.raises(OrchestrationError) as exc_info:
        service.invoke_trusted(
            "project-one",
            entrypoint=AutomationEntrypoint.HARNESS,
            request_id="00000000-0000-4000-8000-000000000002",
            actor=_admin_actor(),
            expected_automation_generation=1,
            contribution_id="lookup-one",
        )

    assert exc_info.value.code == "PROJECT_ENTRYPOINT_DISABLED"
