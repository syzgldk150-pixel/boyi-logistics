"""Transactional persistence for the Agent orchestration control plane.

Callers supply connections; this module never loads configuration or mutates schema.
Every unit of work disables autocommit and rolls back unless explicitly committed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Callable

from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
    InvalidStateError,
    OrchestrationPersistenceError,
    RepositoryBase as _RepositoryBase,
    _canonical_json,
    _created_flag,
    _decode_row,
    _json_hash,
    _json_param,
    _json_value,
    _optional_text,
    _required_text,
    _row_dict,
    _rows,
    _safe_comment,
    _safe_error,
    _status,
)
from shared.orchestration_schema import orchestration_schema_requirements
from shared.scheduled_task_approval_repository import ScheduledTaskApprovalPolicyRepository
from shared.automation_project_policy_repository import AutomationProjectPolicyRepository
from shared.feishu_approval_repository import FeishuApprovalRepository
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.automation_project_authorization import AutomationProjectInvocation
from shared.account_execution_locks import (
    AccountExecutionLockLease,
    AccountExecutionLockUnavailable,
    account_execution_lock_name as _account_execution_lock_name,
    acquire_account_execution_locks as _acquire_account_execution_locks,
)

ConnectionFactory = Callable[[], Any]

COMMAND_STATUSES = frozenset({"RECEIVED", "ACCEPTED", "REJECTED"})
WORK_ITEM_STATUSES = frozenset(
    {
        "OPEN",
        "IN_PROGRESS",
        "NEEDS_CLARIFICATION",
        "WAITING_APPROVAL",
        "BLOCKED_LOGIN",
        "BLOCKED_DATA",
        "RESOLVED",
        "CANCELLED",
    }
)
RUN_STATUSES = frozenset(
    {
        "RECEIVED",
        "CONTEXT_READY",
        "PLANNED",
        "VALIDATED",
        "WAITING_APPROVAL",
        "RUNNING",
        "VERIFYING",
        "COMPLETED",
        "NEEDS_CLARIFICATION",
        "BLOCKED_LOGIN",
        "BLOCKED_DATA",
        "PARTIAL",
        "FAILED_RETRYABLE",
        "FAILED_TERMINAL",
        "CANCELLED",
    }
)
STEP_STATUSES = frozenset(
    {
        "PENDING",
        "WAITING_APPROVAL",
        "RUNNING",
        "VERIFYING",
        "BLOCKED_LOGIN",
        "BLOCKED_DATA",
        "COMPLETED",
        "SKIPPED",
        "FAILED_RETRYABLE",
        "FAILED_TERMINAL",
        "CANCELLED",
    }
)
APPROVAL_STATUSES = frozenset({"PENDING", "APPROVED", "REJECTED", "EXPIRED", "INVALIDATED"})
OUTBOX_STATUSES = frozenset({"PENDING", "PROCESSING", "PUBLISHED", "DEAD_LETTER"})
OUTBOX_CANDIDATE_SCAN_LIMIT = 500
TERMINAL_RUN_STATUSES = frozenset(
    {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"}
)


class CommandRepository(_RepositoryBase):
    JSON_FIELDS = (
        "actor_roles_json",
        "entity_refs_json",
        "parameters_json",
        "automation_invocation_json",
    )

    def get(self, command_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(f"SELECT * FROM agent_commands WHERE command_id=%s{suffix}", (command_id,))
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def get_by_idempotency(
        self,
        source: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM agent_commands WHERE source=%s AND idempotency_key=%s{suffix}",
                (source, idempotency_key),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def create_or_get(self, row: Mapping[str, Any]) -> dict[str, Any]:
        command_id = _required_text(row.get("command_id"), "command_id")
        command_type = _required_text(row.get("command_type"), "command_type")
        source = _required_text(row.get("source"), "source")
        actor_type = _required_text(row.get("actor_type"), "actor_type")
        actor_id = _optional_text(row.get("actor_id"))
        roles = row.get("actor_roles_json", row.get("actor_roles", []))
        entity_refs = row.get("entity_refs_json", row.get("entity_refs", []))
        parameters = row.get("parameters_json", row.get("parameters", {}))
        automation_id = _optional_text(row.get("automation_id"))
        automation_invocation = row.get(
            "automation_invocation_json",
            row.get("automation_invocation"),
        )
        raw_automation_generation = row.get("automation_generation")
        if raw_automation_generation is None:
            automation_generation = None
        elif type(raw_automation_generation) is int and raw_automation_generation > 0:
            automation_generation = raw_automation_generation
        else:
            raise ValueError("automation_generation must be a positive integer")
        if automation_id is None:
            if automation_generation is not None or automation_invocation is not None:
                raise ValueError(
                    "generic commands cannot carry automation project context"
                )
        else:
            if automation_generation is None or not isinstance(
                automation_invocation,
                Mapping,
            ):
                raise ValueError(
                    "automation project commands require a closed invocation"
                )
            parsed_invocation = AutomationProjectInvocation.from_mapping(
                automation_invocation
            )
            if (
                parsed_invocation.automation_id != automation_id
                or parsed_invocation.automation_generation != automation_generation
            ):
                raise ValueError(
                    "automation project command identity does not match its invocation"
                )
            automation_invocation = parsed_invocation.to_dict()
        idempotency_key = _required_text(row.get("idempotency_key"), "idempotency_key")
        correlation_id = _required_text(row.get("correlation_id"), "correlation_id")
        requested_at = row.get("requested_at") or datetime.now()
        status = _status(row.get("status"), COMMAND_STATUSES, "command status", default="RECEIVED")
        params = (
            command_id,
            command_type,
            automation_id,
            automation_generation,
            source,
            actor_type,
            actor_id,
            _json_param(roles, []),
            _json_param(entity_refs, []),
            _json_param(parameters, {}),
            _json_param(automation_invocation, {})
            if automation_invocation is not None
            else None,
            idempotency_key,
            correlation_id,
            status,
            _optional_text(row.get("rejection_code")),
            _safe_error(row.get("rejection_summary")),
            requested_at,
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_commands (
                    command_id, command_type, automation_id, automation_generation,
                    source, actor_type, actor_id,
                    actor_roles_json, entity_refs_json, parameters_json,
                    automation_invocation_json,
                    idempotency_key, correlation_id, status, rejection_code,
                    rejection_summary, requested_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE command_id = command_id
                """,
                params,
            )
            created = int(getattr(cursor, "rowcount", 0) or 0) == 1

        persisted = self.get_by_idempotency(source, idempotency_key, for_update=True)
        if persisted is None:
            raise IdempotencyConflict("command identity collides with a different idempotency key")
        immutable_matches = (
            persisted.get("command_type") == command_type
            and persisted.get("actor_type") == actor_type
            and _optional_text(persisted.get("actor_id")) == actor_id
            and _canonical_json(persisted.get("actor_roles_json")) == _canonical_json(roles)
            and _canonical_json(persisted.get("entity_refs_json")) == _canonical_json(entity_refs)
            and _canonical_json(persisted.get("parameters_json")) == _canonical_json(parameters)
            and _optional_text(persisted.get("automation_id")) == automation_id
            and persisted.get("automation_generation") == automation_generation
            and _canonical_json(persisted.get("automation_invocation_json"))
            == _canonical_json(automation_invocation)
        )
        if not immutable_matches:
            raise IdempotencyConflict("command idempotency key was reused with different immutable input")
        return _created_flag(persisted, created)


