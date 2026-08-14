"""Persistence for per-scheduled-task approval policies and immutable events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    OrchestrationPersistenceError,
    RepositoryBase,
    _decode_row,
    _json_param,
    _optional_text,
    _required_text,
    _row_dict,
    _rows,
    _safe_comment,
)


class ScheduledTaskApprovalPolicyRepository(RepositoryBase):
    """Transactional policy state joined to the exact scheduled task config."""

    POLICY_JSON_FIELDS = ("contract_snapshot_json",)
    EVENT_JSON_FIELDS = ("contract_snapshot_json",)

    @staticmethod
    def _decode_task(row: dict[str, Any] | None) -> dict[str, Any] | None:
        return _decode_row(row, ("tool_params", "contract_snapshot_json"))

    def list_with_tasks(self, *, for_update: bool = False) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT task.id, task.name, task.tool_name, task.tool_params,
                       task.cron_expression, task.enabled,
                       task.configuration_version, task.updated_at,
                       policy.mode, policy.contract_hash,
                       policy.contract_snapshot_json, policy.tool_contract_hash,
                       policy.approved_by_actor_id, policy.approved_by_actor_role,
                       policy.approved_by_actor_display_name, policy.approved_at,
                       policy.comment, policy.version AS policy_version,
                       policy.updated_at AS policy_updated_at
                FROM scheduled_tasks AS task
                LEFT JOIN scheduled_task_approval_policies AS policy
                  ON policy.task_id = task.id
                ORDER BY task.id{suffix}
                """
            )
            rows = _rows(cursor)
        return [self._decode_task(row) or {} for row in rows]

    def lock_task(self, task_id: str) -> dict[str, Any] | None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, tool_name, tool_params, cron_expression,
                       enabled, configuration_version, updated_at
                FROM scheduled_tasks WHERE id=%s FOR UPDATE
                """,
                (_required_text(task_id, "task_id"),),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), ("tool_params",))

    def get_policy(self, task_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM scheduled_task_approval_policies WHERE task_id=%s{suffix}",
                (_required_text(task_id, "task_id"),),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.POLICY_JSON_FIELDS)

    def ensure_default(self, task_id: str) -> dict[str, Any]:
        safe_task_id = _required_text(task_id, "task_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO scheduled_task_approval_policies (task_id, mode)
                VALUES (%s, 'REQUIRE_EACH_RUN')
                ON DUPLICATE KEY UPDATE task_id=task_id
                """,
                (safe_task_id,),
            )
        policy = self.get_policy(safe_task_id, for_update=True)
        if policy is None:
            raise OrchestrationPersistenceError("scheduled task policy did not persist")
        return policy

    def get_event_by_request(self, task_id: str, request_id: str) -> dict[str, Any] | None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM scheduled_task_approval_policy_events
                WHERE task_id=%s AND request_id=%s FOR UPDATE
                """,
                (
                    _required_text(task_id, "task_id"),
                    _required_text(request_id, "request_id"),
                ),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), self.EVENT_JSON_FIELDS)

    def list_events_by_request(self, request_id: str, *, for_update: bool = False) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM scheduled_task_approval_policy_events
                WHERE request_id=%s ORDER BY task_id{suffix}
                """,
                (_required_text(request_id, "request_id"),),
            )
            return [
                _decode_row(row, self.EVENT_JSON_FIELDS) or {}
                for row in _rows(cursor)
            ]

    def update_policy(
        self,
        task_id: str,
        *,
        expected_version: int,
        mode: str,
        contract_hash: str | None,
        contract_snapshot: Mapping[str, Any] | None,
        tool_contract_hash: str | None,
        actor_id: str,
        actor_role: str,
        actor_display_name: str | None,
        comment: str | None,
    ) -> dict[str, Any]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE scheduled_task_approval_policies
                SET mode=%s, contract_hash=%s, contract_snapshot_json=%s,
                    tool_contract_hash=%s, approved_by_actor_id=%s,
                    approved_by_actor_role=%s,
                    approved_by_actor_display_name=%s, approved_at=NOW(6),
                    comment=%s, version=version+1, updated_at=NOW(6)
                WHERE task_id=%s AND version=%s
                """,
                (
                    _required_text(mode, "mode"),
                    _optional_text(contract_hash),
                    _json_param(contract_snapshot, {}) if contract_snapshot is not None else None,
                    _optional_text(tool_contract_hash),
                    _required_text(actor_id, "actor_id"),
                    _required_text(actor_role, "actor_role"),
                    _optional_text(actor_display_name),
                    _safe_comment(comment),
                    _required_text(task_id, "task_id"),
                    int(expected_version),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("scheduled task approval policy version changed")
        policy = self.get_policy(task_id, for_update=True)
        if policy is None:
            raise OrchestrationPersistenceError("scheduled task approval policy disappeared")
        return policy

    def append_event(self, row: Mapping[str, Any]) -> dict[str, Any]:
        task_id = _required_text(row.get("task_id"), "task_id")
        request_id = _required_text(row.get("request_id"), "request_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO scheduled_task_approval_policy_events (
                    task_id, request_id, from_mode, to_mode, contract_hash,
                    contract_snapshot_json, tool_contract_hash, actor_id,
                    actor_role, actor_display_name, reason, comment,
                    occurred_at, correlation_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, COALESCE(%s, NOW(6)), %s
                )
                ON DUPLICATE KEY UPDATE event_id=event_id
                """,
                (
                    task_id,
                    request_id,
                    _optional_text(row.get("from_mode")),
                    _required_text(row.get("to_mode"), "to_mode"),
                    _optional_text(row.get("contract_hash")),
                    _json_param(row.get("contract_snapshot_json"), {})
                    if row.get("contract_snapshot_json") is not None
                    else None,
                    _optional_text(row.get("tool_contract_hash")),
                    _required_text(row.get("actor_id"), "actor_id"),
                    _required_text(row.get("actor_role"), "actor_role"),
                    _optional_text(row.get("actor_display_name")),
                    _required_text(row.get("reason"), "reason"),
                    _safe_comment(row.get("comment")),
                    row.get("occurred_at"),
                    _required_text(row.get("correlation_id"), "correlation_id"),
                ),
            )
        event = self.get_event_by_request(task_id, request_id)
        if event is None:
            raise OrchestrationPersistenceError("scheduled task policy event did not persist")
        return event
