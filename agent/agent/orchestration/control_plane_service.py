"""Safe business facade for FastAPI control-plane routes.

The service owns state/action semantics but has no HTTP, SQL, environment, or
tool-executor dependency.  Persistence mutations go through the existing
repository/UoW abstractions and all externally returned rows are reduced to
explicit, recursively redacted DTOs.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from agent.automation_plugins.first_party_handlers import customer_problem_identity
from agent.orchestration.models import (
    Actor,
    OrchestrationError,
    RunStatus,
    canonical_json,
    new_id,
)
from shared.redaction import is_sensitive_key, redact_sensitive, redact_text


WakeRunner = Callable[[str], Any]
CancelActive = Callable[[str], Awaitable[Any] | Any]

_RUN_PUBLIC_FIELDS = (
    "run_id",
    "work_item_id",
    "command_id",
    "run_no",
    "status",
    "mode",
    "planner_kind",
    "plan_schema_version",
    "plan_hash",
    "tool_catalog_sha256",
    "context_fingerprint_sha256",
    "correlation_id",
    "causation_id",
    "retry_of_run_id",
    "error_code",
    "error_summary",
    "retryable",
    "execution_attempt_count",
    "next_attempt_at",
    "cancel_requested_at",
    "cancel_requested_by_type",
    "cancel_requested_by_id",
    "cancel_reason",
    "version",
    "started_at",
    "finished_at",
    "created_at",
    "updated_at",
)
_STEP_PUBLIC_FIELDS = (
    "step_id",
    "run_id",
    "step_key",
    "step_order",
    "tool_name",
    "tool_version",
    "operation_type",
    "risk_level",
    "status",
    "requires_approval",
    "retry_safe",
    "account_id",
    "attempt_count",
    "postcondition_status",
    "error_code",
    "error_summary",
    "version",
    "started_at",
    "finished_at",
    "created_at",
    "updated_at",
)
_WORK_ITEM_PUBLIC_FIELDS = (
    "work_item_id",
    "command_id",
    "type",
    "title",
    "status",
    "priority",
    "source",
    "owner_type",
    "owner_id",
    "sla_deadline",
    "current_reason_code",
    "current_reason_summary",
    "version",
    "closed_at",
    "created_at",
    "updated_at",
)
_ENTITY_PUBLIC_FIELDS = (
    "relation_type",
    "entity_type",
    "entity_id",
    "source_system",
)
_APPROVAL_PUBLIC_FIELDS = (
    "approval_id",
    "work_item_id",
    "run_id",
    "approval_round",
    "plan_hash",
    "risk_level",
    "required_approvals",
    "required_role",
    "status",
    "requested_by_type",
    "requested_by_id",
    "expires_at",
    "decided_at",
    "created_at",
    "updated_at",
)
_EVENT_PUBLIC_FIELDS = (
    "event_id",
    "event_type",
    "schema_version",
    "source_system",
    "entity_type",
    "entity_id",
    "work_item_id",
    "run_id",
    "step_id",
    "occurred_at",
    "observed_at",
    "correlation_id",
    "causation_id",
    "created_at",
)
_EVIDENCE_PUBLIC_FIELDS = (
    "evidence_id",
    "work_item_id",
    "run_id",
    "step_id",
    "source_system",
    "account_id",
    "source_record_type",
    "source_record_id",
    "entity_type",
    "entity_id",
    "occurred_at",
    "observed_at",
    "completeness_status",
    "pagination_complete",
    "record_count",
    "storage_ref",
    "created_at",
)


class ControlPlaneService:
    """Pure control-plane use cases for an injected FastAPI composition root."""

    def __init__(
        self,
        repository: Any,
        approval_service: Any,
        *,
        wake_runner: WakeRunner | None = None,
        cancel_active: CancelActive | None = None,
        wake_outbox: Callable[[], Any] | None = None,
    ) -> None:
        self._repository = repository
        self._approval_service = approval_service
        self._wake_runner = wake_runner
        self._cancel_active = cancel_active
        self._wake_outbox = wake_outbox

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        return {
            "run": _run_dto(run),
            "allowed_actions": _run_actions(str(run.get("status") or "")),
            "next_poll_after_ms": _next_poll_after_ms(str(run.get("status") or "")),
        }

    def list_work_items(
        self,
        *,
        status: str | None = None,
        item_type: str | None = None,
        priority: str | None = None,
        source: str | None = None,
        query: str | None = None,
        owner_type: str | None = None,
        owner_id: str | None = None,
        sla_from: datetime | None = None,
        sla_before: datetime | None = None,
        sla_missing: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        bounded_limit = _bounded_limit(limit, maximum=500)
        bounded_offset = max(0, int(offset))
        rows = self._repository.list_work_items(
            status=status,
            item_type=item_type,
            priority=priority,
            source=source,
            query=query,
            owner_type=owner_type,
            owner_id=owner_id,
            sla_from=sla_from,
            sla_before=sla_before,
            sla_missing=sla_missing,
            limit=bounded_limit,
            offset=bounded_offset,
        )
        return {
            "items": [_work_item_dto(row) for row in rows],
            "limit": bounded_limit,
            "offset": bounded_offset,
            "has_next": len(rows) == bounded_limit,
        }

    def get_work_item(self, work_item_id: str) -> dict[str, Any]:
        item = self._repository.get_work_item(_required_id(work_item_id, "work_item_id"))
        if item is None:
            raise OrchestrationError("WORK_ITEM_NOT_FOUND", "Work item was not found")
        runs = self._list_runs_for_work_item(str(item["work_item_id"]))
        current = runs[-1] if runs else None
        approval = None
        if current is not None:
            approval = self._repository.get_current_approval(str(current["run_id"]))
        result: dict[str, Any] = {
            "work_item": _work_item_dto(item),
            "runs": [_run_dto(run) for run in runs],
            "allowed_actions": _detail_actions(item, current, approval),
        }
        if current is not None:
            result["run"] = _run_dto(current)
            result["current_run_id"] = str(current["run_id"])
            plan = current.get("plan_json")
            if isinstance(plan, Mapping):
                result["plan"] = _safe_plan(plan)
        if approval is not None:
            result["approval"] = _approval_dto(approval)
        return result

    def get_timeline(self, work_item_id: str, *, limit: int = 500) -> dict[str, Any]:
        item_id = self._require_work_item_id(work_item_id)
        bounded_limit = _bounded_limit(limit, maximum=1000)
        rows = self._repository.get_timeline(item_id, limit=bounded_limit)
        return {"items": [_event_dto(row) for row in rows], "limit": bounded_limit}

    def list_evidence(
        self,
        work_item_id: str,
        *,
        run_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        item_id = self._require_work_item_id(work_item_id)
        bounded_limit = _bounded_limit(limit, maximum=500)
        if run_id:
            run = self._require_run(run_id)
            if str(run.get("work_item_id") or "") != item_id:
                raise OrchestrationError("RUN_WORK_ITEM_MISMATCH", "Run does not belong to the work item")
        rows = self._repository.list_evidence(
            item_id,
            run_id=_required_id(run_id, "run_id") if run_id else None,
            limit=bounded_limit,
        )
        return {"items": [_evidence_dto(row) for row in rows], "limit": bounded_limit}

    async def cancel_run(self, run_id: str, *, actor: Actor, comment: str = "") -> dict[str, Any]:
        current = self._require_run(run_id)
        if RunStatus(str(current["status"])) in {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
        }:
            raise OrchestrationError("RUN_TERMINAL", "Terminal run cannot accept cancellation")
        safe_comment = _bounded_text(comment, 500)
        with self._repository.unit_of_work() as uow:
            locked = uow.runs.get(str(current["run_id"]), for_update=True)
            if locked is None:
                raise OrchestrationError("RUN_NOT_FOUND", "Run was not found")
            try:
                locked_status = RunStatus(str(locked.get("status") or ""))
            except ValueError as exc:
                raise OrchestrationError(
                    "UNKNOWN_RUN_STATUS",
                    "Persisted run has an unknown status",
                ) from exc
            if locked_status in {
                RunStatus.COMPLETED,
                RunStatus.PARTIAL,
                RunStatus.FAILED_TERMINAL,
                RunStatus.CANCELLED,
            }:
                raise OrchestrationError("RUN_TERMINAL", "Terminal run cannot accept cancellation")
            run = uow.runs.request_cancel(
                str(locked["run_id"]),
                requested_by_type=actor.actor_type.value,
                requested_by_id=actor.actor_id,
                reason=safe_comment,
            )
            event = self._append_action_event(
                uow,
                run=run,
                event_type="agent.run.cancel_requested",
                actor=actor,
                payload={
                    "previous_status": locked_status.value,
                    "comment": safe_comment,
                },
            )
            uow.commit()
        if self._cancel_active is not None:
            await _maybe_await(self._cancel_active(str(run["run_id"])))
        self._wake(str(run["run_id"]))
        self._wake_events()
        return {"run": _run_dto(run), "event": _event_dto(event)}

    def retry_run(self, run_id: str, *, actor: Actor, reason: str = "") -> dict[str, Any]:
        source = self._require_run(run_id)
        status = RunStatus(str(source["status"]))
        if status in {
            RunStatus.FAILED_RETRYABLE,
            RunStatus.PARTIAL,
            RunStatus.FAILED_TERMINAL,
        }:
            _require_manual_retry_safe_plan(source)
        event_type: str
        if status is RunStatus.FAILED_RETRYABLE:
            with self._repository.unit_of_work() as uow:
                run = uow.runs.transition(
                    str(source["run_id"]),
                    expected_version=int(source["version"]),
                    expected_statuses=(RunStatus.FAILED_RETRYABLE.value,),
                    status=RunStatus.CONTEXT_READY.value,
                    error_code=None,
                    error_summary=None,
                    retryable=False,
                    next_attempt_at=_naive_utc_now(),
                    finished_at=None,
                )
                event_type = "agent.run.retry_requested"
                self._append_action_event(
                    uow,
                    run=run,
                    event_type=event_type,
                    actor=actor,
                    payload={
                        "source_run_id": str(source["run_id"]),
                        "continued_run_id": str(run["run_id"]),
                        "reason": _bounded_text(reason, 500),
                    },
                )
                uow.commit()
        elif status in {RunStatus.PARTIAL, RunStatus.FAILED_TERMINAL}:
            with self._repository.unit_of_work() as uow:
                create_retry = getattr(uow.runs, "create_linked_retry", None)
                if not callable(create_retry):
                    raise OrchestrationError(
                        "LINKED_RETRY_UNSUPPORTED",
                        "Persistence does not support linked terminal-run retry",
                    )
                run = create_retry(
                    str(source["run_id"]),
                    new_run_id=new_id(),
                    new_command_id=new_id(),
                    expected_statuses=(
                        RunStatus.PARTIAL.value,
                        RunStatus.FAILED_TERMINAL.value,
                    ),
                    now=_naive_utc_now(),
                )
                event_type = "agent.run.linked_retry_created"
                self._append_action_event(
                    uow,
                    run=run,
                    event_type=event_type,
                    actor=actor,
                    payload={
                        "source_run_id": str(source["run_id"]),
                        "new_run_id": str(run["run_id"]),
                        "reason": _bounded_text(reason, 500),
                    },
                    causation_id=str(source["run_id"]),
                )
                uow.commit()
        else:
            raise OrchestrationError(
                "RUN_NOT_RETRYABLE",
                f"Run in status {status.value} cannot be retried",
            )
        self._wake(str(run["run_id"]))
        self._wake_events()
        return {"run": _run_dto(run), "retry_of_run_id": str(source["run_id"])}

    def clarify_run(self, run_id: str, *, actor: Actor, clarification: Any) -> dict[str, Any]:
        source = self._require_run(run_id)
        status = RunStatus(str(source["status"]))
        if status not in {RunStatus.NEEDS_CLARIFICATION, RunStatus.BLOCKED_DATA}:
            raise OrchestrationError(
                "RUN_NOT_CLARIFIABLE",
                "Only NEEDS_CLARIFICATION or BLOCKED_DATA runs accept clarification",
            )
        safe_clarification = _clarification_payload(clarification)
        with self._repository.unit_of_work() as uow:
            run = uow.runs.transition(
                str(source["run_id"]),
                expected_version=int(source["version"]),
                expected_statuses=(status.value,),
                status=RunStatus.CONTEXT_READY.value,
                error_code=None,
                error_summary=None,
                retryable=False,
                next_attempt_at=_naive_utc_now(),
                finished_at=None,
            )
            event = self._append_action_event(
                uow,
                run=run,
                event_type="agent.run.clarified",
                actor=actor,
                payload={
                    "command_id": str(source["command_id"]),
                    "previous_status": status.value,
                    "clarification": safe_clarification,
                },
            )
            uow.commit()
        self._wake(str(run["run_id"]))
        self._wake_events()
        return {
            "run": _run_dto(run),
            "event": _event_dto(event),
            "context_payload": {"clarification": safe_clarification},
        }

    def resolve_command_context(self, command: Any) -> dict[str, Any]:
        """Return authoritative persisted context for deterministic planning."""

        command_id = _required_id(getattr(command, "command_id", None), "command_id")
        parameters = getattr(command, "parameters", {})
        tool_name = str(parameters.get("tool_name") or "") if isinstance(parameters, Mapping) else ""
        needs_customer_problem_refs = _is_customer_problem_project_command(
            command,
            tool_name=tool_name,
        )
        with self._repository.unit_of_work() as uow:
            item = uow.work_items.get_by_command(command_id)
            if item is None:
                return {"clarifications": []}
            events: list[dict[str, Any]] = []
            offset = 0
            while True:
                page = uow.events.list_for_work_item_by_type(
                    str(item["work_item_id"]),
                    "agent.run.clarified",
                    limit=500,
                    offset=offset,
                )
                events.extend(page)
                if len(page) < 500:
                    break
                offset += len(page)
            problem_refs = (
                _customer_problem_open_refs(uow)
                if needs_customer_problem_refs
                else []
            )
        clarifications: list[dict[str, Any]] = []
        effective_account_id: str | None = None
        effective_argument_updates: dict[str, Any] | None = None
        for event in events:
            payload = event.get("payload_json")
            if not isinstance(payload, Mapping):
                continue
            # Domain events are listed by work item.  A clarification may only
            # affect the exact persisted command that was blocked; notes from a
            # previous linked Run must never become parameters of a new command.
            if str(payload.get("command_id") or "") != command_id:
                continue
            clarification = payload.get("clarification")
            if not isinstance(clarification, Mapping) or clarification.get("schema_version") != 1:
                continue
            normalized = _clarification_payload(clarification)
            clarifications.append(
                {
                    "event_id": str(event.get("event_id") or ""),
                    "run_id": str(event.get("run_id") or ""),
                    "observed_at": _json_value(event.get("observed_at")),
                    "clarification": normalized,
                }
            )
            if "account_id" in normalized:
                effective_account_id = str(normalized["account_id"])
            if "argument_updates" in normalized:
                # Each explicit argument_updates object is a full replacement
                # for the previous patch.  This lets an operator correct or
                # clear an invalid patch without a hidden merge residue.
                effective_argument_updates = dict(normalized["argument_updates"])

        clarification_override: dict[str, Any] = {}
        if effective_account_id is not None:
            clarification_override["account_id"] = effective_account_id
        if effective_argument_updates is not None:
            clarification_override["argument_updates"] = effective_argument_updates
        return {
            "clarifications": clarifications,
            **(
                {
                    "clarification_override": {
                        "schema_version": 1,
                        "command_id": command_id,
                        **clarification_override,
                    }
                }
                if clarification_override
                else {}
            ),
            **(
                {"customer_problem_open_refs": problem_refs}
                if needs_customer_problem_refs
                else {}
            ),
        }

    def assign_work_item(
        self,
        work_item_id: str,
        *,
        expected_version: int,
        owner_type: str,
        owner_id: str,
    ) -> dict[str, Any]:
        item = self._repository.assign_work_item(
            _required_id(work_item_id, "work_item_id"),
            expected_version=int(expected_version),
            owner_type=_required_id(owner_type, "owner_type"),
            owner_id=_required_id(owner_id, "owner_id"),
        )
        return {"work_item": _work_item_dto(item)}

    def approve(
        self,
        approval_id: str,
        *,
        plan_hash: str,
        actor: Actor,
        source: str,
        comment: str = "",
    ) -> dict[str, Any]:
        return self._decide(
            approval_id,
            plan_hash=plan_hash,
            actor=actor,
            source=source,
            decision="APPROVED",
            comment=comment,
        )

    def reject(
        self,
        approval_id: str,
        *,
        plan_hash: str,
        actor: Actor,
        source: str,
        comment: str = "",
    ) -> dict[str, Any]:
        return self._decide(
            approval_id,
            plan_hash=plan_hash,
            actor=actor,
            source=source,
            decision="REJECTED",
            comment=comment,
        )

    async def publish_session_restored(self, account_id: str) -> dict[str, Any]:
        account = _required_id(account_id, "account_id")
        page_runs = getattr(self._repository, "page_blocked_login_runs_for_account", None)
        if not callable(page_runs):
            raise OrchestrationError(
                "ACCOUNT_BLOCKED_RUN_QUERY_UNSUPPORTED",
                "Persistence cannot prove complete blocked-run matching for an explicit account",
            )
        offset = 0
        limit = 500
        matched: list[dict[str, Any]] = []
        while True:
            page = page_runs(account, limit=limit, offset=offset)
            if not isinstance(page, Mapping) or not isinstance(page.get("items"), list):
                raise OrchestrationError(
                    "INVALID_BLOCKED_RUN_PAGE",
                    "Persistence returned an invalid blocked-run page",
                )
            matched.extend(item for item in page["items"] if isinstance(item, Mapping))
            if bool(page.get("is_complete")):
                break
            next_offset = page.get("next_offset")
            if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset <= offset:
                raise OrchestrationError(
                    "INCOMPLETE_BLOCKED_RUN_PAGE",
                    "Persistence could not prove complete blocked-run pagination",
                )
            offset = next_offset

        resumed: list[dict[str, Any]] = []
        for source in matched:
            if str(source.get("status") or "") != RunStatus.BLOCKED_LOGIN.value:
                continue
            with self._repository.unit_of_work() as uow:
                run = uow.runs.transition(
                    str(source["run_id"]),
                    expected_version=int(source["version"]),
                    expected_statuses=(RunStatus.BLOCKED_LOGIN.value,),
                    status=RunStatus.CONTEXT_READY.value,
                    error_code=None,
                    error_summary=None,
                    retryable=False,
                    next_attempt_at=_naive_utc_now(),
                    finished_at=None,
                )
                self._append_action_event(
                    uow,
                    run=run,
                    event_type="account.session_restored",
                    actor=None,
                    payload={"account_id": account},
                    source_system="runtime_session",
                    source_event_id=f"{account}:{run['run_id']}:{run['version']}",
                )
                uow.commit()
            resumed.append(_run_dto(run))
            self._wake(str(run["run_id"]))
        if resumed:
            self._wake_events()
        return {"account_id": account, "resumed_runs": resumed, "resumed_count": len(resumed)}

    def _decide(
        self,
        approval_id: str,
        *,
        plan_hash: str,
        actor: Actor,
        source: str,
        decision: str,
        comment: str,
    ) -> dict[str, Any]:
        approval = self._approval_service.decide(
            approval_id=_required_id(approval_id, "approval_id"),
            plan_hash=_required_id(plan_hash, "plan_hash"),
            actor=actor,
            source=_required_id(source, "source"),
            decision=decision,
            comment=_bounded_text(comment, 500),
        )
        run = self._require_run(str(approval["run_id"]))
        return {"approval": _approval_dto(approval), "run": _run_dto(run)}

    def _require_run(self, run_id: str) -> dict[str, Any]:
        normalized = _required_id(run_id, "run_id")
        run = self._repository.get_run(normalized)
        if run is None:
            raise OrchestrationError("RUN_NOT_FOUND", "Run was not found")
        try:
            RunStatus(str(run.get("status") or ""))
        except ValueError as exc:
            raise OrchestrationError("UNKNOWN_RUN_STATUS", "Persisted run has an unknown status") from exc
        return run

    def _require_work_item_id(self, work_item_id: str) -> str:
        normalized = _required_id(work_item_id, "work_item_id")
        if self._repository.get_work_item(normalized) is None:
            raise OrchestrationError("WORK_ITEM_NOT_FOUND", "Work item was not found")
        return normalized

    def _list_runs_for_work_item(self, work_item_id: str) -> list[dict[str, Any]]:
        with self._repository.unit_of_work() as uow:
            list_runs = getattr(uow.runs, "list_for_work_item", None)
            if not callable(list_runs):
                first = uow.runs.get_first_for_work_item(work_item_id)
                return [first] if first is not None else []
            return list_runs(work_item_id, limit=500, offset=0)

    def _append_action_event(
        self,
        uow: Any,
        *,
        run: Mapping[str, Any],
        event_type: str,
        actor: Actor | None,
        payload: Mapping[str, Any],
        causation_id: str | None = None,
        source_system: str = "control_plane",
        source_event_id: str | None = None,
    ) -> dict[str, Any]:
        now = _naive_utc_now()
        safe_payload = dict(redact_sensitive(payload))
        if actor is not None:
            safe_payload["actor"] = actor.to_dict()
        receipt = uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": event_type,
                "schema_version": 1,
                "source_system": source_system,
                "source_event_id": source_event_id,
                "entity_type": "agent_run",
                "entity_id": run["run_id"],
                "work_item_id": run["work_item_id"],
                "run_id": run["run_id"],
                "occurred_at": now,
                "observed_at": now,
                "correlation_id": run["correlation_id"],
                "causation_id": causation_id,
                "payload": safe_payload,
            },
            (
                {
                    "consumer_name": "orchestration.run_worker",
                    "topic": event_type,
                    "partition_key": str(run["work_item_id"]),
                    "max_attempts": 10,
                },
            ),
        )
        return dict(receipt["event"])

    def _wake(self, run_id: str) -> None:
        if self._wake_runner is not None:
            self._wake_runner(run_id)

    def _wake_events(self) -> None:
        if self._wake_outbox is not None:
            self._wake_outbox()


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _required_id(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OrchestrationError("INVALID_IDENTIFIER", f"{field} is required")
    if len(normalized) > 191:
        raise OrchestrationError("INVALID_IDENTIFIER", f"{field} is too long")
    return normalized


def _bounded_text(value: Any, limit: int) -> str:
    return redact_text(value).strip()[:limit]


def _bounded_limit(value: Any, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OrchestrationError("INVALID_LIMIT", "limit must be an integer") from exc
    return max(1, min(parsed, maximum))


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
        return aware.isoformat().replace("+00:00", "Z")
    return redact_sensitive(value)


def _select(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: _json_value(row[field]) for field in fields if field in row}


_OPEN_CUSTOMER_PROBLEM_STATUSES = frozenset(
    {
        "OPEN",
        "IN_PROGRESS",
        "NEEDS_CLARIFICATION",
        "WAITING_APPROVAL",
        "BLOCKED_LOGIN",
        "BLOCKED_DATA",
    }
)
_CUSTOMER_PROBLEM_PLATFORMS = frozenset({"ronghui", "yunda"})
_CUSTOMER_PROBLEM_SOURCE_DIRECTIONS = frozenset(
    {"published", "query", "received", "registered"}
)


def _is_customer_problem_project_command(command: Any, *, tool_name: str) -> bool:
    """Recognize the exact project alias that owns customer recheck context."""

    if tool_name == "sync_customer_service_problems":
        return True
    invocation = getattr(command, "automation_invocation", None)
    automation_id = str(getattr(invocation, "automation_id", "") or "").strip()
    return (
        automation_id == "customer_problems_shadow"
        and tool_name == "automation.customer_problems_shadow.run"
    )


def _customer_problem_open_refs(uow: Any) -> list[dict[str, Any]]:
    """Build exact source identities for detail rechecks without guessing gaps."""

    refs: list[dict[str, Any]] = []
    for item in uow.work_items.list_by_type("CUSTOMER_SERVICE_PROBLEM"):
        if str(item.get("status") or "") not in _OPEN_CUSTOMER_PROBLEM_STATUSES:
            continue
        persisted_key = str(item.get("dedupe_key") or "").strip()
        parts = persisted_key.split(":", 3)
        legacy_identity = len(parts) == 4 and parts[0] == "problem" and all(parts[1:])
        opaque_identity = (
            len(persisted_key) == len("problem:v1:") + 64
            and persisted_key.startswith("problem:v1:")
            and all(character in "0123456789abcdef" for character in persisted_key[-64:])
        )
        if not legacy_identity and not opaque_identity:
            refs.append(
                {
                    "dedupe_key": persisted_key,
                    "context_error": "INVALID_PERSISTED_PROBLEM_IDENTITY",
                }
            )
            continue
        entities = uow.work_items.list_entities(str(item["work_item_id"]))
        subjects = [
            entity
            for entity in entities
            if str(entity.get("relation_type") or "") == "subject"
            and str(entity.get("entity_type") or "") == "customer_problem"
        ]
        platform = ""
        account_id = ""
        external_id = ""
        raw_platform = ""
        raw_external_id = ""
        metadata: Mapping[str, Any] = {}
        if len(subjects) != 1:
            context_error = (
                "SUBJECT_ENTITY_MISSING" if not subjects else "SUBJECT_ENTITY_AMBIGUOUS"
            )
        else:
            subject = subjects[0]
            raw_metadata = subject.get("metadata_json")
            metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
            raw_platform = str(subject.get("source_system") or "").strip().lower()
            raw_external_id = str(subject.get("entity_id") or "").strip()
            account_id = str(metadata.get("account_id") or "").strip()
            context_error = ""
            if raw_platform and raw_platform not in _CUSTOMER_PROBLEM_PLATFORMS:
                platform = ""
                context_error = "SUBJECT_PLATFORM_UNSUPPORTED"
            else:
                platform = raw_platform
            if len(raw_external_id) > 128:
                external_id = ""
                context_error = context_error or "SUBJECT_EXTERNAL_ID_TOO_LONG"
            else:
                external_id = raw_external_id

        if legacy_identity:
            _prefix, legacy_platform, legacy_account_id, legacy_external_id = parts
            if (
                raw_platform != legacy_platform.lower()
                or account_id != legacy_account_id
                or raw_external_id != legacy_external_id
            ):
                context_error = context_error or "SUBJECT_ENTITY_IDENTITY_MISMATCH"
        if not platform or not account_id or not external_id:
            context_error = context_error or "SUBJECT_ENTITY_IDENTITY_MISMATCH"
            opaque_key = persisted_key
        else:
            opaque_key = customer_problem_identity(
                account_id=account_id,
                platform=platform,
                external_id=external_id,
            )
            if opaque_identity and opaque_key != persisted_key:
                context_error = "SUBJECT_ENTITY_IDENTITY_MISMATCH"
        # ``platform`` and ``external_id`` are optional in the signed
        # recheck-item schema so an invalid historical identity can be carried
        # forward with ``context_error`` and handled as BLOCKED_DATA by the
        # action.  Emitting empty strings here makes the otherwise valid
        # server-owned plan fail schema validation before the action gets a
        # chance to record that explicit failure.
        # A context error describes the exact persisted item that produced it.
        # Do not replace that identity with one derived from a contradictory
        # subject row: the derived value may alias a different open item.
        ref: dict[str, Any] = {
            "dedupe_key": persisted_key if context_error else opaque_key
        }
        if platform:
            ref["platform"] = platform
        if external_id:
            ref["external_id"] = external_id
        if context_error:
            ref["context_error"] = context_error
        source_direction = str(metadata.get("source_direction") or "").strip().lower()
        if source_direction:
            if len(source_direction) > 32:
                ref.setdefault("context_error", "SOURCE_DIRECTION_TOO_LONG")
            elif source_direction not in _CUSTOMER_PROBLEM_SOURCE_DIRECTIONS:
                ref.setdefault("context_error", "SOURCE_DIRECTION_UNSUPPORTED")
            else:
                ref["source_direction"] = source_direction

        waybills = [
            str(entity.get("entity_id") or "").strip()
            for entity in entities
            if str(entity.get("relation_type") or "") == "related"
            and str(entity.get("entity_type") or "") == "waybill"
            and str(entity.get("entity_id") or "").strip()
        ]
        if len(set(waybills)) == 1:
            waybill_no = waybills[0]
            if len(waybill_no) > 100:
                ref.setdefault("context_error", "RELATED_WAYBILL_TOO_LONG")
            else:
                ref["waybill_no"] = waybill_no
        elif len(set(waybills)) > 1:
            ref.setdefault("context_error", "RELATED_WAYBILL_AMBIGUOUS")

        if not ref.get("source_direction") and not source_direction:
            evidence_rows = uow.evidence.list(str(item["work_item_id"]), limit=500)
            directions = {
                str(summary.get("source_direction") or "").strip().lower()
                for evidence in evidence_rows
                for summary in (
                    evidence.get("summary_json")
                    if isinstance(evidence.get("summary_json"), Mapping)
                    else {},
                )
                if str(summary.get("source_direction") or "").strip()
            }
            invalid_directions = {
                direction
                for direction in directions
                if len(direction) > 32
                or direction not in _CUSTOMER_PROBLEM_SOURCE_DIRECTIONS
            }
            if invalid_directions:
                ref.setdefault("context_error", "SOURCE_DIRECTION_UNSUPPORTED")
            elif len(directions) == 1:
                ref["source_direction"] = next(iter(directions))
            elif not directions:
                ref.setdefault("context_error", "SOURCE_DIRECTION_MISSING")
            else:
                ref.setdefault("context_error", "SOURCE_DIRECTION_AMBIGUOUS")
        if ref.get("context_error"):
            ref["dedupe_key"] = persisted_key
        refs.append(ref)
    return sorted(refs, key=lambda value: str(value.get("dedupe_key") or ""))


def _run_dto(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _select(row, _RUN_PUBLIC_FIELDS)
    raw_steps = [step for step in row.get("steps", ()) if isinstance(step, Mapping)]
    result["steps"] = [_select(step, _STEP_PUBLIC_FIELDS) for step in raw_steps]
    plan = row.get("plan_json")
    if isinstance(plan, Mapping):
        result["plan"] = _safe_plan(plan)
    stage = _run_stage_projection(row, raw_steps)
    result.update(stage)
    result["allowed_actions"] = _run_actions(str(row.get("status") or ""))
    result["next_poll_after_ms"] = _next_poll_after_ms(str(row.get("status") or ""))
    return result


_PUBLIC_PROBLEM_CODES = {
    "AUTH_REQUIRED": "ACCOUNT_LOGIN_REQUIRED",
    "BLOCKED_LOGIN": "ACCOUNT_LOGIN_REQUIRED",
    "BROKER_ACCOUNT_UNAVAILABLE": "ACCOUNT_LOGIN_REQUIRED",
    "BROKER_CONCURRENCY_BLOCKED": "RESOURCE_BUSY",
    "BROKER_RESOURCE_INVALID": "RESOURCE_UNAVAILABLE",
    "BROKER_RESOURCE_UNAVAILABLE": "RESOURCE_UNAVAILABLE",
    "BROKER_SOURCE_FAILED": "SOURCE_UNAVAILABLE",
    "BROKER_SOURCE_INVALID": "SOURCE_SCHEMA_CHANGED",
    "EXECUTION_LOCK_CONTEXT_REQUIRED": "EXECUTION_CONTEXT_MISSING",
    "PLUGIN_GENERATION_UNAVAILABLE": "RUNTIME_GENERATION_UNSTABLE",
    "PLUGIN_PROCESS_FAILED": "PLUGIN_EXECUTION_FAILED",
    "PROJECT_ROUTE_NOT_FOUND": "PROJECT_ROUTE_NOT_FOUND",
    "RESOURCE_PERMISSION_DENIED": "RESOURCE_PERMISSION_DENIED",
    "RUNTIME_GENERATION_UNSTABLE": "RUNTIME_GENERATION_UNSTABLE",
    "SOURCE_SCHEMA_CHANGED": "SOURCE_SCHEMA_CHANGED",
    "SOURCE_SHEET_NOT_FOUND": "SOURCE_SHEET_NOT_FOUND",
    "SOURCE_UNAVAILABLE": "SOURCE_UNAVAILABLE",
    "WRITE_OUTCOME_UNKNOWN": "WRITE_OUTCOME_UNKNOWN",
}


def _public_problem_code(row: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]) -> str:
    candidates = [str(row.get("error_code") or "").strip().upper()]
    candidates.extend(
        str(step.get("error_code") or "").strip().upper()
        for step in reversed(steps)
    )
    for code in candidates:
        if not code:
            continue
        mapped = _PUBLIC_PROBLEM_CODES.get(code)
        if mapped:
            return mapped
        if "PERMISSION" in code or code.endswith("_FORBIDDEN"):
            return "RESOURCE_PERMISSION_DENIED"
        if "RESOURCE" in code and any(marker in code for marker in ("MISSING", "INVALID", "UNAVAILABLE", "NOT_FOUND")):
            return "RESOURCE_UNAVAILABLE"
        if "SOURCE" in code and any(marker in code for marker in ("SCHEMA", "INVALID", "FIELD")):
            return "SOURCE_SCHEMA_CHANGED"
        if "SOURCE" in code and any(marker in code for marker in ("FAILED", "UNAVAILABLE", "TIMEOUT")):
            return "SOURCE_UNAVAILABLE"
    status = str(row.get("status") or "").upper()
    if status == RunStatus.BLOCKED_LOGIN.value:
        return "ACCOUNT_LOGIN_REQUIRED"
    if status in {
        RunStatus.BLOCKED_DATA.value,
        RunStatus.FAILED_RETRYABLE.value,
        RunStatus.FAILED_TERMINAL.value,
        RunStatus.PARTIAL.value,
    }:
        return "EXECUTION_FAILED"
    return ""


def _run_stage_projection(
    row: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    status = str(row.get("status") or "").upper()
    ordered = sorted(steps, key=lambda value: int(value.get("step_order") or 0))
    active = next(
        (
            step
            for step in ordered
            if str(step.get("status") or "").upper() in {"RUNNING", "VERIFYING"}
        ),
        None,
    )
    terminal = status in {
        RunStatus.COMPLETED.value,
        RunStatus.PARTIAL.value,
        RunStatus.FAILED_TERMINAL.value,
        RunStatus.CANCELLED.value,
        RunStatus.BLOCKED_LOGIN.value,
        RunStatus.BLOCKED_DATA.value,
        RunStatus.FAILED_RETRYABLE.value,
        RunStatus.NEEDS_CLARIFICATION.value,
    }
    if terminal:
        phase, stage_code, description = "finished", "FINISHED", "本次运行已结束"
    elif status == RunStatus.VERIFYING.value or (
        active is not None and str(active.get("status") or "").upper() == "VERIFYING"
    ):
        phase, stage_code, description = "verifying", "VERIFYING_RESULT", "正在核验结果"
    elif active is None:
        if status == RunStatus.WAITING_APPROVAL.value:
            phase, stage_code, description = "queued", "WAITING_APPROVAL", "正在等待审批"
        else:
            phase, stage_code, description = "queued", "WAITING_EXECUTION_SLOT", "正在等待执行通道"
    else:
        operation = str(active.get("operation_type") or "").lower()
        if operation == "read":
            phase, stage_code, description = "source_read", "READING_SOURCE", "正在读取数据"
        elif operation in {
            "internal_projection_write",
            "external_write",
            "financial_write",
            "destructive",
        }:
            phase, stage_code, description = "writing", "WRITING_RESULT", "正在写入结果"
        else:
            phase, stage_code, description = "processing", "PROCESSING_DATA", "正在处理数据"
    stage_started_at = (
        active.get("started_at")
        if active is not None
        else row.get("finished_at") if terminal else row.get("created_at")
    )
    return {
        "execution_phase": phase,
        "stage_code": stage_code,
        "stage_started_at": _json_value(stage_started_at),
        "stage_description": description,
        "public_problem_code": _public_problem_code(row, ordered),
    }


def _work_item_dto(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _select(row, _WORK_ITEM_PUBLIC_FIELDS)
    result["entities"] = [
        _select(entity, _ENTITY_PUBLIC_FIELDS)
        for entity in row.get("entities", ())
        if isinstance(entity, Mapping)
    ]
    if result.get("owner_id"):
        result["owner"] = {
            "owner_type": result.get("owner_type"),
            "owner_id": result.get("owner_id"),
        }
    return result


def _approval_dto(row: Mapping[str, Any]) -> dict[str, Any]:
    return _select(row, _APPROVAL_PUBLIC_FIELDS)


def _event_dto(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _select(row, _EVENT_PUBLIC_FIELDS)
    payload = row.get("payload_json")
    if isinstance(payload, Mapping):
        safe_payload = dict(redact_sensitive(payload))
        result["payload"] = safe_payload
        for field in ("previous_status", "reason", "message", "summary", "actor"):
            if field in safe_payload:
                result[field] = safe_payload[field]
    return result


def _evidence_dto(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _select(row, _EVIDENCE_PUBLIC_FIELDS)
    summary = row.get("summary_json")
    if isinstance(summary, Mapping):
        result["summary"] = redact_sensitive(summary)
    return result


def _safe_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    safe = _select(
        plan,
        (
            "schema_version",
            "command_type",
            "context_fingerprint",
            "tool_catalog_hash",
            "plan_hash",
            "impact",
        ),
    )
    steps: list[dict[str, Any]] = []
    for value in plan.get("steps", ()):
        if not isinstance(value, Mapping):
            continue
        steps.append(
            _select(
                value,
                (
                    "step_key",
                    "tool_name",
                    "tool_version",
                    "operation_type",
                    "account_id",
                    "depends_on",
                    "risk_level",
                    "requires_approval",
                    "expected_evidence",
                    "postconditions",
                ),
            )
        )
    safe["steps"] = steps
    return safe


_CLARIFICATION_FIELDS = frozenset(
    {"schema_version", "note", "account_id", "argument_updates"}
)


def _clarification_payload(value: Any) -> dict[str, Any]:
    """Normalize the closed v1 clarification contract.

    Legacy text remains an audited note.  Only explicit ``account_id`` and
    ``argument_updates`` fields can affect deterministic replanning.
    """

    if isinstance(value, str):
        text = _bounded_text(value, 4000)
        if not text:
            raise OrchestrationError("CLARIFICATION_REQUIRED", "Clarification is required")
        return {"schema_version": 1, "note": text}
    if not isinstance(value, Mapping) or not value:
        raise OrchestrationError("CLARIFICATION_REQUIRED", "Clarification must be a non-empty string or object")
    unknown = sorted(str(key) for key in value if str(key) not in _CLARIFICATION_FIELDS)
    if unknown:
        raise OrchestrationError(
            "INVALID_CLARIFICATION",
            f"Unsupported clarification fields: {', '.join(unknown)}",
        )
    schema_version = value.get("schema_version", 1)
    if isinstance(schema_version, bool) or schema_version != 1:
        raise OrchestrationError(
            "UNSUPPORTED_CLARIFICATION_SCHEMA",
            "Only clarification schema_version 1 is supported",
        )

    normalized: dict[str, Any] = {"schema_version": 1}
    if "note" in value:
        if not isinstance(value.get("note"), str):
            raise OrchestrationError("INVALID_CLARIFICATION", "Clarification note must be a string")
        note = _bounded_text(value.get("note"), 4000)
        if note:
            normalized["note"] = note

    if "account_id" in value:
        if not isinstance(value.get("account_id"), str):
            raise OrchestrationError("INVALID_CLARIFICATION", "Clarification account_id must be a string")
        normalized["account_id"] = _required_id(value.get("account_id"), "account_id")

    if "argument_updates" in value:
        raw_updates = value.get("argument_updates")
        if not isinstance(raw_updates, Mapping):
            raise OrchestrationError(
                "INVALID_CLARIFICATION",
                "Clarification argument_updates must be a JSON object",
            )
        if _contains_sensitive_field(raw_updates):
            raise OrchestrationError(
                "CLARIFICATION_SENSITIVE_FIELD_FORBIDDEN",
                "Clarification argument_updates cannot contain credential fields",
            )
        try:
            serialized = canonical_json(raw_updates)
        except (TypeError, ValueError) as exc:
            raise OrchestrationError(
                "INVALID_CLARIFICATION",
                "Clarification argument_updates must contain canonical JSON values",
            ) from exc
        if len(serialized.encode("utf-8")) > 64 * 1024:
            raise OrchestrationError(
                "CLARIFICATION_TOO_LARGE",
                "Clarification argument_updates exceeds 64 KiB",
            )
        normalized["argument_updates"] = dict(redact_sensitive(dict(raw_updates)))

    if len(normalized) == 1:
        raise OrchestrationError(
            "CLARIFICATION_REQUIRED",
            "Clarification must include a note, account_id, or argument_updates",
        )
    return normalized


def _contains_sensitive_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            is_sensitive_key(key) or _contains_sensitive_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_field(item) for item in value)
    return False


def _run_actions(status: str) -> list[str]:
    if status in {
        RunStatus.RECEIVED.value,
        RunStatus.CONTEXT_READY.value,
        RunStatus.PLANNED.value,
        RunStatus.VALIDATED.value,
        RunStatus.WAITING_APPROVAL.value,
        RunStatus.RUNNING.value,
        RunStatus.VERIFYING.value,
        RunStatus.NEEDS_CLARIFICATION.value,
        RunStatus.BLOCKED_LOGIN.value,
        RunStatus.BLOCKED_DATA.value,
        RunStatus.FAILED_RETRYABLE.value,
    }:
        actions = ["cancel"]
    else:
        actions = []
    if status in {RunStatus.NEEDS_CLARIFICATION.value, RunStatus.BLOCKED_DATA.value}:
        actions.append("clarify")
    if status in {
        RunStatus.FAILED_RETRYABLE.value,
        RunStatus.PARTIAL.value,
        RunStatus.FAILED_TERMINAL.value,
    }:
        actions.append("retry")
    return actions


def _require_manual_retry_safe_plan(run: Mapping[str, Any]) -> None:
    """Reject a human retry that could replay any write-side effect.

    Automatic retry eligibility is established while executing the original
    plan. A later human action must not copy Scheduler identity or an exact
    schedule exemption into a fresh write. Writes need a newly submitted
    Command and a new policy decision (or an authoritative NOT_APPLIED
    reconciliation path), neither of which this endpoint represents.
    """

    plan = run.get("plan_json")
    steps = plan.get("steps") if isinstance(plan, Mapping) else None
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)) or not steps:
        raise OrchestrationError(
            "RUN_RETRY_PROOF_MISSING",
            "The original plan is unavailable; submit a new command instead",
        )
    for step in steps:
        if not isinstance(step, Mapping):
            raise OrchestrationError(
                "RUN_RETRY_PROOF_MISSING",
                "The original plan is unavailable; submit a new command instead",
            )
        operation = str(step.get("operation_type") or "").strip().lower()
        if operation not in {"read", "compute"}:
            raise OrchestrationError(
                "UNSAFE_WRITE_RETRY_REQUIRES_NEW_COMMAND",
                "Write runs cannot be replayed; submit a new command for approval",
            )


def _detail_actions(
    item: Mapping[str, Any],
    run: Mapping[str, Any] | None,
    approval: Mapping[str, Any] | None,
) -> list[str]:
    actions = ["assign"] if str(item.get("status") or "") not in {"RESOLVED", "CANCELLED"} else []
    if run is not None:
        actions.extend(_run_actions(str(run.get("status") or "")))
    if approval is not None and str(approval.get("status") or "") == "PENDING":
        actions.extend(("approve", "reject"))
    return sorted(set(actions))


def _next_poll_after_ms(status: str) -> int:
    if status in {
        RunStatus.COMPLETED.value,
        RunStatus.PARTIAL.value,
        RunStatus.FAILED_TERMINAL.value,
        RunStatus.CANCELLED.value,
    }:
        return 0
    if status in {
        RunStatus.WAITING_APPROVAL.value,
        RunStatus.NEEDS_CLARIFICATION.value,
        RunStatus.BLOCKED_LOGIN.value,
        RunStatus.BLOCKED_DATA.value,
        RunStatus.FAILED_RETRYABLE.value,
    }:
        return 5000
    return 1000
