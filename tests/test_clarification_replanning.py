from __future__ import annotations

import pytest

from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
)
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner


class _Catalog:
    catalog_hash = "catalog-hash"

    @staticmethod
    def get_capability(tool_name: str):
        if tool_name != "account_query":
            return None
        return {
            "version": "1.0.0",
            "operation_type": "read",
            "risk_level": "low",
            "llm_exposed": False,
            "approval": {"mode": "none"},
            "account_scope": {"mode": "single"},
            "evidence": [],
            "postconditions": [],
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "account_id": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["published_to_me", "my_published"],
                    },
                },
                "required": ["account_id", "direction"],
            },
        }

    @staticmethod
    def validate_arguments(_tool_name: str, arguments):
        if set(arguments) != {"account_id", "direction"}:
            raise ValueError("arguments must contain account_id and direction")
        if arguments["direction"] not in {"published_to_me", "my_published"}:
            raise ValueError("direction is outside the governed enum")


def _command() -> Command:
    return Command(
        command_id="command-1",
        command_type="tool.execute",
        source="console",
        actor=Actor(ActorType.CONSOLE_ADMIN, "admin-1", ("admin",)),
        parameters={
            "tool_name": "account_query",
            "arguments": {"direction": "published_to_me"},
            "account_id": None,
        },
        idempotency_key="console:admin-1:tool.execute:request-1",
    )


def _context(*, override, account_ids=("account-1", "account-2")):
    return ContextBuilder(
        account_resolver=lambda _command: [
            {"account_id": account_id, "system": "ronghui"}
            for account_id in account_ids
        ],
        resource_resolver=lambda _command: {
            "clarifications": [{"note": "audit-only"}],
            "clarification_override": override,
        },
    ).build(_command())


def test_structured_clarification_changes_same_command_plan_and_hash() -> None:
    catalog = _Catalog()
    original_context = _context(override={})
    clarified_context = _context(
        override={
            "schema_version": 1,
            "command_id": "command-1",
            "account_id": "account-2",
            "argument_updates": {"direction": "my_published"},
        }
    )

    original_plan = DeterministicPlanner(catalog).plan(_command(), original_context)
    clarified_plan = DeterministicPlanner(catalog).plan(_command(), clarified_context)
    PlanValidator(catalog).validate(clarified_plan, clarified_context)

    assert clarified_plan.steps[0].account_id == "account-2"
    assert clarified_plan.steps[0].arguments == {
        "account_id": "account-2",
        "direction": "my_published",
    }
    assert clarified_plan.context_fingerprint != original_plan.context_fingerprint
    assert clarified_plan.plan_hash != original_plan.plan_hash


def test_note_only_clarification_does_not_change_planning_context() -> None:
    baseline = _context(override={})
    audit_only = ContextBuilder(
        account_resolver=lambda _command: [
            {"account_id": "account-1", "system": "ronghui"},
            {"account_id": "account-2", "system": "ronghui"},
        ],
        resource_resolver=lambda _command: {
            "clarifications": [{"clarification": {"note": "use account-2"}}],
        },
    ).build(_command())

    assert audit_only.fingerprint == baseline.fingerprint
    assert DeterministicPlanner(_Catalog()).plan(
        _command(), audit_only
    ).steps[0].account_id is None


def test_unknown_account_and_schema_invalid_update_fail_closed() -> None:
    catalog = _Catalog()
    unknown_account = _context(
        override={
            "schema_version": 1,
            "command_id": "command-1",
            "account_id": "missing-account",
        },
        account_ids=("account-1",),
    )
    unknown_plan = DeterministicPlanner(catalog).plan(_command(), unknown_account)
    with pytest.raises(OrchestrationError, match="authoritative context") as account_error:
        PlanValidator(catalog).validate(unknown_plan, unknown_account)
    assert account_error.value.code == "UNKNOWN_ACCOUNT"
    assert account_error.value.details["status"] == "NEEDS_CLARIFICATION"

    invalid_update = _context(
        override={
            "schema_version": 1,
            "command_id": "command-1",
            "account_id": "account-1",
            "argument_updates": {"direction": "guessed-value"},
        },
        account_ids=("account-1",),
    )
    invalid_plan = DeterministicPlanner(catalog).plan(_command(), invalid_update)
    with pytest.raises(OrchestrationError, match="input_schema") as argument_error:
        PlanValidator(catalog).validate(invalid_plan, invalid_update)
    assert argument_error.value.code == "INVALID_TOOL_ARGUMENTS"
    assert argument_error.value.details["status"] == "NEEDS_CLARIFICATION"


def test_context_rejects_clarification_from_another_command() -> None:
    with pytest.raises(OrchestrationError, match="current command"):
        _context(
            override={
                "schema_version": 1,
                "command_id": "different-command",
                "account_id": "account-1",
            }
        )


def test_broker_bound_project_skips_legacy_account_schema_validation() -> None:
    class _ProjectCatalog:
        catalog_hash = "project-catalog"
        validate_calls = 0

        @staticmethod
        def get_capability(tool_name: str):
            if tool_name != "automation.arrival_stats.run":
                return None
            return {
                "version": "1.0.0",
                "operation_type": "internal_projection_write",
                "risk_level": "medium",
                "account_scope": {"required": True},
                "evidence": [],
                "_plugin_runtime": {"automation_id": "arrival_stats"},
            }

        def validate_arguments(self, _tool_name: str, _arguments):
            self.validate_calls += 1
            raise ValueError("legacy schema requires broker-only account_id")

    catalog = _ProjectCatalog()
    context = ContextSnapshot(values={})
    step = PlanStep(
        step_key="arrival-stats",
        tool_name="automation.arrival_stats.run",
        tool_version="1.0.0",
        operation_type=OperationType.INTERNAL_PROJECTION_WRITE,
        arguments={"pending_sheet_disabled": True},
        account_id=None,
        depends_on=(),
        idempotency_key="arrival-stats:request-1",
        expected_evidence=(),
        postconditions=(),
        risk_level=RiskLevel.MEDIUM,
    )
    plan = Plan(
        schema_version=1,
        command_type="automation.project.invoke",
        context_fingerprint=context.fingerprint,
        tool_catalog_hash=catalog.catalog_hash,
        steps=(step,),
        impact={},
        automation_id="arrival_stats",
        automation_generation=1,
        automation_contract_hash="a" * 64,
    )

    PlanValidator(catalog).validate(plan, context)

    assert catalog.validate_calls == 0
