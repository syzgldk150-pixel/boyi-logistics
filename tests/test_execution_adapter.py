from __future__ import annotations

import asyncio

import pytest

from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import OperationType, PlanStep, RiskLevel, sha256_json


class _Catalog:
    def __init__(self, capability):
        self.capability = capability

    def get_capability(self, tool_name):
        return self.capability if tool_name == "write_tool" else None


class _Executor:
    def __init__(self, result):
        self.result = result

    async def execute(self, capability, arguments):
        return self.result

    def running_tool_info(self, tool_name):
        return {"started_at": "2026-08-13T00:00:00Z"}


class _CapturingExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, capability, arguments, **kwargs):
        self.calls.append((capability, arguments, kwargs))
        return {"success": True, "data": {"ok": True}}

    def running_tool_info(self, tool_name):
        return {"started_at": "2026-08-13T00:00:00Z"}


class _R7Catalog:
    def get_capability(self, tool_name):
        if tool_name != "r7_arrival_checkin":
            return None
        return {
            "name": tool_name,
            "version": "1.0.0",
            "timeout": 30,
            "evidence": [],
            "postconditions": [],
        }


def _step() -> PlanStep:
    return PlanStep(
        step_key="write",
        tool_name="write_tool",
        tool_version="1.0.0",
        operation_type=OperationType.EXTERNAL_WRITE,
        arguments={"entity_id": "entity-1"},
        account_id="account-1",
        depends_on=(),
        idempotency_key="write-1",
        expected_evidence=(),
        postconditions=({"name": "executor_reported_success"},),
        risk_level=RiskLevel.HIGH,
        requires_approval=True,
    )


def _capability():
    return {
        "version": "1.0.0",
        "timeout": 30,
        "evidence": [{"required": True, "source_system": "external", "pagination_complete": False}],
        "postconditions": [{"name": "executor_reported_success"}],
    }


def _r7_step() -> PlanStep:
    return PlanStep(
        step_key="r7-arrival",
        tool_name="r7_arrival_checkin",
        tool_version="1.0.0",
        operation_type=OperationType.EXTERNAL_WRITE,
        arguments={"account_id": "r7_default", "headless": True},
        account_id="r7_default",
        depends_on=(),
        idempotency_key="r7-arrival-0900",
        expected_evidence=(),
        postconditions=(),
        risk_level=RiskLevel.HIGH,
        requires_approval=True,
    )


def _trusted_r7_execution_context():
    return {
        "source": "scheduler",
        "actor": {
            "actor_type": "scheduler",
            "actor_id": "r7_arrival_checkin_0900",
            "roles": ["system"],
        },
        "task_id": "r7_arrival_checkin_0900",
        "configuration_version": 7,
        "scheduled_for": "2026-08-14T09:00:00+08:00",
        "cron_expression": "0 9 * * *",
    }


def test_valid_r7_scheduler_metadata_uses_private_executor_side_channel_only():
    executor = _CapturingExecutor()
    step = _r7_step()
    original_arguments = dict(step.arguments)
    adapter = RegisteredToolExecutionAdapter(catalog=_R7Catalog(), executor=executor)

    result = asyncio.run(
        adapter.execute_step(
            step,
            run_id="run-r7",
            step_id="step-r7",
            execution_context=_trusted_r7_execution_context(),
        )
    )

    assert result["status"] == "SUCCESS"
    assert step.arguments == original_arguments
    _, executed_arguments, kwargs = executor.calls[0]
    assert executed_arguments == original_arguments
    assert set(executed_arguments).isdisjoint(
        {"task_id", "scheduled_for", "configuration_version", "source", "actor"}
    )
    assert kwargs["trusted_scheduler_context"] == {
        "schema_version": 1,
        "source": "scheduler",
        "actor_type": "scheduler",
        "actor_id": "r7_arrival_checkin_0900",
        "task_id": "r7_arrival_checkin_0900",
        "configuration_version": 7,
        "scheduled_for": "2026-08-14T09:00:00+08:00",
        "cron_expression": "0 9 * * *",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source", "console"),
        ("task_id", "r7_arrival_checkin_0915"),
        ("configuration_version", True),
        ("configuration_version", 0),
        ("configuration_version", "7"),
        ("scheduled_for", "2026-08-14T09:00:00"),
        ("scheduled_for", "2026-08-14T09:00:01+08:00"),
        ("scheduled_for", "2026-08-14T09:30:00+08:00"),
        ("cron_expression", "30 9 * * *"),
        ("cron_expression", "0 9 * * 1-5"),
    ),
)
def test_invalid_or_non_scheduler_r7_context_is_not_forwarded(field, value):
    executor = _CapturingExecutor()
    context = _trusted_r7_execution_context()
    context[field] = value
    adapter = RegisteredToolExecutionAdapter(catalog=_R7Catalog(), executor=executor)

    asyncio.run(
        adapter.execute_step(
            _r7_step(),
            run_id="run-r7",
            step_id="step-r7",
            execution_context=context,
        )
    )

    assert executor.calls[0][2] == {}


