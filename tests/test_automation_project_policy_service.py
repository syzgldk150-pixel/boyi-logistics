from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase

from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    CommandReceipt,
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
    RunStatus,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    CompiledAutomationProjectContract,
    InvocationArgumentContract,
)
from shared.account_execution_locks import AccountExecutionLockUnavailable
from shared.orchestration_repository import InvalidStateError


AUTOMATION_ID = "instance-one"
CONTRACT_HASH = "a" * 64
TOOL_HASH = "b" * 64
PLUGIN_HASH = "c" * 64
MANIFEST_HASH = "d" * 64


def _admin() -> Actor:
    return Actor(
        actor_type=ActorType.CONSOLE_ADMIN,
        actor_id="admin-one",
        roles=("admin", "super_admin"),
        display_name="Administrator",
        authenticated_by="mysql_admin_session",
    )


def _contract() -> CompiledAutomationProjectContract:
    return CompiledAutomationProjectContract(
        automation_id=AUTOMATION_ID,
        automation_generation=1,
        manifest_sha256=MANIFEST_HASH,
        tool_name=f"automation.{AUTOMATION_ID}.run",
        tool_version="1.0.0",
        operation_type=OperationType.EXTERNAL_WRITE.value,
        risk_level=RiskLevel.HIGH.value,
        invocation_contracts={
            "console": InvocationArgumentContract(
                contract_id="console",
                entrypoint="console",
                expected_arguments={"mode": "saved"},
                dynamic_argument_resolvers={},
            )
        },
        account_bindings={"primary": "account-one"},
        allowed_entrypoints=frozenset({"console"}),
        contract_hash=CONTRACT_HASH,
        tool_contract_hash=TOOL_HASH,
        plugin_contract_hash=PLUGIN_HASH,
        project_configuration_version=1,
        snapshot={"automation_id": AUTOMATION_ID},
        can_full_auto=True,
    )


def _contract_for(
    entrypoint: AutomationEntrypoint,
    *,
    dynamic_resolvers: dict[str, str] | None = None,
) -> CompiledAutomationProjectContract:
    source = entrypoint.value
    contract_id = "scheduler:schedule-one" if entrypoint is AutomationEntrypoint.SCHEDULER else source
    return replace(
        _contract(),
        invocation_contracts={
            contract_id: InvocationArgumentContract(
                contract_id=contract_id,
                entrypoint=source,
                expected_arguments={"mode": "saved"},
                dynamic_argument_resolvers=dynamic_resolvers or {},
            )
        },
        allowed_entrypoints=frozenset({source}),
    )


def _invocation(*, request_id: str = "invoke-one") -> AutomationProjectInvocation:
    return AutomationProjectInvocation(
        automation_id=AUTOMATION_ID,
        automation_generation=1,
        entrypoint=AutomationEntrypoint.CONSOLE,
        contract_id="console",
        contract_hash=CONTRACT_HASH,
        policy_version=1,
        project_configuration_version=1,
        request_id=request_id,
    )


def _plan(invocation: AutomationProjectInvocation) -> Plan:
    return Plan(
        command_type="automation.project.invoke",
        context_fingerprint="context-one",
        tool_catalog_hash="catalog-one",
        steps=(
            PlanStep(
                step_key="execute",
                tool_name=f"automation.{AUTOMATION_ID}.run",
                tool_version="1.0.0",
                operation_type=OperationType.EXTERNAL_WRITE,
                arguments={"mode": "saved"},
                account_id=None,
                depends_on=(),
                idempotency_key="step-one",
                expected_evidence=(),
                postconditions=(),
                risk_level=RiskLevel.HIGH,
                requires_approval=True,
            ),
        ),
        automation_id=AUTOMATION_ID,
        automation_generation=invocation.automation_generation,
        automation_contract_hash=invocation.contract_hash,
    )


def _pending(approval_id: str, invocation: AutomationProjectInvocation) -> dict:
    plan = _plan(invocation)
    return {
        "approval_id": approval_id,
        "work_item_id": f"work-{approval_id}",
        "run_id": f"run-{approval_id}",
        "approval_round": 1,
        "plan_hash": plan.plan_hash,
        "current_plan_hash": plan.plan_hash,
        "risk_level": "HIGH",
        "required_role": "super_admin",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "created_at": datetime.now(timezone.utc),
        "source": "console",
        "run_status": "WAITING_APPROVAL",
        "plan_json": plan.to_dict(),
        "parameters_json": {
            "tool_name": f"automation.{AUTOMATION_ID}.run",
            "arguments": {"mode": "saved"},
            "execution_context": {},
        },
        "automation_invocation_json": invocation.to_dict(),
        "correlation_id": f"correlation-{approval_id}",
        "causation_id": None,
    }


