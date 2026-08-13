"""Persistent approval requests bound to an immutable plan hash."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from agent.orchestration.models import Actor, OrchestrationError, Plan, RiskLevel, new_id, sha256_json
from agent.orchestration.policy_engine import PolicyDecision, PolicyEngine
from shared.orchestration_repository import InvalidStateError


APPROVAL_TTL = timedelta(minutes=15)


class ApprovalService:
    def __init__(self, repository: Any, policy: PolicyEngine, *, wake_runner=None) -> None:
        self._repository = repository
        self._policy = policy
        self._wake_runner = wake_runner

    def request(
        self,
        *,
        run: dict[str, Any],
        plan: Plan,
        policy_decision: PolicyDecision,
        requested_by: Actor,
    ) -> dict[str, Any]:
        if not policy_decision.requires_approval or not policy_decision.required_role:
            raise OrchestrationError("APPROVAL_NOT_REQUIRED", "The plan does not require approval")
        now = datetime.now(timezone.utc)
        with self._repository.unit_of_work() as uow:
            uow.approvals.expire_stale(str(run["run_id"]), plan.plan_hash)
            latest = uow.approvals.get_latest_for_run(str(run["run_id"]), for_update=True)
            if latest and (
                str(latest.get("plan_hash") or "") == plan.plan_hash
                and str(latest.get("status") or "") in {"PENDING", "APPROVED", "REJECTED"}
            ):
                uow.commit()
                return latest
            approval_round = int((latest or {}).get("approval_round") or 0) + 1
            if latest and latest.get("status") in {"PENDING", "APPROVED"}:
                uow.approvals.invalidate_pending(run_id=str(run["run_id"]))
            approval = uow.approvals.create_or_get(
                {
                    "approval_id": new_id(),
                    "work_item_id": run["work_item_id"],
                    "run_id": run["run_id"],
                    "approval_round": approval_round,
                    "plan_hash": plan.plan_hash,
                    "impact": dict(plan.impact),
                    "impact_sha256": sha256_json(plan.impact),
                    "risk_level": policy_decision.risk_level.value.upper(),
                    "required_approvals": 1,
                    "required_role": policy_decision.required_role,
                    "status": "PENDING",
                    "requested_by_type": requested_by.actor_type.value,
                    "requested_by_id": requested_by.actor_id,
                    "expires_at": (now + APPROVAL_TTL).replace(tzinfo=None),
                }
            )
            run_row = uow.runs.get(str(run["run_id"]), for_update=False) or run
            self._append_event(
                uow,
                event_type="agent.approval.requested",
                approval=approval,
                run=run_row,
                payload={
                    "approval_round": approval_round,
                    "plan_hash": plan.plan_hash,
                    "impact_sha256": sha256_json(plan.impact),
                    "risk_level": policy_decision.risk_level.value.upper(),
                    "required_role": policy_decision.required_role,
                    "expires_at": (now + APPROVAL_TTL).isoformat(),
                },
            )
            uow.commit()
        return approval

    def decide(
        self,
        *,
        approval_id: str,
        plan_hash: str,
        actor: Actor,
        source: str,
        decision: str,
        comment: str = "",
    ) -> dict[str, Any]:
        normalized_decision = str(decision or "").strip().upper()
        if normalized_decision not in {"APPROVED", "REJECTED"}:
            raise OrchestrationError("INVALID_APPROVAL_DECISION", "Decision must be APPROVED or REJECTED")
        approval = self._repository.get_approval(approval_id)
        if approval is None:
            raise OrchestrationError("APPROVAL_NOT_FOUND", "Approval request was not found")
        required_role = str(approval.get("required_role") or "").strip()
        if not required_role:
            raise OrchestrationError("INVALID_APPROVAL_REQUEST", "Approval request has no required role")
        if not self._policy.can_decide(actor, required_role=required_role, source=source):
            raise OrchestrationError("APPROVAL_FORBIDDEN", "The current administrator cannot decide this approval")
        if str(approval.get("plan_hash") or "") != str(plan_hash or ""):
            raise OrchestrationError("PLAN_STALE", "Approval plan hash no longer matches")
        if str(approval.get("status") or "") != "PENDING":
            raise OrchestrationError("APPROVAL_NOT_PENDING", "Approval request is no longer pending")
        decision_error = ""
        try:
            with self._repository.unit_of_work() as uow:
                current = uow.approvals.record_decision(
                    {
                        "decision_id": new_id(),
                        "approval_id": approval_id,
                        "actor_type": actor.actor_type.value,
                        "actor_id": actor.actor_id,
                        "actor_roles": list(actor.roles),
                        "decision": normalized_decision,
                        "reason": str(comment or "")[:500],
                        "decided_at": datetime.now(timezone.utc).replace(tzinfo=None),
                    },
                    expected_plan_hash=plan_hash,
                )
                decision_error = str(current.get("_decision_error") or "")
                run = uow.runs.get(str(current["run_id"]), for_update=False)
                if run is None:
                    raise OrchestrationError("RUN_NOT_FOUND", "Approval run was not found")
                self._append_event(
                    uow,
                    event_type=(
                        "agent.approval.expired"
                        if decision_error == "APPROVAL_EXPIRED"
                        else "agent.approval.decided"
                    ),
                    approval=current,
                    run=run,
                    payload={
                        "decision": "EXPIRED" if decision_error else normalized_decision,
                        "plan_hash": plan_hash,
                        "actor_type": actor.actor_type.value,
                        "actor_id": actor.actor_id,
                        "actor_roles": list(actor.roles),
                        "comment": str(comment or "")[:500],
                    },
                )
                uow.commit()
        except InvalidStateError as exc:
            message = str(exc).lower()
            if "expired" in message:
                code = "APPROVAL_EXPIRED"
            elif "stale" in message or "hash" in message:
                code = "PLAN_STALE"
            else:
                code = "APPROVAL_NOT_PENDING"
            raise OrchestrationError(code, "Approval decision lost a concurrent state change") from exc
        if decision_error == "APPROVAL_EXPIRED":
            raise OrchestrationError("APPROVAL_EXPIRED", "Approval request has expired")
        if self._wake_runner is not None:
            self._wake_runner(str(current["run_id"]))
        return current

    def expire(self, approval_id: str) -> dict[str, Any]:
        with self._repository.unit_of_work() as uow:
            approval = uow.approvals.expire(approval_id)
            uow.commit()
        return approval

    def invalidate_for_stale_plan(self, run_id: str) -> None:
        with self._repository.unit_of_work() as uow:
            uow.approvals.invalidate_pending(run_id=run_id)
            uow.commit()

    @staticmethod
    def _append_event(
        uow: Any,
        *,
        event_type: str,
        approval: dict[str, Any],
        run: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": event_type,
                "schema_version": 1,
                "source_system": "agent",
                "source_event_id": None,
                "entity_type": "approval_request",
                "entity_id": approval["approval_id"],
                "work_item_id": approval["work_item_id"],
                "run_id": approval["run_id"],
                "step_id": None,
                "occurred_at": now,
                "observed_at": now,
                "correlation_id": run["correlation_id"],
                "causation_id": run.get("causation_id"),
                "payload": payload,
            },
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": event_type,
                    "partition_key": str(approval["work_item_id"]),
                    "max_attempts": 10,
                },
            ),
        )