@pytest.mark.parametrize(
    ("actor_field", "value"),
    (
        ("actor_type", "console_admin"),
        ("actor_id", "another-task"),
        ("roles", []),
        ("roles", ["system", "super_admin"]),
    ),
)
def test_forged_scheduler_actor_is_not_forwarded(actor_field, value):
    executor = _CapturingExecutor()
    context = _trusted_r7_execution_context()
    context["actor"][actor_field] = value
    adapter = RegisteredToolExecutionAdapter(catalog=_R7Catalog(), executor=executor)

    asyncio.run(
        adapter.execute_step(
            _r7_step(),
            run_id="run-r7",
            step_id="step-r7",
            execution_context=context,
        )
    )

    assert executor.calls[0][2] == {}


def test_exit_success_without_explicit_business_success_has_no_postcondition_proof():
    adapter = RegisteredToolExecutionAdapter(
        catalog=_Catalog(_capability()),
        executor=_Executor({"success": True, "data": {"message": "process exited"}}),
    )

    result = asyncio.run(
        adapter.execute_step(
            _step(),
            run_id="run-1",
            step_id="step-1",
            execution_context={"source": "console"},
        )
    )

    assert result["status"] == "SUCCESS"
    assert "postconditions" not in result["meta"]
    assert "postcondition_evidence" not in result["meta"]


def test_explicit_nested_success_is_bound_to_the_actual_result_hash():
    payload = {"ok": True, "entity_id": "entity-1", "state": "updated"}
    adapter = RegisteredToolExecutionAdapter(
        catalog=_Catalog(_capability()),
        executor=_Executor({"success": True, "data": payload}),
    )

    result = asyncio.run(
        adapter.execute_step(
            _step(),
            run_id="run-1",
            step_id="step-1",
            execution_context={"source": "console"},
        )
    )

    digest = sha256_json(result["data"])
    proof = result["meta"]["postcondition_evidence"]["0"]
    assert result["meta"]["postconditions"] == {"0": True}
    assert proof["condition"] == "executor_reported_success"
    assert proof["details"] == {"result_sha256": digest}
    assert proof["evidence_ref"] == f"tool-result:write_tool:{digest}"
    assert proof["evidence_ref"] in result["meta"]["evidence_refs"]
    assert proof["observed_at"] == result["meta"]["observed_at"]


def test_cancelled_process_is_not_normalized_as_a_terminal_tool_failure():
    adapter = RegisteredToolExecutionAdapter(
        catalog=_Catalog(_capability()),
        executor=_Executor(
            {
                "success": False,
                "canceled": True,
                "error": "Tool execution cancelled",
            }
        ),
    )

    result = asyncio.run(
        adapter.execute_step(
            _step(),
            run_id="run-1",
            step_id="step-1",
            execution_context={"source": "console"},
        )
    )

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "CANCELLED"
    assert result["error"]["retryable"] is False


def test_british_spelling_cancelled_process_is_normalized_to_cancelled():
    adapter = RegisteredToolExecutionAdapter(
        catalog=_Catalog(_capability()),
        executor=_Executor(
            {
                "success": False,
                "cancelled": True,
                "error": "Tool execution cancelled",
            }
        ),
    )

    result = asyncio.run(
        adapter.execute_step(
            _step(),
            run_id="run-1",
            step_id="step-1",
            execution_context={"source": "console"},
        )
    )

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "CANCELLED"
    assert result["error"]["retryable"] is False


def test_nested_unified_retryable_failure_is_preserved():
    nested_result = {
        "status": "FAILED",
        "data": {},
        "meta": {},
        "warnings": [],
        "error": {
            "code": "TRANSIENT_SOURCE_FAILURE",
            "message": "source is temporarily unavailable",
            "retryable": True,
        },
    }
    adapter = RegisteredToolExecutionAdapter(
        catalog=_Catalog(_capability()),
        executor=_Executor(
            {
                "success": False,
                "error": "source is temporarily unavailable",
                "error_code": "TRANSIENT_SOURCE_FAILURE",
                "retryable": True,
                "data": nested_result,
            }
        ),
    )

    result = asyncio.run(
        adapter.execute_step(
            _step(),
            run_id="run-1",
            step_id="step-1",
            execution_context={"source": "scheduler"},
        )
    )

    assert result == nested_result
