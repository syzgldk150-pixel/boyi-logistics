"""Durable run worker. This is the only production caller of ToolExecutionPort."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    EntityRef,
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
    RunStatus,
    WorkItemStatus,
    assert_run_transition,
    assert_work_item_transition,
    new_id,
    sha256_json,
)
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.pilot_projection import PilotProjectionService
from agent.orchestration.policy_engine import PolicyEngine
from agent.orchestration.result_verifier import ResultVerifier
from shared.automation_project_authorization import (
    AutomationProjectContractError,
    AutomationProjectInvocation,
)
from shared.redaction import redact_text


logger = logging.getLogger("agent")
RUNNABLE_STATUSES = (
    RunStatus.RECEIVED.value,
    RunStatus.CONTEXT_READY.value,
    RunStatus.PLANNED.value,
    RunStatus.VALIDATED.value,
    RunStatus.WAITING_APPROVAL.value,
    RunStatus.RUNNING.value,
    RunStatus.VERIFYING.value,
    RunStatus.FAILED_RETRYABLE.value,
)
TERMINAL_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.PARTIAL.value,
    RunStatus.FAILED_TERMINAL.value,
    RunStatus.CANCELLED.value,
}
PROTECTED_STEP_LOCK_WAIT_SECONDS = 5.0
PROTECTED_STEP_LOCK_RETRY_SECONDS = 0.1
SCHEDULER_SUPERSESSION_MAX_NO_PROGRESS_BATCHES = 2


class WorkflowRunner:
    def __init__(
        self,
        *,
        repository: Any,
        catalog: Any,
        execution_port: Any,
        context_builder: ContextBuilder,
        planner: DeterministicPlanner,
        validator: PlanValidator,
        policy: PolicyEngine,
        approval_service: ApprovalService,
        verifier: ResultVerifier,
        worker_id: str,
        pilot_projection: PilotProjectionService | None = None,
        protected_step_start_guard: (
            Callable[[PlanStep], Callable[[], None]] | None
        ) = None,
        poll_interval_seconds: float = 0.5,
        lease_seconds: int = 120,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._execution_port = execution_port
        self._context_builder = context_builder
        self._planner = planner
        self._validator = validator
        self._policy = policy
        self._approval_service = approval_service
        self._verifier = verifier
        self._pilot_projection = pilot_projection or PilotProjectionService()
        self._protected_step_start_guard = protected_step_start_guard
        self._worker_id = worker_id
        self._poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._lease_seconds = max(10, int(lease_seconds))
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: dict[str, tuple[str, asyncio.Task]] = {}
        self._release_hold = False

    async def start(self, *, held_for_release: bool = False) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._release_hold = bool(held_for_release)
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name=f"run-worker:{self._worker_id}")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            await task
        self._loop = None

    def wake(self, _run_id: str | None = None) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._wake.set()
            return
        loop.call_soon_threadsafe(self._wake.set)

    def hold_for_release(self) -> dict[str, Any]:
        """Stop new durable claims while a deployment marker is active."""

        if self._task is None or self._task.done():
            raise RuntimeError("Workflow runner is not available for release hold")
        self._release_hold = True
        self.wake()
        return self.runtime_status()

    def resume_after_release(self) -> dict[str, Any]:
        """Idempotently allow durable claims after all release gates pass."""

        if self._task is None or self._task.done():
            raise RuntimeError("Workflow runner is not available for release activation")
        self._release_hold = False
        self.wake()
        return self.runtime_status()

    def runtime_status(self) -> dict[str, Any]:
        task = self._task
        if task is None or task.done():
            state = "stopped"
        elif self._release_hold:
            state = "held"
        else:
            state = "running"
        return {
            "state": state,
            "release_hold": self._release_hold,
            "active_runs": len(self._active),
        }

    async def cancel_active(self, run_id: str) -> None:
        active = self._active.get(run_id)
        if active is not None:
            step_id, _task = active
            await self._execution_port.cancel_step(run_id=run_id, step_id=step_id)
        self.wake(run_id)

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            if self._release_hold:
                self._wake.clear()
                if not self._release_hold or self._stop.is_set():
                    continue
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self._poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            claimed_any = False
            try:
                cancellations = await asyncio.to_thread(
                    self._repository.claim_cancel_requested_runs,
                    self._worker_id,
                    limit=1,
                    lease_seconds=self._lease_seconds,
                )
                for run in cancellations:
                    claimed_any = True
                    await self._cancel_claimed(run)
                claimed = await asyncio.to_thread(
                    self._repository.claim_runs,
                    self._worker_id,
                    RUNNABLE_STATUSES,
                    limit=1,
                    lease_seconds=self._lease_seconds,
                )
                for run in claimed:
                    claimed_any = True
                    await self._process_claimed(run)
            except Exception:
                logger.exception("Run worker iteration failed")
            if claimed_any:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _cancel_claimed(self, run: Mapping[str, Any]) -> None:
        run_id = str(run["run_id"])
        active = self._active.get(run_id)
        if active is not None:
            await self._execution_port.cancel_step(run_id=run_id, step_id=active[0])
        await asyncio.to_thread(
            self._release,
            run_id,
            status=RunStatus.CANCELLED.value,
            error_code="CANCELLED_BY_ACTOR",
            error_summary=str(run.get("cancel_reason") or "Run cancellation was requested"),
            finished=True,
        )

    async def _process_claimed(self, claimed: Mapping[str, Any]) -> None:
        run_id = str(claimed["run_id"])
        try:
            run = self._repository.get_run(run_id)
            if run is None:
                raise OrchestrationError("RUN_NOT_FOUND", "Claimed run was not found")
            if run.get("cancel_requested_at"):
                await self._cancel_claimed(run)
                return
            command = self._load_command(str(run["command_id"]))
            status = RunStatus(str(run["status"]))
            recovered_running = status is RunStatus.RUNNING

            if status in {RunStatus.RECEIVED, RunStatus.FAILED_RETRYABLE}:
                context = self._context_builder.build(command)
                run = self._transition(
                    run,
                    RunStatus.CONTEXT_READY,
                    context_fingerprint_sha256=context.fingerprint,
                )
                status = RunStatus.CONTEXT_READY
            else:
                context = self._context_builder.build(command)

            if status is RunStatus.CONTEXT_READY:
                plan = self._planner.plan(command, context, llm_selected=bool(command.parameters.get("llm_selected")))
                run = self._transition(
                    run,
                    RunStatus.PLANNED,
                    plan=plan.to_dict(),
                    plan_hash=plan.plan_hash,
                    plan_schema_version=plan.schema_version,
                    tool_catalog_sha256=plan.tool_catalog_hash,
                    context_fingerprint_sha256=plan.context_fingerprint,
                )
                status = RunStatus.PLANNED
            else:
                plan = self._plan_from_run(run)

            if status is RunStatus.PLANNED:
                self._validator.validate(plan, context, llm_selected=bool(command.parameters.get("llm_selected")))
                decision = self._evaluate_policy(plan, command)
                if not decision.allowed:
                    raise OrchestrationError(decision.code, decision.reason)
                plan = _annotate_approval(plan, decision.requires_approval)
                run = self._transition(run, RunStatus.VALIDATED, plan=plan.to_dict())
                status = RunStatus.VALIDATED
            else:
                decision = self._evaluate_policy(plan, command)
            if not decision.allowed:
                raise OrchestrationError(decision.code, decision.reason)
            if status is RunStatus.VALIDATED and decision.requires_approval:
                run = self._transition(run, RunStatus.WAITING_APPROVAL)
                request_outcome, request_detail = (
                    self._request_approval_with_policy_fence(
                        run=run,
                        plan=plan,
                        decision=decision,
                        command=command,
                    )
                )
                if request_outcome == "FAILED":
                    exc = request_detail
                    # Persist WAITING before creating the requested Outbox so a
                    # fast Feishu reply can never observe a VALIDATED Run. If
                    # request persistence itself fails, the waiting Run remains
                    # recoverable and is retried without executing the tool.
                    logger.error(
                        "Approval request or policy fence failed run_id=%s error=%s",
                        run_id,
                        redact_text(exc)[:500],
                    )
                    await asyncio.to_thread(
                        self._release,
                        run_id,
                        status=RunStatus.WAITING_APPROVAL.value,
                        error_code="APPROVAL_REQUEST_PENDING",
                        error_summary=redact_text(exc)[:500],
                    )
                    return
                await asyncio.to_thread(
                    self._release,
                    run_id,
                    status=RunStatus.WAITING_APPROVAL.value,
                )
                return
            if status is RunStatus.WAITING_APPROVAL:
                fresh_context = self._context_builder.build(command)
                fresh_plan = self._planner.plan(
                    command,
                    fresh_context,
                    llm_selected=bool(command.parameters.get("llm_selected")),
                )
                self._validator.validate(fresh_plan, fresh_context, llm_selected=bool(command.parameters.get("llm_selected")))
                if fresh_plan.plan_hash != plan.plan_hash:
                    fresh_decision = self._evaluate_policy(fresh_plan, command)
                    if not fresh_decision.allowed:
                        raise OrchestrationError(fresh_decision.code, fresh_decision.reason)
                    fresh_plan = _annotate_approval(fresh_plan, fresh_decision.requires_approval)
                    self._approval_service.invalidate_for_stale_plan(run_id)
                    with self._repository.unit_of_work() as uow:
                        refreshed = uow.runs.refresh_waiting_plan(
                            run_id,
                            expected_version=int(run["version"]),
                            plan=fresh_plan.to_dict(),
                            plan_hash=fresh_plan.plan_hash,
                            catalog_hash=fresh_plan.tool_catalog_hash,
                            context_hash=fresh_plan.context_fingerprint,
                        )
                        uow.commit()
                    plan = fresh_plan
                    decision = fresh_decision
                    if fresh_decision.requires_approval:
                        request_outcome, request_detail = (
                            self._request_approval_with_policy_fence(
                                run=refreshed,
                                plan=fresh_plan,
                                decision=decision,
                                command=command,
                            )
                        )
                        if request_outcome == "FAILED":
                            exc = request_detail
                            logger.error(
                                "Approval request or policy fence failed after plan refresh run_id=%s error=%s",
                                run_id,
                                redact_text(exc)[:500],
                            )
                            await asyncio.to_thread(
                                self._release,
                                run_id,
                                status=RunStatus.WAITING_APPROVAL.value,
                                error_code="APPROVAL_REQUEST_PENDING",
                                error_summary=redact_text(exc)[:500],
                            )
                            return
                        await asyncio.to_thread(
                            self._release,
                            run_id,
                            status=RunStatus.WAITING_APPROVAL.value,
                            error_code="PLAN_STALE",
                            error_summary="Plan changed and requires a new approval",
                        )
                        return
                    run = self._transition(
                        refreshed,
                        RunStatus.RUNNING,
                        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        increment_execution_attempt=True,
                    )
                    status = RunStatus.RUNNING
                elif not decision.requires_approval:
                    # A durable policy may become fully automatic while an
                    # earlier run is waiting.  The old approval must not keep
                    # that run parked forever or be consumed as authority for
                    # the new policy.  Invalidate it and resume the already
                    # validated plan through the normal run-state CAS.
                    self._approval_service.invalidate_for_stale_plan(run_id)
                    run = self._transition(
                        run,
                        RunStatus.RUNNING,
                        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        increment_execution_attempt=True,
                    )
                    status = RunStatus.RUNNING
                else:
                    run, approval_outcome = self._consume_approved_plan(
                        run_id,
                        plan.plan_hash,
                    )
                    if run is None:
                        if approval_outcome == "REJECTED":
                            raise OrchestrationError("APPROVAL_REJECTED", "The plan was rejected by an administrator")
                        if approval_outcome == "PLAN_STALE":
                            raise OrchestrationError("PLAN_STALE", "The persisted plan changed before approval consumption")
                        if approval_outcome in {"EXPIRED", "INVALIDATED", "MISSING"}:
                            request_outcome, request_detail = (
                                self._request_approval_with_policy_fence(
                                    run=(
                                        self._repository.get_run(run_id)
                                        or claimed
                                    ),
                                    plan=plan,
                                    decision=decision,
                                    command=command,
                                )
                            )
                            if request_outcome == "FAILED":
                                exc = request_detail
                                logger.error(
                                    "Approval request recovery or policy fence failed run_id=%s error=%s",
                                    run_id,
                                    redact_text(exc)[:500],
                                )
                                await asyncio.to_thread(
                                    self._release,
                                    run_id,
                                    status=RunStatus.WAITING_APPROVAL.value,
                                    error_code="APPROVAL_REQUEST_PENDING",
                                    error_summary=redact_text(exc)[:500],
                                )
                                return
                        await asyncio.to_thread(
                            self._release,
                            run_id,
                            status=RunStatus.WAITING_APPROVAL.value,
                        )
                        return
                    status = RunStatus.RUNNING
            elif status is RunStatus.VALIDATED:
                run = self._transition(
                    run,
                    RunStatus.RUNNING,
                    started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    increment_execution_attempt=True,
                )
                status = RunStatus.RUNNING

            if recovered_running and status is RunStatus.RUNNING and decision.requires_approval:
                waiting_run = self._defer_unstarted_running_plan_for_approval(
                    run=run,
                    plan=plan,
                )
                if waiting_run is None:
                    recovery_complete = await self._reconcile_started_steps_for_policy_recheck(
                        run=run,
                        plan=plan,
                        command=command,
                    )
                    if recovery_complete:
                        run = self._transition(run, RunStatus.VERIFYING)
                        status = RunStatus.VERIFYING
                    else:
                        waiting_run = self._defer_reconciled_running_plan_for_approval(
                            run=run,
                            plan=plan,
                        )
                if waiting_run is not None:
                    request_outcome, request_detail = (
                        self._request_approval_with_policy_fence(
                            run=waiting_run,
                            plan=_annotate_approval(plan, True),
                            decision=decision,
                            command=command,
                        )
                    )
                    if request_outcome == "FAILED":
                        exc = request_detail
                        logger.error(
                            "Approval request or policy fence failed after safely pausing run_id=%s error=%s",
                            run_id,
                            redact_text(exc)[:500],
                        )
                        await asyncio.to_thread(
                            self._release,
                            run_id,
                            status=RunStatus.WAITING_APPROVAL.value,
                            error_code="APPROVAL_REQUEST_PENDING",
                            error_summary=redact_text(exc)[:500],
                        )
                        return
                    await asyncio.to_thread(
                        self._release,
                        run_id,
                        status=RunStatus.WAITING_APPROVAL.value,
                    )
                    return

            if status is RunStatus.RUNNING:
                run = await self._execute_plan(run, plan, command)
                status = RunStatus(str(run["status"]))
            if status is RunStatus.VERIFYING:
                run = self._complete_run_and_supersede_scheduler_failures(run, command)
            await asyncio.to_thread(
                self._release,
                run_id,
                status=str(run["status"]),
                finished=str(run["status"]) in TERMINAL_STATUSES,
            )
        except OrchestrationError as exc:
            await asyncio.to_thread(self._fail_claimed, run_id, exc)
        except Exception as exc:
            logger.exception("Run processing failed run_id=%s", run_id)
            await asyncio.to_thread(
                self._fail_claimed,
                run_id,
                OrchestrationError(type(exc).__name__.upper(), redact_text(exc)[:500]),
            )

    def _evaluate_policy(
        self,
        plan: Plan,
        command: Command,
        *,
        project_transaction: Any | None = None,
    ):
        return self._policy.evaluate(
            plan,
            command.actor,
            source=command.source,
            execution_context=dict(command.parameters.get("execution_context") or {}),
            automation_invocation=command.automation_invocation,
            project_transaction=project_transaction,
        )

    def _request_approval_with_policy_fence(
        self,
        *,
        run: Mapping[str, Any],
        plan: Plan,
        decision: Any,
        command: Command,
    ) -> tuple[str, Any]:
        """Create an approval, then close the project-change wakeup gap.

        A project/config/plugin update can commit after the first policy read
        but before a new approval row is created.  Its invalidation wake would
        then precede that row and `_release` could sleep until expiry.  Reading
        policy again after the request commit closes that gap: an obsolete
        approval is invalidated immediately; any later update will invalidate
        it itself while holding the Run lock.
        """

        try:
            approval = self._approval_service.request(
                run=dict(run),
                plan=plan,
                policy_decision=decision,
                requested_by=command.actor,
            )
        except Exception as exc:
            return "FAILED", exc
        try:
            current = self._evaluate_policy(plan, command)
            if not current.allowed or not current.requires_approval:
                self._approval_service.invalidate_for_stale_plan(
                    str(run["run_id"])
                )
                return "SUPERSEDED", current
        except Exception as exc:
            # The approval is durable already, but execution must remain
            # waiting until the post-request policy fence can be read.
            return "FAILED", exc
        return "PENDING", approval

    @staticmethod
    def _trusted_execution_context(command: Command) -> dict[str, Any]:
        context = {
            **dict(command.parameters.get("execution_context") or {}),
            "source": command.source,
            "actor": command.actor.to_dict(),
        }
        if command.automation_invocation is not None:
            context["_automation_project_invocation"] = (
                command.automation_invocation.to_dict()
            )
        return context

    def _consume_approved_plan(
        self,
        run_id: str,
        plan_hash: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Atomically consume a live approval and start its locked Run."""

        with self._repository.unit_of_work() as uow:
            prepared = uow.approvals.prepare_approved_execution(
                run_id,
                expected_plan_hash=plan_hash,
            )
            outcome = str(prepared["outcome"])
            if outcome != "APPROVED":
                uow.commit()
                return None, outcome
            locked_run = prepared["run"]
            approval = prepared["approval"] or {}
            assert_run_transition(str(locked_run["status"]), RunStatus.RUNNING)
            updated = uow.runs.transition(
                run_id,
                expected_version=int(locked_run["version"]),
                expected_statuses=(RunStatus.WAITING_APPROVAL.value,),
                status=RunStatus.RUNNING.value,
                started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                increment_execution_attempt=True,
            )
            self._sync_work_item_status(uow, updated, RunStatus.RUNNING)
            self._append_event(
                uow,
                event_type="agent.run.status_changed",
                run=updated,
                payload={
                    "from": RunStatus.WAITING_APPROVAL.value,
                    "to": RunStatus.RUNNING.value,
                    "approval_id": approval.get("approval_id"),
                    "approval_round": approval.get("approval_round"),
                    "plan_hash": plan_hash,
                },
            )
            uow.commit()
        return updated, "APPROVED"

    def _defer_unstarted_running_plan_for_approval(
        self,
        *,
        run: Mapping[str, Any],
        plan: Plan,
    ) -> dict[str, Any] | None:
        """Pause a recovered Run before any newly-required write can start.

        A Run can be persisted as RUNNING before its first step is started.  If
        an exact scheduler exemption is revoked during that crash window, the
        fresh policy decision must win.  The Run row is the serialization lock
        shared with step start, so either this CAS reaches WAITING_APPROVAL or a
        write step reaches RUNNING first; the two outcomes cannot cross.

        An already-started write is deliberately left on the existing
        reconciliation path.  Resetting it to PENDING or starting a new
        approval round before its outcome is known could replay an external
        side effect.
        """

        annotated_plan = _annotate_approval(plan, True)
        with self._repository.unit_of_work() as uow:
            locked_run = uow.runs.get(str(run["run_id"]), for_update=True)
            if locked_run is None:
                raise OrchestrationError(
                    "RUN_NOT_FOUND",
                    "Run was not found while rechecking execution approval",
                )
            if str(locked_run.get("status") or "") != RunStatus.RUNNING.value:
                raise OrchestrationError(
                    "RUN_STATE_CONFLICT",
                    "Run state changed while rechecking execution approval",
                    details={"status": RunStatus.BLOCKED_DATA.value},
                )
            if str(locked_run.get("worker_id") or "") != self._worker_id:
                raise OrchestrationError(
                    "RUN_LEASE_LOST",
                    "Run lease changed while rechecking execution approval",
                    details={"status": RunStatus.BLOCKED_DATA.value},
                )
            updated = self._defer_locked_unstarted_running_plan_for_approval(
                uow,
                locked_run=locked_run,
                plan=annotated_plan,
            )
            if updated is None:
                return None
            uow.commit()
        return updated

    def _defer_locked_unstarted_running_plan_for_approval(
        self,
        uow: Any,
        *,
        locked_run: Mapping[str, Any],
        plan: Plan,
    ) -> dict[str, Any] | None:
        """Move an unstarted locked run to approval inside the caller's UoW."""

        step_operations = {step.step_key: step.operation_type for step in plan.steps}
        persisted_steps = uow.steps.list_for_run(str(locked_run["run_id"]))
        if any(
            _is_started_write_step(step_row, step_operations)
            for step_row in persisted_steps
        ):
            return None
        annotated_plan = _annotate_approval(plan, True)
        assert_run_transition(RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
        updated = uow.runs.transition(
            str(locked_run["run_id"]),
            expected_version=int(locked_run["version"]),
            expected_statuses=(RunStatus.RUNNING.value,),
            status=RunStatus.WAITING_APPROVAL.value,
            plan=annotated_plan.to_dict(),
        )
        self._sync_work_item_status(uow, updated, RunStatus.WAITING_APPROVAL)
        self._append_event(
            uow,
            event_type="agent.run.status_changed",
            run=updated,
            payload={
                "from": RunStatus.RUNNING.value,
                "to": RunStatus.WAITING_APPROVAL.value,
                "reason_code": "FRESH_POLICY_REQUIRES_APPROVAL",
                "plan_hash": plan.plan_hash,
            },
        )
        return updated

    async def _reconcile_started_steps_for_policy_recheck(
        self,
        *,
        run: Mapping[str, Any],
        plan: Plan,
        command: Command,
    ) -> bool:
        """Resolve in-flight steps without starting any new tool execution."""

        with self._repository.unit_of_work() as uow:
            persisted_steps = uow.steps.list_for_run(str(run["run_id"]))
        plan_by_key = {step.step_key: step for step in plan.steps}
        for step_row in persisted_steps:
            status = str(step_row.get("status") or "").strip().upper()
            if status not in {"RUNNING", "VERIFYING"}:
                continue
            step_key = str(step_row.get("step_key") or "")
            step = plan_by_key.get(step_key)
            if step is None:
                raise OrchestrationError(
                    "STEP_STATE_CONFLICT",
                    "Persisted in-flight step is not present in the approved plan",
                    details={"status": RunStatus.BLOCKED_DATA.value},
                )
            await self._recover_interrupted_step(
                run,
                step,
                step_row,
                command,
            )

        with self._repository.unit_of_work() as uow:
            reconciled_steps = uow.steps.list_for_run(str(run["run_id"]))
        status_by_key = {
            str(step_row.get("step_key") or ""): str(
                step_row.get("status") or ""
            ).strip().upper()
            for step_row in reconciled_steps
        }
        invalid = sorted(
            f"{step_key}:{step_status or '<empty>'}"
            for step_key, step_status in status_by_key.items()
            if step_key not in plan_by_key
            or step_status not in {"PENDING", "FAILED_RETRYABLE", "COMPLETED"}
        )
        if invalid:
            raise OrchestrationError(
                "STEP_STATE_CONFLICT",
                "Recovered steps cannot safely enter approval: " + ", ".join(invalid),
                details={"status": RunStatus.BLOCKED_DATA.value},
            )
        return all(
            status_by_key.get(step.step_key) == "COMPLETED"
            for step in plan.steps
        )

    def _defer_reconciled_running_plan_for_approval(
        self,
        *,
        run: Mapping[str, Any],
        plan: Plan,
    ) -> dict[str, Any]:
        """Move a reconciled RUNNING Run to approval without replaying a step."""

        annotated_plan = _annotate_approval(plan, True)
        with self._repository.unit_of_work() as uow:
            locked_run = uow.runs.get(str(run["run_id"]), for_update=True)
            if locked_run is None:
                raise OrchestrationError(
                    "RUN_NOT_FOUND",
                    "Run was not found after reconciling its interrupted steps",
                )
            if (
                str(locked_run.get("status") or "") != RunStatus.RUNNING.value
                or str(locked_run.get("worker_id") or "") != self._worker_id
            ):
                raise OrchestrationError(
                    "RUN_LEASE_LOST",
                    "Run lease changed after reconciling its interrupted steps",
                    details={"status": RunStatus.BLOCKED_DATA.value},
                )
            persisted_steps = uow.steps.list_for_run(str(run["run_id"]))
            if any(
                str(step_row.get("status") or "").strip().upper()
                in {"RUNNING", "VERIFYING"}
                for step_row in persisted_steps
            ):
                raise OrchestrationError(
                    "STEP_RECOVERY_INCOMPLETE",
                    "An interrupted step still requires reconciliation",
                    details={"status": RunStatus.BLOCKED_DATA.value},
                )
            assert_run_transition(RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
            updated = uow.runs.transition(
                str(run["run_id"]),
                expected_version=int(locked_run["version"]),
                expected_statuses=(RunStatus.RUNNING.value,),
                status=RunStatus.WAITING_APPROVAL.value,
                plan=annotated_plan.to_dict(),
            )
            self._sync_work_item_status(
                uow,
                updated,
                RunStatus.WAITING_APPROVAL,
            )
            self._append_event(
                uow,
                event_type="agent.run.status_changed",
                run=updated,
                payload={
                    "from": RunStatus.RUNNING.value,
                    "to": RunStatus.WAITING_APPROVAL.value,
                    "reason_code": "FRESH_POLICY_REQUIRES_APPROVAL_AFTER_RECOVERY",
                    "plan_hash": plan.plan_hash,
                },
            )
            uow.commit()
        return updated

    async def _execute_plan(self, run: dict[str, Any], plan: Plan, command: Command) -> dict[str, Any]:
        for order, step in enumerate(plan.steps, start=1):
            step_row = self._get_or_create_step(run, step, order)
            if step_row.get("status") == "COMPLETED":
                continue
            if step_row.get("status") in {"RUNNING", "VERIFYING"}:
                step_row = await self._recover_interrupted_step(run, step, step_row, command)
                if step_row.get("status") == "COMPLETED":
                    continue
            if step_row.get("status") not in {"PENDING", "FAILED_RETRYABLE"}:
                raise OrchestrationError(
                    "STEP_STATE_CONFLICT",
                    f"Step {step.step_key} cannot resume from {step_row.get('status')}",
                    details={"status": RunStatus.BLOCKED_DATA.value},
                )
            finish_protected_step_start = await self._acquire_protected_step_start(
                step
            )
            try:
                if (
                    getattr(self, "_protected_step_start_guard", None) is not None
                    and command.automation_invocation is None
                ):
                    fresh_decision = self._evaluate_policy(plan, command)
                    if not fresh_decision.allowed:
                        raise OrchestrationError(
                            fresh_decision.code,
                            fresh_decision.reason,
                        )
                    if fresh_decision.requires_approval and not step.requires_approval:
                        waiting_run = self._defer_unstarted_running_plan_for_approval(
                            run=run,
                            plan=plan,
                        )
                        if waiting_run is None:
                            raise OrchestrationError(
                                "ACCOUNT_POLICY_RECHECK_UNSAFE",
                                "A protected write already started before its account policy recheck",
                                details={"status": RunStatus.BLOCKED_DATA.value},
                            )
                        request_outcome, request_detail = (
                            self._request_approval_with_policy_fence(
                                run=waiting_run,
                                plan=_annotate_approval(plan, True),
                                decision=fresh_decision,
                                command=command,
                            )
                        )
                        if request_outcome == "FAILED":
                            logger.error(
                                "Approval request or policy fence failed after account policy recheck run_id=%s error=%s",
                                run["run_id"],
                                redact_text(request_detail)[:500],
                            )
                        return waiting_run
                project_waiting: tuple[dict[str, Any], Any] | None = None
                with self._repository.unit_of_work() as uow:
                    project_decision = None
                    if command.automation_invocation is not None:
                        # Project state is locked before the Run row.  Policy
                        # changes, grouped approval, generation switch and
                        # uninstall use the same project serialization lock.
                        project_decision = self._evaluate_policy(
                            plan,
                            command,
                            project_transaction=uow,
                        )
                        if not project_decision.allowed:
                            raise OrchestrationError(
                                project_decision.code,
                                project_decision.reason,
                            )
                    locked_run = uow.runs.get(str(run["run_id"]), for_update=True)
                    if locked_run is None:
                        raise OrchestrationError(
                            "RUN_NOT_FOUND",
                            "Run was not found before starting a tool step",
                        )
                    if (
                        str(locked_run.get("status") or "") != RunStatus.RUNNING.value
                        or str(locked_run.get("worker_id") or "") != self._worker_id
                    ):
                        raise OrchestrationError(
                            "RUN_EXECUTION_LEASE_LOST",
                            "Run is no longer owned for tool execution",
                            details={"status": RunStatus.BLOCKED_DATA.value},
                        )
                    if (
                        project_decision is not None
                        and project_decision.requires_approval
                        and not step.requires_approval
                    ):
                        waiting_run = self._defer_locked_unstarted_running_plan_for_approval(
                            uow,
                            locked_run=locked_run,
                            plan=plan,
                        )
                        if waiting_run is None:
                            raise OrchestrationError(
                                "PROJECT_POLICY_RECHECK_UNSAFE",
                                "A project write already started before its policy recheck",
                                details={"status": RunStatus.BLOCKED_DATA.value},
                            )
                        project_waiting = (waiting_run, project_decision)
                    else:
                        started_step = uow.steps.transition(
                            str(step_row["step_id"]),
                            expected_version=int(step_row["version"]),
                            expected_statuses=("PENDING", "FAILED_RETRYABLE"),
                            status="RUNNING",
                            increment_attempt=True,
                            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        )
                    uow.commit()
                if project_waiting is not None:
                    waiting_run, project_decision = project_waiting
                    request_outcome, request_detail = (
                        self._request_approval_with_policy_fence(
                            run=waiting_run,
                            plan=_annotate_approval(plan, True),
                            decision=project_decision,
                            command=command,
                        )
                    )
                    if request_outcome == "FAILED":
                        logger.error(
                            "Approval request or policy fence failed after project policy recheck run_id=%s error=%s",
                            run["run_id"],
                            redact_text(request_detail)[:500],
                        )
                    return waiting_run
            finally:
                finish_protected_step_start()
            # Keep the exact capability that was admitted before execution.
            # The Catalog may be blocked by a concurrent generation fence
            # after the subprocess returns; re-querying it here used to turn
            # the plugin's real safe error into an opaque runtime block.
            capability = self._catalog.get_capability(step.tool_name) or {}
            execution_task = asyncio.create_task(
                self._execution_port.execute_step(
                    step,
                    run_id=str(run["run_id"]),
                    step_id=str(step_row["step_id"]),
                    execution_context=self._trusted_execution_context(command),
                )
            )
            self._active[str(run["run_id"])] = (str(step_row["step_id"]), execution_task)
            try:
                raw_result = await self._await_with_lease_heartbeat(
                    execution_task,
                    run_id=str(run["run_id"]),
                    step_id=str(step_row["step_id"]),
                )
            finally:
                self._active.pop(str(run["run_id"]), None)
            outcome = self._verifier.verify(step, raw_result, capability)
            if outcome.accepted:
                projection_error: OrchestrationError | None = None
                try:
                    with self._repository.unit_of_work() as uow:
                        completed_step = uow.steps.transition(
                            str(step_row["step_id"]),
                            expected_version=int(started_step["version"]),
                            expected_statuses=("RUNNING",),
                            status="COMPLETED",
                            result_summary=outcome.result.to_dict() if outcome.result else {},
                            postcondition_status="VERIFIED",
                            finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        )
                        self._persist_evidence(uow, run, completed_step, step, outcome.result)
                        if outcome.result is not None:
                            try:
                                self._pilot_projection.project_successful_step(
                                    uow=uow,
                                    run=run,
                                    step_row=completed_step,
                                    step=step,
                                    command=command,
                                    result=outcome.result,
                                    generation_verification=outcome.generation_verification,
                                )
                            except OrchestrationError as exc:
                                projection_error = exc
                                raise
                        self._append_event(
                            uow,
                            event_type="agent.step.completed",
                            run=run,
                            step_id=str(step_row["step_id"]),
                            payload={"step_key": step.step_key, "status": "COMPLETED"},
                        )
                        uow.commit()
                except OrchestrationError:
                    if projection_error is not None:
                        blocked_error = OrchestrationError(
                            projection_error.code,
                            projection_error.message,
                            details={
                                **projection_error.details,
                                "status": RunStatus.BLOCKED_DATA.value,
                            },
                        )
                        self._persist_blocked_pilot_projection(
                            run=run,
                            started_step=started_step,
                            step=step,
                            command=command,
                            raw_result=raw_result,
                            result=outcome.result,
                            error=blocked_error,
                        )
                        raise blocked_error
                    raise
            else:
                failure_status = outcome.run_status
                failure_code = outcome.code
                failure_message = outcome.message
                step_status = failure_status.value
                if step_status not in {
                    "BLOCKED_LOGIN",
                    "BLOCKED_DATA",
                    "FAILED_RETRYABLE",
                    "FAILED_TERMINAL",
                    "CANCELLED",
                }:
                    step_status = "FAILED_TERMINAL"
                with self._repository.unit_of_work() as uow:
                    current_run = uow.runs.get(
                        str(run["run_id"]),
                        for_update=True,
                    )
                    if current_run is None:
                        raise OrchestrationError(
                            "RUN_NOT_FOUND",
                            "Run was not found while persisting the step result",
                        )
                    if (
                        current_run.get("cancel_requested_at")
                        and not _is_governing_unknown_write(failure_status.value, failure_code)
                    ):
                        failure_status = RunStatus.CANCELLED
                        step_status = RunStatus.CANCELLED.value
                        if outcome.run_status is not RunStatus.CANCELLED:
                            failure_code = "CANCELLED_BY_ACTOR"
                            failure_message = str(
                                current_run.get("cancel_reason")
                                or "Run cancellation was requested"
                            )
                    failed_step = uow.steps.transition(
                        str(step_row["step_id"]),
                        expected_version=int(started_step["version"]),
                        expected_statuses=("RUNNING",),
                        status=step_status,
                        result_summary=dict(raw_result),
                        error_code=failure_code,
                        error_summary=failure_message,
                        finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                    self._pilot_projection.record_incomplete_attempt(
                        uow=uow,
                        run=run,
                        step_row=failed_step,
                        step=step,
                        command=command,
                        failure_code=failure_code,
                        result=outcome.result,
                        raw_result=raw_result,
                    )
                    self._append_event(
                        uow,
                        event_type="agent.step.failed",
                        run=run,
                        step_id=str(step_row["step_id"]),
                        payload={"step_key": step.step_key, "code": failure_code, "status": step_status},
                    )
                    if step_status == RunStatus.BLOCKED_LOGIN.value:
                        failure_meta = (
                            raw_result.get("meta")
                            if isinstance(raw_result.get("meta"), Mapping)
                            else {}
                        )
                        degraded_account_id = str(
                            failure_meta.get("account_id") or step.account_id or ""
                        ).strip()
                        if degraded_account_id:
                            self._append_event(
                                uow,
                                event_type="account.session_degraded",
                                run=run,
                                step_id=str(step_row["step_id"]),
                                payload={
                                    "account_id": degraded_account_id,
                                    "source_system": str(
                                        failure_meta.get("source_system") or ""
                                    ),
                                    "reason_code": failure_code,
                                },
                            )
                    uow.commit()
                raise OrchestrationError(
                    failure_code,
                    failure_message,
                    details={"status": failure_status.value},
                )
        return self._transition(run, RunStatus.VERIFYING)

    async def _acquire_protected_step_start(
        self,
        step: PlanStep,
    ) -> Callable[[], None]:
        guard = getattr(self, "_protected_step_start_guard", None)
        if guard is None:
            return _noop_finish
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROTECTED_STEP_LOCK_WAIT_SECONDS
        while True:
            try:
                finish = guard(step)
                if not callable(finish):
                    raise OrchestrationError(
                        "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE",
                        "Protected step guard returned no cleanup callback",
                        details={"status": RunStatus.BLOCKED_DATA.value},
                    )
                return finish
            except OrchestrationError as exc:
                if exc.code != "ACCOUNT_CREDENTIAL_CHANGE_IN_PROGRESS":
                    raise
                if loop.time() >= deadline:
                    raise OrchestrationError(
                        "ACCOUNT_CREDENTIAL_CHANGE_TIMEOUT",
                        "Protected execution stayed blocked by a credential change",
                        details={"status": RunStatus.BLOCKED_DATA.value},
                    ) from exc
                await asyncio.sleep(PROTECTED_STEP_LOCK_RETRY_SECONDS)

    def _persist_blocked_pilot_projection(
        self,
        *,
        run: Mapping[str, Any],
        started_step: Mapping[str, Any],
        step: PlanStep,
        command: Command,
        raw_result: Mapping[str, Any],
        result: Any,
        error: OrchestrationError,
    ) -> None:
        """Commit projection-failure evidence after the projection UoW rolled back."""

        with self._repository.unit_of_work() as uow:
            blocked_step = uow.steps.transition(
                str(started_step["step_id"]),
                expected_version=int(started_step["version"]),
                expected_statuses=("RUNNING",),
                status=RunStatus.BLOCKED_DATA.value,
                result_summary=result.to_dict() if result is not None else dict(raw_result),
                postcondition_status="PROJECTION_INCOMPLETE",
                error_code=error.code,
                error_summary=error.message,
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            self._pilot_projection.record_incomplete_attempt(
                uow=uow,
                run=run,
                step_row=blocked_step,
                step=step,
                command=command,
                failure_code=error.code,
                result=result,
                raw_result=raw_result,
            )
            self._append_event(
                uow,
                event_type="agent.step.failed",
                run=run,
                step_id=str(started_step["step_id"]),
                payload={
                    "step_key": step.step_key,
                    "code": error.code,
                    "status": RunStatus.BLOCKED_DATA.value,
                },
            )
            uow.commit()

    async def _await_with_lease_heartbeat(
        self,
        task: asyncio.Task,
        *,
        run_id: str,
        step_id: str,
    ) -> Any:
        heartbeat_seconds = max(1.0, min(30.0, self._lease_seconds / 3))
        while True:
            done, _pending = await asyncio.wait({task}, timeout=heartbeat_seconds)
            if done:
                return await task
            try:
                await asyncio.to_thread(
                    self._repository.renew_run_lease,
                    run_id,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception as exc:
                try:
                    await self._execution_port.cancel_step(run_id=run_id, step_id=step_id)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                raise OrchestrationError(
                    "RUN_LEASE_LOST",
                    "Run lease was lost while a tool step was executing",
                    details={"status": RunStatus.BLOCKED_DATA.value},
                ) from exc

    async def _recover_interrupted_step(
        self,
        run: Mapping[str, Any],
        step: PlanStep,
        step_row: Mapping[str, Any],
        command: Command,
    ) -> dict[str, Any]:
        """Resolve a stale RUNNING/VERIFYING step without replaying an external write."""

        capability = self._catalog.get_capability(step.tool_name) or {}
        operation = step.operation_type
        if operation in {OperationType.READ, OperationType.COMPUTE}:
            return self._mark_interrupted_step_retryable(
                step_row,
                code="INTERRUPTED_READ_RETRY",
                summary="Interrupted read/compute step is safe to execute again",
            )
        if operation is OperationType.INTERNAL_PROJECTION_WRITE and _is_contractually_replay_safe(capability):
            return self._mark_interrupted_step_retryable(
                step_row,
                code="INTERRUPTED_IDEMPOTENT_PROJECTION_RETRY",
                summary="Interrupted projection has an explicit idempotency and retry contract",
            )

        reconcile = getattr(self._execution_port, "reconcile_step", None)
        if not callable(reconcile):
            self._block_interrupted_write(
                run,
                step,
                step_row,
                code="WRITE_OUTCOME_UNKNOWN",
                summary="No read-after-write reconciler is configured for the interrupted write",
                reconciliation={"resolution": "UNSUPPORTED"},
            )
        reconciliation_task = asyncio.create_task(
            reconcile(
                step,
                run_id=str(run["run_id"]),
                step_id=str(step_row["step_id"]),
                persisted_step=dict(step_row),
                execution_context=self._trusted_execution_context(command),
            )
        )
        reconciliation = await self._await_with_lease_heartbeat(
            reconciliation_task,
            run_id=str(run["run_id"]),
            step_id=str(step_row["step_id"]),
        )
        resolution = str(reconciliation.get("resolution") or "UNKNOWN").upper()
        if resolution == "APPLIED":
            raw_result = reconciliation.get("result")
            if isinstance(raw_result, Mapping):
                outcome = self._verifier.verify(step, raw_result, capability)
                if outcome.accepted:
                    with self._repository.unit_of_work() as uow:
                        completed = uow.steps.transition(
                            str(step_row["step_id"]),
                            expected_version=int(step_row["version"]),
                            expected_statuses=("RUNNING", "VERIFYING"),
                            status="COMPLETED",
                            result_summary=outcome.result.to_dict() if outcome.result else {},
                            postcondition_status="VERIFIED_AFTER_RECOVERY",
                            postcondition={"reconciliation": "APPLIED"},
                            finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        )
                        self._persist_evidence(uow, run, completed, step, outcome.result)
                        if outcome.result is not None:
                            self._pilot_projection.project_successful_step(
                                uow=uow,
                                run=run,
                                step_row=completed,
                                step=step,
                                command=command,
                                result=outcome.result,
                                generation_verification=outcome.generation_verification,
                            )
                        self._append_event(
                            uow,
                            event_type="agent.step.reconciled",
                            run=run,
                            step_id=str(step_row["step_id"]),
                            payload={"step_key": step.step_key, "resolution": "APPLIED"},
                        )
                        uow.commit()
                    return completed
            self._block_interrupted_write(
                run,
                step,
                step_row,
                code="RECONCILIATION_EVIDENCE_INVALID",
                summary="Read-after-write evidence did not satisfy the persisted plan",
                reconciliation=reconciliation,
            )
        if resolution == "NOT_APPLIED" and _is_contractually_replay_safe(capability):
            return self._mark_interrupted_step_retryable(
                step_row,
                code="RECONCILED_NOT_APPLIED",
                summary="Read-after-write verification proved the idempotent operation was not applied",
                result_summary=reconciliation,
            )
        self._block_interrupted_write(
            run,
            step,
            step_row,
            code="WRITE_OUTCOME_UNKNOWN" if resolution != "NOT_APPLIED" else "UNSAFE_WRITE_RETRY_BLOCKED",
            summary=(
                "Read-after-write verification could not determine the external write outcome"
                if resolution != "NOT_APPLIED"
                else "The write was not applied but has no explicit safe idempotent retry contract"
            ),
            reconciliation=reconciliation,
        )
        raise AssertionError("unreachable")

    def _mark_interrupted_step_retryable(
        self,
        step_row: Mapping[str, Any],
        *,
        code: str,
        summary: str,
        result_summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._repository.unit_of_work() as uow:
            updated = uow.steps.transition(
                str(step_row["step_id"]),
                expected_version=int(step_row["version"]),
                expected_statuses=("RUNNING", "VERIFYING"),
                status="FAILED_RETRYABLE",
                result_summary=dict(result_summary or {}),
                postcondition_status="RETRY_ALLOWED",
                error_code=code,
                error_summary=summary,
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            uow.commit()
        return updated

    def _block_interrupted_write(
        self,
        run: Mapping[str, Any],
        step: PlanStep,
        step_row: Mapping[str, Any],
        *,
        code: str,
        summary: str,
        reconciliation: Mapping[str, Any],
    ) -> None:
        with self._repository.unit_of_work() as uow:
            uow.steps.transition(
                str(step_row["step_id"]),
                expected_version=int(step_row["version"]),
                expected_statuses=("RUNNING", "VERIFYING"),
                status="BLOCKED_DATA",
                result_summary=dict(reconciliation),
                postcondition_status="UNKNOWN",
                postcondition={"reconciliation": str(reconciliation.get("resolution") or "UNKNOWN")},
                error_code=code,
                error_summary=summary,
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            self._append_event(
                uow,
                event_type="agent.step.reconciliation_blocked",
                run=run,
                step_id=str(step_row["step_id"]),
                payload={"step_key": step.step_key, "code": code},
            )
            uow.commit()
        raise OrchestrationError(code, summary, details={"status": RunStatus.BLOCKED_DATA.value})

    def _get_or_create_step(self, run: Mapping[str, Any], step: PlanStep, order: int) -> dict[str, Any]:
        with self._repository.unit_of_work() as uow:
            persisted = uow.steps.create_or_get(
                {
                    "step_id": new_id(),
                    "run_id": run["run_id"],
                    "step_key": step.step_key,
                    "step_order": order,
                    "tool_name": step.tool_name,
                    "tool_version": step.tool_version,
                    "operation_type": step.operation_type.value.upper(),
                    "risk_level": step.risk_level.value.upper(),
                    "status": "PENDING",
                    "requires_approval": step.requires_approval,
                    "retry_safe": bool((self._catalog.get_capability(step.tool_name) or {}).get("retry", {}).get("safe")),
                    "idempotency_key": step.idempotency_key,
                    "account_id": step.account_id,
                    "input_summary_json": {"arguments": dict(step.arguments)},
                    "input_sha256": sha256_json(step.arguments),
                }
            )
            uow.commit()
        return persisted

    @staticmethod
    def _persist_evidence(uow: Any, run: Mapping[str, Any], step_row: Mapping[str, Any], step: PlanStep, result) -> None:
        if result is None:
            return
        meta = result.meta
        observed_at = _parse_datetime(meta.get("observed_at"))
        refs = meta.get("evidence_refs") if isinstance(meta.get("evidence_refs"), list) else []
        for index, reference in enumerate(refs):
            storage_ref = str(reference or "").strip()
            if not storage_ref:
                continue
            uow.evidence.add(
                {
                    "evidence_id": new_id(),
                    "work_item_id": run["work_item_id"],
                    "run_id": run["run_id"],
                    "step_id": step_row["step_id"],
                    "source_system": str(meta.get("source_system") or "unknown"),
                    "account_id": str(meta.get("account_id") or "") or None,
                    "source_record_type": "tool_evidence",
                    "source_record_id": f"{step.step_key}:{index}",
                    "entity_type": "agent_run",
                    "entity_id": run["run_id"],
                    "observed_at": observed_at,
                    "completeness_status": "COMPLETE" if meta.get("pagination_complete") is True else "UNKNOWN",
                    "pagination_complete": meta.get("pagination_complete"),
                    "record_count": meta.get("record_count"),
                    "content_sha256": sha256_json({"storage_ref": storage_ref, "meta": meta}),
                    "summary_json": {
                        "tool_name": step.tool_name,
                        "account_id": meta.get("account_id"),
                        "pagination_complete": meta.get("pagination_complete"),
                        "record_count": meta.get("record_count"),
                    },
                    "storage_ref": storage_ref,
                }
            )

    def _load_command(self, command_id: str) -> Command:
        with self._repository.unit_of_work() as uow:
            row = uow.commands.get(command_id)
        if row is None:
            raise OrchestrationError("COMMAND_NOT_FOUND", "Run command was not found")
        try:
            actor = Actor(
                actor_type=ActorType(str(row["actor_type"])),
                actor_id=str(row["actor_id"]),
                roles=tuple(row.get("actor_roles_json") or ()),
            )
            refs = tuple(
                EntityRef(
                    entity_type=str(item["entity_type"]),
                    entity_id=str(item["entity_id"]),
                    source_system=str(item.get("source_system") or ""),
                    relation_type=str(item.get("relation_type") or "subject"),
                    metadata=dict(item.get("metadata") or {}),
                )
                for item in (row.get("entity_refs_json") or [])
            )
            raw_invocation = row.get("automation_invocation_json")
            invocation = (
                None
                if raw_invocation is None
                else AutomationProjectInvocation.from_mapping(raw_invocation)
            )
            persisted_automation_id = row.get("automation_id")
            persisted_generation = row.get("automation_generation")
            if invocation is None:
                if persisted_automation_id is not None or persisted_generation is not None:
                    raise AutomationProjectContractError(
                        "AUTOMATION_INVOCATION_REQUIRED"
                    )
            elif (
                persisted_automation_id != invocation.automation_id
                or type(persisted_generation) is not int
                or persisted_generation != invocation.automation_generation
            ):
                raise AutomationProjectContractError(
                    "AUTOMATION_INVOCATION_IDENTITY_MISMATCH"
                )
            return Command(
                command_id=str(row["command_id"]),
                command_type=str(row["command_type"]),
                source=str(row["source"]),
                actor=actor,
                parameters=dict(row.get("parameters_json") or {}),
                idempotency_key=str(row["idempotency_key"]),
                entity_refs=refs,
                automation_invocation=invocation,
                correlation_id=str(row["correlation_id"]),
                requested_at=_parse_datetime(row["requested_at"]).replace(tzinfo=timezone.utc),
            )
        except (
            AutomationProjectContractError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise OrchestrationError("INVALID_PERSISTED_COMMAND", "Persisted command is invalid") from exc

    @staticmethod
    def _plan_from_run(run: Mapping[str, Any]) -> Plan:
        raw = run.get("plan_json")
        if not isinstance(raw, Mapping):
            raise OrchestrationError("PLAN_MISSING", "Run has no persisted plan")
        try:
            steps = tuple(
                PlanStep(
                    step_key=str(item["step_key"]),
                    tool_name=str(item["tool_name"]),
                    tool_version=str(item["tool_version"]),
                    operation_type=OperationType(str(item["operation_type"])),
                    arguments=dict(item["arguments"]),
                    account_id=str(item["account_id"]) if item.get("account_id") not in (None, "") else None,
                    depends_on=tuple(item.get("depends_on") or ()),
                    idempotency_key=str(item["idempotency_key"]),
                    expected_evidence=tuple(item.get("expected_evidence") or ()),
                    postconditions=tuple(item.get("postconditions") or ()),
                    risk_level=RiskLevel(str(item["risk_level"])),
                    requires_approval=bool(item.get("requires_approval")),
                )
                for item in raw["steps"]
            )
            plan = Plan(
                command_type=str(raw["command_type"]),
                context_fingerprint=str(raw["context_fingerprint"]),
                tool_catalog_hash=str(raw["tool_catalog_hash"]),
                steps=steps,
                impact=dict(raw.get("impact") or {}),
                automation_id=raw.get("automation_id"),
                automation_generation=raw.get("automation_generation"),
                automation_contract_hash=raw.get("automation_contract_hash"),
                schema_version=int(raw.get("schema_version") or 1),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OrchestrationError("INVALID_PERSISTED_PLAN", "Persisted plan is invalid") from exc
        if plan.plan_hash != str(run.get("plan_hash") or ""):
            raise OrchestrationError("PLAN_HASH_MISMATCH", "Persisted plan hash does not match its content")
        return plan

    def _transition(self, run: Mapping[str, Any], target: RunStatus, **values: Any) -> dict[str, Any]:
        assert_run_transition(str(run["status"]), target)
        with self._repository.unit_of_work() as uow:
            updated = uow.runs.transition(
                str(run["run_id"]),
                expected_version=int(run["version"]),
                expected_statuses=(str(run["status"]),),
                status=target.value,
                **values,
            )
            self._sync_work_item_status(uow, updated, target)
            self._append_event(
                uow,
                event_type="agent.run.status_changed",
                run=updated,
                payload={"from": str(run["status"]), "to": target.value},
            )
            uow.commit()
        return updated

    def _complete_run_and_supersede_scheduler_failures(
        self,
        run: Mapping[str, Any],
        command: Command,
    ) -> dict[str, Any]:
        """Complete a run and atomically retire only superseded scheduler failures."""

        identity = self._scheduler_automation_identity(command)
        if identity is None:
            return self._transition(
                run,
                RunStatus.COMPLETED,
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )

        scheduler_task_id, automation_id, successful_occurrence = identity
        assert_run_transition(str(run["status"]), RunStatus.COMPLETED)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._repository.unit_of_work() as uow:
            updated = uow.runs.transition(
                str(run["run_id"]),
                expected_version=int(run["version"]),
                expected_statuses=(str(run["status"]),),
                status=RunStatus.COMPLETED.value,
                finished_at=now,
            )
            self._sync_work_item_status(uow, updated, RunStatus.COMPLETED)
            self._append_event(
                uow,
                event_type="agent.run.status_changed",
                run=updated,
                payload={"from": str(run["status"]), "to": RunStatus.COMPLETED.value},
            )
            self._supersede_scheduler_failure_batch(
                uow,
                successful_run=updated,
                scheduler_task_id=scheduler_task_id,
                automation_id=automation_id,
                successful_occurrence=successful_occurrence,
                occurred_at=now,
            )
            uow.commit()
        self._drain_scheduler_failure_supersession(
            successful_run=updated,
            scheduler_task_id=scheduler_task_id,
            automation_id=automation_id,
            successful_occurrence=successful_occurrence,
        )
        return updated

    def _drain_scheduler_failure_supersession(
        self,
        *,
        successful_run: Mapping[str, Any],
        scheduler_task_id: str,
        automation_id: str,
        successful_occurrence: datetime,
    ) -> None:
        """Drain additional bounded batches after the successful completion commits."""

        try:
            no_progress_batches = 0
            while True:
                with self._repository.unit_of_work() as uow:
                    selected, retired = self._supersede_scheduler_failure_batch(
                        uow,
                        successful_run=successful_run,
                        scheduler_task_id=scheduler_task_id,
                        automation_id=automation_id,
                        successful_occurrence=successful_occurrence,
                        occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                    if selected == 0:
                        return
                    if retired:
                        no_progress_batches = 0
                        uow.commit()
                    else:
                        no_progress_batches += 1
                if no_progress_batches >= SCHEDULER_SUPERSESSION_MAX_NO_PROGRESS_BATCHES:
                    logger.warning(
                        "scheduler supersession drain stopped after bounded no-progress retries",
                        extra={
                            "run_id": successful_run.get("run_id"),
                            "scheduler_task_id": scheduler_task_id,
                            "automation_id": automation_id,
                            "no_progress_batches": no_progress_batches,
                        },
                    )
                    return
        except Exception:
            logger.exception(
                "scheduler supersession drain failed after successful run commit",
                extra={
                    "run_id": successful_run.get("run_id"),
                    "scheduler_task_id": scheduler_task_id,
                    "automation_id": automation_id,
                },
            )

    def _supersede_scheduler_failure_batch(
        self,
        uow: Any,
        *,
        successful_run: Mapping[str, Any],
        scheduler_task_id: str,
        automation_id: str,
        successful_occurrence: datetime,
        occurred_at: datetime,
    ) -> tuple[int, int]:
        """Cancel one bounded, revalidated batch of older scheduler failures."""

        candidate_run_ids = uow.runs.list_open_failed_scheduler_run_ids_for_supersession(
            automation_id=automation_id,
            scheduler_task_id=scheduler_task_id,
            successful_work_item_id=str(successful_run["work_item_id"]),
            successful_occurrence=successful_occurrence,
        )
        retired = 0
        for candidate_run_id in candidate_run_ids:
            prior, prior_item = self._lock_supersedable_scheduler_failure(
                uow,
                run_id=candidate_run_id,
                scheduler_task_id=scheduler_task_id,
                automation_id=automation_id,
                successful_work_item_id=str(successful_run["work_item_id"]),
                successful_occurrence=successful_occurrence,
            )
            if prior is None or prior_item is None:
                continue
            assert_work_item_transition(WorkItemStatus.OPEN, WorkItemStatus.CANCELLED)
            superseded = uow.work_items.transition(
                str(prior_item["work_item_id"]),
                expected_version=int(prior_item["version"]),
                expected_statuses=(WorkItemStatus.OPEN.value,),
                status=WorkItemStatus.CANCELLED.value,
                reason_code="SUPERSEDED_BY_LATER_SUCCESS",
                reason_summary="已由后续成功运行取代",
                resolution={"successful_run_id": successful_run["run_id"]},
                closed_at=occurred_at,
            )
            self._append_work_item_supersession_event(
                uow,
                prior_work_item=superseded,
                successful_run=successful_run,
                failed_run_id=str(prior["run_id"]),
                scheduler_task_id=scheduler_task_id,
                automation_id=automation_id,
                occurred_at=occurred_at,
            )
            retired += 1
        return len(candidate_run_ids), retired

    @staticmethod
    def _scheduler_automation_identity(command: Command) -> tuple[str, str, datetime] | None:
        invocation = command.automation_invocation
        if (
            command.source != "scheduler"
            or command.actor.actor_type is not ActorType.SCHEDULER
            or invocation is None
        ):
            return None
        task_id = str(command.actor.actor_id or "").strip()
        automation_id = str(invocation.automation_id or "").strip()
        execution_context = command.parameters.get("execution_context")
        if (
            not task_id
            or not automation_id
            or not isinstance(execution_context, Mapping)
            or str(execution_context.get("task_id") or "").strip() != task_id
        ):
            return None
        occurrence = _scheduler_occurrence(command.parameters, command.requested_at)
        if occurrence is None:
            return None
        return task_id, automation_id, occurrence

    @staticmethod
    def _lock_supersedable_scheduler_failure(
        uow: Any,
        *,
        run_id: str,
        scheduler_task_id: str,
        automation_id: str,
        successful_work_item_id: str,
        successful_occurrence: datetime,
    ) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        """Lock and revalidate one candidate in the terminal-retry lock order."""

        prior = uow.runs.get(run_id, for_update=True)
        if prior is None or str(prior.get("status") or "") != RunStatus.FAILED_TERMINAL.value:
            return None, None
        latest = uow.runs.get_latest_for_work_item(
            str(prior["work_item_id"]),
            for_update=True,
        )
        if latest is None or str(latest.get("run_id") or "") != str(prior["run_id"]):
            return None, None
        prior_command = uow.commands.get(str(prior["command_id"]), for_update=True)
        if prior_command is None or not _matches_scheduler_automation_identity(
            prior_command,
            scheduler_task_id=scheduler_task_id,
            automation_id=automation_id,
        ):
            return None, None
        prior_occurrence = _scheduler_occurrence(
            prior_command.get("parameters_json"),
            prior_command.get("requested_at"),
        )
        if prior_occurrence is None or prior_occurrence >= successful_occurrence:
            return None, None
        prior_item = uow.work_items.get(str(prior["work_item_id"]), for_update=True)
        if (
            prior_item is None
            or str(prior_item.get("work_item_id") or "") == successful_work_item_id
            or str(prior_item.get("status") or "") != WorkItemStatus.OPEN.value
        ):
            return None, None
        return prior, prior_item

    def _release(
        self,
        run_id: str,
        *,
        status: str,
        error_code: str | None = None,
        error_summary: str | None = None,
        finished: bool = False,
        honor_cancel_request: bool = False,
    ) -> None:
        with self._repository.unit_of_work() as uow:
            current = uow.runs.get(run_id, for_update=True)
            if current is None:
                raise OrchestrationError("RUN_NOT_FOUND", "Run was not found while releasing its lease")
            if (
                honor_cancel_request
                and current.get("cancel_requested_at")
                and status != RunStatus.CANCELLED.value
                and not _is_governing_unknown_write(status, error_code)
            ):
                status = RunStatus.CANCELLED.value
                error_code = "CANCELLED_BY_ACTOR"
                error_summary = str(
                    current.get("cancel_reason")
                    or "Run cancellation was requested"
                )
                finished = True
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            next_attempt_at = None
            if status == RunStatus.WAITING_APPROVAL.value:
                if error_code == "APPROVAL_REQUEST_PENDING":
                    # A persistence/Outbox failure is recoverable but should
                    # not hot-loop merely because the previous approval is
                    # already INVALIDATED or EXPIRED.
                    next_attempt_at = now + timedelta(seconds=5)
                latest = uow.approvals.get_latest_for_run(
                    run_id,
                    for_update=False,
                )
                approval_status = (
                    str(latest.get("status") or "")
                    if isinstance(latest, Mapping)
                    else ""
                )
                expires_at = (
                    latest.get("expires_at")
                    if isinstance(latest, Mapping)
                    and approval_status == "PENDING"
                    else None
                )
                if next_attempt_at is not None:
                    pass
                elif isinstance(expires_at, datetime):
                    if expires_at.tzinfo is not None:
                        expires_at = expires_at.astimezone(timezone.utc).replace(
                            tzinfo=None
                        )
                    next_attempt_at = max(expires_at, now)
                elif approval_status:
                    # A decision or invalidation that raced with this worker's
                    # short lease must remain immediately claimable.
                    next_attempt_at = now
                else:
                    # No durable pending approval exists, so retry only the
                    # approval request/recovery path after a short delay.
                    next_attempt_at = now + timedelta(seconds=5)
            elif status == RunStatus.FAILED_RETRYABLE.value:
                next_attempt_at = now + timedelta(seconds=5)
            updated = uow.runs.release_or_schedule(
                run_id,
                worker_id=self._worker_id,
                status=status,
                error_code=error_code,
                error_summary=error_summary,
                retryable=status == RunStatus.FAILED_RETRYABLE.value,
                next_attempt_at=next_attempt_at,
                finished_at=now if finished else None,
            )
            target = RunStatus(status)
            if str(current.get("status")) != status:
                assert_run_transition(str(current["status"]), target)
                self._sync_work_item_status(uow, updated, target, error_code=error_code, error_summary=error_summary)
                self._append_event(
                    uow,
                    event_type="agent.run.status_changed",
                    run=updated,
                    payload={
                        "from": str(current["status"]),
                        "to": status,
                        "error_code": error_code,
                        "error_summary": error_summary,
                    },
                )
            uow.commit()

    @staticmethod
    def _sync_work_item_status(
        uow: Any,
        run: Mapping[str, Any],
        run_status: RunStatus,
        *,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        desired = _work_item_status_for_run(run_status)
        item = uow.work_items.get(str(run["work_item_id"]), for_update=True)
        if item is None:
            raise OrchestrationError("WORK_ITEM_NOT_FOUND", "Run work item was not found")
        current = WorkItemStatus(str(item["status"]))
        if current is desired:
            return
        assert_work_item_transition(current, desired)
        uow.work_items.transition(
            str(item["work_item_id"]),
            expected_version=int(item["version"]),
            expected_statuses=(current.value,),
            status=desired.value,
            reason_code=error_code,
            reason_summary=error_summary,
            resolution={"run_id": run["run_id"]} if desired is WorkItemStatus.RESOLVED else None,
            closed_at=(
                datetime.now(timezone.utc).replace(tzinfo=None)
                if desired in {WorkItemStatus.RESOLVED, WorkItemStatus.CANCELLED}
                else None
            ),
        )

    @staticmethod
    def _append_event(
        uow: Any,
        *,
        event_type: str,
        run: Mapping[str, Any],
        payload: Mapping[str, Any],
        step_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": event_type,
                "schema_version": 1,
                "source_system": "agent",
                "source_event_id": None,
                "entity_type": "agent_run",
                "entity_id": run["run_id"],
                "work_item_id": run["work_item_id"],
                "run_id": run["run_id"],
                "step_id": step_id,
                "occurred_at": now,
                "observed_at": now,
                "correlation_id": run["correlation_id"],
                "causation_id": run.get("causation_id"),
                "payload": dict(payload),
            },
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": event_type,
                    "partition_key": str(run["work_item_id"]),
                    "max_attempts": 10,
                },
            ),
        )

    @staticmethod
    def _append_work_item_supersession_event(
        uow: Any,
        *,
        prior_work_item: Mapping[str, Any],
        successful_run: Mapping[str, Any],
        failed_run_id: str,
        scheduler_task_id: str,
        automation_id: str,
        occurred_at: datetime,
    ) -> None:
        work_item_id = str(prior_work_item["work_item_id"])
        successful_run_id = str(successful_run["run_id"])
        uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": "work_item.superseded_by_later_success",
                "schema_version": 1,
                "source_system": "agent",
                "source_event_id": f"{work_item_id}:{successful_run_id}",
                "entity_type": "work_item",
                "entity_id": work_item_id,
                "work_item_id": work_item_id,
                "run_id": failed_run_id,
                "step_id": None,
                "occurred_at": occurred_at,
                "observed_at": occurred_at,
                "correlation_id": successful_run["correlation_id"],
                "causation_id": successful_run.get("causation_id"),
                "payload": {
                    "successful_run_id": successful_run_id,
                    "failed_run_id": failed_run_id,
                    "scheduler_task_id": scheduler_task_id,
                    "automation_id": automation_id,
                },
            },
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": "work_item.superseded_by_later_success",
                    "partition_key": work_item_id,
                    "max_attempts": 10,
                },
            ),
        )

    def _fail_claimed(self, run_id: str, exc: OrchestrationError) -> None:
        desired = str(exc.details.get("status") or "")
        if desired not in {
            RunStatus.NEEDS_CLARIFICATION.value,
            RunStatus.BLOCKED_LOGIN.value,
            RunStatus.BLOCKED_DATA.value,
            RunStatus.FAILED_RETRYABLE.value,
            RunStatus.FAILED_TERMINAL.value,
            RunStatus.CANCELLED.value,
        }:
            if exc.code in {"ACCOUNT_REQUIRED", "ACCOUNT_AMBIGUOUS", "TOOL_NAME_REQUIRED"}:
                desired = RunStatus.NEEDS_CLARIFICATION.value
            elif exc.code in {"LOGIN_REQUIRED", "AUTH_REQUIRED", "SESSION_EXPIRED"}:
                desired = RunStatus.BLOCKED_LOGIN.value
            elif exc.code in {"SOURCE_INCOMPLETE", "PAGINATION_INCOMPLETE", "EVIDENCE_MISSING"}:
                desired = RunStatus.BLOCKED_DATA.value
            else:
                desired = RunStatus.FAILED_TERMINAL.value
        if desired == RunStatus.FAILED_RETRYABLE.value and not self._can_retry_run(run_id):
            desired = RunStatus.FAILED_TERMINAL.value
            exc = OrchestrationError(
                "UNSAFE_RETRY_BLOCKED",
                "The failed operation is not explicitly safe and idempotent to retry",
            )
        self._release(
            run_id,
            status=desired,
            error_code=exc.code,
            error_summary=exc.message,
            finished=desired in {
                RunStatus.FAILED_TERMINAL.value,
                RunStatus.CANCELLED.value,
            },
            honor_cancel_request=True,
        )

    def _can_retry_run(self, run_id: str) -> bool:
        run = self._repository.get_run(run_id) or {}
        if int(run.get("execution_attempt_count") or 0) >= 3:
            return False
        raw_plan = run.get("plan_json")
        if not isinstance(raw_plan, Mapping):
            return True
        for step in raw_plan.get("steps") or []:
            if not isinstance(step, Mapping):
                return False
            capability = self._catalog.get_capability(str(step.get("tool_name") or "")) or {}
            retry = capability.get("retry") if isinstance(capability.get("retry"), Mapping) else {}
            idempotency = (
                capability.get("idempotency")
                if isinstance(capability.get("idempotency"), Mapping)
                else {}
            )
            operation = str(capability.get("operation_type") or "")
            if operation not in {"read", "compute"} and (
                not bool(retry.get("safe")) or str(idempotency.get("mode") or "") != "key"
            ):
                return False
        return True


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    raise OrchestrationError("INVALID_TIMESTAMP", "Persisted timestamp is invalid")


def _scheduler_occurrence(
    parameters: Any,
    requested_at: Any,
) -> datetime | None:
    """Return the scheduler occurrence in UTC, with requested time as fallback."""

    context = parameters.get("execution_context") if isinstance(parameters, Mapping) else None
    scheduled_for = context.get("scheduled_for") if isinstance(context, Mapping) else None
    if isinstance(scheduled_for, str) and scheduled_for.strip():
        try:
            parsed = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(requested_at, datetime):
        if requested_at.tzinfo is not None:
            return requested_at.astimezone(timezone.utc).replace(tzinfo=None)
        return requested_at
    if isinstance(requested_at, str):
        try:
            parsed = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def _annotate_approval(plan: Plan, requires_approval: bool) -> Plan:
    return replace(
        plan,
        steps=tuple(replace(step, requires_approval=requires_approval) for step in plan.steps),
    )


def _noop_finish() -> None:
    return None


def _is_started_write_step(
    step_row: Mapping[str, Any],
    step_operations: Mapping[str, OperationType],
) -> bool:
    status = str(step_row.get("status") or "").strip().upper()
    if status in {"", "PENDING"}:
        return False
    raw_operation = str(step_row.get("operation_type") or "").strip().lower()
    try:
        operation = OperationType(raw_operation)
    except ValueError:
        operation = step_operations.get(str(step_row.get("step_key") or ""))
    if operation is None:
        return True
    return operation not in {OperationType.READ, OperationType.COMPUTE}


def _is_contractually_replay_safe(capability: Mapping[str, Any]) -> bool:
    retry = capability.get("retry") if isinstance(capability.get("retry"), Mapping) else {}
    idempotency = (
        capability.get("idempotency")
        if isinstance(capability.get("idempotency"), Mapping)
        else {}
    )
    return bool(retry.get("safe")) and str(idempotency.get("mode") or "") == "key"


def _is_governing_unknown_write(status: str, error_code: str | None) -> bool:
    """Keep a durable started-write unknown outcome ahead of cancellation.

    A cancellation request can stop further work, but it cannot make a write
    that already crossed the signed broker boundary safe to replay or appear
    conclusively cancelled.
    """

    return (
        status == RunStatus.BLOCKED_DATA.value
        and str(error_code or "").upper() == "WRITE_OUTCOME_UNKNOWN"
    )


def _matches_scheduler_automation_identity(
    command: Mapping[str, Any],
    *,
    scheduler_task_id: str,
    automation_id: str,
) -> bool:
    execution_context = command.get("parameters_json")
    if not isinstance(execution_context, Mapping):
        return False
    execution_context = execution_context.get("execution_context")
    return (
        str(command.get("source") or "") == "scheduler"
        and str(command.get("actor_type") or "") == ActorType.SCHEDULER.value
        and str(command.get("actor_id") or "") == scheduler_task_id
        and str(command.get("automation_id") or "") == automation_id
        and isinstance(execution_context, Mapping)
        and str(execution_context.get("task_id") or "") == scheduler_task_id
    )


def _work_item_status_for_run(status: RunStatus) -> WorkItemStatus:
    mapping = {
        RunStatus.RECEIVED: WorkItemStatus.OPEN,
        RunStatus.CONTEXT_READY: WorkItemStatus.IN_PROGRESS,
        RunStatus.PLANNED: WorkItemStatus.IN_PROGRESS,
        RunStatus.VALIDATED: WorkItemStatus.IN_PROGRESS,
        RunStatus.RUNNING: WorkItemStatus.IN_PROGRESS,
        RunStatus.VERIFYING: WorkItemStatus.IN_PROGRESS,
        RunStatus.WAITING_APPROVAL: WorkItemStatus.WAITING_APPROVAL,
        RunStatus.NEEDS_CLARIFICATION: WorkItemStatus.NEEDS_CLARIFICATION,
        RunStatus.BLOCKED_LOGIN: WorkItemStatus.BLOCKED_LOGIN,
        RunStatus.BLOCKED_DATA: WorkItemStatus.BLOCKED_DATA,
        RunStatus.COMPLETED: WorkItemStatus.RESOLVED,
        RunStatus.CANCELLED: WorkItemStatus.CANCELLED,
        RunStatus.PARTIAL: WorkItemStatus.OPEN,
        RunStatus.FAILED_RETRYABLE: WorkItemStatus.OPEN,
        RunStatus.FAILED_TERMINAL: WorkItemStatus.OPEN,
    }
    return mapping[status]
