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
from agent.orchestration.policy_engine import (
    PolicyEngine,
    ProjectPolicyEvaluation,
    ScheduledAllowlistEntry,
)
from agent.tool_registry import ToolRegistry
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
)
from shared.scheduled_task_contracts import APPROVED_SCHEDULED_TASK_PROFILES


class _Catalog:
    catalog_hash = "catalog-hash"

    def __init__(self, capability: dict, *, tool_name: str = "governed_tool") -> None:
        self.capability = capability
        self.tool_name = tool_name

    def get_capability(self, tool_name: str):
        return self.capability if tool_name == self.tool_name else None


def _plan(
    operation_type: OperationType,
    risk_level: RiskLevel = RiskLevel.LOW,
    *,
    arguments: dict | None = None,
    tool_name: str = "governed_tool",
    tool_version: str = "1.0.0",
) -> Plan:
    return Plan(
        command_type="tool.execute",
        context_fingerprint="context-hash",
        tool_catalog_hash="catalog-hash",
        steps=(
            PlanStep(
                step_key="step_1",
                tool_name=tool_name,
                tool_version=tool_version,
                operation_type=operation_type,
                arguments={} if arguments is None else arguments,
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


def test_current_business_day_allowlist_matches_exact_shanghai_occurrence() -> None:
    entry = ScheduledAllowlistEntry.from_arguments(
        task_id="site_send_0500",
        tool_name="sync_site_send_list",
        tool_version="1.0.0",
        arguments={"account_id": "ronghui_default"},
        dynamic_argument_rules={"target_date": "current_business_day"},
        cron_expression="0 5 * * *",
    )
    context = {
        "scheduled_for": "2026-08-14T21:00:00+00:00",
        "cron_expression": "0 5 * * *",
    }

    assert entry.matches(
        step=_plan(
            OperationType.INTERNAL_PROJECTION_WRITE,
            arguments={
                "account_id": "ronghui_default",
                "target_date": "2026-08-15",
            },
            tool_name="sync_site_send_list",
        ).steps[0],
        execution_context=context,
    )
    assert not entry.matches(
        step=_plan(
            OperationType.INTERNAL_PROJECTION_WRITE,
            arguments={
                "account_id": "ronghui_default",
                "target_date": "2026-08-14",
            },
            tool_name="sync_site_send_list",
        ).steps[0],
        execution_context=context,
    )


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


def test_exact_scan_preview_uses_read_policy_instead_of_static_write_approval() -> None:
    capability = _capability(
        OperationType.INTERNAL_PROJECTION_WRITE,
        roles=["super_admin"],
        approval={"mode": "required", "required_role": "super_admin"},
    )
    capability["_plugin_runtime"] = {
        "automation_id": "scan_codes",
        "plugin_id": "sync_scan_codes",
        "trust_source": "ed25519_first_party",
    }
    engine = PolicyEngine(
        _Catalog(capability, tool_name="automation.scan_codes.run")
    )

    decision = engine.evaluate(
        _plan(
            OperationType.READ,
            RiskLevel.LOW,
            arguments={"dry_run": True},
            tool_name="automation.scan_codes.run",
        ),
        Actor(ActorType.CONSOLE_ADMIN, "admin-1", roles=("admin",)),
        source="console",
    )

    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.risk_level is RiskLevel.LOW


def _clock_policy(group_id: str) -> tuple[PolicyEngine, Plan, Actor, dict]:
    profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
    task_id = next(iter(profile.approved_task_ids))
    cron_expression = (
        "30 18 * * *" if task_id == "clockin_daxiang_1830" else "33 18 * * *"
    )
    capability = _capability(
        OperationType.EXTERNAL_WRITE,
        roles=["super_admin"],
        approval={"mode": "schedule_allowlist", "required_role": "super_admin"},
    )
    entry = ScheduledAllowlistEntry.from_arguments(
        task_id=task_id,
        tool_name="clock_in_dual",
        tool_version="1.1.0",
        arguments=profile.approved_arguments,
        cron_expression=cron_expression,
    )
    return (
        PolicyEngine(
            _Catalog(capability, tool_name="clock_in_dual"),
            scheduler_allowlist=(entry,),
        ),
        _plan(
            OperationType.EXTERNAL_WRITE,
            RiskLevel.HIGH,
            arguments=dict(profile.approved_arguments),
            tool_name="clock_in_dual",
            tool_version="1.1.0",
        ),
        Actor(ActorType.SCHEDULER, task_id, roles=("system",)),
        {
            "task_id": task_id,
            "scheduled_for": (
                "2026-08-13T18:30:00+08:00"
                if task_id == "clockin_daxiang_1830"
                else "2026-08-13T18:33:00+08:00"
            ),
            "cron_expression": cron_expression,
        },
    )


def test_exact_two_clock_schedules_bypass_only_the_separate_approval() -> None:
    for group_id in ("clockin_daxiang", "clockin_daxiang_s"):
        engine, plan, actor, execution_context = _clock_policy(group_id)

        decision = engine.evaluate(
            plan,
            actor,
            source="scheduler",
            execution_context=execution_context,
        )

        assert decision.allowed is True
        assert decision.requires_approval is False
        assert decision.required_role is None
        assert decision.code == "ALLOWED"


def test_clock_schedule_change_matrix_requires_super_admin_approval() -> None:
    engine, exact_plan, exact_actor, exact_context = _clock_policy("clockin_daxiang")
    exact_arguments = dict(exact_plan.steps[0].arguments)
    changed_arguments = {
        "account": {**exact_arguments, "account_id": "ronghui_daxiang_s"},
        "site": {**exact_arguments, "sitecode": "7390017"},
        "delay": {**exact_arguments, "delay_seconds": 3},
        "extra": {**exact_arguments, "extra": "not-approved"},
    }
    cases = {
        "task_id": (
            exact_plan,
            Actor(ActorType.SCHEDULER, "clockin_daxiang_1831", roles=("system",)),
            {**exact_context, "task_id": "clockin_daxiang_1831"},
        ),
        "actor_id": (
            exact_plan,
            Actor(ActorType.SCHEDULER, "different-scheduler", roles=("system",)),
            exact_context,
        ),
        "version": (
            _plan(
                OperationType.EXTERNAL_WRITE,
                RiskLevel.HIGH,
                arguments=exact_arguments,
                tool_name="clock_in_dual",
                tool_version="1.1.1",
            ),
            exact_actor,
            exact_context,
        ),
        "cron": (
            exact_plan,
            exact_actor,
            {**exact_context, "cron_expression": "31 18 * * *"},
        ),
        "scheduled_time": (
            exact_plan,
            exact_actor,
            {**exact_context, "scheduled_for": "2026-08-13T18:31:00+08:00"},
        ),
        "scheduled_for_missing": (
            exact_plan,
            exact_actor,
            {key: value for key, value in exact_context.items() if key != "scheduled_for"},
        ),
        "scheduled_for_naive": (
            exact_plan,
            exact_actor,
            {**exact_context, "scheduled_for": "2026-08-13T18:30:00"},
        ),
        **{
            label: (
                _plan(
                    OperationType.EXTERNAL_WRITE,
                    RiskLevel.HIGH,
                    arguments=arguments,
                    tool_name="clock_in_dual",
                    tool_version="1.1.0",
                ),
                exact_actor,
                exact_context,
            )
            for label, arguments in changed_arguments.items()
        },
    }

    for label, (plan, actor, execution_context) in cases.items():
        decision = engine.evaluate(
            plan,
            actor,
            source="scheduler",
            execution_context=execution_context,
        )

        assert decision.allowed is True, label
        assert decision.requires_approval is True, label
        assert decision.required_role == "super_admin", label
        assert decision.code == "APPROVAL_REQUIRED", label


def test_exact_clock_arguments_from_non_scheduler_sources_require_approval() -> None:
    engine, plan, scheduler_actor, execution_context = _clock_policy("clockin_daxiang")
    cases = (
        (scheduler_actor, "manual"),
        (scheduler_actor, "console"),
        (Actor(ActorType.CONSOLE_ADMIN, "admin-1", roles=("super_admin",)), "console"),
        (Actor(ActorType.FEISHU_USER, "open-id"), "feishu"),
        (Actor(ActorType.LEGACY_API, "legacy-client", roles=("super_admin",)), "legacy_api"),
    )

    for actor, source in cases:
        decision = engine.evaluate(
            plan,
            actor,
            source=source,
            execution_context=execution_context,
        )

        assert decision.allowed is True, source
        assert decision.requires_approval is True, source
        assert decision.required_role == "super_admin", source


def test_finance_scheduler_fanout_validates_against_real_catalog_and_allowlist() -> None:
    catalog = ToolRegistry()
    arguments = {
        "mode": "sync",
        "platform": "ronghui",
        "rescan_days": 7,
        "_startup_catchup": True,
    }
    command = Command(
        command_type="tool.execute",
        source="scheduler",
        actor=Actor(ActorType.SCHEDULER, "finance_startup_catchup", roles=("system",)),
        parameters={"tool_name": "sync_finance_bills", "arguments": arguments},
        idempotency_key="scheduler:finance_startup_catchup:2026-08-13T00:10:00+08:00",
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
            "scheduled_for": "2026-08-13T00:10:00+08:00",
            "cron_expression": "@startup",
        },
    )

    assert plan.steps[0].account_id is None
    assert decision.allowed is True
    assert decision.requires_approval is False

    for scheduled_for in (
        "2026-08-13T00:10:00",
        "2026-08-13T00:09:00+08:00",
        "2026-08-13T00:10:01+08:00",
        "2026-08-12T16:10:00+00:00",
    ):
        invalid = policy.evaluate(
            plan,
            command.actor,
            source=command.source,
            execution_context={
                "task_id": "finance_startup_catchup",
                "scheduled_for": scheduled_for,
                "cron_expression": "@startup",
            },
        )
        if scheduled_for.endswith("+00:00"):
            # 16:10 UTC is exactly 00:10 in the locked Asia/Shanghai zone.
            assert invalid.requires_approval is False
        else:
            assert invalid.requires_approval is True


def test_scheduler_allowlist_provider_is_reloaded_for_each_policy_evaluation() -> None:
    calls = 0
    entry = ScheduledAllowlistEntry.from_arguments(
        task_id="persisted_task_0900",
        tool_name="governed_tool",
        tool_version="1.0.0",
        arguments={},
        cron_expression="0 9 * * *",
    )

    def provider():
        nonlocal calls
        calls += 1
        return (entry,) if calls == 1 else ()

    engine = PolicyEngine(
        _Catalog(
            _capability(
                OperationType.INTERNAL_PROJECTION_WRITE,
                roles=["admin"],
                approval={"mode": "schedule_allowlist", "required_role": "admin"},
            )
        ),
        scheduler_allowlist_provider=provider,
    )
    context = {
        "task_id": "persisted_task_0900",
        "scheduled_for": "2026-08-13T09:00:00+08:00",
        "cron_expression": "0 9 * * *",
    }
    actor = Actor(ActorType.SCHEDULER, "persisted_task_0900", roles=("system",))

    first = engine.evaluate(
        _plan(OperationType.INTERNAL_PROJECTION_WRITE, RiskLevel.MEDIUM),
        actor,
        source="scheduler",
        execution_context=context,
    )
    second = engine.evaluate(
        _plan(OperationType.INTERNAL_PROJECTION_WRITE, RiskLevel.MEDIUM),
        actor,
        source="scheduler",
        execution_context=context,
    )

    assert calls == 2
    assert first.requires_approval is False
    assert second.requires_approval is True


def test_scheduler_allowlist_provider_failure_requires_approval() -> None:
    def unavailable():
        raise RuntimeError("repository unavailable")

    engine = PolicyEngine(
        _Catalog(
            _capability(
                OperationType.INTERNAL_PROJECTION_WRITE,
                roles=["admin"],
                approval={"mode": "schedule_allowlist", "required_role": "admin"},
            )
        ),
        scheduler_allowlist_provider=unavailable,
    )

    decision = engine.evaluate(
        _plan(OperationType.INTERNAL_PROJECTION_WRITE, RiskLevel.MEDIUM),
        Actor(ActorType.SCHEDULER, "persisted_task_0900", roles=("system",)),
        source="scheduler",
        execution_context={
            "task_id": "persisted_task_0900",
            "scheduled_for": "2026-08-13T09:00:00+08:00",
            "cron_expression": "0 9 * * *",
        },
    )

    assert decision.allowed is True
    assert decision.requires_approval is True


def test_configuration_version_must_match_the_loaded_scheduler_occurrence() -> None:
    entry = ScheduledAllowlistEntry.from_arguments(
        task_id="persisted_task_0900",
        tool_name="governed_tool",
        tool_version="1.0.0",
        arguments={},
        cron_expression="0 9 * * *",
        configuration_version=7,
    )
    engine = PolicyEngine(
        _Catalog(
            _capability(
                OperationType.INTERNAL_PROJECTION_WRITE,
                roles=["admin"],
                approval={"mode": "schedule_allowlist", "required_role": "admin"},
            )
        ),
        scheduler_allowlist=(entry,),
    )
    actor = Actor(ActorType.SCHEDULER, "persisted_task_0900", roles=("system",))
    base_context = {
        "task_id": "persisted_task_0900",
        "scheduled_for": "2026-08-13T09:00:00+08:00",
        "cron_expression": "0 9 * * *",
    }

    exact = engine.evaluate(
        _plan(OperationType.INTERNAL_PROJECTION_WRITE, RiskLevel.MEDIUM),
        actor,
        source="scheduler",
        execution_context={**base_context, "configuration_version": 7},
    )
    stale = engine.evaluate(
        _plan(OperationType.INTERNAL_PROJECTION_WRITE, RiskLevel.MEDIUM),
        actor,
        source="scheduler",
        execution_context={**base_context, "configuration_version": 6},
    )
    missing = engine.evaluate(
        _plan(OperationType.INTERNAL_PROJECTION_WRITE, RiskLevel.MEDIUM),
        actor,
        source="scheduler",
        execution_context=base_context,
    )

    assert exact.requires_approval is False
    assert stale.requires_approval is True
    assert missing.requires_approval is True
    assert stale.code == "APPROVAL_REQUIRED"


def test_scheduler_allowlist_requires_exact_persisted_arguments_hash() -> None:
    entry = ScheduledAllowlistEntry.from_arguments(
        task_id="persisted_task_0900",
        tool_name="governed_tool",
        tool_version="1.0.0",
        arguments={"account_id": "account-a"},
        cron_expression="0 9 * * *",
    )
    engine = PolicyEngine(
        _Catalog(
            _capability(
                OperationType.INTERNAL_PROJECTION_WRITE,
                roles=["admin"],
                approval={"mode": "schedule_allowlist", "required_role": "admin"},
            )
        ),
        scheduler_allowlist_provider=lambda: (entry,),
    )

    decision = engine.evaluate(
        _plan(
            OperationType.INTERNAL_PROJECTION_WRITE,
            RiskLevel.MEDIUM,
            arguments={"account_id": "account-b"},
        ),
        Actor(ActorType.SCHEDULER, "persisted_task_0900", roles=("system",)),
        source="scheduler",
        execution_context={
            "task_id": "persisted_task_0900",
            "scheduled_for": "2026-08-13T09:00:00+08:00",
            "cron_expression": "0 9 * * *",
        },
    )

    assert decision.requires_approval is True


def _project_invocation() -> AutomationProjectInvocation:
    return AutomationProjectInvocation(
        automation_id="instance-one",
        automation_generation=1,
        entrypoint=AutomationEntrypoint.CONSOLE,
        contract_id="console",
        contract_hash="a" * 64,
        policy_version=1,
        project_configuration_version=1,
        request_id="request-one",
    )


def _project_plan(operation_type: OperationType) -> Plan:
    base = _plan(operation_type)
    return Plan(
        command_type="automation.project.invoke",
        context_fingerprint=base.context_fingerprint,
        tool_catalog_hash=base.tool_catalog_hash,
        steps=base.steps,
        automation_id="instance-one",
        automation_generation=1,
        automation_contract_hash="a" * 64,
    )


def test_project_require_each_run_forces_approval_even_for_read_none() -> None:
    engine = PolicyEngine(
        _Catalog(_capability(OperationType.READ, roles=["admin"])),
        project_policy_provider=lambda *_args: ProjectPolicyEvaluation(
            allowed=True,
            requires_approval=True,
            code="PROJECT_APPROVAL_REQUIRED",
            reason="project requires approval",
        ),
    )
    decision = engine.evaluate(
        _project_plan(OperationType.READ),
        Actor(ActorType.CONSOLE_ADMIN, "admin-one", roles=("admin",)),
        source="console",
        automation_invocation=_project_invocation(),
    )
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.required_role == "super_admin"


def test_project_full_auto_can_remove_required_approval_below_the_safety_ceiling() -> None:
    engine = PolicyEngine(
        _Catalog(
            _capability(
                OperationType.EXTERNAL_WRITE,
                roles=["super_admin"],
                approval={"mode": "required", "required_role": "super_admin"},
            )
        ),
        project_policy_provider=lambda *_args: ProjectPolicyEvaluation(
            allowed=True,
            requires_approval=False,
            code="PROJECT_FULL_AUTO",
            reason="exact committed project contract",
        ),
    )
    decision = engine.evaluate(
        _project_plan(OperationType.EXTERNAL_WRITE),
        Actor(ActorType.CONSOLE_ADMIN, "admin-one", roles=("admin",)),
        source="console",
        automation_invocation=_project_invocation(),
    )
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_project_full_auto_can_override_signed_destructive_or_extreme_ceiling() -> None:
    calls = 0

    def project_policy(*_args):
        nonlocal calls
        calls += 1
        return ProjectPolicyEvaluation(
            allowed=True,
            requires_approval=False,
            code="PROJECT_FULL_AUTO",
            reason="exact committed project contract",
        )

    engine = PolicyEngine(
        _Catalog(
            _capability(
                OperationType.DESTRUCTIVE,
                roles=["super_admin"],
                approval={"mode": "required", "required_role": "super_admin"},
            )
        ),
        project_policy_provider=project_policy,
    )
    for plan in (
        _project_plan(OperationType.DESTRUCTIVE),
        Plan(
            command_type="automation.project.invoke",
            context_fingerprint="context-hash",
            tool_catalog_hash="catalog-hash",
            steps=(
                PlanStep(
                    step_key="step_1",
                    tool_name="governed_tool",
                    tool_version="1.0.0",
                    operation_type=OperationType.DESTRUCTIVE,
                    arguments={},
                    account_id=None,
                    depends_on=(),
                    idempotency_key="step-key",
                    expected_evidence=(),
                    postconditions=(),
                    risk_level=RiskLevel.EXTREME,
                ),
            ),
            automation_id="instance-one",
            automation_generation=1,
            automation_contract_hash="a" * 64,
        ),
    ):
        decision = engine.evaluate(
            plan,
            Actor(
                ActorType.CONSOLE_ADMIN,
                "admin-one",
                roles=("super_admin",),
            ),
            source="console",
            automation_invocation=_project_invocation(),
        )
        assert decision.allowed is True
        assert decision.requires_approval is False
    assert calls == 2


def test_unsigned_destructive_operation_keeps_the_hard_ceiling() -> None:
    engine = PolicyEngine(
        _Catalog(
            _capability(
                OperationType.DESTRUCTIVE,
                roles=["super_admin"],
                approval={"mode": "required", "required_role": "super_admin"},
            )
        )
    )

    decision = engine.evaluate(
        _plan(OperationType.DESTRUCTIVE, RiskLevel.EXTREME),
        Actor(ActorType.CONSOLE_ADMIN, "admin-one", roles=("super_admin",)),
        source="console",
    )

    assert decision.allowed is False
    assert decision.code == "OPERATION_DISABLED"
