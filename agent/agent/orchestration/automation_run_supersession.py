"""Fail-closed supersession for safely suspended automation project Runs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from agent.orchestration.models import (
    OrchestrationError,
    RunStatus,
    WorkItemStatus,
    assert_run_transition,
    assert_work_item_transition,
    new_id,
)
from shared.automation_run_lookup import AUTOMATION_UNFINISHED_RUN_LIMIT


AUTOMATION_BLOCKING_SAFE_SUPERSEDE = "SAFE_SUPERSEDE"
AUTOMATION_BLOCKING_ACTIVE = "ACTIVE"
AUTOMATION_BLOCKING_RETRY_PENDING = "RETRY_PENDING"
AUTOMATION_BLOCKING_UNKNOWN_WRITE = "UNKNOWN_WRITE"
AUTOMATION_BLOCKING_NEEDS_ATTENTION = "NEEDS_ATTENTION"
_SAFE_SUPERSEDE_RUN_STATUSES = frozenset(
    {
        RunStatus.NEEDS_CLARIFICATION.value,
        RunStatus.BLOCKED_LOGIN.value,
        RunStatus.BLOCKED_DATA.value,
    }
)
_SAFE_SUPERSEDE_WORK_ITEM_STATUS = {
    RunStatus.NEEDS_CLARIFICATION.value: WorkItemStatus.NEEDS_CLARIFICATION.value,
    RunStatus.BLOCKED_LOGIN.value: WorkItemStatus.BLOCKED_LOGIN.value,
    RunStatus.BLOCKED_DATA.value: WorkItemStatus.BLOCKED_DATA.value,
}
_TERMINAL_RUN_STATUS_VALUES = frozenset(
    {
        RunStatus.COMPLETED.value,
        RunStatus.PARTIAL.value,
        RunStatus.FAILED_TERMINAL.value,
        RunStatus.CANCELLED.value,
    }
)


def classify_automation_run_blocking_kind(
    run: Mapping[str, Any],
    work_item: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """Classify one locked automation Run without guessing from elapsed time."""

    status = str(run.get("status") or "").strip().upper()
    item_status = str(work_item.get("status") or "").strip().upper()
    if status in _TERMINAL_RUN_STATUS_VALUES:
        return AUTOMATION_BLOCKING_NEEDS_ATTENTION
    if (
        str(run.get("error_code") or "").strip().upper()
        == "WRITE_OUTCOME_UNKNOWN"
        or bool(facts.get("has_unknown_step"))
        or bool(facts.get("has_unknown_generation_lease"))
        or bool(facts.get("has_unknown_write_receipt"))
        or bool(facts.get("has_unclosed_protected_write"))
    ):
        return AUTOMATION_BLOCKING_UNKNOWN_WRITE

    effective_now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if effective_now.tzinfo is not None:
        effective_now = effective_now.astimezone(timezone.utc).replace(tzinfo=None)
    lease_expires_at = run.get("lease_expires_at")
    if isinstance(lease_expires_at, datetime) and lease_expires_at.tzinfo is not None:
        lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(
            tzinfo=None
        )
    has_worker = bool(str(run.get("worker_id") or "").strip())
    has_valid_run_lease = (
        has_worker
        and isinstance(lease_expires_at, datetime)
        and lease_expires_at > effective_now
    )
    has_invalid_worker_lease = has_worker and not isinstance(
        lease_expires_at,
        datetime,
    )
    has_orphaned_lease = not has_worker and lease_expires_at is not None
    if (
        has_valid_run_lease
        or bool(facts.get("has_inflight_step"))
        or bool(facts.get("has_live_generation_lease"))
        or status
        in {
            RunStatus.RECEIVED.value,
            RunStatus.CONTEXT_READY.value,
            RunStatus.PLANNED.value,
            RunStatus.VALIDATED.value,
            RunStatus.RUNNING.value,
            RunStatus.VERIFYING.value,
            RunStatus.WAITING_APPROVAL.value,
        }
    ):
        return AUTOMATION_BLOCKING_ACTIVE
    if status == RunStatus.FAILED_RETRYABLE.value:
        return AUTOMATION_BLOCKING_RETRY_PENDING
    if bool(facts.get("has_protected_write_receipt")):
        return AUTOMATION_BLOCKING_NEEDS_ATTENTION
    if (
        status in _SAFE_SUPERSEDE_RUN_STATUSES
        and not has_invalid_worker_lease
        and not has_orphaned_lease
        and item_status == _SAFE_SUPERSEDE_WORK_ITEM_STATUS[status]
    ):
        return AUTOMATION_BLOCKING_SAFE_SUPERSEDE
    return AUTOMATION_BLOCKING_NEEDS_ATTENTION


def supersede_safely_suspended_runs(
    uow: Any,
    *,
    automation_id: str,
    successor: Mapping[str, str],
    source: str,
    request_id: str,
) -> None:
    """Atomically cancel every safe blocker or reject without mutation."""

    superseded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    candidate_ids = uow.runs.list_unfinished_for_automation(
        automation_id,
        limit=AUTOMATION_UNFINISHED_RUN_LIMIT + 1,
    )
    if len(candidate_ids) > AUTOMATION_UNFINISHED_RUN_LIMIT:
        raise OrchestrationError(
            "AUTOMATION_ALREADY_RUNNING",
            "该脚本存在过多未结束事项",
            details={
                "blocking_kind": AUTOMATION_BLOCKING_NEEDS_ATTENTION,
                "active_status": "UNFINISHED_RUN_LIMIT_EXCEEDED",
                "blocking_count": len(candidate_ids),
            },
        )

    safe_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    blockers: list[tuple[str, dict[str, Any]]] = []
    for run_id in candidate_ids:
        run = uow.runs.get(run_id, for_update=True)
        if run is None or str(run.get("status") or "") in _TERMINAL_RUN_STATUS_VALUES:
            continue
        command_id = str(run.get("command_id") or "").strip()
        work_item_id = str(run.get("work_item_id") or "").strip()
        if not command_id or not work_item_id:
            blockers.append((AUTOMATION_BLOCKING_NEEDS_ATTENTION, run))
            continue
        command = uow.commands.get(command_id, for_update=True)
        item = uow.work_items.get(work_item_id, for_update=True)
        if (
            command is None
            or item is None
            or str(command.get("automation_id") or "") != automation_id
            or str(item.get("work_item_id") or "")
            != str(run.get("work_item_id") or "")
        ):
            blockers.append((AUTOMATION_BLOCKING_NEEDS_ATTENTION, run))
            continue
        facts = uow.runs.get_automation_supersession_facts(run_id)
        kind = classify_automation_run_blocking_kind(
            run,
            item,
            facts,
            now=superseded_at,
        )
        if kind == AUTOMATION_BLOCKING_SAFE_SUPERSEDE:
            safe_rows.append((dict(run), dict(item)))
        else:
            blockers.append((kind, run))

    if blockers:
        priority = {
            AUTOMATION_BLOCKING_UNKNOWN_WRITE: 0,
            AUTOMATION_BLOCKING_ACTIVE: 1,
            AUTOMATION_BLOCKING_RETRY_PENDING: 2,
            AUTOMATION_BLOCKING_NEEDS_ATTENTION: 3,
        }
        blocking_kind, blocking_run = sorted(
            blockers,
            key=lambda pair: priority.get(pair[0], 99),
        )[0]
        raise OrchestrationError(
            "AUTOMATION_ALREADY_RUNNING",
            "该脚本存在未结束任务",
            details={
                "blocking_kind": blocking_kind,
                "active_run_id": str(blocking_run.get("run_id") or ""),
                "active_status": str(blocking_run.get("status") or ""),
                "blocking_count": len(blockers),
            },
        )

    for previous_run, previous_item in safe_rows:
        previous_status = str(previous_run["status"])
        assert_run_transition(previous_status, RunStatus.CANCELLED)
        cancelled_run = uow.runs.cancel_suspended(
            str(previous_run["run_id"]),
            expected_version=int(previous_run["version"]),
            expected_statuses=(previous_status,),
            error_code="SUPERSEDED_BY_NEW_INVOCATION",
            error_summary="已由新的自动化触发取代",
            finished_at=superseded_at,
        )
        previous_item_status = WorkItemStatus(str(previous_item["status"]))
        assert_work_item_transition(
            previous_item_status,
            WorkItemStatus.CANCELLED,
        )
        cancelled_item = uow.work_items.transition(
            str(previous_item["work_item_id"]),
            expected_version=int(previous_item["version"]),
            expected_statuses=(previous_item_status.value,),
            status=WorkItemStatus.CANCELLED.value,
            reason_code="SUPERSEDED_BY_NEW_INVOCATION",
            reason_summary="已由新的自动化触发取代",
            resolution={
                "successor_command_id": successor["command_id"],
                "successor_run_id": successor["run_id"],
                "successor_source": source,
                "successor_request_id": request_id,
            },
            closed_at=superseded_at,
        )
        _append_supersession_events(
            uow,
            previous_run=previous_run,
            cancelled_run=cancelled_run,
            cancelled_item=cancelled_item,
            previous_status=previous_status,
            successor=successor,
            source=source,
            request_id=request_id,
            occurred_at=superseded_at,
        )


def _append_supersession_events(
    uow: Any,
    *,
    previous_run: Mapping[str, Any],
    cancelled_run: Mapping[str, Any],
    cancelled_item: Mapping[str, Any],
    previous_status: str,
    successor: Mapping[str, str],
    source: str,
    request_id: str,
    occurred_at: datetime,
) -> None:
    common_payload = {
        "reason_code": "SUPERSEDED_BY_NEW_INVOCATION",
        "successor_command_id": successor["command_id"],
        "successor_run_id": successor["run_id"],
        "successor_source": source,
        "successor_request_id": request_id,
    }
    event_rows = (
        {
            "event_id": new_id(),
            "event_type": "agent.run.status_changed",
            "schema_version": 1,
            "source_system": "agent",
            "source_event_id": (
                f"{cancelled_run['run_id']}:{successor['run_id']}:cancelled"
            ),
            "entity_type": "agent_run",
            "entity_id": cancelled_run["run_id"],
            "work_item_id": cancelled_run["work_item_id"],
            "run_id": cancelled_run["run_id"],
            "occurred_at": occurred_at,
            "observed_at": occurred_at,
            "correlation_id": cancelled_run["correlation_id"],
            "causation_id": successor["command_id"],
            "payload": {
                "from": previous_status,
                "to": RunStatus.CANCELLED.value,
                **common_payload,
            },
        },
        {
            "event_id": new_id(),
            "event_type": "work_item.superseded_by_new_invocation",
            "schema_version": 1,
            "source_system": "agent",
            "source_event_id": (
                f"{cancelled_item['work_item_id']}:{successor['run_id']}"
            ),
            "entity_type": "work_item",
            "entity_id": cancelled_item["work_item_id"],
            "work_item_id": cancelled_item["work_item_id"],
            "run_id": previous_run["run_id"],
            "occurred_at": occurred_at,
            "observed_at": occurred_at,
            "correlation_id": cancelled_run["correlation_id"],
            "causation_id": successor["command_id"],
            "payload": {
                "previous_run_id": previous_run["run_id"],
                **common_payload,
            },
        },
    )
    for event in event_rows:
        uow.events.append_with_outbox(
            event,
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": event["event_type"],
                    "partition_key": str(event["work_item_id"]),
                    "max_attempts": 10,
                },
            ),
        )
