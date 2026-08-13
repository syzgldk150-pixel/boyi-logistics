from __future__ import annotations

import asyncio

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