class WorkItemRepository(_RepositoryBase):
    JSON_FIELDS = ("resolution_json",)

    def get(self, work_item_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(f"SELECT * FROM work_items WHERE work_item_id=%s{suffix}", (work_item_id,))
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def get_by_command(self, command_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT w.*
                FROM work_items w
                WHERE w.command_id=%s
                  AND w.dedupe_key LIKE 'command:%%'
                ORDER BY w.created_at, w.work_item_id LIMIT 1{suffix}
                """,
                (command_id,),
            )
            item = _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)
            if item is not None:
                return item

            # A linked retry intentionally receives a fresh Command so step
            # idempotency keys cannot collide with the terminal source Run.
            # That Command still belongs to the same gateway Work Item through
            # agent_runs, even though work_items.command_id remains immutable.
            cursor.execute(
                f"""
                SELECT w.*
                FROM work_items w
                JOIN agent_runs r ON r.work_item_id=w.work_item_id
                WHERE r.command_id=%s
                ORDER BY r.run_no, r.created_at, r.run_id LIMIT 1{suffix}
                """,
                (command_id,),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def list(
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
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=%s")
            params.append(_status(status, WORK_ITEM_STATUSES, "work item status"))
        if item_type:
            clauses.append("type=%s")
            params.append(_required_text(item_type, "type"))
        if priority:
            normalized_priority = str(priority).strip().upper()
            if normalized_priority not in {"LOW", "NORMAL", "HIGH", "URGENT"}:
                raise InvalidStateError(f"unsupported work item priority: {normalized_priority}")
            clauses.append("priority=%s")
            params.append(normalized_priority)
        if source:
            clauses.append("source=%s")
            params.append(_required_text(source, "source"))
        if query:
            text = str(query).strip()
            if len(text) > 200:
                raise ValueError("work item query exceeds 200 characters")
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(title LIKE %s ESCAPE '\\\\' OR work_item_id LIKE %s ESCAPE '\\\\' "
                "OR dedupe_key LIKE %s ESCAPE '\\\\' OR EXISTS ("
                "SELECT 1 FROM work_item_entities wie WHERE wie.work_item_id=work_items.work_item_id "
                "AND wie.entity_id LIKE %s ESCAPE '\\\\'))"
            )
            params.extend((pattern, pattern, pattern, pattern))
        if owner_type:
            clauses.append("owner_type=%s")
            params.append(str(owner_type).strip())
        if owner_id:
            clauses.append("owner_id=%s")
            params.append(str(owner_id).strip())
        if sla_missing is True:
            clauses.append("sla_deadline IS NULL")
        elif sla_missing is False:
            clauses.append("sla_deadline IS NOT NULL")
        if sla_from is not None:
            clauses.append("sla_deadline >= %s")
            params.append(sla_from)
        if sla_before is not None:
            clauses.append("sla_deadline < %s")
            params.append(sla_before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend((max(1, min(int(limit), 500)), max(0, int(offset))))
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM work_items{where} ORDER BY updated_at DESC, work_item_id DESC LIMIT %s OFFSET %s",
                params,
            )
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def list_by_type(
        self,
        item_type: str,
        *,
        for_update: bool = False,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM work_items
                WHERE type=%s
                ORDER BY created_at, work_item_id
                LIMIT %s{suffix}
                """,
                (_required_text(item_type, "type"), max(1, min(int(limit), 50000))),
            )
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def create_or_get(self, row: Mapping[str, Any]) -> dict[str, Any]:
        work_item_id = _required_text(row.get("work_item_id"), "work_item_id")
        command_id = _required_text(row.get("command_id"), "command_id")
        item_type = _required_text(row.get("type"), "type")
        title = _required_text(row.get("title"), "title")
        status = _status(row.get("status"), WORK_ITEM_STATUSES, "work item status", default="OPEN")
        priority = str(row.get("priority") or "NORMAL").strip().upper()
        if priority not in {"LOW", "NORMAL", "HIGH", "URGENT"}:
            raise InvalidStateError(f"unsupported work item priority: {priority}")
        source = _required_text(row.get("source"), "source")
        dedupe_key = _required_text(row.get("dedupe_key"), "dedupe_key")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO work_items (
                    work_item_id, command_id, type, title, status, priority, source,
                    dedupe_key, owner_type, owner_id, sla_deadline,
                    current_reason_code, current_reason_summary, resolution_json, closed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE work_item_id = work_item_id
                """,
                (
                    work_item_id,
                    command_id,
                    item_type,
                    title,
                    status,
                    priority,
                    source,
                    dedupe_key,
                    _optional_text(row.get("owner_type")),
                    _optional_text(row.get("owner_id")),
                    row.get("sla_deadline"),
                    _optional_text(row.get("current_reason_code")),
                    _safe_error(row.get("current_reason_summary")),
                    _json_param(row.get("resolution_json"), {}) if row.get("resolution_json") is not None else None,
                    row.get("closed_at"),
                ),
            )
            created = int(getattr(cursor, "rowcount", 0) or 0) == 1
            cursor.execute(
                "SELECT * FROM work_items WHERE type=%s AND dedupe_key=%s FOR UPDATE",
                (item_type, dedupe_key),
            )
            persisted = _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)
        if persisted is None:
            raise IdempotencyConflict("work item identity collides with a different dedupe key")
        return _created_flag(persisted, created)

    def transition(
        self,
        work_item_id: str,
        *,
        expected_version: int,
        expected_statuses: Iterable[str],
        status: str,
        reason_code: str | None = None,
        reason_summary: str | None = None,
        resolution: Any = None,
        closed_at: Any = None,
    ) -> dict[str, Any]:
        next_status = _status(status, WORK_ITEM_STATUSES, "work item status")
        allowed = sorted(
            {_status(item, WORK_ITEM_STATUSES, "expected work item status") for item in expected_statuses}
        )
        if not allowed:
            raise ValueError("expected_statuses is required")
        placeholders = ", ".join("%s" for _ in allowed)
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE work_items
                SET status=%s, current_reason_code=%s, current_reason_summary=%s,
                    resolution_json=%s, closed_at=%s, version=version+1
                WHERE work_item_id=%s AND version=%s AND status IN ({placeholders})
                """,
                (
                    next_status,
                    _optional_text(reason_code),
                    _safe_error(reason_summary),
                    _json_param(resolution, {}) if resolution is not None else None,
                    closed_at,
                    work_item_id,
                    int(expected_version),
                    *allowed,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("work item state changed before transition")
        return self.get(work_item_id, for_update=True) or {}

    def assign(
        self,
        work_item_id: str,
        expected_version: int,
        owner_type: str,
        owner_id: str,
    ) -> dict[str, Any]:
        normalized_owner_type = _required_text(owner_type, "owner_type")
        normalized_owner_id = _required_text(owner_id, "owner_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE work_items
                SET owner_type=%s, owner_id=%s, version=version+1
                WHERE work_item_id=%s AND version=%s
                """,
                (
                    normalized_owner_type,
                    normalized_owner_id,
                    _required_text(work_item_id, "work_item_id"),
                    int(expected_version),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("work item changed before assignment")
        return self.get(work_item_id, for_update=True) or {}

    def refresh_projection(
        self,
        work_item_id: str,
        *,
        expected_version: int,
        title: str,
        priority: str,
        source: str,
        sla_deadline: Any,
        reason_code: str | None,
        reason_summary: str | None,
    ) -> dict[str, Any]:
        normalized_priority = str(priority or "NORMAL").strip().upper()
        if normalized_priority not in {"LOW", "NORMAL", "HIGH", "URGENT"}:
            raise InvalidStateError(f"unsupported work item priority: {normalized_priority}")
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE work_items
                SET title=%s, priority=%s, source=%s, sla_deadline=%s,
                    current_reason_code=%s, current_reason_summary=%s,
                    version=version+1
                WHERE work_item_id=%s AND version=%s
                """,
                (
                    _required_text(title, "title"),
                    normalized_priority,
                    _required_text(source, "source"),
                    sla_deadline,
                    _optional_text(reason_code),
                    _safe_error(reason_summary),
                    _required_text(work_item_id, "work_item_id"),
                    int(expected_version),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("work item changed before projection refresh")
        return self.get(work_item_id, for_update=True) or {}

    def list_entities(self, work_item_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM work_item_entities WHERE work_item_id=%s ORDER BY id",
                (work_item_id,),
            )
            return [_decode_row(row, ("metadata_json",)) or {} for row in _rows(cursor)]

    def add_entity(self, row: Mapping[str, Any]) -> None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO work_item_entities (
                    work_item_id, relation_type, entity_type, entity_id, source_system, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE id = id
                """,
                (
                    _required_text(row.get("work_item_id"), "work_item_id"),
                    _required_text(row.get("relation_type"), "relation_type"),
                    _required_text(row.get("entity_type"), "entity_type"),
                    _required_text(row.get("entity_id"), "entity_id"),
                    _required_text(row.get("source_system"), "source_system"),
                    _json_param(row.get("metadata_json"), {}) if row.get("metadata_json") is not None else None,
                ),
            )


class PilotProjectionSourceRepository(_RepositoryBase):
    """Read authoritative pilot sources without exposing raw source payloads."""

    def get_daily_sign_sync_run(
        self,
        run_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM daily_sign_sync_runs WHERE run_id=%s{suffix}",
                (_required_text(run_id, "daily sign source run_id"),),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), ("diagnostics_json",))

    def list_daily_sign_ledger(self, *, for_update: bool = False) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT tracking_number, r13_plan_sign_at, r13_sign_status, r13_sign_at,
                       first_seen_r13_at, last_seen_r13_at, r13_current,
                       first_arrival_date, completion_date, expected_quantity,
                       arrived_quantity, arrival_status, system_sign_due_at,
                       tms_signed, tms_signed_at, data_quality_flags,
                       calculation_trace, created_at, updated_at
                FROM daily_sign_ledger
                ORDER BY tracking_number{suffix}
                """
            )
            return [
                _decode_row(row, ("data_quality_flags", "calculation_trace")) or {}
                for row in _rows(cursor)
            ]

    def list_active_arrival_evidence(self) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.tracking_number, i.run_id AS snapshot_run_id,
                       i.expected_quantity, i.arrived_quantity,
                       r.business_date, r.row_count AS snapshot_row_count,
                       r.fingerprint AS snapshot_fingerprint, r.completed_at
                FROM arrival_stat_items i
                JOIN arrival_stat_runs r ON r.run_id=i.run_id
                WHERE r.status='success' AND r.is_active=TRUE
                ORDER BY i.tracking_number, r.business_date, i.id
                """
            )
            return _rows(cursor)

    def list_valid_problem_evidence(self) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT source, external_id, tracking_number, problem_type,
                       registered_at, registered_site, upload_complete,
                       before_cutoff, postpones_sign, updated_at
                FROM waybill_problem_events
                WHERE upload_complete=TRUE AND before_cutoff=TRUE
                ORDER BY tracking_number, registered_at, source, external_id
                """
            )
            return _rows(cursor)

    def list_main_sign_evidence(self) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT source, external_id, tracking_number, scan_code,
                       scan_type, scanned_at, scan_site
                FROM waybill_sign_events
                WHERE is_main_waybill=TRUE AND scan_type='签收'
                ORDER BY tracking_number, scanned_at, source, external_id
                """
            )
            return _rows(cursor)


class AgentRunRepository(_RepositoryBase):
    JSON_FIELDS = ("plan_json",)
    RETRY_SOURCE_STATUSES = frozenset({"PARTIAL", "FAILED_TERMINAL"})

    def get(self, run_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(f"SELECT * FROM agent_runs WHERE run_id=%s{suffix}", (run_id,))
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def list_nonterminal_with_commands(self) -> list[dict[str, Any]]:
        """Return the complete non-terminal Run set with command parameters.

        Credential replacement is rare and must fail closed, so this read is
        intentionally unbounded instead of silently truncating a safety scan.
        The caller serializes account-bound step starts with a MySQL named lock;
        no row transaction is held while an external credential store changes.
        """

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.*,
                       c.command_type AS command_type,
                       c.source AS command_source,
                       c.actor_type AS command_actor_type,
                       c.actor_id AS command_actor_id,
                       c.parameters_json AS command_parameters_json
                FROM agent_runs r
                INNER JOIN agent_commands c ON c.command_id=r.command_id
                WHERE r.status NOT IN ('COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED')
                ORDER BY r.run_id
                """
            )
            rows = _rows(cursor)
        decoded: list[dict[str, Any]] = []
        for row in rows:
            item = _decode_row(row, self.JSON_FIELDS) or {}
            item["command_parameters_json"] = _json_value(
                item.get("command_parameters_json"),
                None,
            )
            decoded.append(item)
        return decoded

    def get_first_for_work_item(
        self,
        work_item_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM agent_runs WHERE work_item_id=%s
                ORDER BY run_no, created_at, run_id LIMIT 1{suffix}
                """,
                (work_item_id,),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def list_for_work_item(
        self,
        work_item_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM agent_runs
                WHERE work_item_id=%s
                ORDER BY run_no, created_at, run_id
                LIMIT %s OFFSET %s
                """,
                (
                    _required_text(work_item_id, "work_item_id"),
                    max(1, min(int(limit), 500)),
                    max(0, int(offset)),
                ),
            )
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def create_linked_retry(
        self,
        source_run_id: str,
        *,
        new_run_id: str,
        new_command_id: str,
        expected_statuses: Iterable[str] = ("PARTIAL", "FAILED_TERMINAL"),
        now: Any = None,
    ) -> dict[str, Any]:
        """Create a fresh RECEIVED run linked to one eligible terminal source run."""

        source_id = _required_text(source_run_id, "source_run_id")
        retry_id = _required_text(new_run_id, "new_run_id")
        retry_command_id = _required_text(new_command_id, "new_command_id")
        if retry_id == source_id:
            raise ValueError("new_run_id must not reuse source_run_id")
        allowed = sorted({_status(item, RUN_STATUSES, "retry source status") for item in expected_statuses})
        if not allowed:
            raise ValueError("expected_statuses is required")
        unsupported = sorted(set(allowed) - self.RETRY_SOURCE_STATUSES)
        if unsupported:
            raise InvalidStateError(
                "retry source statuses must be terminal retry states: " + ", ".join(unsupported)
            )
        effective_now = now or datetime.now()
        placeholders = ", ".join("%s" for _ in allowed)
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM agent_runs WHERE run_id=%s AND status IN ({placeholders}) FOR UPDATE",
                (source_id, *allowed),
            )
            source = _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)
            if source is None:
                with self.cursor() as lookup_cursor:
                    lookup_cursor.execute("SELECT status FROM agent_runs WHERE run_id=%s", (source_id,))
                    existing = _row_dict(lookup_cursor, lookup_cursor.fetchone())
                if existing is None:
                    raise KeyError("retry source run not found")
                raise InvalidStateError("retry source run is not in an expected terminal state")

            # A retry action may be delivered more than once by the browser or
            # reverse proxy.  The locked source Run is the idempotency anchor:
            # reuse its existing child instead of creating duplicate execution.
            cursor.execute(
                """
                SELECT * FROM agent_runs
                WHERE retry_of_run_id=%s
                ORDER BY created_at, run_id
                LIMIT 1 FOR UPDATE
                """,
                (source_id,),
            )
            existing_retry = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self.JSON_FIELDS,
            )
            if existing_retry is not None:
                return existing_retry

            work_item_id = _required_text(source.get("work_item_id"), "source work_item_id")
            source_command_id = _required_text(source.get("command_id"), "source command_id")
            cursor.execute(
                "SELECT * FROM agent_commands WHERE command_id=%s FOR UPDATE",
                (source_command_id,),
            )
            source_command = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                CommandRepository.JSON_FIELDS,
            )
            if source_command is None:
                raise KeyError("retry source command not found")
            cursor.execute("SELECT work_item_id FROM work_items WHERE work_item_id=%s FOR UPDATE", (work_item_id,))
            if cursor.fetchone() is None:
                raise KeyError("retry source work item not found")
            cursor.execute(
                "SELECT COALESCE(MAX(run_no), 0) AS max_run_no FROM agent_runs WHERE work_item_id=%s",
                (work_item_id,),
            )
            run_number_row = _row_dict(cursor, cursor.fetchone()) or {}
            run_no = int(run_number_row.get("max_run_no") or 0) + 1
            retry_idempotency_key = f"retry:{source_id}:{retry_id}"
            cursor.execute(
                """
                INSERT INTO agent_commands (
                    command_id, command_type, automation_id, automation_generation,
                    source, actor_type, actor_id,
                    actor_roles_json, entity_refs_json, parameters_json,
                    automation_invocation_json,
                    idempotency_key, correlation_id, status, requested_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, 'RECEIVED', %s
                )
                """,
                (
                    retry_command_id,
                    _required_text(source_command.get("command_type"), "source command_type"),
                    _optional_text(source_command.get("automation_id")),
                    source_command.get("automation_generation"),
                    _required_text(source_command.get("source"), "source command source"),
                    _required_text(source_command.get("actor_type"), "source actor_type"),
                    _optional_text(source_command.get("actor_id")),
                    _json_param(source_command.get("actor_roles_json"), []),
                    _json_param(source_command.get("entity_refs_json"), []),
                    _json_param(source_command.get("parameters_json"), {}),
                    _json_param(source_command.get("automation_invocation_json"), {})
                    if source_command.get("automation_invocation_json") is not None
                    else None,
                    retry_idempotency_key,
                    _required_text(source.get("correlation_id"), "source correlation_id"),
                    effective_now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO agent_runs (
                    run_id, work_item_id, command_id, run_no, status, mode,
                    planner_kind, planner_provider, planner_model, correlation_id,
                    retry_of_run_id, next_attempt_at
                ) VALUES (
                    %s, %s, %s, %s, 'RECEIVED', %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    retry_id,
                    work_item_id,
                    retry_command_id,
                    run_no,
                    _required_text(source.get("mode"), "source mode"),
                    _required_text(source.get("planner_kind"), "source planner_kind"),
                    _optional_text(source.get("planner_provider")),
                    _optional_text(source.get("planner_model")),
                    _required_text(source.get("correlation_id"), "source correlation_id"),
                    source_id,
                    effective_now,
                ),
            )
        created = self.get(retry_id, for_update=True)
        if created is None:
            raise OrchestrationPersistenceError("linked retry insert did not persist a run")
        return created

    def list_blocked_login_for_account(
        self,
        account_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return only explicitly account-bound BLOCKED_LOGIN runs."""

        account = _required_text(account_id, "account_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.*
                FROM agent_runs r
                WHERE r.status='BLOCKED_LOGIN'
                  AND r.cancel_requested_at IS NULL
                  AND (
                      EXISTS (
                          SELECT 1 FROM agent_run_steps s
                          WHERE s.run_id=r.run_id
                            AND (
                                BINARY s.account_id=BINARY %s
                                OR BINARY JSON_UNQUOTE(
                                    JSON_EXTRACT(s.result_summary_json, '$.meta.account_id')
                                )=BINARY %s
                            )
                      )
                      OR JSON_CONTAINS(
                          r.plan_json, JSON_OBJECT('account_id', %s), '$.steps'
                      ) = 1
                  )
                ORDER BY r.updated_at, r.run_id
                LIMIT %s OFFSET %s
                """,
                (
                    account,
                    account,
                    account,
                    max(1, min(int(limit), 500)),
                    max(0, int(offset)),
                ),
            )
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def count_blocked_login_for_account(self, account_id: str) -> int:
        account = _required_text(account_id, "account_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM agent_runs r
                WHERE r.status='BLOCKED_LOGIN'
                  AND r.cancel_requested_at IS NULL
                  AND (
                      EXISTS (
                          SELECT 1 FROM agent_run_steps s
                          WHERE s.run_id=r.run_id
                            AND (
                                BINARY s.account_id=BINARY %s
                                OR BINARY JSON_UNQUOTE(
                                    JSON_EXTRACT(s.result_summary_json, '$.meta.account_id')
                                )=BINARY %s
                            )
                      )
                      OR JSON_CONTAINS(
                          r.plan_json, JSON_OBJECT('account_id', %s), '$.steps'
                      ) = 1
                  )
                """,
                (account, account, account),
            )
            row = _row_dict(cursor, cursor.fetchone()) or {}
        return int(row.get("total") or 0)

    def page_blocked_login_for_account(
        self,
        account_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        page_limit = max(1, min(int(limit), 500))
        page_offset = max(0, int(offset))
        total = self.count_blocked_login_for_account(account_id)
        items = self.list_blocked_login_for_account(
            account_id,
            limit=page_limit,
            offset=page_offset,
        )
        next_offset = page_offset + len(items) if page_offset + len(items) < total else None
        return {
            "items": items,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "next_offset": next_offset,
            "is_complete": next_offset is None,
        }

    def list_runnable(
        self,
        *,
        statuses: Iterable[str],
        limit: int = 100,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        allowed = sorted({_status(item, RUN_STATUSES, "runnable run status") for item in statuses})
        if not allowed:
            raise ValueError("statuses is required")
        placeholders = ", ".join("%s" for _ in allowed)
        effective_now = now or datetime.now()
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM agent_runs
                WHERE status IN ({placeholders})
                  AND next_attempt_at <= %s
                  AND (worker_id IS NULL OR lease_expires_at <= %s)
                  AND cancel_requested_at IS NULL
                ORDER BY next_attempt_at, created_at, run_id
                LIMIT %s
                """,
                (*allowed, effective_now, effective_now, max(1, min(int(limit), 500))),
            )
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def claim(
        self,
        worker_id: str,
        statuses: Iterable[str],
        *,
        limit: int = 20,
        lease_seconds: int = 60,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        worker = _required_text(worker_id, "worker_id")
        allowed = sorted({_status(item, RUN_STATUSES, "claim run status") for item in statuses})
        if not allowed:
            raise ValueError("statuses is required")
        terminal = sorted(set(allowed) & TERMINAL_RUN_STATUSES)
        if terminal:
            raise InvalidStateError(
                f"terminal run statuses are not claimable: {', '.join(terminal)}"
            )
        placeholders = ", ".join("%s" for _ in allowed)
        effective_now = now or datetime.now()
        batch_size = max(1, min(int(limit), 500))
        lease = max(1, min(int(lease_seconds), 3600))
        with self.cursor() as cursor:
            # A locking read must follow the claim index order.  A filesort can
            # scan and lock more rows than LIMIT returns, leaving a concurrent
            # SKIP LOCKED worker with no row even when other work is available.
            cursor.execute(
                f"""
                SELECT * FROM agent_runs FORCE INDEX (idx_agent_runs_claim)
                WHERE status IN ({placeholders})
                  AND next_attempt_at <= %s
                  AND (worker_id IS NULL OR lease_expires_at <= %s)
                  AND cancel_requested_at IS NULL
                ORDER BY status, next_attempt_at, lease_expires_at, run_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (*allowed, effective_now, effective_now, batch_size),
            )
            claimed = _rows(cursor)
            if not claimed:
                return []
            run_ids = [str(item["run_id"]) for item in claimed]
            id_placeholders = ", ".join("%s" for _ in run_ids)
            cursor.execute(
                f"""
                UPDATE agent_runs
                SET worker_id=%s,
                    lease_expires_at=DATE_ADD(%s, INTERVAL {lease} SECOND),
                    version=version+1
                WHERE run_id IN ({id_placeholders})
                """,
                (worker, effective_now, *run_ids),
            )
        for item in claimed:
            item["worker_id"] = worker
            item["version"] = int(item.get("version") or 0) + 1
            for field in self.JSON_FIELDS:
                item[field] = _json_value(item.get(field), None)
        return claimed

    def claim_cancel_requested(
        self,
        worker_id: str,
        *,
        limit: int = 20,
        lease_seconds: int = 60,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        """Lease cancellation work so queued runs cannot become stranded."""

        worker = _required_text(worker_id, "worker_id")
        effective_now = now or datetime.now()
        batch_size = max(1, min(int(limit), 500))
        lease = max(1, min(int(lease_seconds), 3600))
        with self.cursor() as cursor:
            # Keep the locking read on the cancellation index order.  As with
            # ordinary run claims, a filesort may lock rows that LIMIT does not
            # return and starve another SKIP LOCKED worker.
            cursor.execute(
                """
                SELECT * FROM agent_runs FORCE INDEX (idx_agent_runs_cancel_requested)
                WHERE cancel_requested_at IS NOT NULL
                  AND status NOT IN ('COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED')
                  AND (worker_id IS NULL OR lease_expires_at <= %s)
                ORDER BY status, cancel_requested_at, lease_expires_at, run_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (effective_now, batch_size),
            )
            claimed = _rows(cursor)
            if not claimed:
                return []
            run_ids = [str(item["run_id"]) for item in claimed]
            placeholders = ", ".join("%s" for _ in run_ids)
            cursor.execute(
                f"""
                UPDATE agent_runs
                SET worker_id=%s,
                    lease_expires_at=DATE_ADD(%s, INTERVAL {lease} SECOND),
                    version=version+1
                WHERE run_id IN ({placeholders})
                """,
                (worker, effective_now, *run_ids),
            )
        for item in claimed:
            item["worker_id"] = worker
            item["version"] = int(item.get("version") or 0) + 1
            for field in self.JSON_FIELDS:
                item[field] = _json_value(item.get(field), None)
        return claimed

    def renew_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now: Any = None,
    ) -> dict[str, Any]:
        """Extend an owned run lease without changing the business CAS version."""

        worker = _required_text(worker_id, "worker_id")
        effective_now = now or datetime.now()
        lease = max(1, min(int(lease_seconds), 3600))
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE agent_runs
                SET lease_expires_at=DATE_ADD(%s, INTERVAL {lease} SECOND)
                WHERE run_id=%s
                  AND worker_id=%s
                  AND status NOT IN ('COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED')
                """,
                (effective_now, _required_text(run_id, "run_id"), worker),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("run lease is no longer owned by this worker")
        return self.get(run_id, for_update=True) or {}

    def release_or_schedule(
        self,
        run_id: str,
        *,
        worker_id: str,
        status: str,
        next_attempt_at: Any = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        retryable: bool = False,
        finished_at: Any = None,
    ) -> dict[str, Any]:
        next_status = _status(status, RUN_STATUSES, "run status")
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_runs
                SET status=%s, next_attempt_at=COALESCE(%s, next_attempt_at),
                    worker_id=NULL, lease_expires_at=NULL,
                    error_code=%s, error_summary=%s, retryable=%s,
                    finished_at=%s, version=version+1
                WHERE run_id=%s AND worker_id=%s
                """,
                (
                    next_status,
                    next_attempt_at,
                    _optional_text(error_code),
                    _safe_error(error_summary),
                    bool(retryable),
                    finished_at,
                    run_id,
                    _required_text(worker_id, "worker_id"),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("run lease is no longer owned by this worker")
        return self.get(run_id, for_update=True) or {}

    def release_recovered(
        self,
        run_id: str,
        *,
        expected_version: int,
        expected_statuses: Iterable[str],
        status: str,
        error_code: str | None = None,
        error_summary: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        """Release an interrupted Run after an authoritative recovery.

        This is intentionally narrower than normal worker release: recovery
        owns an exact locked Run but never impersonates its previous worker.
        Clearing that stale lease makes the durable runner claim it promptly.
        """

        next_status = _status(status, RUN_STATUSES, "run status")
        allowed = sorted(
            {_status(item, RUN_STATUSES, "expected run status") for item in expected_statuses}
        )
        if not allowed:
            raise ValueError("expected_statuses is required")
        placeholders = ", ".join("%s" for _ in allowed)
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE agent_runs
                SET status=%s, worker_id=NULL, lease_expires_at=NULL,
                    next_attempt_at=NOW(6), error_code=%s, error_summary=%s,
                    retryable=%s, version=version+1
                WHERE run_id=%s AND version=%s AND status IN ({placeholders})
                """,
                (
                    next_status,
                    _optional_text(error_code),
                    _safe_error(error_summary),
                    bool(retryable),
                    _required_text(run_id, "run_id"),
                    int(expected_version),
                    *allowed,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("run state changed before recovery release")
        return self.get(run_id, for_update=True) or {}

    def make_waiting_approval_runnable(self, run_id: str) -> dict[str, Any]:
        """Make a decided approval claimable before the post-commit wake signal.

        A runner may hold a short polling lease while the run remains in
        ``WAITING_APPROVAL``.  The approval decision is the business event that
        makes the run runnable, so it must not be rejected merely because that
        transient lease is present.  Updating only the schedule also avoids
        invalidating the runner's optimistic-lock version before it releases
        the lease.
        """

        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_runs
                SET next_attempt_at=NOW(6)
                WHERE run_id=%s AND status='WAITING_APPROVAL'
                """,
                (_required_text(run_id, "run_id"),),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise InvalidStateError("approval run is not waiting")
        return self.get(run_id, for_update=True) or {}

    def request_cancel(
        self,
        run_id: str,
        *,
        requested_by_type: str,
        requested_by_id: str | None = None,
        reason: str | None = None,
        requested_at: Any = None,
    ) -> dict[str, Any]:
        with self.cursor() as cursor:
            cursor.execute("SELECT status FROM agent_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            current = _row_dict(cursor, cursor.fetchone())
            if current is None:
                raise KeyError("run not found")
            if current.get("status") in {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"}:
                raise InvalidStateError("terminal run cannot accept cancellation")
            cursor.execute(
                """
                UPDATE agent_runs
                SET cancel_requested_at=COALESCE(cancel_requested_at, %s),
                    cancel_requested_by_type=COALESCE(cancel_requested_by_type, %s),
                    cancel_requested_by_id=COALESCE(cancel_requested_by_id, %s),
                    cancel_reason=COALESCE(cancel_reason, %s),
                    version=version+1
                WHERE run_id=%s
                """,
                (
                    requested_at or datetime.now(),
                    _required_text(requested_by_type, "requested_by_type"),
                    _optional_text(requested_by_id),
                    _safe_error(reason),
                    run_id,
                ),
            )
        return self.get(run_id, for_update=True) or {}

    def create_or_get(self, row: Mapping[str, Any]) -> dict[str, Any]:
        run_id = _required_text(row.get("run_id"), "run_id")
        work_item_id = _required_text(row.get("work_item_id"), "work_item_id")
        command_id = _required_text(row.get("command_id"), "command_id")
        run_no = int(row.get("run_no") or 0)
        if run_no <= 0:
            raise ValueError("run_no must be positive")
        status = _status(row.get("status"), RUN_STATUSES, "run status", default="RECEIVED")
        plan_json = row.get("plan_json")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_runs (
                    run_id, work_item_id, command_id, run_no, status, mode,
                    planner_kind, planner_provider, planner_model, plan_schema_version,
                    plan_json, plan_hash, tool_catalog_sha256, context_fingerprint_sha256,
                    correlation_id, causation_id, retry_of_run_id, error_code,
                    error_summary, retryable, next_attempt_at, started_at, finished_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW(6)), %s, %s
                )
                ON DUPLICATE KEY UPDATE run_id = run_id
                """,
                (
                    run_id,
                    work_item_id,
                    command_id,
                    run_no,
                    status,
                    _required_text(row.get("mode"), "mode"),
                    _required_text(row.get("planner_kind") or "NONE", "planner_kind"),
                    _optional_text(row.get("planner_provider")),
                    _optional_text(row.get("planner_model")),
                    row.get("plan_schema_version"),
                    _json_param(plan_json, {}) if plan_json is not None else None,
                    _optional_text(row.get("plan_hash")),
                    _optional_text(row.get("tool_catalog_sha256")),
                    _optional_text(row.get("context_fingerprint_sha256")),
                    _required_text(row.get("correlation_id"), "correlation_id"),
                    _optional_text(row.get("causation_id")),
                    _optional_text(row.get("retry_of_run_id")),
                    _optional_text(row.get("error_code")),
                    _safe_error(row.get("error_summary")),
                    bool(row.get("retryable", False)),
                    row.get("next_attempt_at"),
                    row.get("started_at"),
                    row.get("finished_at"),
                ),
            )
            created = int(getattr(cursor, "rowcount", 0) or 0) == 1
            cursor.execute(
                "SELECT * FROM agent_runs WHERE work_item_id=%s AND run_no=%s FOR UPDATE",
                (work_item_id, run_no),
            )
            persisted = _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)
        if persisted is None:
            raise IdempotencyConflict("run identity collides with a different work item run number")
        if persisted.get("command_id") != command_id:
            raise IdempotencyConflict("work item run number was reused for a different command")
        return _created_flag(persisted, created)

    def transition(
        self,
        run_id: str,
        *,
        expected_version: int,
        expected_statuses: Iterable[str],
        status: str,
        plan: Any = None,
        plan_hash: str | None = None,
        plan_schema_version: int | None = None,
        tool_catalog_sha256: str | None = None,
        context_fingerprint_sha256: str | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        retryable: bool = False,
        worker_id: str | None = None,
        lease_expires_at: Any = None,
        next_attempt_at: Any = None,
        started_at: Any = None,
        finished_at: Any = None,
        increment_execution_attempt: bool = False,
    ) -> dict[str, Any]:
        next_status = _status(status, RUN_STATUSES, "run status")
        allowed = sorted({_status(item, RUN_STATUSES, "expected run status") for item in expected_statuses})
        if not allowed:
            raise ValueError("expected_statuses is required")
        plan_updates = (
            plan,
            plan_hash,
            plan_schema_version,
            tool_catalog_sha256,
            context_fingerprint_sha256,
        )
        if next_status in allowed and any(value is not None for value in plan_updates):
            raise InvalidStateError(
                "same-state plan refresh must use refresh_waiting_plan"
            )
        placeholders = ", ".join("%s" for _ in allowed)
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE agent_runs
                SET status=%s,
                    plan_json=COALESCE(%s, plan_json),
                    plan_hash=COALESCE(%s, plan_hash),
                    plan_schema_version=COALESCE(%s, plan_schema_version),
                    tool_catalog_sha256=COALESCE(%s, tool_catalog_sha256),
                    context_fingerprint_sha256=COALESCE(%s, context_fingerprint_sha256),
                    error_code=%s, error_summary=%s, retryable=%s,
                    worker_id=COALESCE(%s, worker_id),
                    lease_expires_at=COALESCE(%s, lease_expires_at),
                    next_attempt_at=COALESCE(%s, next_attempt_at),
                    started_at=COALESCE(%s, started_at), finished_at=%s,
                    execution_attempt_count=execution_attempt_count+%s,
                    version=version+1
                WHERE run_id=%s AND version=%s AND status IN ({placeholders})
                """,
                (
                    next_status,
                    _json_param(plan, {}) if plan is not None else None,
                    _optional_text(plan_hash),
                    int(plan_schema_version) if plan_schema_version is not None else None,
                    _optional_text(tool_catalog_sha256),
                    _optional_text(context_fingerprint_sha256),
                    _optional_text(error_code),
                    _safe_error(error_summary),
                    bool(retryable),
                    _optional_text(worker_id),
                    lease_expires_at,
                    next_attempt_at,
                    started_at,
                    finished_at,
                    1 if increment_execution_attempt else 0,
                    run_id,
                    int(expected_version),
                    *allowed,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("run state changed before transition")
        return self.get(run_id, for_update=True) or {}

    def refresh_waiting_plan(
        self,
        run_id: str,
        *,
        expected_version: int,
        plan: Any,
        plan_hash: str,
        catalog_hash: str,
        context_hash: str,
        plan_schema_version: int = 1,
    ) -> dict[str, Any]:
        """CAS-refresh the only run state allowed to replace an existing plan."""

        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_runs
                SET plan_json=%s, plan_hash=%s, plan_schema_version=%s,
                    tool_catalog_sha256=%s, context_fingerprint_sha256=%s,
                    version=version+1
                WHERE run_id=%s AND version=%s AND status='WAITING_APPROVAL'
                """,
                (
                    _json_param(plan, {}),
                    _required_text(plan_hash, "plan_hash"),
                    int(plan_schema_version),
                    _required_text(catalog_hash, "catalog_hash"),
                    _required_text(context_hash, "context_hash"),
                    run_id,
                    int(expected_version),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("waiting approval plan changed before refresh")
        return self.get(run_id, for_update=True) or {}


class AgentRunStepRepository(_RepositoryBase):
    JSON_FIELDS = ("input_summary_json", "result_summary_json", "postcondition_json")

    def get(self, step_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(f"SELECT * FROM agent_run_steps WHERE step_id=%s{suffix}", (step_id,))
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute("SELECT * FROM agent_run_steps WHERE run_id=%s ORDER BY step_order", (run_id,))
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def list_interrupted_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Lock only stale in-flight steps for a no-receipt recovery."""

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM agent_run_steps
                WHERE run_id=%s AND status IN (
                    'RUNNING', 'VERIFYING', 'BLOCKED_DATA', 'FAILED_RETRYABLE'
                )
                ORDER BY step_order, step_id FOR UPDATE
                """,
                (_required_text(run_id, "run_id"),),
            )
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def create_or_get(self, row: Mapping[str, Any]) -> dict[str, Any]:
        step_id = _required_text(row.get("step_id"), "step_id")
        run_id = _required_text(row.get("run_id"), "run_id")
        step_key = _required_text(row.get("step_key"), "step_key")
        step_order = int(row.get("step_order") or 0)
        if step_order <= 0:
            raise ValueError("step_order must be positive")
        status = _status(row.get("status"), STEP_STATUSES, "step status", default="PENDING")
        operation_value = getattr(row.get("operation_type"), "value", row.get("operation_type"))
        risk_value = getattr(row.get("risk_level"), "value", row.get("risk_level"))
        operation_type = str(operation_value or "").strip().upper()
        risk_level = str(risk_value or "").strip().upper()
        if operation_type not in {
            "READ",
            "COMPUTE",
            "INTERNAL_PROJECTION_WRITE",
            "EXTERNAL_WRITE",
            "FINANCIAL_WRITE",
            "DESTRUCTIVE",
        }:
            raise InvalidStateError(f"unsupported operation_type: {operation_type or '<empty>'}")
        if risk_level not in {"LOW", "MEDIUM", "HIGH", "EXTREME"}:
            raise InvalidStateError(f"unsupported risk_level: {risk_level or '<empty>'}")
        input_summary = row.get("input_summary_json")
        result_summary = row.get("result_summary_json")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_run_steps (
                    step_id, run_id, step_key, step_order, tool_name, tool_version,
                    operation_type, risk_level, status, requires_approval, retry_safe,
                    idempotency_key, account_id, input_summary_json, input_sha256,
                    result_summary_json, result_sha256, attempt_count,
                    postcondition_status, postcondition_json, error_code, error_summary,
                    started_at, finished_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE step_id = step_id
                """,
                (
                    step_id,
                    run_id,
                    step_key,
                    step_order,
                    _required_text(row.get("tool_name"), "tool_name"),
                    _required_text(row.get("tool_version"), "tool_version"),
                    operation_type,
                    risk_level,
                    status,
                    bool(row.get("requires_approval", False)),
                    bool(row.get("retry_safe", False)),
                    _optional_text(row.get("idempotency_key")),
                    _optional_text(row.get("account_id")),
                    _json_param(input_summary, {}) if input_summary is not None else None,
                    _optional_text(row.get("input_sha256"))
                    or (_json_hash(input_summary) if input_summary is not None else None),
                    _json_param(result_summary, {}) if result_summary is not None else None,
                    _optional_text(row.get("result_sha256")) or (
                        _json_hash(result_summary) if result_summary is not None else None
                    ),
                    max(0, int(row.get("attempt_count") or 0)),
                    _optional_text(row.get("postcondition_status")),
                    _json_param(row.get("postcondition_json"), {})
                    if row.get("postcondition_json") is not None
                    else None,
                    _optional_text(row.get("error_code")),
                    _safe_error(row.get("error_summary")),
                    row.get("started_at"),
                    row.get("finished_at"),
                ),
            )
            created = int(getattr(cursor, "rowcount", 0) or 0) == 1
            cursor.execute(
                "SELECT * FROM agent_run_steps WHERE run_id=%s AND step_key=%s FOR UPDATE",
                (run_id, step_key),
            )
            persisted = _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)
        if persisted is None:
            raise IdempotencyConflict("step identity collides with a different run step key")
        return _created_flag(persisted, created)

    def transition(
        self,
        step_id: str,
        *,
        expected_version: int,
        expected_statuses: Iterable[str],
        status: str,
        result_summary: Any = None,
        postcondition_status: str | None = None,
        postcondition: Any = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        increment_attempt: bool = False,
        started_at: Any = None,
        finished_at: Any = None,
    ) -> dict[str, Any]:
        next_status = _status(status, STEP_STATUSES, "step status")
        allowed = sorted({_status(item, STEP_STATUSES, "expected step status") for item in expected_statuses})
        if not allowed:
            raise ValueError("expected_statuses is required")
        placeholders = ", ".join("%s" for _ in allowed)
        result_json = _json_param(result_summary, {}) if result_summary is not None else None
        result_sha = _json_hash(result_summary) if result_summary is not None else None
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE agent_run_steps
                SET status=%s,
                    result_summary_json=COALESCE(%s, result_summary_json),
                    result_sha256=COALESCE(%s, result_sha256),
                    postcondition_status=%s,
                    postcondition_json=%s,
                    error_code=%s, error_summary=%s,
                    attempt_count=attempt_count+%s,
                    started_at=COALESCE(%s, started_at), finished_at=%s,
                    version=version+1
                WHERE step_id=%s AND version=%s AND status IN ({placeholders})
                """,
                (
                    next_status,
                    result_json,
                    result_sha,
                    _optional_text(postcondition_status),
                    _json_param(postcondition, {}) if postcondition is not None else None,
                    _optional_text(error_code),
                    _safe_error(error_summary),
                    1 if increment_attempt else 0,
                    started_at,
                    finished_at,
                    step_id,
                    int(expected_version),
                    *allowed,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("step state changed before transition")
        return self.get(step_id, for_update=True) or {}


class ApprovalRepository(_RepositoryBase):
    def get(self, approval_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(f"SELECT * FROM approval_requests WHERE approval_id=%s{suffix}", (approval_id,))
            request = _decode_row(_row_dict(cursor, cursor.fetchone()), ("impact_json",))
            if request is None:
                return None
            cursor.execute(
                "SELECT * FROM approval_decisions WHERE approval_id=%s ORDER BY decided_at, decision_id",
                (approval_id,),
            )
            request["decisions"] = [
                _decode_row(row, ("actor_roles_json",)) or {} for row in _rows(cursor)
            ]
            return request

    def create_or_get(self, row: Mapping[str, Any]) -> dict[str, Any]:
        approval_id = _required_text(row.get("approval_id"), "approval_id")
        run_id = _required_text(row.get("run_id"), "run_id")
        plan_hash = _required_text(row.get("plan_hash"), "plan_hash")
        approval_round = int(row.get("approval_round") or 0)
        required_approvals = int(row.get("required_approvals") or 1)
        if approval_round <= 0 or required_approvals <= 0:
            raise ValueError("approval_round and required_approvals must be positive")
        risk_value = getattr(row.get("risk_level"), "value", row.get("risk_level"))
        risk_level = str(risk_value or "").strip().upper()
        if risk_level not in {"LOW", "MEDIUM", "HIGH", "EXTREME"}:
            raise InvalidStateError(f"unsupported approval risk: {risk_level or '<empty>'}")
        impact = row.get("impact_json", row.get("impact", {}))
        impact_sha = _optional_text(row.get("impact_sha256")) or _json_hash(impact)
        status = _status(row.get("status"), APPROVAL_STATUSES, "approval status", default="PENDING")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT work_item_id, plan_hash FROM agent_runs WHERE run_id=%s FOR UPDATE",
                (run_id,),
            )
            run = _row_dict(cursor, cursor.fetchone())
            if run is None:
                raise KeyError("approval run not found")
            if str(run.get("plan_hash") or "") != plan_hash:
                raise InvalidStateError("approval plan hash does not match the persisted run")
            if str(run.get("work_item_id") or "") != _required_text(row.get("work_item_id"), "work_item_id"):
                raise InvalidStateError("approval work item does not match the persisted run")
            cursor.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, work_item_id, run_id, approval_round, plan_hash,
                    impact_json, impact_sha256, risk_level, required_role, required_approvals,
                    status, requested_by_type, requested_by_id, expires_at, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE approval_id = approval_id
                """,
                (
                    approval_id,
                    _required_text(row.get("work_item_id"), "work_item_id"),
                    run_id,
                    approval_round,
                    plan_hash,
                    _json_param(impact, {}),
                    impact_sha,
                    risk_level,
                    _required_text(row.get("required_role"), "required_role"),
                    required_approvals,
                    status,
                    _required_text(row.get("requested_by_type"), "requested_by_type"),
                    _optional_text(row.get("requested_by_id")),
                    row.get("expires_at"),
                    row.get("decided_at"),
                ),
            )
            created = int(getattr(cursor, "rowcount", 0) or 0) == 1
            cursor.execute(
                """
                SELECT * FROM approval_requests
                WHERE run_id=%s AND plan_hash=%s AND approval_round=%s FOR UPDATE
                """,
                (run_id, plan_hash, approval_round),
            )
            persisted = _decode_row(_row_dict(cursor, cursor.fetchone()), ("impact_json",))
        if persisted is None:
            raise IdempotencyConflict("approval identity collides with a different plan round")
        if str(persisted.get("impact_sha256") or "") != impact_sha:
            raise IdempotencyConflict("approval plan round was reused with a different impact")
        return _created_flag(persisted, created)

    def create_request(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return self.create_or_get(row)

    def get_current_by_run(self, run_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM approval_requests
                WHERE run_id=%s AND status='PENDING'
                ORDER BY approval_round DESC, created_at DESC LIMIT 1{suffix}
                """,
                (run_id,),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), ("impact_json",))

    def get_latest_for_run(self, run_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM approval_requests
                WHERE run_id=%s
                ORDER BY approval_round DESC, created_at DESC LIMIT 1{suffix}
                """,
                (run_id,),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), ("impact_json",))

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM approval_requests
                WHERE run_id=%s ORDER BY approval_round, created_at, approval_id
                """,
                (run_id,),
            )
            return [_decode_row(row, ("impact_json",)) or {} for row in _rows(cursor)]

    def expire_stale(
        self,
        run_id: str,
        plan_hash: str,
        *,
        now: Any = None,
    ) -> dict[str, int]:
        if now is not None:
            raise ValueError("approval expiry uses the MySQL clock and does not accept caller time")
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE approval_requests
                SET status='EXPIRED', decided_at=NOW(6)
                WHERE run_id=%s AND status='PENDING'
                  AND expires_at <= NOW(6)
                """,
                (run_id,),
            )
            expired = int(getattr(cursor, "rowcount", 0) or 0)
            cursor.execute(
                """
                UPDATE approval_requests
                SET status='INVALIDATED', decided_at=NOW(6)
                WHERE run_id=%s AND status IN ('PENDING', 'APPROVED')
                  AND plan_hash <> %s
                """,
                (run_id, _required_text(plan_hash, "plan_hash")),
            )
            invalidated = int(getattr(cursor, "rowcount", 0) or 0)
        return {"expired": expired, "invalidated": invalidated}

    def invalidate_pending(
        self,
        run_id: str,
        *,
        except_plan_hash: str | None = None,
        reason: str | None = None,
        decided_at: Any = None,
    ) -> int:
        del reason  # Reserved for a future approval transition audit event.
        if decided_at is not None:
            raise ValueError("approval invalidation uses the MySQL clock")
        sql = """
            UPDATE approval_requests
            SET status='INVALIDATED', decided_at=NOW(6)
            WHERE run_id=%s AND status IN ('PENDING', 'APPROVED')
        """
        params: list[Any] = [run_id]
        if except_plan_hash:
            sql += " AND plan_hash <> %s"
            params.append(except_plan_hash)
        with self.cursor() as cursor:
            cursor.execute(sql, params)
            return int(getattr(cursor, "rowcount", 0) or 0)

    def expire(self, approval_id: str, *, now: Any = None) -> dict[str, Any]:
        if now is not None:
            raise ValueError("approval expiry uses the MySQL clock and does not accept caller time")
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE approval_requests
                SET status='EXPIRED', decided_at=NOW(6)
                WHERE approval_id=%s AND status='PENDING'
                  AND expires_at <= NOW(6)
                """,
                (approval_id,),
            )
        current = self.get(approval_id, for_update=True)
        if current is None:
            raise KeyError("approval request not found")
        return current

    def record_decision(
        self,
        row: Mapping[str, Any],
        *,
        expected_plan_hash: str,
    ) -> dict[str, Any]:
        approval_id = _required_text(row.get("approval_id"), "approval_id")
        decision = str(row.get("decision") or "").strip().upper()
        if decision not in {"APPROVED", "REJECTED"}:
            raise InvalidStateError(f"unsupported approval decision: {decision or '<empty>'}")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT ar.*, r.plan_hash AS current_plan_hash,
                       (ar.expires_at <= NOW(6)) AS is_expired
                FROM approval_requests ar
                JOIN agent_runs r ON r.run_id = ar.run_id
                WHERE ar.approval_id=%s FOR UPDATE
                """,
                (approval_id,),
            )
            request = _row_dict(cursor, cursor.fetchone())
            if request is None:
                raise KeyError("approval request not found")
            if request.get("status") != "PENDING":
                raise InvalidStateError("approval request is no longer pending")
            if bool(request.get("is_expired")):
                cursor.execute(
                    """
                    UPDATE approval_requests
                    SET status='EXPIRED', decided_at=NOW(6)
                    WHERE approval_id=%s AND status='PENDING' AND expires_at <= NOW(6)
                    """,
                    (approval_id,),
                )
                expired = self.get(approval_id, for_update=True) or {}
                expired["_decision_error"] = "APPROVAL_EXPIRED"
                return expired
            if (
                request.get("plan_hash") != expected_plan_hash
                or request.get("current_plan_hash") != expected_plan_hash
            ):
                raise InvalidStateError("approval plan hash is stale")
            cursor.execute(
                """
                INSERT INTO approval_decisions (
                    decision_id, approval_id, actor_type, actor_id,
                    actor_roles_json, decision, reason, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE decision_id = decision_id
                """,
                (
                    _required_text(row.get("decision_id"), "decision_id"),
                    approval_id,
                    _required_text(row.get("actor_type"), "actor_type"),
                    _required_text(row.get("actor_id"), "actor_id"),
                    _json_param(row.get("actor_roles_json", row.get("actor_roles", [])), []),
                    decision,
                    _safe_error(row.get("reason")),
                    row.get("decided_at") or datetime.now(),
                ),
            )
            cursor.execute(
                """
                SELECT
                    SUM(decision='APPROVED') AS approved_count,
                    SUM(decision='REJECTED') AS rejected_count
                FROM approval_decisions WHERE approval_id=%s
                """,
                (approval_id,),
            )
            counts = _row_dict(cursor, cursor.fetchone()) or {}
            approved = int(counts.get("approved_count") or 0)
            rejected = int(counts.get("rejected_count") or 0)
            next_status = "REJECTED" if rejected else (
                "APPROVED" if approved >= int(request.get("required_approvals") or 1) else "PENDING"
            )
            if next_status != "PENDING":
                cursor.execute(
                    "UPDATE approval_requests SET status=%s, decided_at=NOW(6) WHERE approval_id=%s",
                    (next_status, approval_id),
                )
        return self.get(approval_id, for_update=True) or {}

    def prepare_approved_execution(
        self,
        run_id: str,
        *,
        expected_plan_hash: str,
    ) -> dict[str, Any]:
        """Lock Run+Approval and evaluate approval against the MySQL clock.

        The caller must transition the returned locked Run to ``RUNNING`` in
        this same Unit of Work.  Returning an APPROVED outcome without that
        transition being committed is harmless because no execution authority
        escapes the transaction.
        """

        plan_hash = _required_text(expected_plan_hash, "expected_plan_hash")
        with self.cursor() as cursor:
            cursor.execute("SELECT * FROM agent_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            run = _decode_row(_row_dict(cursor, cursor.fetchone()), AgentRunRepository.JSON_FIELDS)
            if run is None:
                raise KeyError("approval run not found")
            if str(run.get("status") or "") != "WAITING_APPROVAL":
                raise InvalidStateError("approval run is no longer waiting for approval")

            cursor.execute(
                """
                UPDATE approval_requests
                SET status='EXPIRED', decided_at=NOW(6)
                WHERE run_id=%s AND status='PENDING'
                  AND expires_at <= NOW(6)
                """,
                (run_id,),
            )
            expired_count = int(getattr(cursor, "rowcount", 0) or 0)
            cursor.execute(
                """
                UPDATE approval_requests
                SET status='INVALIDATED', decided_at=NOW(6)
                WHERE run_id=%s AND status IN ('PENDING', 'APPROVED')
                  AND plan_hash <> %s
                """,
                (run_id, plan_hash),
            )
            invalidated_count = int(getattr(cursor, "rowcount", 0) or 0)
            cursor.execute(
                """
                SELECT ar.*
                FROM approval_requests ar
                WHERE ar.run_id=%s
                ORDER BY ar.approval_round DESC, ar.created_at DESC
                LIMIT 1 FOR UPDATE
                """,
                (run_id,),
            )
            approval = _decode_row(_row_dict(cursor, cursor.fetchone()), ("impact_json",))

        if str(run.get("plan_hash") or "") != plan_hash:
            outcome = "PLAN_STALE"
        elif approval is None:
            outcome = "MISSING"
        elif str(approval.get("plan_hash") or "") != plan_hash:
            outcome = "INVALIDATED"
        elif approval.get("status") == "APPROVED":
            outcome = "APPROVED"
        else:
            outcome = str(approval.get("status") or "MISSING")
        return {
            "outcome": outcome,
            "run": run,
            "approval": approval,
            "expired_count": expired_count,
            "invalidated_count": invalidated_count,
        }


class EvidenceRepository(_RepositoryBase):
    def add(self, row: Mapping[str, Any]) -> dict[str, Any]:
        evidence_id = _required_text(row.get("evidence_id"), "evidence_id")
        summary = row.get("summary_json", row.get("summary"))
        content_sha = _optional_text(row.get("content_sha256")) or _json_hash(summary or {})
        completeness = str(row.get("completeness_status") or "UNKNOWN").strip().upper()
        if completeness not in {"COMPLETE", "INCOMPLETE", "UNKNOWN"}:
            raise InvalidStateError(f"unsupported evidence completeness: {completeness}")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO evidence_records (
                    evidence_id, work_item_id, run_id, step_id, source_system,
                    account_id, source_record_type, source_record_id, entity_type,
                    entity_id, occurred_at, observed_at, completeness_status,
                    pagination_complete, record_count, content_sha256, summary_json, storage_ref
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE evidence_id = evidence_id
                """,
                (
                    evidence_id,
                    _required_text(row.get("work_item_id"), "work_item_id"),
                    _optional_text(row.get("run_id")),
                    _optional_text(row.get("step_id")),
                    _required_text(row.get("source_system"), "source_system"),
                    _optional_text(row.get("account_id")),
                    _required_text(row.get("source_record_type"), "source_record_type"),
                    _required_text(row.get("source_record_id"), "source_record_id"),
                    _required_text(row.get("entity_type"), "entity_type"),
                    _required_text(row.get("entity_id"), "entity_id"),
                    row.get("occurred_at"),
                    row.get("observed_at") or datetime.now(),
                    completeness,
                    row.get("pagination_complete"),
                    row.get("record_count"),
                    content_sha,
                    _json_param(summary, {}) if summary is not None else None,
                    _optional_text(row.get("storage_ref")),
                ),
            )
            created = int(getattr(cursor, "rowcount", 0) or 0) == 1
            cursor.execute("SELECT * FROM evidence_records WHERE evidence_id=%s", (evidence_id,))
            persisted = _decode_row(_row_dict(cursor, cursor.fetchone()), ("summary_json",))
        if persisted is None or str(persisted.get("content_sha256") or "") != content_sha:
            raise IdempotencyConflict("evidence id was reused with different content")
        return _created_flag(persisted, created)

    def list(
        self,
        work_item_id: str,
        *,
        run_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM evidence_records WHERE work_item_id=%s"
        params: list[Any] = [work_item_id]
        if run_id:
            sql += " AND run_id=%s"
            params.append(run_id)
        sql += " ORDER BY observed_at, evidence_id LIMIT %s"
        params.append(max(1, min(int(limit), 500)))
        with self.cursor() as cursor:
            cursor.execute(sql, params)
            return [_decode_row(row, ("summary_json",)) or {} for row in _rows(cursor)]


class ExternalEntityLinkRepository(_RepositoryBase):
    def upsert(self, row: Mapping[str, Any]) -> dict[str, Any]:
        identity = (
            _required_text(row.get("source_system"), "source_system"),
            _required_text(row.get("account_scope"), "account_scope"),
            _required_text(row.get("external_entity_type"), "external_entity_type"),
            _required_text(row.get("external_id"), "external_id"),
        )
        canonical_type = _required_text(row.get("canonical_entity_type"), "canonical_entity_type")
        canonical_id = _required_text(row.get("canonical_entity_id"), "canonical_entity_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO external_entity_links (
                    link_id, canonical_entity_type, canonical_entity_id, source_system,
                    account_scope, external_entity_type, external_id, parent_external_id,
                    relation_type, verified_at, valid_from, valid_to, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    parent_external_id=VALUES(parent_external_id),
                    relation_type=VALUES(relation_type),
                    verified_at=VALUES(verified_at),
                    valid_from=VALUES(valid_from),
                    valid_to=VALUES(valid_to),
                    metadata_json=VALUES(metadata_json)
                """,
                (
                    _required_text(row.get("link_id"), "link_id"),
                    canonical_type,
                    canonical_id,
                    *identity,
                    _optional_text(row.get("parent_external_id")),
                    _required_text(row.get("relation_type"), "relation_type"),
                    row.get("verified_at"),
                    row.get("valid_from"),
                    row.get("valid_to"),
                    _json_param(row.get("metadata_json"), {}) if row.get("metadata_json") is not None else None,
                ),
            )
            cursor.execute(
                """
                SELECT * FROM external_entity_links
                WHERE source_system=%s AND account_scope=%s
                  AND external_entity_type=%s AND external_id=%s FOR UPDATE
                """,
                identity,
            )
            persisted = _decode_row(_row_dict(cursor, cursor.fetchone()), ("metadata_json",))
        if persisted is None:
            raise IdempotencyConflict("external entity identity collides with another link")
        if (
            persisted.get("canonical_entity_type") != canonical_type
            or persisted.get("canonical_entity_id") != canonical_id
        ):
            raise IdempotencyConflict("external entity identity is already bound to another canonical entity")
        return persisted


class DomainEventRepository(_RepositoryBase):
    JSON_FIELDS = ("payload_json", "headers_json")

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.cursor() as cursor:
            cursor.execute("SELECT * FROM domain_events WHERE event_id=%s", (event_id,))
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def get_first_for_entity(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM domain_events
                WHERE entity_type=%s AND entity_id=%s
                ORDER BY occurred_at, created_at, event_id LIMIT 1
                """,
                (entity_type, entity_id),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)

    def list_for_work_item(self, work_item_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM domain_events
                WHERE work_item_id=%s
                ORDER BY occurred_at, created_at, event_id LIMIT %s
                """,
                (work_item_id, max(1, min(int(limit), 1000))),
            )
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def list_for_work_item_by_type(
        self,
        work_item_id: str,
        event_type: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Page one typed event stream deterministically without silent truncation."""

        page_limit = max(1, min(int(limit), 1000))
        page_offset = max(0, int(offset))
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM domain_events
                WHERE work_item_id=%s AND event_type=%s
                ORDER BY occurred_at, created_at, event_id
                LIMIT %s OFFSET %s
                """,
                (
                    _required_text(work_item_id, "work_item_id"),
                    _required_text(event_type, "event_type"),
                    page_limit,
                    page_offset,
                ),
            )
            return [_decode_row(row, self.JSON_FIELDS) or {} for row in _rows(cursor)]

    def append_with_outbox(
        self,
        event_row: Mapping[str, Any],
        outbox_rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        event_id = _required_text(event_row.get("event_id"), "event_id")
        event_type = _required_text(event_row.get("event_type"), "event_type")
        source_system = _required_text(event_row.get("source_system"), "source_system")
        source_event_id = _optional_text(event_row.get("source_event_id"))
        payload = event_row.get("payload_json", event_row.get("payload", {}))
        payload_sha = _optional_text(event_row.get("payload_sha256")) or _json_hash(payload)
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO domain_events (
                    event_id, event_type, schema_version, source_system, source_event_id,
                    entity_type, entity_id, work_item_id, run_id, step_id, occurred_at,
                    observed_at, correlation_id, causation_id, payload_json,
                    payload_sha256, headers_json
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE event_id = event_id
                """,
                (
                    event_id,
                    event_type,
                    int(event_row.get("schema_version") or 1),
                    source_system,
                    source_event_id,
                    _required_text(event_row.get("entity_type"), "entity_type"),
                    _required_text(event_row.get("entity_id"), "entity_id"),
                    _optional_text(event_row.get("work_item_id")),
                    _optional_text(event_row.get("run_id")),
                    _optional_text(event_row.get("step_id")),
                    event_row.get("occurred_at") or datetime.now(),
                    event_row.get("observed_at") or datetime.now(),
                    _required_text(event_row.get("correlation_id"), "correlation_id"),
                    _optional_text(event_row.get("causation_id")),
                    _json_param(payload, {}),
                    payload_sha,
                    _json_param(event_row.get("headers_json"), {})
                    if event_row.get("headers_json") is not None
                    else None,
                ),
            )
            created = int(getattr(cursor, "rowcount", 0) or 0) == 1
            if source_event_id:
                cursor.execute(
                    """
                    SELECT * FROM domain_events
                    WHERE source_system=%s AND event_type=%s AND source_event_id=%s FOR UPDATE
                    """,
                    (source_system, event_type, source_event_id),
                )
            else:
                cursor.execute("SELECT * FROM domain_events WHERE event_id=%s FOR UPDATE", (event_id,))
            persisted = _decode_row(_row_dict(cursor, cursor.fetchone()), self.JSON_FIELDS)
            if persisted is None:
                raise IdempotencyConflict("domain event identity collides with another event")
            immutable_matches = (
                persisted.get("event_type") == event_type
                and persisted.get("source_system") == source_system
                and int(persisted.get("schema_version") or 0)
                == int(event_row.get("schema_version") or 1)
                and persisted.get("entity_type") == _required_text(event_row.get("entity_type"), "entity_type")
                and persisted.get("entity_id") == _required_text(event_row.get("entity_id"), "entity_id")
                and _optional_text(persisted.get("work_item_id"))
                == _optional_text(event_row.get("work_item_id"))
                and _optional_text(persisted.get("run_id")) == _optional_text(event_row.get("run_id"))
                and _optional_text(persisted.get("step_id")) == _optional_text(event_row.get("step_id"))
                and persisted.get("correlation_id")
                == _required_text(event_row.get("correlation_id"), "correlation_id")
                and _optional_text(persisted.get("causation_id"))
                == _optional_text(event_row.get("causation_id"))
                and str(persisted.get("payload_sha256") or "") == payload_sha
            )
            if not immutable_matches:
                raise IdempotencyConflict("domain event identity was reused with different immutable content")
            resolved_event_id = str(persisted["event_id"])
            deliveries: list[dict[str, Any]] = []
            for delivery in outbox_rows:
                consumer_name = _required_text(delivery.get("consumer_name"), "consumer_name")
                cursor.execute(
                    """
                    INSERT INTO outbox_events (
                        event_id, consumer_name, topic, partition_key,
                        status, available_at, max_attempts
                    ) VALUES (%s, %s, %s, %s, 'PENDING', COALESCE(%s, NOW(6)), %s)
                    ON DUPLICATE KEY UPDATE outbox_id = outbox_id
                    """,
                    (
                        resolved_event_id,
                        consumer_name,
                        _required_text(delivery.get("topic"), "topic"),
                        _required_text(delivery.get("partition_key"), "partition_key"),
                        delivery.get("available_at"),
                        max(1, int(delivery.get("max_attempts") or 10)),
                    ),
                )
                cursor.execute(
                    "SELECT * FROM outbox_events WHERE event_id=%s AND consumer_name=%s",
                    (resolved_event_id, consumer_name),
                )
                item = _row_dict(cursor, cursor.fetchone())
                if item:
                    if (
                        str(item.get("topic") or "")
                        != _required_text(delivery.get("topic"), "topic")
                        or str(item.get("partition_key") or "")
                        != _required_text(delivery.get("partition_key"), "partition_key")
                        or int(item.get("max_attempts") or 0)
                        != max(1, int(delivery.get("max_attempts") or 10))
                    ):
                        raise IdempotencyConflict(
                            "outbox event consumer was reused with different immutable delivery routing"
                        )
                    deliveries.append(item)
        return {
            "event": _created_flag(persisted, created),
            "outbox": deliveries,
        }


class OutboxRepository(_RepositoryBase):
    EVENT_JSON_FIELDS = ("payload_json", "headers_json")

    def list_for_event(self, event_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM outbox_events WHERE event_id=%s ORDER BY outbox_id",
                (event_id,),
            )
            return _rows(cursor)

    def health(self) -> dict[str, Any]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    SUM(status='PENDING') AS pending,
                    SUM(status='PROCESSING') AS processing,
                    SUM(status='DEAD_LETTER') AS dead_letter,
                    MIN(CASE WHEN status='PENDING' THEN available_at END) AS oldest_pending_at
                FROM outbox_events
                """
            )
            row = _row_dict(cursor, cursor.fetchone()) or {}
        return {
            "pending": int(row.get("pending") or 0),
            "processing": int(row.get("processing") or 0),
            "dead_letter": int(row.get("dead_letter") or 0),
            "oldest_pending_at": row.get("oldest_pending_at"),
        }

    def claim(
        self,
        worker_id: str,
        *,
        limit: int = 50,
        lease_seconds: int = 60,
        consumer_name: str | None = None,
    ) -> list[dict[str, Any]]:
        worker = _required_text(worker_id, "worker_id")
        batch_size = max(1, min(int(limit), 500))
        lease = max(1, min(int(lease_seconds), 3600))
        consumer = _optional_text(consumer_name)
        with self.cursor() as cursor:
            # Recover expired deliveries before new work so a steady pending
            # stream cannot indefinitely starve an interrupted delivery.
            ids = self._lock_outbox_ids(
                cursor,
                status="PROCESSING",
                index_name="idx_outbox_lease",
                due_column="locked_until",
                due_required=True,
                attempts_operator="<",
                limit=batch_size,
                consumer_name=consumer,
            )
            if len(ids) < batch_size:
                ids.extend(
                    self._lock_outbox_ids(
                        cursor,
                        status="PENDING",
                        index_name=(
                            "idx_outbox_consumer_status" if consumer else "idx_outbox_claim"
                        ),
                        due_column="available_at",
                        due_required=True,
                        attempts_operator="<",
                        limit=batch_size - len(ids),
                        consumer_name=consumer,
                    )
                )
            if not ids:
                # Cleanup is deliberately deferred until this worker found no
                # runnable row.  Scanning a residual attempt-count predicate
                # before the claim can lock non-exhausted rows and defeat
                # concurrent SKIP LOCKED workers.
                self._dead_letter_exhausted(cursor)
                return []
            placeholders = ", ".join("%s" for _ in ids)
            cursor.execute(
                f"""
                UPDATE outbox_events
                SET status='PROCESSING', locked_by=%s,
                    locked_until=DATE_ADD(NOW(6), INTERVAL {lease} SECOND),
                    attempt_count=attempt_count+1
                WHERE outbox_id IN ({placeholders})
                  AND attempt_count < max_attempts
                  AND (
                      status='PENDING'
                      OR (status='PROCESSING' AND locked_until <= NOW(6))
                  )
                """,
                (worker, *ids),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != len(ids):
                raise ConcurrentUpdateError("outbox claim lost its locked candidate")
            cursor.execute(
                f"""
                SELECT o.*, d.event_type, d.schema_version, d.source_system,
                       d.source_event_id, d.entity_type, d.entity_id,
                       d.work_item_id, d.run_id, d.step_id, d.occurred_at,
                       d.observed_at, d.correlation_id, d.causation_id,
                       d.payload_json, d.payload_sha256, d.headers_json
                FROM outbox_events o
                JOIN domain_events d ON d.event_id = o.event_id
                WHERE o.outbox_id IN ({placeholders})
                """,
                ids,
            )
            rows_by_id = {int(item["outbox_id"]): item for item in _rows(cursor)}
            if set(rows_by_id) != set(ids):
                raise OrchestrationPersistenceError("claimed outbox event payload is missing")
            claimed = [rows_by_id[outbox_id] for outbox_id in ids]
        decoded_claimed: list[dict[str, Any]] = []
        for item in claimed:
            item["status"] = "PROCESSING"
            item["locked_by"] = worker
            item["attempt_count"] = int(item.get("attempt_count") or 0) + 1
            decoded_claimed.append(_decode_row(item, self.EVENT_JSON_FIELDS) or {})
        return decoded_claimed

    def _dead_letter_exhausted(self, cursor: Any) -> None:
        exhausted_ids = self._lock_outbox_ids(
            cursor,
            status="PENDING",
            index_name="idx_outbox_claim",
            due_column="available_at",
            due_required=False,
            attempts_operator=">=",
            limit=500,
        )
        exhausted_ids.extend(
            self._lock_outbox_ids(
                cursor,
                status="PROCESSING",
                index_name="idx_outbox_lease",
                due_column="locked_until",
                due_required=True,
                attempts_operator=">=",
                limit=max(0, 500 - len(exhausted_ids)),
            )
        )
        if not exhausted_ids:
            return
        placeholders = ", ".join("%s" for _ in exhausted_ids)
        cursor.execute(
            f"""
            UPDATE outbox_events
            SET status='DEAD_LETTER', locked_by=NULL, locked_until=NULL,
                last_error_code=COALESCE(last_error_code, 'LEASE_EXHAUSTED'),
                last_error_summary=COALESCE(
                    last_error_summary,
                    'delivery lease expired at max attempts'
                )
            WHERE outbox_id IN ({placeholders})
              AND status IN ('PENDING', 'PROCESSING')
              AND attempt_count >= max_attempts
              AND (status='PENDING' OR locked_until <= NOW(6))
            """,
            exhausted_ids,
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != len(exhausted_ids):
            raise ConcurrentUpdateError("outbox dead-letter lease cleanup lost its lock")

    @staticmethod
    def _lock_outbox_ids(
        cursor: Any,
        *,
        status: str,
        index_name: str,
        due_column: str,
        due_required: bool,
        attempts_operator: str,
        limit: int,
        consumer_name: str | None = None,
    ) -> list[int]:
        """Lock revalidated primary-key rows without secondary-index gap locks."""

        if limit <= 0:
            return []
        consumer_clause = " AND consumer_name=%s" if consumer_name else ""
        due_clause = f" AND {due_column} <= NOW(6)" if due_required else ""
        order_prefix = "consumer_name, " if consumer_name else ""
        params: list[Any] = [consumer_name] if consumer_name else []
        params.append(OUTBOX_CANDIDATE_SCAN_LIMIT)
        cursor.execute(
            f"""
            SELECT outbox_id FROM outbox_events FORCE INDEX ({index_name})
            WHERE status='{status}' AND attempt_count {attempts_operator} max_attempts
              {due_clause} {consumer_clause}
            ORDER BY {order_prefix}status, {due_column}, outbox_id
            LIMIT %s
            """,
            params,
        )
        candidate_ids = [int(item["outbox_id"]) for item in _rows(cursor)]
        if not candidate_ids:
            return []
        placeholders = ", ".join("%s" for _ in candidate_ids)
        lock_params = [*candidate_ids, *([consumer_name] if consumer_name else []), limit]
        # Exact primary-key locks avoid REPEATABLE READ secondary-index gap locks.
        cursor.execute(
            f"""
            SELECT outbox_id FROM outbox_events FORCE INDEX (PRIMARY)
            WHERE outbox_id IN ({placeholders}) AND status='{status}'
              AND attempt_count {attempts_operator} max_attempts
              {due_clause} {consumer_clause}
            ORDER BY outbox_id LIMIT %s FOR UPDATE SKIP LOCKED
            """,
            lock_params,
        )
        return [int(item["outbox_id"]) for item in _rows(cursor)]

    def mark_published(self, outbox_id: int, *, worker_id: str) -> None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE outbox_events
                SET status='PUBLISHED', published_at=NOW(6),
                    locked_by=NULL, locked_until=NULL,
                    last_error_code=NULL, last_error_summary=NULL
                WHERE outbox_id=%s AND status='PROCESSING' AND locked_by=%s
                """,
                (int(outbox_id), _required_text(worker_id, "worker_id")),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("outbox delivery lease is no longer owned by this worker")

    def reschedule(
        self,
        outbox_id: int,
        *,
        worker_id: str,
        delay_seconds: int,
        error_code: str,
        error_summary: str,
    ) -> str:
        delay = max(0, min(int(delay_seconds), 86_400))
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_count, max_attempts
                FROM outbox_events
                WHERE outbox_id=%s AND status='PROCESSING' AND locked_by=%s FOR UPDATE
                """,
                (int(outbox_id), _required_text(worker_id, "worker_id")),
            )
            current = _row_dict(cursor, cursor.fetchone())
            if current is None:
                raise ConcurrentUpdateError("outbox delivery lease is no longer owned by this worker")
            exhausted = int(current.get("attempt_count") or 0) >= int(current.get("max_attempts") or 0)
            next_status = "DEAD_LETTER" if exhausted else "PENDING"
            cursor.execute(
                f"""
                UPDATE outbox_events
                SET status=%s,
                    available_at=DATE_ADD(NOW(6), INTERVAL {delay} SECOND),
                    locked_by=NULL, locked_until=NULL,
                    last_error_code=%s, last_error_summary=%s
                WHERE outbox_id=%s
                """,
                (
                    next_status,
                    _required_text(error_code, "error_code")[:64],
                    _safe_error(error_summary),
                    int(outbox_id),
                ),
            )
        return next_status

    def record_consumption(
        self,
        *,
        consumer_name: str,
        event_id: str,
        result_summary: Any = None,
        processed_at: Any = None,
    ) -> bool:
        result_json = _json_param(result_summary, {}) if result_summary is not None else None
        result_sha = _json_hash(result_summary) if result_summary is not None else None
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT IGNORE INTO event_consumptions (
                    consumer_name, event_id, processed_at, result_sha256, result_summary_json
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    _required_text(consumer_name, "consumer_name"),
                    _required_text(event_id, "event_id"),
                    processed_at or datetime.now(),
                    result_sha,
                    result_json,
                ),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def was_consumed(self, *, consumer_name: str, event_id: str) -> bool:
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM event_consumptions WHERE consumer_name=%s AND event_id=%s",
                (consumer_name, event_id),
            )
            return cursor.fetchone() is not None


class OrchestrationUnitOfWork:
    """One explicit MySQL transaction spanning all orchestration repositories."""

    def __init__(self, connection_factory: ConnectionFactory, cursor_factory: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self._cursor_factory = cursor_factory
        self.connection: Any | None = None
        self._completed = False
        self._entered = False

    def __enter__(self) -> "OrchestrationUnitOfWork":
        if self._entered:
            raise RuntimeError("unit of work cannot be entered twice")
        connection = self._connection_factory()
        if connection is None:
            raise RuntimeError("connection_factory returned no connection")
        try:
            set_autocommit = getattr(connection, "autocommit", None)
            if callable(set_autocommit):
                set_autocommit(False)
            elif hasattr(connection, "autocommit"):
                setattr(connection, "autocommit", False)
            begin = getattr(connection, "begin", None)
            if callable(begin):
                begin()
        except Exception:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            raise
        self.connection = connection
        self._entered = True
        self.commands = CommandRepository(connection, self._cursor_factory)
        self.work_items = WorkItemRepository(connection, self._cursor_factory)
        self.runs = AgentRunRepository(connection, self._cursor_factory)
        self.steps = AgentRunStepRepository(connection, self._cursor_factory)
        self.approvals = ApprovalRepository(connection, self._cursor_factory)
        self.evidence = EvidenceRepository(connection, self._cursor_factory)
        self.events = DomainEventRepository(connection, self._cursor_factory)
        self.outbox = OutboxRepository(connection, self._cursor_factory)
        self.entity_links = ExternalEntityLinkRepository(connection, self._cursor_factory)
        self.pilot_sources = PilotProjectionSourceRepository(connection, self._cursor_factory)
        self.scheduled_policies = ScheduledTaskApprovalPolicyRepository(
            connection,
            self._cursor_factory,
        )
        self.automation_projects = AutomationProjectPolicyRepository(
            connection,
            self._cursor_factory,
        )
        self.automation_plugins = AutomationPluginRepository(
            connection,
            self._cursor_factory,
        )
        self.feishu_approvals = FeishuApprovalRepository(
            connection,
            self._cursor_factory,
        )
        return self

    def _require_active(self) -> Any:
        if self.connection is None or not self._entered:
            raise RuntimeError("unit of work is not active")
        if self._completed:
            raise RuntimeError("unit of work transaction is already completed")
        return self.connection

    def commit(self) -> None:
        connection = self._require_active()
        connection.commit()
        self._completed = True

    def rollback(self) -> None:
        connection = self._require_active()
        connection.rollback()
        self._completed = True

    def validate_schema(self, *, include_windows_worker: bool = True) -> None:
        self._require_active()
        required_tables, required_columns = orchestration_schema_requirements(
            include_windows_worker=include_windows_worker
        )
        with self.commands.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
            present = {
                str(item.get("TABLE_NAME") or "")
                for item in _rows(cursor)
            }
        missing = sorted(required_tables - present)
        if missing:
            raise RuntimeError(
                "orchestration schema is not migrated; run deployment migrations first: "
                + ", ".join(missing)
            )
        table_names = sorted({table for table, _ in required_columns})
        placeholders = ", ".join("%s" for _ in table_names)
        with self.commands.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT TABLE_NAME, COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})
                """,
                table_names,
            )
            columns = {
                (str(item.get("TABLE_NAME") or ""), str(item.get("COLUMN_NAME") or ""))
                for item in _rows(cursor)
            }
        missing_columns = sorted(
            f"{table}.{column}" for table, column in required_columns - columns
        )
        if missing_columns:
            raise RuntimeError(
                "orchestration schema is not migrated; run deployment migrations first: "
                + ", ".join(missing_columns)
            )

    def command_gateway_create(
        self,
        command_row: Mapping[str, Any],
        work_item_row: Mapping[str, Any],
        run_row: Mapping[str, Any],
        event_row: Mapping[str, Any],
        outbox_rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Create the command gateway aggregate without committing the unit of work."""

        self._require_active()
        command = self.commands.create_or_get(command_row)
        if not command.get("_created"):
            work_item = self.work_items.get_by_command(command["command_id"], for_update=True)
            if work_item is None:
                raise IdempotencyConflict("persisted command has no gateway work item")
            run = self.runs.get_first_for_work_item(work_item["work_item_id"], for_update=True)
            if run is None:
                raise IdempotencyConflict("persisted gateway work item has no initial run")
            event = self.events.get_first_for_entity("agent_command", command["command_id"])
            if event is None:
                raise IdempotencyConflict("persisted command has no command event")
            return {
                "command_id": command["command_id"],
                "work_item_id": work_item["work_item_id"],
                "run_id": run["run_id"],
                "event_id": event["event_id"],
                "created": {
                    "command": False,
                    "work_item": False,
                    "run": False,
                    "event": False,
                },
                "outbox": self.outbox.list_for_event(event["event_id"]),
            }
        item_input = dict(work_item_row)
        item_input["command_id"] = command["command_id"]
        work_item = self.work_items.create_or_get(item_input)
        run_input = dict(run_row)
        run_input["command_id"] = command["command_id"]
        run_input["work_item_id"] = work_item["work_item_id"]
        run_input["correlation_id"] = command["correlation_id"]
        run = self.runs.create_or_get(run_input)
        event_input = dict(event_row)
        event_input["entity_type"] = "agent_command"
        event_input["entity_id"] = command["command_id"]
        event_input["source_event_id"] = command["command_id"]
        event_input["work_item_id"] = work_item["work_item_id"]
        event_input["run_id"] = run["run_id"]
        event_input["correlation_id"] = command["correlation_id"]
        event_receipt = self.events.append_with_outbox(event_input, outbox_rows)
        return {
            "command_id": command["command_id"],
            "work_item_id": work_item["work_item_id"],
            "run_id": run["run_id"],
            "event_id": event_receipt["event"]["event_id"],
            "created": {
                "command": bool(command.get("_created")),
                "work_item": bool(work_item.get("_created")),
                "run": bool(run.get("_created")),
                "event": bool(event_receipt["event"].get("_created")),
            },
            "outbox": event_receipt["outbox"],
        }

    def recover_unknown_automation_write(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        # Public UoW entry point; implementation is configuration-free.
        from shared.automation_unknown_write_recovery import recover_unknown_automation_write

        return recover_unknown_automation_write(
            self, automation_id=automation_id, generation=generation,
            lease_id=lease_id, request_id=request_id, actor_id=actor_id,
            actor_role=actor_role,
        )

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        connection = self.connection
        try:
            if connection is not None and not self._completed:
                connection.rollback()
                self._completed = True
        finally:
            if connection is not None:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()
            self.connection = None
        return False


from shared.orchestration_repository_facade import (
    OrchestrationRepositoryFacadeMixin,
)  # noqa: E402


class OrchestrationRepository(OrchestrationRepositoryFacadeMixin):
    """Stable facade for orchestration transactions and read models."""