class _State:
    def __init__(self) -> None:
        self.project = {
            "automation_id": AUTOMATION_ID,
            "enabled": True,
            "state": "ENABLED",
            "target_generation": 1,
            "committed_generation": 1,
            "reconcile_state": "STABLE",
        }
        self.config = {"automation_id": AUTOMATION_ID, "config_version": 1}
        self.policy = {
            "automation_id": AUTOMATION_ID,
            "mode": "REQUIRE_EACH_RUN",
            "version": 1,
            "project_generation": 1,
            "project_configuration_version": 1,
        }
        self.pending: list[dict] = []
        self.decisions: list[dict] = []
        self.batches: dict[tuple[str, str], dict] = {}
        self.policy_events: list[dict] = []
        self.domain_events: list[dict] = []
        self.fail_decision_at: int | None = None


class _AutomationProjects:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    @property
    def _state(self) -> _State:
        return self._repository.state

    def list_policies(self):
        return [dict(self._state.policy)]

    def get_policy(self, automation_id, **_kwargs):
        return dict(self._state.policy) if automation_id == AUTOMATION_ID else None

    def get_event_by_request(self, automation_id, request_id, **_kwargs):
        return next(
            (
                dict(row)
                for row in self._state.policy_events
                if row["automation_id"] == automation_id
                and row["request_id"] == request_id
            ),
            None,
        )

    def update_policy(self, automation_id, *, expected_version, **values):
        if automation_id != AUTOMATION_ID or self._state.policy["version"] != expected_version:
            raise AssertionError("policy CAS mismatch")
        self._state.policy.update(values)
        self._state.policy["version"] += 1
        self._state.policy["updated_at"] = datetime.now(timezone.utc)
        self._state.policy["approved_by_actor_id"] = values.get("actor_id")
        return dict(self._state.policy)

    def append_event(self, row):
        self._state.policy_events.append(dict(row))
        return dict(row)

    def list_configuration_rows(self, _automation_id, **_kwargs):
        return []

    def expire_pending_approvals(self, _automation_id):
        return 0

    def list_pending_approvals(self, automation_id, **_kwargs):
        if automation_id != AUTOMATION_ID:
            return []
        return [copy.deepcopy(row) for row in self._state.pending]

    def get_batch_by_request(self, automation_id, request_id, **_kwargs):
        row = self._state.batches.get((automation_id, request_id))
        return copy.deepcopy(row) if row is not None else None

    def create_batch(self, row):
        key = (row["automation_id"], row["request_id"])
        self._state.batches[key] = copy.deepcopy(dict(row))
        return copy.deepcopy(dict(row))


class _AutomationPlugins:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def get_project(self, automation_id, **_kwargs):
        return (
            dict(self._repository.state.project)
            if automation_id == AUTOMATION_ID
            else None
        )

    def get_project_config(self, automation_id, **_kwargs):
        return (
            dict(self._repository.state.config)
            if automation_id == AUTOMATION_ID
            else None
        )


class _Approvals:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def record_decision(self, row, *, expected_plan_hash):
        state = self._repository.state
        if state.fail_decision_at is not None and len(state.decisions) + 1 == state.fail_decision_at:
            raise InvalidStateError("approval changed")
        target = next(
            item
            for item in state.pending
            if item["approval_id"] == row["approval_id"]
        )
        if target["plan_hash"] != expected_plan_hash:
            return {"_decision_error": "PLAN_CHANGED"}
        state.pending.remove(target)
        state.decisions.append(dict(row))
        return dict(row)


class _Events:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def append_with_outbox(self, event, _outbox):
        self._repository.state.domain_events.append(copy.deepcopy(dict(event)))


class _Runs:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def make_waiting_approval_runnable(self, run_id):
        self._repository.runnable_run_ids.append(str(run_id))
        return {"run_id": str(run_id), "status": "WAITING_APPROVAL"}


