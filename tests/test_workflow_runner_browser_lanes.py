from __future__ import annotations

from agent.orchestration.models import OperationType, PlanStep
from agent.orchestration.workflow_runner import WorkflowRunner


def _step(account_id: str | None = None) -> PlanStep:
    return PlanStep(
        step_key="read",
        tool_name="plugin.browser.read",
        tool_version="1.0.0",
        operation_type=OperationType.READ,
        arguments={},
        account_id=account_id,
        depends_on=(),
        idempotency_key="read-1",
        expected_evidence=(),
        postconditions=(),
    )


def _capability(account_id: str | None = None) -> dict:
    bindings = {"account_id": account_id} if account_id else {}
    return {
        "_plugin_runtime": {
            "runtime_permissions": {"browser": True},
            "account_bindings": bindings,
        }
    }


def test_browser_lanes_are_serialized_per_exact_account() -> None:
    runner = object.__new__(WorkflowRunner)
    first = runner._browser_session_lock_keys(_step("account-a"), _capability())
    same = runner._browser_session_lock_keys(_step(), _capability("account-a"))
    other = runner._browser_session_lock_keys(_step("account-b"), _capability())

    assert first == same == (("browser-account", "account-a"),)
    assert other == (("browser-account", "account-b"),)
    assert first != other


def test_unbound_browser_work_uses_one_conservative_lane() -> None:
    runner = object.__new__(WorkflowRunner)
    assert runner._browser_session_lock_keys(_step(), _capability()) == (
        ("browser-account", "unbound"),
    )
