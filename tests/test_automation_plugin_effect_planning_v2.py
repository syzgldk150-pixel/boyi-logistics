from __future__ import annotations

from dataclasses import replace

import pytest

from agent.automation_plugins.host_capability_registry import governance_for_effect
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OperationType,
    OrchestrationError,
    RiskLevel,
    sha256_json,
)
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.workflow_runner import WorkflowRunner
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    CompiledAutomationProjectContract,
    InvocationArgumentContract,
)


_TOOL_NAME = "automation.mixed_effects.run"


def _governance(effect: str) -> dict[str, object]:
    return governance_for_effect(effect).to_mapping()


class _Catalog:
    def __init__(self) -> None:
        read = _governance("read")
        write = _governance("internal_write")
        self.capability = {
            "name": _TOOL_NAME,
            "version": "1.1.0",
            "description": "Mixed-effect Service v2 project",
            **write,
            "permissions": [],
            "account_scope": {"required": False},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "llm_exposed": False,
            "_plugin_runtime": {
                "automation_id": "mixed_effects",
                "runtime_model": "SERVICE_V2",
                "compiled_invocations": {
                    "inspect": {
                        "arguments": {},
                        "dynamic_resolvers": {},
                        "target": {
                            "service": "plugin.mixed_effects.runner@1",
                            "operation": "inspect",
                            "contribution_id": "inspect",
                            "contribution_kind": "console",
                        },
                        "governance": read,
                    },
                    "apply": {
                        "arguments": {},
                        "dynamic_resolvers": {},
                        "target": {
                            "service": "plugin.mixed_effects.runner@1",
                            "operation": "apply",
                            "contribution_id": "apply",
                            "contribution_kind": "console",
                        },
                        "governance": write,
                    },
                },
            },
        }

    @property
    def catalog_hash(self) -> str:
        return sha256_json(self.capability)

    def get_capability(self, tool_name: str):
        return self.capability if tool_name == _TOOL_NAME else None

    @staticmethod
    def validate_arguments(_tool_name: str, _arguments: object) -> None:
        return None


def _invocation(contribution_id: str) -> AutomationProjectInvocation:
    return AutomationProjectInvocation(
        automation_id="mixed_effects",
        automation_generation=1,
        entrypoint=AutomationEntrypoint.CONSOLE,
        contract_id=contribution_id,
        contract_hash="a" * 64,
        policy_version=1,
        project_configuration_version=1,
        request_id=f"request-{contribution_id}",
    )


def _command(contribution_id: str) -> Command:
    return Command(
        command_type="automation.project.invoke",
        source="console",
        actor=Actor(ActorType.CONSOLE_ADMIN, "admin-one", ("admin",)),
        parameters={
            "tool_name": _TOOL_NAME,
            "arguments": {},
            "execution_context": {"contribution_id": contribution_id},
        },
        idempotency_key=f"mixed-effects:{contribution_id}",
        automation_invocation=_invocation(contribution_id),
    )


@pytest.mark.parametrize(
    ("contribution_id", "operation_type", "risk_level", "evidence_required"),
    (
        ("inspect", OperationType.READ, RiskLevel.LOW, False),
        ("apply", OperationType.INTERNAL_PROJECTION_WRITE, RiskLevel.MEDIUM, True),
    ),
)
def test_planner_uses_exact_service_v2_contribution_governance(
    contribution_id: str,
    operation_type: OperationType,
    risk_level: RiskLevel,
    evidence_required: bool,
) -> None:
    plan = DeterministicPlanner(_Catalog()).plan(
        _command(contribution_id),
        ContextSnapshot(values={}),
    )
    step = plan.steps[0]

    assert step.operation_type is operation_type
    assert step.risk_level is risk_level
    assert step.expected_evidence[0]["required"] is evidence_required
    assert bool(step.postconditions) is evidence_required


def test_compiled_policy_rejects_plan_governance_drift() -> None:
    governance = _governance("read")
    invocation = _invocation("inspect")
    plan = DeterministicPlanner(_Catalog()).plan(
        _command("inspect"),
        ContextSnapshot(values={}),
    )
    contract = CompiledAutomationProjectContract(
        automation_id="mixed_effects",
        automation_generation=1,
        manifest_sha256="b" * 64,
        tool_name=_TOOL_NAME,
        tool_version="1.1.0",
        operation_type="external_write",
        risk_level="high",
        invocation_contracts={
            "inspect": InvocationArgumentContract(
                contract_id="inspect",
                entrypoint="console",
                expected_arguments={},
                dynamic_argument_resolvers={},
                contribution_id="inspect",
                governance=governance,
            )
        },
        account_bindings={},
        allowed_entrypoints=frozenset({"console"}),
        contract_hash="a" * 64,
        tool_contract_hash="c" * 64,
        plugin_contract_hash="d" * 64,
        project_configuration_version=1,
        snapshot={"automation_id": "mixed_effects"},
        can_full_auto=True,
    )

    assert contract.matches_plan(plan, invocation, source="console")
    drifted_step = replace(plan.steps[0], risk_level=RiskLevel.HIGH)
    drifted_plan = replace(plan, steps=(drifted_step,))
    assert not contract.matches_plan(drifted_plan, invocation, source="console")


def test_planner_fails_closed_when_contribution_governance_is_missing() -> None:
    catalog = _Catalog()
    del catalog.capability["_plugin_runtime"]["compiled_invocations"]["inspect"][
        "governance"
    ]

    with pytest.raises(OrchestrationError) as raised:
        DeterministicPlanner(catalog).plan(
            _command("inspect"),
            ContextSnapshot(values={}),
        )

    assert raised.value.code == "PROJECT_INVOCATION_STALE"


class _RetryRunRepository:
    def __init__(self, operation_type: str) -> None:
        self.run = {
            "command_id": "command-one",
            "execution_attempt_count": 1,
            "plan_json": {
                "steps": [
                    {
                        "tool_name": _TOOL_NAME,
                        "operation_type": operation_type,
                    }
                ]
            },
        }

    def get_run(self, _run_id: str):
        return self.run


@pytest.mark.parametrize(
    ("contribution_id", "operation_type", "retry_allowed"),
    (
        ("inspect", "read", True),
        ("apply", "internal_projection_write", False),
    ),
)
def test_workflow_retry_uses_exact_contribution_governance(
    contribution_id: str,
    operation_type: str,
    retry_allowed: bool,
) -> None:
    runner = object.__new__(WorkflowRunner)
    runner._repository = _RetryRunRepository(operation_type)
    runner._catalog = _Catalog()
    runner._load_command = lambda _command_id: _command(contribution_id)

    assert runner._can_retry_run("run-one") is retry_allowed