class _Uow:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository
        self.automation_projects = _AutomationProjects(repository)
        self.automation_plugins = _AutomationPlugins(repository)
        self.approvals = _Approvals(repository)
        self.events = _Events(repository)
        self.runs = _Runs(repository)
        self.scheduled_policies = SimpleNamespace()
        self._snapshot: _State | None = None

    def __enter__(self):
        self._snapshot = copy.deepcopy(self._repository.state)
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is not None and self._snapshot is not None:
            self._repository.state = self._snapshot
        return False

    def commit(self):
        self._repository.account_lock_events.append(("commit",))
        return None


class _Repository:
    def __init__(self) -> None:
        self.state = _State()
        self.account_lock_events: list[tuple] = []
        self.block_account_locks = False
        self.runnable_run_ids: list[str] = []

    def unit_of_work(self):
        return _Uow(self)

    def acquire_account_execution_locks(self, account_ids, *, timeout_seconds=0):
        if timeout_seconds != 0:
            raise AssertionError("project policy account lock must not wait")
        normalized = tuple(sorted(account_ids))
        self.account_lock_events.append(("acquire", normalized))
        if self.block_account_locks:
            raise AccountExecutionLockUnavailable("synthetic credential change")
        repository = self

        class _Lease:
            def release(self):
                repository.account_lock_events.append(("release", normalized))

        return _Lease()


class _Catalog:
    def __init__(self, entry) -> None:
        self._entry = entry

    def require(self, automation_id):
        if automation_id != AUTOMATION_ID:
            raise KeyError(automation_id)
        return self._entry

    def get(self, automation_id):
        return self._entry if automation_id == AUTOMATION_ID else None

    def list(self):
        return (self._entry,)


class _Gateway:
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository
        self.command = None

    def submit(self, command, *, uow_guard=None):
        with self.repository.unit_of_work() as uow:
            if uow_guard is not None:
                uow_guard(uow)
            uow.commit()
        self.command = command
        return CommandReceipt(
            command_id=command.command_id,
            work_item_id="work-invoke",
            run_id="run-invoke",
            status=RunStatus.RECEIVED,
            reused=False,
        )

    async def wait_for_run(self, run_id, *, timeout_seconds):
        self.waited = (run_id, timeout_seconds)
        return {
            "run_id": run_id,
            "command_id": self.command.command_id,
            "work_item_id": "work-invoke",
            "status": "COMPLETED",
            "correlation_id": self.command.correlation_id,
        }


