from __future__ import annotations

from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OperationType,
    Plan,
    PlanStep,
    RiskLevel,
)
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.policy_engine import PolicyEngine, ScheduledAllowlistEntry
from agent.tool_registry import ToolRegistry


class _Catalog:
    catalog_hash = "catalog-hash"

    def __init__(self, capability: dict) -> None:
        self.capability = capability

    def get_capability(self, tool_name: str):
        return self.capability if tool_name == "governed_tool" else None


def _plan(operation_type: OperationType, risk_level: RiskLevel = RiskLevel.LOW) -> Plan:
    return Plan(
        command_type="tool.execute",
        context_fingerprint="context-hash",
        tool_catalog_hash="catalog-hash",
        steps=(
            PlanStep(
                step_key="step_1",
                tool_name="governed_tool",
                tool_version="1.0.0",
                operation_type=operation_type,
                arguments={},
                account_id=None,
                depends_on=(),
                idempotency_key="step-key",
                expected_evidence=(),
                postconditions=(),
                risk_level=risk_level,
            ),
        ),
    )


def _capability(
    operation_type: OperationType,
    *,
    roles: list[str],
    approval: dict | None = None,
) -> dict:
    return {
        "operation_type": operation_type.value,
        "permissions": {"required_roles": roles},
        "approval": approval or {"mode": "none"},
    }


def test_legacy_read_requires_the_governed_tool_role() -> None:
    engine = PolicyEngine(_Catalog(_capability(OperationType.READ, roles=["admin"])))

    denied = engine.evaluate(
        _plan(OperationType.READ),
        Actor(ActorType.LEGACY_API, "legacy-client"),
        source="legacy_api",
    )
    allowed = engine.evaluate(
        _plan(OperationType.READ),
        Actor(ActorType.LEGACY_API, "legacy-client", roles=("admin",)),
        source="legacy_api",
    )

    assert denied.allowed is False
    assert denied.code == "TOOL_PERMISSION_DENIED"
    assert allowed.allowed is True
    assert allowed.requires_approval is False


def test_super_admin_inherits_admin_read_permission() -> None:
    engine = PolicyEngine(_Catalog(_capability(OperationType.READ, roles=["admin"])))

    decision = engine.evaluate(
        _plan(OperationType.READ),
        Actor(ActorType.CONSOLE_ADMIN, "admin-1", roles=("super_admin",)),
        source="console",
    )

    assert decision.allowed is True


def test_feishu_user_can_submit_governed_read_without_console_role() -> None:
    engine = PolicyEngine(_Catalog(_capability(OperationType.READ, roles=["admin"])))

    decision = engine.evaluate(
        _plan(OperationType.READ),
        Actor(ActorType.FEISHU_USER, "open-id"),
        source="feishu",
    )

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_high_risk_write_submission_is_separate_from_super_admin_approval() -> None:
    engine = PolicyEngine(
        _Catalog(
            _capability(
                OperationType.EXTERNAL_WRITE,
                roles=["super_admin"],
                approval={"mode": "required", "required_role": "super_admin"},
            )
        )
    )

    decision = engine.evaluate(
        _plan(OperationType.EXTERNAL_WRITE, RiskLevel.HIGH),
        Actor(ActorType.FEISHU_USER, "open-id"),
        source="feishu",
    )

    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.required_role == "super_admin"


def test_finance_scheduler_fanout_validates_against_real_catalog_and_allowlist() -> None:
    catalog = ToolRegistry()
    arguments = {"mode": "sync", "rescan_days": 7}
    command = Command(
        command_type="tool.execute",
        source="scheduler",
        actor=Actor(ActorType.SCHEDULER, "finance_startup_catchup", roles=("system",)),
        parameters={"tool_name": "sync_finance_bills", "arguments": arguments},
        idempotency_key="scheduler:finance_startup_catchup:2026-08-13T00:00:00+08:00",
    )
    context = ContextSnapshot(
        values={"accounts": [{"account_id": value} for value in (
            "price_default",
            "ronghui_daxiang_s",
            "ronghui_self_pickup_problem",
            "yunda_default",
        )]},
        account_ids=(
            "price_default",
            "ronghui_daxiang_s",
            "ronghui_self_pickup_problem",
            "yunda_default",
        ),
    )

    plan = DeterministicPlanner(catalog).plan(command, context)
    PlanValidator(catalog).validate(plan, context)
    capability = catalog.get_capability("sync_finance_bills")
    policy = PolicyEngine(
        catalog,
        scheduler_allowlist=(
            ScheduledAllowlistEntry.from_arguments(
                task_id="finance_startup_catchup",
                tool_name="sync_finance_bills",
                tool_version=str(capability["version"]),
                arguments=arguments,
                cron_expression="@startup",
            ),
        ),
    )
    decision = policy.evaluate(
        plan,
        command.actor,
        source=command.source,
        execution_context={
            "task_id": "finance_startup_catchup",
            "scheduled_for": "2026-08-13T00:00:00+08:00",
            "cron_expression": "@startup",
        },
    )

    assert plan.steps[0].account_id is None
    assert decision.allowed is True
    assert decision.requires_approval is False
