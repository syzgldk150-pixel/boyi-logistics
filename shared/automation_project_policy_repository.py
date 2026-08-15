"""Transactional persistence for project policies and grouped approvals."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
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


PROJECT_POLICY_MODES = frozenset(
    {"PROJECT_FULL_AUTO", "REQUIRE_EACH_RUN", "LEGACY_SCHEDULE_ONLY"}
)


class AutomationProjectPolicyRepository(RepositoryBase):
    POLICY_JSON_FIELDS = ("contract_snapshot_json",)
    EVENT_JSON_FIELDS = ("contract_snapshot_json",)

    def list_policies(self) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_project_policies ORDER BY automation_id"
            )
            return [
                _decode_row(row, self.POLICY_JSON_FIELDS) or {}
                for row in _rows(cursor)
            ]

    def list_account_binding_policy_rows(
        self,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        """Return policy/account binding pairs under one optional write lock."""

        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT policy.*, config.account_bindings_json,
                       config.account_bindings_sha256,
                       config.config_version
                FROM automation_project_policies AS policy
                INNER JOIN automation_project_configs AS config
                    ON config.automation_id=policy.automation_id
                ORDER BY policy.automation_id{suffix}
                """
            )
            return [
                _decode_row(
                    row,
                    (*self.POLICY_JSON_FIELDS, "account_bindings_json"),
                )
                or {}
                for row in _rows(cursor)
            ]

    def get_policy(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM automation_project_policies WHERE automation_id=%s{suffix}",
                (_required_text(automation_id, "automation_id"),),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self.POLICY_JSON_FIELDS,
            )

    def ensure_default(
        self,
        automation_id: str,
        *,
        mode: str,
        project_generation: int,
        project_configuration_version: int,
    ) -> dict[str, Any]:
        normalized_mode = _policy_mode(mode)
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_project_policies (
                    automation_id, project_generation, mode,
                    project_configuration_version, version
                ) VALUES (%s, %s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE automation_id=automation_id
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _project_generation(project_generation),
                    normalized_mode,
                    _configuration_version(project_configuration_version),
                ),
            )
        policy = self.get_policy(automation_id, for_update=True)
        if policy is None:
            raise OrchestrationPersistenceError("automation project policy did not persist")
        return policy

    def update_policy(
        self,
        automation_id: str,
        *,
        expected_version: int,
        mode: str,
        contract_hash: str | None,
        contract_snapshot: Mapping[str, Any] | None,
        tool_contract_hash: str | None,
        plugin_contract_hash: str | None,
        project_generation: int,
        project_configuration_version: int,
        actor_id: str,
        actor_role: str,
        actor_display_name: str | None,
        comment: str | None,
    ) -> dict[str, Any]:
        normalized_mode = _policy_mode(mode)
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_project_policies
                SET mode=%s, contract_hash=%s, contract_snapshot_json=%s,
                    tool_contract_hash=%s, plugin_contract_hash=%s,
                    project_generation=%s,
                    project_configuration_version=%s,
                    approved_by_actor_id=%s, approved_by_actor_role=%s,
                    approved_by_actor_display_name=%s, approved_at=NOW(6),
                    comment=%s, version=version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND version=%s
                """,
                (
                    normalized_mode,
                    _optional_text(contract_hash),
                    _json_param(contract_snapshot, {})
                    if contract_snapshot is not None
                    else None,
                    _optional_text(tool_contract_hash),
                    _optional_text(plugin_contract_hash),
                    _project_generation(project_generation),
                    _configuration_version(project_configuration_version),
                    _required_text(actor_id, "actor_id"),
                    _required_text(actor_role, "actor_role"),
                    _optional_text(actor_display_name),
                    _safe_comment(comment),
                    _required_text(automation_id, "automation_id"),
                    int(expected_version),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("automation project policy version changed")
        policy = self.get_policy(automation_id, for_update=True)
        if policy is None:
            raise OrchestrationPersistenceError("automation project policy disappeared")
        return policy

    def get_event_by_request(
        self,
        automation_id: str,
        request_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM automation_project_policy_events
                WHERE automation_id=%s AND request_id=%s{suffix}
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _required_text(request_id, "request_id"),
                ),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self.EVENT_JSON_FIELDS,
            )

    def append_event(self, row: Mapping[str, Any]) -> dict[str, Any]:
        automation_id = _required_text(row.get("automation_id"), "automation_id")
        request_id = _required_text(row.get("request_id"), "request_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_project_policy_events (
                    automation_id, request_id, from_mode, to_mode,
                    contract_hash, contract_snapshot_json, tool_contract_hash,
                    plugin_contract_hash, project_configuration_version,
                    project_generation, actor_id, actor_role,
                    actor_display_name, reason, comment,
                    correlation_id, occurred_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    COALESCE(%s, NOW(6))
                )
                ON DUPLICATE KEY UPDATE event_id=event_id
                """,
                (
                    automation_id,
                    request_id,
                    _optional_text(row.get("from_mode")),
                    _policy_mode(row.get("to_mode")),
                    _optional_text(row.get("contract_hash")),
                    _json_param(row.get("contract_snapshot_json"), {})
                    if row.get("contract_snapshot_json") is not None
                    else None,
                    _optional_text(row.get("tool_contract_hash")),
                    _optional_text(row.get("plugin_contract_hash")),
                    _configuration_version(row.get("project_configuration_version")),
                    _project_generation(row.get("project_generation")),
                    _required_text(row.get("actor_id"), "actor_id"),
                    _required_text(row.get("actor_role"), "actor_role"),
                    _optional_text(row.get("actor_display_name")),
                    _required_text(row.get("reason"), "reason"),
                    _safe_comment(row.get("comment")),
                    _required_text(row.get("correlation_id"), "correlation_id"),
                    row.get("occurred_at"),
                ),
            )
        event = self.get_event_by_request(automation_id, request_id)
        if event is None:
            raise OrchestrationPersistenceError("automation project policy event did not persist")
        return event

    def list_configuration_rows(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT task.id, task.automation_id, task.name, task.tool_name,
                       task.tool_params, task.cron_expression, task.enabled,
                       task.automation_generation, task.configuration_version,
                       task.updated_at,
                       policy.mode AS scheduled_policy_mode,
                       policy.version AS scheduled_policy_version,
                       policy.contract_hash AS scheduled_contract_hash
                FROM scheduled_tasks AS task
                LEFT JOIN scheduled_task_approval_policies AS policy
                  ON policy.task_id=task.id
                WHERE task.automation_id=%s
                ORDER BY task.id{suffix}
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            return [
                _decode_row(row, ("tool_params",)) or {}
                for row in _rows(cursor)
            ]

    def expire_pending_approvals(self, automation_id: str) -> int:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE approval_requests AS approval
                INNER JOIN agent_runs AS run ON run.run_id=approval.run_id
                INNER JOIN agent_commands AS command
                    ON command.command_id=run.command_id
                SET approval.status='EXPIRED', approval.decided_at=NOW(6)
                WHERE command.automation_id=%s
                  AND approval.status='PENDING'
                  AND approval.expires_at <= NOW(6)
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)

    def list_pending_approvals(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT approval.approval_id, approval.work_item_id,
                       approval.run_id, approval.approval_round,
                       approval.plan_hash, approval.risk_level,
                       approval.required_role, approval.expires_at,
                       approval.created_at, run.status AS run_status,
                       run.plan_hash AS current_plan_hash,
                       run.plan_json, run.correlation_id, run.causation_id,
                       item.title AS work_item_title,
                       command.source, command.requested_at,
                       command.parameters_json,
                       command.automation_invocation_json
                FROM approval_requests AS approval
                INNER JOIN agent_runs AS run ON run.run_id=approval.run_id
                INNER JOIN work_items AS item
                    ON item.work_item_id=approval.work_item_id
                INNER JOIN agent_commands AS command
                    ON command.command_id=run.command_id
                WHERE command.automation_id=%s
                  AND approval.status='PENDING'
                  AND approval.expires_at > NOW(6)
                  AND run.status='WAITING_APPROVAL'
                ORDER BY approval.approval_id{suffix}
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            return [
                _decode_row(
                    row,
                    (
                        "automation_invocation_json",
                        "parameters_json",
                        "plan_json",
                    ),
                )
                or {}
                for row in _rows(cursor)
            ]

    def get_batch_by_request(
        self,
        automation_id: str,
        request_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM automation_project_approval_batches
                WHERE automation_id=%s AND request_id=%s{suffix}
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _required_text(request_id, "request_id"),
                ),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("result_json",),
            )

    def create_batch(self, row: Mapping[str, Any]) -> dict[str, Any]:
        automation_id = _required_text(row.get("automation_id"), "automation_id")
        request_id = _required_text(row.get("request_id"), "request_id")
        decision = str(row.get("decision") or "").strip().upper()
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("invalid project approval batch decision")
        expected_set_hash = _required_text(
            row.get("expected_pending_set_hash"),
            "expected_pending_set_hash",
        )
        decided_set_hash = _required_text(
            row.get("decided_pending_set_hash"),
            "decided_pending_set_hash",
        )
        decided_count = int(row.get("decided_count") or 0)
        actor_id = _required_text(row.get("actor_id"), "actor_id")
        actor_role = _required_text(row.get("actor_role"), "actor_role")
        comment = _safe_comment(row.get("comment"))
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_project_approval_batches (
                    batch_id, automation_id, request_id, decision,
                    expected_pending_set_hash, decided_pending_set_hash,
                    decided_count, actor_id, actor_role, comment, result_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE batch_id=batch_id
                """,
                (
                    _required_text(row.get("batch_id"), "batch_id"),
                    automation_id,
                    request_id,
                    decision,
                    expected_set_hash,
                    decided_set_hash,
                    decided_count,
                    actor_id,
                    actor_role,
                    comment,
                    _json_param(row.get("result_json"), {}),
                ),
            )
            created = int(getattr(cursor, "rowcount", 0) or 0) == 1
        persisted = self.get_batch_by_request(
            automation_id,
            request_id,
            for_update=True,
        )
        if persisted is None:
            raise OrchestrationPersistenceError("automation approval batch did not persist")
        immutable = (
            str(persisted.get("decision") or "") == decision
            and str(persisted.get("expected_pending_set_hash") or "")
            == expected_set_hash
            and str(persisted.get("decided_pending_set_hash") or "")
            == decided_set_hash
            and int(persisted.get("decided_count") or 0) == decided_count
            and str(persisted.get("actor_id") or "") == actor_id
            and str(persisted.get("actor_role") or "") == actor_role
            and _safe_comment(persisted.get("comment")) == comment
        )
        if not immutable:
            raise IdempotencyConflict(
                "automation approval batch request was reused with different input"
            )
        persisted["_created"] = created
        return persisted


def _policy_mode(value: Any) -> str:
    mode = str(getattr(value, "value", value) or "").strip().upper()
    if mode not in PROJECT_POLICY_MODES:
        raise ValueError(f"invalid automation project policy mode: {mode or '<empty>'}")
    return mode


def _configuration_version(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("project_configuration_version must be a positive integer")
    return value


def _project_generation(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("project_generation must be a positive integer")
    return value