class AutomationProjectPolicyServiceTests(TestCase):
    def setUp(self) -> None:
        self.repository = _Repository()
        self.contract = _contract()
        self.entry = SimpleNamespace(automation_id=AUTOMATION_ID)
        self.gateway = _Gateway(self.repository)
        self.woken_run_ids: list[str] = []
        self.service = AutomationProjectPolicyService(
            self.repository,
            core_catalog=SimpleNamespace(),
            plugin_catalog=_Catalog(self.entry),
            command_gateway=self.gateway,
            wake_runner=self.woken_run_ids.append,
        )
        self.service._load_contract = lambda _automation_id: (  # type: ignore[method-assign]
            self.entry,
            self.contract,
        )
        self.service._lock_and_compile_contract = (  # type: ignore[method-assign]
            lambda _uow, _entry, **_kwargs: (
                self.contract,
                dict(self.repository.state.config),
            )
        )

    def test_console_invoke_builds_only_server_owned_project_identity(self):
        receipt = self.service.invoke_console(
            AUTOMATION_ID,
            request_id="request-console",
            actor=_admin(),
        )

        self.assertEqual("run-invoke", receipt.run_id)
        command = self.gateway.command
        self.assertIsNotNone(command)
        self.assertEqual("automation.project.invoke", command.command_type)
        self.assertEqual({"mode": "saved"}, command.parameters["arguments"])
        self.assertEqual(1, command.automation_invocation.automation_generation)
        self.assertEqual(CONTRACT_HASH, command.automation_invocation.contract_hash)

    def test_release_hold_blocks_project_writes_and_typed_invoke(self):
        service = AutomationProjectPolicyService(
            self.repository,
            core_catalog=SimpleNamespace(),
            plugin_catalog=_Catalog(self.entry),
            command_gateway=self.gateway,
            release_hold_provider=lambda: True,
        )
        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_console(
                AUTOMATION_ID,
                request_id="request-held",
                actor=_admin(),
            )
        self.assertEqual("RELEASE_HELD", raised.exception.code)
        self.assertIsNone(self.gateway.command)

    def test_full_auto_policy_save_does_not_lock_credentials_or_stage_runtime(self):
        self.service.get_policy_projection = (  # type: ignore[method-assign]
            lambda _automation_id: {
                "configured_mode": self.repository.state.policy["mode"]
            }
        )
        result = self.service.update_policy(
            AUTOMATION_ID,
            mode="PROJECT_FULL_AUTO",
            request_id="policy-full-auto-one",
            comment="reviewed",
            expected_policy_version=1,
            expected_project_configuration_version=1,
            actor=_admin(),
        )

        self.assertEqual("PROJECT_FULL_AUTO", result["configured_mode"])
        self.assertEqual([("commit",)], self.repository.account_lock_events)

    def test_project_takeover_event_request_fits_legacy_char36_and_is_idempotent(self):
        class _ScheduledPolicies:
            def __init__(self):
                self.events = []

            def ensure_default(self, task_id):
                return {"task_id": task_id, "mode": "REQUIRE_EACH_RUN", "version": 1}

            def get_event_by_request(self, task_id, request_id):
                return next(
                    (
                        event
                        for event in self.events
                        if event["task_id"] == task_id
                        and event["request_id"] == request_id
                    ),
                    None,
                )

            def append_event(self, row):
                self.events.append(dict(row))
                return dict(row)

        task_id = "scheduled_task_identifier_0001"
        scheduled = _ScheduledPolicies()
        uow = SimpleNamespace(scheduled_policies=scheduled)
        takeover = AutomationProjectPolicyService._retire_legacy_schedule_policies
        kwargs = {
            "uow": uow,
            "automation_id": AUTOMATION_ID,
            "rows": [{"id": task_id}],
            "actor": _admin(),
            "request_id": "project-policy-request-with-a-longer-id",
            "correlation_id": "correlation-id",
            "occurred_at": datetime.now(timezone.utc),
        }

        takeover(**kwargs)
        takeover(**kwargs)

        self.assertEqual(1, len(scheduled.events))
        self.assertLessEqual(len(scheduled.events[0]["request_id"]), 36)

    def test_full_auto_policy_save_is_independent_of_credential_change(self):
        self.repository.block_account_locks = True
        self.service.update_policy(
            AUTOMATION_ID,
            mode="PROJECT_FULL_AUTO",
            request_id="policy-full-auto-blocked",
            comment="reviewed",
            expected_policy_version=1,
            expected_project_configuration_version=1,
            actor=_admin(),
        )
        self.assertEqual("PROJECT_FULL_AUTO", self.repository.state.policy["mode"])

    def test_policy_save_preserves_target_generation_while_runtime_reconciles(self):
        self.repository.state.project.update(
            {
                "target_generation": 2,
                "committed_generation": 1,
                "reconcile_state": "PREPARING",
            }
        )
        self.repository.state.config["config_version"] = 2
        self.repository.state.policy.update(
            {
                "project_generation": 2,
                "project_configuration_version": 2,
            }
        )

        self.service.update_policy(
            AUTOMATION_ID,
            mode="PROJECT_FULL_AUTO",
            request_id="policy-during-reconcile",
            comment="keep intent",
            expected_policy_version=1,
            expected_project_configuration_version=2,
            actor=_admin(),
        )

        self.assertEqual(2, self.repository.state.policy["project_generation"])

    def test_startup_defaults_bootstrapped_policy_to_durable_full_auto(self):
        result = self.service.ensure_default_full_auto_policies()

        self.assertEqual({"changed": 1}, result)
        self.assertEqual("PROJECT_FULL_AUTO", self.repository.state.policy["mode"])
        self.assertIsNone(self.repository.state.policy["contract_hash"])
        self.assertEqual(
            "AUTOMATION_DEFAULT_FULL_AUTO",
            self.repository.state.policy_events[0]["reason"],
        )

        replay = self.service.ensure_default_full_auto_policies()
        self.assertEqual({"changed": 0}, replay)
        self.assertEqual(1, len(self.repository.state.policy_events))

    def test_default_full_auto_mode_is_approval_free_and_toggle_requires_approval(self):
        invocation = _invocation()
        self.repository.state.policy["mode"] = "PROJECT_FULL_AUTO"

        automatic = self.service.evaluate_invocation(
            _plan(invocation),
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertTrue(automatic.allowed)
        self.assertFalse(automatic.requires_approval)

        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"
        approval_required = self.service.evaluate_invocation(
            _plan(invocation),
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertTrue(approval_required.allowed)
        self.assertTrue(approval_required.requires_approval)

    def test_scheduler_invocation_binds_exact_row_generation_and_context(self):
        self.contract = _contract_for(AutomationEntrypoint.SCHEDULER)
        actor = Actor(
            ActorType.SCHEDULER,
            "schedule-one",
            roles=("system",),
            authenticated_by="apscheduler",
        )

        receipt = self.service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint=AutomationEntrypoint.SCHEDULER,
            request_id="scheduler:schedule-one:2026-08-15T07:00:00+08:00",
            actor=actor,
            trusted_context={
                "task_id": "schedule-one",
                "scheduled_for": "2026-08-15T07:00:00+08:00",
                "cron_expression": "0 7 * * *",
                "configuration_version": 1,
            },
            idempotency_key="scheduler:schedule-one:2026-08-15T07:00:00+08:00",
            expected_automation_generation=1,
            expected_project_configuration_version=1,
        )

        self.assertEqual("run-invoke", receipt.run_id)
        command = self.gateway.command
        self.assertEqual("scheduler", command.source)
        self.assertEqual(
            "scheduler:schedule-one",
            command.automation_invocation.contract_id,
        )
        self.assertEqual(
            "schedule-one",
            command.parameters["execution_context"]["task_id"],
        )

    def test_scheduler_invocation_rejects_missing_or_different_task_contract(self):
        self.contract = _contract_for(AutomationEntrypoint.SCHEDULER)
        actor = Actor(
            ActorType.SCHEDULER,
            "schedule-one",
            roles=("system",),
            authenticated_by="apscheduler",
        )
        for context, code in (
            ({}, "PROJECT_SCHEDULE_ID_REQUIRED"),
            ({"task_id": "schedule-two"}, "PROJECT_ENTRYPOINT_DISABLED"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(OrchestrationError) as raised:
                    self.service.invoke_trusted(
                        AUTOMATION_ID,
                        entrypoint=AutomationEntrypoint.SCHEDULER,
                        request_id=f"scheduler-{code.lower()}",
                        actor=actor,
                        trusted_context=context,
                        expected_automation_generation=1,
                    )
                self.assertEqual(code, raised.exception.code)
        self.assertIsNone(self.gateway.command)

    def test_webhook_dynamic_values_come_only_from_verified_route_context(self):
        self.contract = _contract_for(
            AutomationEntrypoint.WEBHOOK,
            dynamic_resolvers={"BILL_CODE": "verified_webhook_field"},
        )
        self.service._dynamic_resolver = (  # type: ignore[attr-defined]
            lambda resolver_id, field, context: (
                context["dynamic_inputs"][field]
                if resolver_id == "verified_webhook_field"
                else None
            )
        )
        actor = Actor(
            ActorType.WEBHOOK,
            "route-one",
            authenticated_by="signed_webhook_route",
        )

        self.service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint="webhook",
            request_id="event-one",
            actor=actor,
            trusted_context={
                "route_id": "route-one",
                "route_revision": 3,
                "source_event_id": "event-one",
                "webhook_path": "scan-sync",
                "dynamic_inputs": {"BILL_CODE": "10001"},
            },
            expected_automation_generation=1,
        )

        command = self.gateway.command
        self.assertEqual(
            {"mode": "saved", "BILL_CODE": "10001"},
            command.parameters["arguments"],
        )
        self.assertNotIn("account_id", command.parameters["arguments"])

    def test_trusted_invocation_rejects_untrusted_actor_and_context_override(self):
        self.contract = _contract_for(AutomationEntrypoint.WEBHOOK)
        actor = Actor(ActorType.WEBHOOK, "route-one")
        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint="webhook",
                request_id="event-one",
                actor=actor,
                expected_automation_generation=1,
            )
        self.assertEqual("TRUSTED_ENTRYPOINT_REQUIRED", raised.exception.code)

        actor = replace(actor, authenticated_by="signed_webhook_route")
        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint="webhook",
                request_id="event-one",
                actor=actor,
                trusted_context={"arguments": {"account_id": "override"}},
                expected_automation_generation=1,
            )
        self.assertEqual("TRUSTED_CONTEXT_INVALID", raised.exception.code)

    def test_grouped_approval_is_atomic_when_one_member_changes(self):
        invocation = _invocation()
        self.repository.state.pending = [
            _pending("approval-one", invocation),
            _pending("approval-two", invocation),
        ]
        pending = self.service.pending_approvals(AUTOMATION_ID, actor=_admin())
        self.repository.state.fail_decision_at = 2

        with self.assertRaises(OrchestrationError) as raised:
            self.service.decide_pending_approvals(
                AUTOMATION_ID,
                decision="APPROVED",
                expected_pending_set_hash=pending["pending_set_hash"],
                request_id="batch-one",
                comment="approve both",
                actor=_admin(),
            )

        self.assertEqual("PENDING_SET_CHANGED", raised.exception.code)
        self.assertEqual(2, len(self.repository.state.pending))
        self.assertEqual([], self.repository.state.decisions)
        self.assertEqual({}, self.repository.state.batches)

    def test_grouped_approval_returns_one_safe_receipt_per_visible_run(self):
        invocation = _invocation()
        self.repository.state.pending = [
            _pending("approval-one", invocation),
            _pending("approval-two", invocation),
        ]
        pending = self.service.pending_approvals(AUTOMATION_ID, actor=_admin())

        result = self.service.decide_pending_approvals(
            AUTOMATION_ID,
            decision="APPROVED",
            expected_pending_set_hash=pending["pending_set_hash"],
            request_id="batch-safe-receipts",
            comment="approve visible set",
            actor=_admin(),
        )

        self.assertEqual(2, result["decided_count"])
        self.assertEqual(
            {"run-approval-one", "run-approval-two"},
            {receipt["run_id"] for receipt in result["run_receipts"]},
        )
        self.assertTrue(
            all(
                set(receipt)
                == {"automation_id", "work_item_id", "run_id", "status"}
                for receipt in result["run_receipts"]
            )
        )

    def test_grouped_approval_replay_is_exact_and_does_not_repeat_decisions(self):
        invocation = _invocation()
        self.repository.state.pending = [_pending("approval-one", invocation)]
        pending = self.service.pending_approvals(AUTOMATION_ID, actor=_admin())
        first = self.service.decide_pending_approvals(
            AUTOMATION_ID,
            decision="APPROVED",
            expected_pending_set_hash=pending["pending_set_hash"],
            request_id="batch-one",
            comment="approve",
            actor=_admin(),
        )
        second = self.service.decide_pending_approvals(
            AUTOMATION_ID,
            decision="APPROVED",
            expected_pending_set_hash=pending["pending_set_hash"],
            request_id="batch-one",
            comment="approve",
            actor=_admin(),
        )

        self.assertEqual(first, second)
        self.assertEqual(0, first["pending_count"])
        self.assertEqual("APPROVED", first["decision"])
        self.assertEqual(1, first["decided_count"])
        self.assertEqual(
            [
                {
                    "automation_id": AUTOMATION_ID,
                    "work_item_id": "work-approval-one",
                    "run_id": "run-approval-one",
                    "status": "WAITING_APPROVAL",
                }
            ],
            first["run_receipts"],
        )
        self.assertNotIn("approval_id", first["run_receipts"][0])
        self.assertNotIn("plan_hash", first["run_receipts"][0])
        self.assertEqual(1, len(self.repository.state.decisions))
        self.assertEqual(["run-approval-one"], self.repository.runnable_run_ids)
        self.assertEqual(["run-approval-one"], self.woken_run_ids)
        with self.assertRaises(OrchestrationError) as raised:
            self.service.decide_pending_approvals(
                AUTOMATION_ID,
                decision="APPROVED",
                expected_pending_set_hash=pending["pending_set_hash"],
                request_id="batch-one",
                comment="different",
                actor=_admin(),
            )
        self.assertEqual("REQUEST_ID_REUSED", raised.exception.code)


if __name__ == "__main__":
    import unittest

    unittest.main()
