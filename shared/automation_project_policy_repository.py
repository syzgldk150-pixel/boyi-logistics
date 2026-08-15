"""Transactional persistence for project policies and grouped approvals."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from shared.automation_project_authorization import canonical_sha256
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
    OrchestrationPersistenceError,
    RepositoryBase,
    _decode_row,
    _json_hash,
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
AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ID = (
    "system:automation-project-bootstrap-018"
)
AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ROLE = "system"
AUTOMATION_PROJECT_BOOTSTRAP_REASON = "AUTOMATION_PROJECT_BOOTSTRAP_018"
AUTOMATION_PROJECT_BOOTSTRAP_COMPLETED_BY = (
    AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ID
)
LEGACY_SCHEDULE_GRANT_ACTOR_ID = "system:migration:control-plane-v1"
LEGACY_SCHEDULE_GRANT_ACTOR_ROLE = "migration_authority"
LEGACY_SCHEDULE_GRANT_REASON = "control_plane_v1_bootstrap"
PLUGIN_CONFIGURATION_ACTOR_ID = "system:migration:automation-plugin-v1"
PLUGIN_CONFIGURATION_ACTOR_ROLE = "migration_authority"
PLUGIN_CONFIGURATION_REASON = "PROJECT_CONFIGURATION_CHANGED"
SUPER_ADMIN_PROJECT_POLICY_REASON = "SUPER_ADMIN_PROJECT_POLICY_CHANGED"
AUTOMATION_PROJECT_BOOTSTRAP_POLICY_COMMENT = (
    "Transferred reviewed legacy schedule authorization"
)
AUTOMATION_PROJECT_BOOTSTRAP_EVENT_COMMENT = (
    "Release-held one-time policy bootstrap"
)


class AutomationProjectBootstrapContractError(ValueError):
    """A persisted 018 bootstrap input is incomplete or inconsistent."""

    def __init__(self, code: str) -> None:
        super().__init__(str(code))
        self.code = str(code)


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

    def get_bootstrap_marker_018(
        self,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT marker_id, release_sha, project_set_sha256,
                       completed_by, completed_at
                FROM automation_project_bootstrap_marker_018
                WHERE marker_id=1{suffix}
                """
            )
            return _row_dict(cursor, cursor.fetchone())

    def list_bootstrap_items_018(
        self,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT automation_id, initial_mode, source_set_sha256,
                       source_snapshot_json, policy_version, completed_at
                FROM automation_project_bootstrap_items_018
                ORDER BY automation_id{suffix}
                """
            )
            return [
                _decode_row(row, ("source_snapshot_json",)) or {}
                for row in _rows(cursor)
            ]

    def create_bootstrap_item_018(
        self,
        *,
        automation_id: str,
        initial_mode: str,
        source_set_sha256: str,
        source_snapshot: Mapping[str, Any],
        policy_version: int,
    ) -> dict[str, Any]:
        normalized_mode = _policy_mode(initial_mode)
        if normalized_mode == "PROJECT_FULL_AUTO":
            raise ValueError("bootstrap initial mode cannot be PROJECT_FULL_AUTO")
        normalized_snapshot = validate_automation_project_bootstrap_source_snapshot(
            source_snapshot
        )
        if normalized_mode != automation_project_bootstrap_initial_mode(
            normalized_snapshot
        ):
            raise ValueError("bootstrap initial mode does not match source snapshot")
        normalized_source_hash = _sha256(
            source_set_sha256,
            "source_set_sha256",
        )
        if canonical_sha256(normalized_snapshot) != normalized_source_hash:
            raise ValueError("bootstrap source snapshot hash mismatch")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_project_bootstrap_items_018 (
                    automation_id, initial_mode, source_set_sha256,
                    source_snapshot_json,
                    policy_version
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    normalized_mode,
                    normalized_source_hash,
                    _json_param(normalized_snapshot, {}),
                    _positive_version(policy_version, "policy_version"),
                ),
            )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT automation_id, initial_mode, source_set_sha256,
                       source_snapshot_json, policy_version, completed_at
                FROM automation_project_bootstrap_items_018
                WHERE automation_id=%s FOR UPDATE
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            persisted = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("source_snapshot_json",),
            )
        if persisted is None:
            raise OrchestrationPersistenceError(
                "automation project bootstrap item did not persist"
            )
        return persisted

    def create_bootstrap_marker_018(
        self,
        *,
        release_sha: str,
        project_set_sha256: str,
        completed_by: str,
    ) -> dict[str, Any]:
        safe_release_sha = _release_sha(release_sha)
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_project_bootstrap_marker_018 (
                    marker_id, release_sha, project_set_sha256, completed_by
                ) VALUES (1, %s, %s, %s)
                """,
                (
                    safe_release_sha,
                    _sha256(project_set_sha256, "project_set_sha256"),
                    _required_text(completed_by, "completed_by"),
                ),
            )
        marker = self.get_bootstrap_marker_018(for_update=True)
        if marker is None:
            raise OrchestrationPersistenceError(
                "automation project bootstrap marker did not persist"
            )
        return marker

    def list_policy_events(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM automation_project_policy_events
                WHERE automation_id=%s
                ORDER BY event_id{suffix}
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            return [
                _decode_row(row, self.EVENT_JSON_FIELDS) or {}
                for row in _rows(cursor)
            ]

    def list_configuration_event_evidence(
        self,
        automation_id: str,
        *,
        project_configuration_version: int,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT policy_event.event_id AS policy_event_id,
                       policy_event.request_id,
                       policy_event.from_mode,
                       policy_event.to_mode,
                       policy_event.contract_hash AS policy_contract_hash,
                       policy_event.contract_snapshot_json AS policy_contract_snapshot_json,
                       policy_event.tool_contract_hash AS policy_tool_contract_hash,
                       policy_event.plugin_contract_hash AS policy_plugin_contract_hash,
                       policy_event.project_configuration_version AS policy_configuration_version,
                       policy_event.project_generation AS policy_project_generation,
                       policy_event.actor_id AS policy_actor_id,
                       policy_event.actor_role AS policy_actor_role,
                       policy_event.actor_display_name AS policy_actor_display_name,
                       policy_event.reason AS policy_reason,
                       policy_event.comment AS policy_comment,
                       policy_event.correlation_id AS policy_correlation_id,
                       project_event.event_id AS configuration_event_id,
                       project_event.event_type AS configuration_event_type,
                       project_event.from_state AS configuration_from_state,
                       project_event.to_state AS configuration_to_state,
                       project_event.metadata_json AS configuration_metadata_json,
                       project_event.metadata_sha256 AS configuration_metadata_sha256,
                       project_event.actor_id AS configuration_actor_id,
                       project_event.actor_role AS configuration_actor_role
                FROM automation_project_policy_events AS policy_event
                INNER JOIN automation_project_events AS project_event
                  ON project_event.automation_id=policy_event.automation_id
                 AND project_event.request_id=policy_event.request_id
                WHERE policy_event.automation_id=%s
                  AND policy_event.project_configuration_version=%s
                ORDER BY policy_event.event_id{suffix}
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _configuration_version(project_configuration_version),
                ),
            )
            return [
                _decode_row(
                    row,
                    (
                        "policy_contract_snapshot_json",
                        "configuration_metadata_json",
                    ),
                )
                or {}
                for row in _rows(cursor)
            ]

    def list_scheduled_policy_events(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT policy_event.*
                FROM scheduled_task_approval_policy_events AS policy_event
                INNER JOIN scheduled_tasks AS task
                  ON task.id=policy_event.task_id
                WHERE task.automation_id=%s
                ORDER BY policy_event.task_id, policy_event.event_id{suffix}
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            return [
                _decode_row(row, self.EVENT_JSON_FIELDS) or {}
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
                       policy.contract_hash AS scheduled_contract_hash,
                       policy.contract_snapshot_json AS scheduled_contract_snapshot_json,
                       policy.tool_contract_hash AS scheduled_tool_contract_hash
                FROM scheduled_tasks AS task
                LEFT JOIN scheduled_task_approval_policies AS policy
                  ON policy.task_id=task.id
                WHERE task.automation_id=%s
                ORDER BY task.id{suffix}
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            return [
                _decode_row(
                    row,
                    ("tool_params", "scheduled_contract_snapshot_json"),
                )
                or {}
                for row in _rows(cursor)
            ]

    def list_automation_identity_backup_rows_018(
        self,
        task_ids: Sequence[str],
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        safe_ids = tuple(_required_text(value, "task_id") for value in task_ids)
        if not safe_ids:
            return []
        if len(safe_ids) > 256 or len(safe_ids) != len(set(safe_ids)):
            raise OrchestrationPersistenceError(
                "automation project bootstrap task identities are invalid"
            )
        placeholders = ", ".join("%s" for _value in safe_ids)
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, name, tool_name, tool_params, cron_expression,
                       enabled, configuration_version, updated_at
                FROM scheduled_task_automation_identity_backup_018
                WHERE BINARY id IN ({placeholders})
                ORDER BY BINARY id{suffix}
                """,
                safe_ids,
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


_BOOTSTRAP_SOURCE_TASK_FIELDS = frozenset(
    {
        "task_id",
        "tool_name",
        "automation_generation",
        "configuration_version",
        "enabled",
        "cron_expression_hash",
        "arguments_hash",
        "source_policy_mode",
        "source_policy_version",
        "legacy_authorized",
        "legacy_grant_request_id",
        "legacy_grant_contract_hash",
        "legacy_grant_tool_contract_hash",
        "retirement_kind",
        "retirement_request_id",
    }
)
_BOOTSTRAP_PROJECT_CONTRACT_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "automation_id",
        "automation_generation",
        "manifest_sha256",
        "tool_name",
        "governance_anchor_name",
        "tool_version",
        "operation_type",
        "risk_level",
        "allowed_entrypoints",
        "invocation_contracts",
        "account_bindings_sha256",
        "resource_bindings_sha256",
        "device_binding_sha256",
        "project_config_sha256",
        "tool_contract_hash",
        "plugin_contract_hash",
        "scheduled_configurations",
    }
)
_BOOTSTRAP_PROJECT_SCHEDULE_FIELDS = frozenset(
    {
        "task_id",
        "contract_id",
        "configuration_version",
        "enabled",
        "cron_expression_hash",
        "arguments_hash",
        "dynamic_resolvers_hash",
    }
)


def automation_project_bootstrap_release_sha(value: Any) -> str:
    """Return one full Git SHA-1 or reject before a one-time marker write."""

    normalized = str(value or "").strip().lower()
    if len(normalized) != 40 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise AutomationProjectBootstrapContractError(
            "PROJECT_POLICY_BOOTSTRAP_RELEASE_INVALID"
        )
    return normalized


def automation_project_configuration_bootstrap_request_id(
    release_sha: Any,
    automation_id: Any,
) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "boyi:first-party-plugin-config:"
            f"{automation_project_bootstrap_release_sha(release_sha)}:"
            f"{_bootstrap_required_text(automation_id)}",
        )
    )


def automation_project_policy_bootstrap_request_id(automation_id: Any) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "boyi:automation-project-bootstrap-018:"
            f"{_bootstrap_required_text(automation_id)}",
        )
    )


def legacy_scheduled_policy_grant_request_id(task_id: Any) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"boyi:control-plane-v1:{_bootstrap_required_text(task_id)}",
        )
    )


def build_automation_project_bootstrap_source_snapshot(
    *,
    automation_id: Any,
    automation_generation: Any,
    project_configuration_version: Any,
    contract_hash: Any,
    configuration_request_id: Any,
    configuration_event_metadata_sha256: Any,
    scheduled_tasks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the only canonical privacy-safe source-set payload for 018."""

    safe_automation_id = _bootstrap_required_text(automation_id)
    safe_generation = _bootstrap_positive_int(automation_generation)
    safe_configuration = _bootstrap_positive_int(
        project_configuration_version
    )
    safe_contract_hash = _bootstrap_sha256(contract_hash)
    safe_configuration_request = _bootstrap_uuid(configuration_request_id)
    safe_configuration_metadata_hash = _bootstrap_sha256(
        configuration_event_metadata_sha256
    )
    if isinstance(scheduled_tasks, (str, bytes)) or not isinstance(
        scheduled_tasks,
        Sequence,
    ):
        _raise_bootstrap_source_invalid()
    normalized_tasks: list[dict[str, Any]] = []
    for raw in scheduled_tasks:
        if not isinstance(raw, Mapping) or set(raw) != _BOOTSTRAP_SOURCE_TASK_FIELDS:
            _raise_bootstrap_source_invalid()
        task_id = _bootstrap_required_text(raw.get("task_id"))
        tool_name = _bootstrap_required_text(raw.get("tool_name"))
        generation = _bootstrap_positive_int(raw.get("automation_generation"))
        configuration = _bootstrap_positive_int(raw.get("configuration_version"))
        source_version = _bootstrap_positive_int(raw.get("source_policy_version"))
        if (
            generation != safe_generation
            or configuration != safe_configuration
            or type(raw.get("enabled")) is not bool
            or str(raw.get("source_policy_mode") or "") != "REQUIRE_EACH_RUN"
            or type(raw.get("legacy_authorized")) is not bool
        ):
            _raise_bootstrap_source_invalid()
        cron_hash = _bootstrap_sha256(raw.get("cron_expression_hash"))
        arguments_hash = _bootstrap_sha256(raw.get("arguments_hash"))
        grant_request = str(raw.get("legacy_grant_request_id") or "")
        grant_contract_hash = str(raw.get("legacy_grant_contract_hash") or "")
        grant_tool_hash = str(
            raw.get("legacy_grant_tool_contract_hash") or ""
        )
        if grant_request:
            if (
                grant_request != legacy_scheduled_policy_grant_request_id(task_id)
                or not _bootstrap_is_sha256(grant_contract_hash)
                or not _bootstrap_is_sha256(grant_tool_hash)
            ):
                _raise_bootstrap_source_invalid()
        elif grant_contract_hash or grant_tool_hash:
            _raise_bootstrap_source_invalid()
        retirement_kind = str(raw.get("retirement_kind") or "")
        retirement_request = str(raw.get("retirement_request_id") or "")
        if retirement_kind == "CONFIGURATION_MIGRATION":
            if retirement_request != safe_configuration_request:
                _raise_bootstrap_source_invalid()
        elif retirement_kind == "NONE":
            if retirement_request:
                _raise_bootstrap_source_invalid()
        else:
            _raise_bootstrap_source_invalid()
        legacy_authorized = bool(raw["legacy_authorized"])
        if legacy_authorized and (
            raw["enabled"] is not True
            or not grant_request
            or retirement_kind != "CONFIGURATION_MIGRATION"
        ):
            _raise_bootstrap_source_invalid()
        normalized_tasks.append(
            {
                "task_id": task_id,
                "tool_name": tool_name,
                "automation_generation": generation,
                "configuration_version": configuration,
                "enabled": raw["enabled"],
                "cron_expression_hash": cron_hash,
                "arguments_hash": arguments_hash,
                "source_policy_mode": "REQUIRE_EACH_RUN",
                "source_policy_version": source_version,
                "legacy_authorized": legacy_authorized,
                "legacy_grant_request_id": grant_request,
                "legacy_grant_contract_hash": grant_contract_hash,
                "legacy_grant_tool_contract_hash": grant_tool_hash,
                "retirement_kind": retirement_kind,
                "retirement_request_id": retirement_request,
            }
        )
    normalized_tasks.sort(key=lambda row: row["task_id"])
    task_ids = [row["task_id"] for row in normalized_tasks]
    if len(task_ids) != len(set(task_ids)):
        _raise_bootstrap_source_invalid()
    return {
        "schema_version": 1,
        "automation_id": safe_automation_id,
        "automation_generation": safe_generation,
        "project_configuration_version": safe_configuration,
        "contract_hash": safe_contract_hash,
        "configuration_request_id": safe_configuration_request,
        "configuration_event_metadata_sha256": (
            safe_configuration_metadata_hash
        ),
        "scheduled_tasks": normalized_tasks,
    }


def derive_automation_project_bootstrap_source_snapshot(
    *,
    automation_id: str,
    automation_generation: int,
    project_configuration_version: int,
    contract_hash: str,
    configuration_request_id: str,
    configuration_event_metadata_sha256: str,
    rows: Sequence[Mapping[str, Any]],
    legacy_rows: Sequence[Mapping[str, Any]],
    scheduled_events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], bool, int]:
    """Derive the initial source set from locked current rows and audit events."""

    events_by_task: dict[str, list[Mapping[str, Any]]] = {}
    for event in scheduled_events:
        task_id = str(event.get("task_id") or "").strip()
        if not task_id:
            _raise_bootstrap_source_invalid()
        events_by_task.setdefault(task_id, []).append(event)
    row_task_ids = {str(row.get("id") or "").strip() for row in rows}
    legacy_by_task = {
        str(row.get("id") or "").strip(): row for row in legacy_rows
    }
    if (
        "" in row_task_ids
        or "" in legacy_by_task
        or len(legacy_by_task) != len(legacy_rows)
        or set(legacy_by_task) != row_task_ids
        or set(events_by_task) - row_task_ids
    ):
        _raise_bootstrap_source_invalid()

    snapshots: list[dict[str, Any]] = []
    all_legacy_authorized = bool(rows)
    retired_count = 0
    for row in sorted(rows, key=lambda item: str(item.get("id") or "")):
        task_id = str(row.get("id") or "").strip()
        current_mode = str(row.get("scheduled_policy_mode") or "").strip()
        current_version = row.get("scheduled_policy_version")
        if current_mode == "EXACT_SCHEDULE_EXEMPT":
            _raise_bootstrap_source_invalid()
        if (
            current_mode != "REQUIRE_EACH_RUN"
            or type(current_version) is not int
            or current_version <= 0
            or not str(row.get("cron_expression") or "").strip()
            or not isinstance(row.get("tool_params"), Mapping)
            or row.get("scheduled_contract_hash") is not None
            or row.get("scheduled_contract_snapshot_json") is not None
            or row.get("scheduled_tool_contract_hash") is not None
        ):
            _raise_bootstrap_source_invalid()
        task_events = events_by_task.get(task_id, [])
        event_ids = [event.get("event_id") for event in task_events]
        if (
            any(type(event_id) is not int or event_id <= 0 for event_id in event_ids)
            or len(event_ids) != len(set(event_ids))
        ):
            _raise_bootstrap_source_invalid()
        task_events = sorted(task_events, key=lambda event: int(event["event_id"]))
        grant_request_id = legacy_scheduled_policy_grant_request_id(task_id)
        grants = [
            event
            for event in task_events
            if str(event.get("request_id") or "") == grant_request_id
        ]
        if len(grants) > 1:
            _raise_bootstrap_source_invalid()
        grant = grants[0] if grants else None
        if grant is not None:
            legacy_row = legacy_by_task[task_id]
            validate_legacy_scheduled_grant_event(grant, row=legacy_row)
            if (
                str(legacy_row.get("cron_expression") or "")
                != str(row.get("cron_expression") or "")
                or _bootstrap_boolean(legacy_row.get("enabled"))
                is not _bootstrap_boolean(row.get("enabled"))
            ):
                _raise_bootstrap_source_invalid()
        migration_events = [
            event
            for event in task_events
            if str(event.get("request_id") or "")
            == configuration_request_id
        ]
        if len(migration_events) > 1:
            _raise_bootstrap_source_invalid()
        migration_event = migration_events[0] if migration_events else None
        if migration_event is not None:
            validate_plugin_configuration_retirement_event(
                migration_event,
                configuration_request_id=configuration_request_id,
            )

        retirement_kind = "NONE"
        retirement_request_id = ""
        legacy_authorized = False
        if migration_event is not None:
            retired_count += 1
            retirement_kind = "CONFIGURATION_MIGRATION"
            retirement_request_id = configuration_request_id
            legacy_authorized = bool(
                grant is not None
                and _bootstrap_boolean(row.get("enabled"))
                and task_events
                and int(task_events[-1]["event_id"])
                == int(migration_event["event_id"])
                and int(grant["event_id"]) < int(migration_event["event_id"])
            )
        all_legacy_authorized = all_legacy_authorized and legacy_authorized
        snapshots.append(
            {
                "task_id": task_id,
                "tool_name": str(row.get("tool_name") or ""),
                "automation_generation": row.get("automation_generation"),
                "configuration_version": row.get("configuration_version"),
                "enabled": _bootstrap_boolean(row.get("enabled")),
                "cron_expression_hash": canonical_sha256(
                    str(row.get("cron_expression") or "")
                ),
                "arguments_hash": canonical_sha256(row.get("tool_params")),
                "source_policy_mode": current_mode,
                "source_policy_version": current_version,
                "legacy_authorized": legacy_authorized,
                "legacy_grant_request_id": grant_request_id if grant else "",
                "legacy_grant_contract_hash": (
                    str(grant.get("contract_hash") or "") if grant else ""
                ),
                "legacy_grant_tool_contract_hash": (
                    str(grant.get("tool_contract_hash") or "") if grant else ""
                ),
                "retirement_kind": retirement_kind,
                "retirement_request_id": retirement_request_id,
            }
        )
    snapshot = build_automation_project_bootstrap_source_snapshot(
        automation_id=automation_id,
        automation_generation=automation_generation,
        project_configuration_version=project_configuration_version,
        contract_hash=contract_hash,
        configuration_request_id=configuration_request_id,
        configuration_event_metadata_sha256=(
            configuration_event_metadata_sha256
        ),
        scheduled_tasks=snapshots,
    )
    return snapshot, all_legacy_authorized, retired_count


def automation_project_bootstrap_source_set_sha256(
    *,
    automation_id: Any,
    automation_generation: Any,
    project_configuration_version: Any,
    contract_hash: Any,
    configuration_request_id: Any,
    configuration_event_metadata_sha256: Any,
    scheduled_tasks: Sequence[Mapping[str, Any]],
) -> str:
    return canonical_sha256(
        build_automation_project_bootstrap_source_snapshot(
            automation_id=automation_id,
            automation_generation=automation_generation,
            project_configuration_version=project_configuration_version,
            contract_hash=contract_hash,
            configuration_request_id=configuration_request_id,
            configuration_event_metadata_sha256=(
                configuration_event_metadata_sha256
            ),
            scheduled_tasks=scheduled_tasks,
        )
    )


def validate_automation_project_bootstrap_source_snapshot(
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "automation_id",
        "automation_generation",
        "project_configuration_version",
        "contract_hash",
        "configuration_request_id",
        "configuration_event_metadata_sha256",
        "scheduled_tasks",
    } or type(value.get("schema_version")) is not int or value.get(
        "schema_version"
    ) != 1:
        _raise_bootstrap_source_invalid()
    normalized = build_automation_project_bootstrap_source_snapshot(
        automation_id=value.get("automation_id"),
        automation_generation=value.get("automation_generation"),
        project_configuration_version=value.get(
            "project_configuration_version"
        ),
        contract_hash=value.get("contract_hash"),
        configuration_request_id=value.get("configuration_request_id"),
        configuration_event_metadata_sha256=value.get(
            "configuration_event_metadata_sha256"
        ),
        scheduled_tasks=value.get("scheduled_tasks"),
    )
    if dict(value) != normalized:
        _raise_bootstrap_source_invalid()
    return normalized


def automation_project_bootstrap_source_snapshot_sha256(value: Any) -> str:
    return canonical_sha256(
        validate_automation_project_bootstrap_source_snapshot(value)
    )


def automation_project_bootstrap_initial_mode(value: Any) -> str:
    snapshot = validate_automation_project_bootstrap_source_snapshot(value)
    scheduled_tasks = snapshot["scheduled_tasks"]
    return (
        "LEGACY_SCHEDULE_ONLY"
        if scheduled_tasks
        and all(task["legacy_authorized"] is True for task in scheduled_tasks)
        else "REQUIRE_EACH_RUN"
    )


def automation_project_bootstrap_project_set_sha256(
    release_sha: Any,
    items: Sequence[Mapping[str, Any]],
) -> str:
    safe_release_sha = automation_project_bootstrap_release_sha(release_sha)
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        _raise_bootstrap_marker_invalid()
    projects: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            _raise_bootstrap_marker_invalid()
        automation_id = _bootstrap_required_text(raw.get("automation_id"))
        mode = str(raw.get("initial_mode") or "")
        if mode not in {"REQUIRE_EACH_RUN", "LEGACY_SCHEDULE_ONLY"}:
            _raise_bootstrap_marker_invalid()
        try:
            source_snapshot = (
                validate_automation_project_bootstrap_source_snapshot(
                    raw.get("source_snapshot_json")
                )
            )
        except AutomationProjectBootstrapContractError:
            _raise_bootstrap_marker_invalid()
        source_set_sha256 = _bootstrap_sha256(raw.get("source_set_sha256"))
        derived_mode = automation_project_bootstrap_initial_mode(source_snapshot)
        if (
            source_snapshot["automation_id"] != automation_id
            or mode != derived_mode
            or canonical_sha256(source_snapshot) != source_set_sha256
        ):
            _raise_bootstrap_marker_invalid()
        projects.append(
            {
                "automation_id": automation_id,
                "initial_mode": mode,
                "source_set_sha256": source_set_sha256,
                "policy_version": _bootstrap_positive_int(
                    raw.get("policy_version")
                ),
            }
        )
    projects.sort(key=lambda row: row["automation_id"])
    project_ids = [row["automation_id"] for row in projects]
    if len(project_ids) != len(set(project_ids)):
        _raise_bootstrap_marker_invalid()
    return canonical_sha256(
        {
            "schema_version": 1,
            "release_sha": safe_release_sha,
            "projects": projects,
        }
    )


def validate_existing_automation_project_bootstrap(
    marker: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    *,
    expected_automation_ids: Sequence[str],
) -> dict[str, Any]:
    try:
        expected_ids = tuple(
            sorted(_bootstrap_required_text(value) for value in expected_automation_ids)
        )
        release_sha = automation_project_bootstrap_release_sha(
            marker.get("release_sha")
        )
        item_ids = tuple(
            sorted(_bootstrap_required_text(item.get("automation_id")) for item in items)
        )
        project_set_sha256 = automation_project_bootstrap_project_set_sha256(
            release_sha,
            items,
        )
    except (AutomationProjectBootstrapContractError, TypeError, AttributeError):
        _raise_bootstrap_marker_invalid()
    if (
        type(marker.get("marker_id")) is not int
        or marker.get("marker_id") != 1
        or str(marker.get("completed_by") or "")
        != AUTOMATION_PROJECT_BOOTSTRAP_COMPLETED_BY
        or not expected_ids
        or len(expected_ids) != len(set(expected_ids))
        or item_ids != expected_ids
        or project_set_sha256 != str(marker.get("project_set_sha256") or "")
    ):
        _raise_bootstrap_marker_invalid()
    legacy_count = sum(
        item.get("initial_mode") == "LEGACY_SCHEDULE_ONLY" for item in items
    )
    return {
        "status": "already_present",
        "project_count": len(items),
        "legacy_schedule_only": legacy_count,
        "require_each_run": len(items) - legacy_count,
        "retired_scheduled_exact": 0,
        "project_set_sha256": project_set_sha256,
        "release_sha": release_sha,
    }


def validate_unconfigured_automation_project_policy(
    policy: Mapping[str, Any],
    *,
    automation_generation: int,
    project_configuration_version: int,
) -> None:
    if (
        not isinstance(policy, Mapping)
        or str(policy.get("mode") or "") != "REQUIRE_EACH_RUN"
        or type(policy.get("version")) is not int
        or int(policy.get("version") or 0) <= 0
        or policy.get("project_generation") != automation_generation
        or policy.get("project_configuration_version")
        != project_configuration_version
        or policy.get("contract_hash") is not None
        or policy.get("contract_snapshot_json") is not None
        or policy.get("tool_contract_hash") is not None
        or policy.get("plugin_contract_hash") is not None
        or policy.get("approved_by_actor_id") is not None
        or policy.get("approved_by_actor_role") is not None
        or policy.get("approved_by_actor_display_name") is not None
        or policy.get("approved_at") is not None
        or policy.get("comment") is not None
    ):
        _raise_bootstrap_source_invalid()


def validate_automation_project_bootstrap_policy_event(
    event: Mapping[str, Any],
    *,
    item: Mapping[str, Any],
) -> None:
    try:
        source_snapshot = validate_automation_project_bootstrap_source_snapshot(
            item.get("source_snapshot_json")
        )
        source_hash = _bootstrap_sha256(item.get("source_set_sha256"))
        policy_version = _bootstrap_positive_int(item.get("policy_version"))
    except AutomationProjectBootstrapContractError:
        _raise_bootstrap_source_invalid()
    del policy_version
    automation_id = source_snapshot["automation_id"]
    mode = str(item.get("initial_mode") or "")
    if (
        canonical_sha256(source_snapshot) != source_hash
        or str(item.get("automation_id") or "") != automation_id
        or mode != automation_project_bootstrap_initial_mode(source_snapshot)
        or type(event.get("event_id")) is not int
        or int(event.get("event_id") or 0) <= 0
        or str(event.get("automation_id") or "") != automation_id
        or str(event.get("request_id") or "")
        != automation_project_policy_bootstrap_request_id(automation_id)
        or str(event.get("from_mode") or "") != "REQUIRE_EACH_RUN"
        or str(event.get("to_mode") or "") != mode
        or event.get("project_generation")
        != source_snapshot["automation_generation"]
        or event.get("project_configuration_version")
        != source_snapshot["project_configuration_version"]
        or str(event.get("actor_id") or "")
        != AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ID
        or str(event.get("actor_role") or "")
        != AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ROLE
        or str(event.get("actor_display_name") or "")
        != "Automation project bootstrap 018"
        or str(event.get("reason") or "")
        != AUTOMATION_PROJECT_BOOTSTRAP_REASON
        or str(event.get("comment") or "")
        != AUTOMATION_PROJECT_BOOTSTRAP_EVENT_COMMENT
        or str(event.get("correlation_id") or "")
        != automation_project_policy_bootstrap_request_id(automation_id)
    ):
        _raise_bootstrap_source_invalid()
    if mode == "LEGACY_SCHEDULE_ONLY":
        contract_snapshot = event.get("contract_snapshot_json")
        if (
            str(event.get("contract_hash") or "")
            != source_snapshot["contract_hash"]
            or not isinstance(contract_snapshot, Mapping)
            or canonical_sha256(contract_snapshot)
            != source_snapshot["contract_hash"]
        ):
            _raise_bootstrap_source_invalid()
        _validate_bootstrap_project_contract_snapshot(
            contract_snapshot,
            source_snapshot=source_snapshot,
            tool_contract_hash=event.get("tool_contract_hash"),
            plugin_contract_hash=event.get("plugin_contract_hash"),
        )
    elif any(
        event.get(field) is not None
        for field in (
            "contract_hash",
            "contract_snapshot_json",
            "tool_contract_hash",
            "plugin_contract_hash",
        )
    ):
        _raise_bootstrap_source_invalid()


def validate_initial_automation_project_bootstrap_policy(
    policy: Mapping[str, Any],
    *,
    item: Mapping[str, Any],
    bootstrap_event: Mapping[str, Any],
) -> None:
    validate_automation_project_bootstrap_policy_event(
        bootstrap_event,
        item=item,
    )
    source_snapshot = validate_automation_project_bootstrap_source_snapshot(
        item.get("source_snapshot_json")
    )
    mode = str(item.get("initial_mode") or "")
    if (
        not isinstance(policy, Mapping)
        or str(policy.get("automation_id") or "")
        != source_snapshot["automation_id"]
        or str(policy.get("mode") or "") != mode
        or policy.get("version") != item.get("policy_version")
        or policy.get("project_generation")
        != source_snapshot["automation_generation"]
        or policy.get("project_configuration_version")
        != source_snapshot["project_configuration_version"]
    ):
        _raise_bootstrap_source_invalid()
    if mode == "LEGACY_SCHEDULE_ONLY":
        if (
            str(policy.get("contract_hash") or "")
            != source_snapshot["contract_hash"]
            or policy.get("contract_snapshot_json")
            != bootstrap_event.get("contract_snapshot_json")
            or policy.get("tool_contract_hash")
            != bootstrap_event.get("tool_contract_hash")
            or policy.get("plugin_contract_hash")
            != bootstrap_event.get("plugin_contract_hash")
            or str(policy.get("approved_by_actor_id") or "")
            != AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ID
            or str(policy.get("approved_by_actor_role") or "")
            != AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ROLE
            or str(policy.get("approved_by_actor_display_name") or "")
            != "Automation project bootstrap 018"
            or policy.get("approved_at") is None
            or str(policy.get("comment") or "")
            != AUTOMATION_PROJECT_BOOTSTRAP_POLICY_COMMENT
        ):
            _raise_bootstrap_source_invalid()
    elif any(
        policy.get(field) is not None
        for field in (
            "contract_hash",
            "contract_snapshot_json",
            "tool_contract_hash",
            "plugin_contract_hash",
            "approved_by_actor_id",
            "approved_by_actor_role",
            "approved_by_actor_display_name",
            "approved_at",
            "comment",
        )
    ):
        _raise_bootstrap_source_invalid()


def _validate_bootstrap_project_contract_snapshot(
    snapshot: Mapping[str, Any],
    *,
    source_snapshot: Mapping[str, Any],
    tool_contract_hash: Any,
    plugin_contract_hash: Any,
) -> None:
    if (
        set(snapshot) != _BOOTSTRAP_PROJECT_CONTRACT_SNAPSHOT_FIELDS
        or type(snapshot.get("schema_version")) is not int
        or snapshot.get("schema_version") != 1
        or str(snapshot.get("automation_id") or "")
        != source_snapshot["automation_id"]
        or snapshot.get("automation_generation")
        != source_snapshot["automation_generation"]
        or not _bootstrap_required_text(snapshot.get("tool_name"))
        or not _bootstrap_required_text(snapshot.get("governance_anchor_name"))
        or not _bootstrap_required_text(snapshot.get("tool_version"))
        or not _bootstrap_required_text(snapshot.get("operation_type"))
        or not _bootstrap_required_text(snapshot.get("risk_level"))
        or any(
            not _bootstrap_is_sha256(snapshot.get(field))
            for field in (
                "manifest_sha256",
                "account_bindings_sha256",
                "resource_bindings_sha256",
                "device_binding_sha256",
                "project_config_sha256",
                "tool_contract_hash",
                "plugin_contract_hash",
            )
        )
        or str(snapshot.get("tool_contract_hash") or "")
        != _bootstrap_sha256(tool_contract_hash)
        or str(snapshot.get("plugin_contract_hash") or "")
        != _bootstrap_sha256(plugin_contract_hash)
    ):
        _raise_bootstrap_source_invalid()

    allowed_entrypoints = snapshot.get("allowed_entrypoints")
    if (
        not isinstance(allowed_entrypoints, list)
        or any(
            str(value or "") not in {"console", "scheduler", "feishu", "webhook"}
            for value in allowed_entrypoints
        )
        or allowed_entrypoints != sorted(set(allowed_entrypoints))
    ):
        _raise_bootstrap_source_invalid()

    raw_invocations = snapshot.get("invocation_contracts")
    if not isinstance(raw_invocations, list):
        _raise_bootstrap_source_invalid()
    invocation_by_id: dict[str, Mapping[str, Any]] = {}
    for invocation in raw_invocations:
        if (
            not isinstance(invocation, Mapping)
            or set(invocation)
            != {
                "contract_id",
                "entrypoint",
                "arguments_hash",
                "dynamic_resolvers_hash",
            }
            or not _bootstrap_required_text(invocation.get("contract_id"))
            or str(invocation.get("entrypoint") or "") not in allowed_entrypoints
            or not _bootstrap_is_sha256(invocation.get("arguments_hash"))
            or not _bootstrap_is_sha256(invocation.get("dynamic_resolvers_hash"))
        ):
            _raise_bootstrap_source_invalid()
        contract_id = str(invocation["contract_id"])
        if contract_id in invocation_by_id:
            _raise_bootstrap_source_invalid()
        invocation_by_id[contract_id] = invocation
    if list(invocation_by_id) != sorted(invocation_by_id):
        _raise_bootstrap_source_invalid()

    raw_schedules = snapshot.get("scheduled_configurations")
    if not isinstance(raw_schedules, list):
        _raise_bootstrap_source_invalid()
    schedules_by_id: dict[str, Mapping[str, Any]] = {}
    for schedule in raw_schedules:
        if (
            not isinstance(schedule, Mapping)
            or set(schedule) != _BOOTSTRAP_PROJECT_SCHEDULE_FIELDS
            or type(schedule.get("configuration_version")) is not int
            or int(schedule.get("configuration_version") or 0) <= 0
            or type(schedule.get("enabled")) is not bool
            or any(
                not _bootstrap_is_sha256(schedule.get(field))
                for field in (
                    "cron_expression_hash",
                    "arguments_hash",
                    "dynamic_resolvers_hash",
                )
            )
        ):
            _raise_bootstrap_source_invalid()
        task_id = _bootstrap_required_text(schedule.get("task_id"))
        contract_id = f"scheduler:{task_id}"
        invocation = invocation_by_id.get(contract_id)
        if (
            task_id in schedules_by_id
            or str(schedule.get("contract_id") or "") != contract_id
            or not isinstance(invocation, Mapping)
            or str(invocation.get("entrypoint") or "") != "scheduler"
            or invocation.get("arguments_hash") != schedule.get("arguments_hash")
            or invocation.get("dynamic_resolvers_hash")
            != schedule.get("dynamic_resolvers_hash")
        ):
            _raise_bootstrap_source_invalid()
        schedules_by_id[task_id] = schedule
    if list(schedules_by_id) != sorted(schedules_by_id):
        _raise_bootstrap_source_invalid()

    source_tasks = {
        str(task["task_id"]): task for task in source_snapshot["scheduled_tasks"]
    }
    if set(schedules_by_id) != set(source_tasks):
        _raise_bootstrap_source_invalid()
    for task_id, source_task in source_tasks.items():
        schedule = schedules_by_id[task_id]
        if (
            schedule.get("configuration_version")
            != source_task["configuration_version"]
            or str(source_task.get("tool_name") or "")
            != f"automation.{source_snapshot['automation_id']}.run"
            or schedule.get("enabled") is not source_task["enabled"]
            or str(schedule.get("cron_expression_hash") or "")
            != source_task["cron_expression_hash"]
            or str(schedule.get("arguments_hash") or "")
            != source_task["arguments_hash"]
        ):
            _raise_bootstrap_source_invalid()
    expected_invocation_ids = {
        *(f"scheduler:{task_id}" for task_id in source_tasks),
        *(
            entrypoint
            for entrypoint in allowed_entrypoints
            if entrypoint != "scheduler"
        ),
    }
    if set(invocation_by_id) != expected_invocation_ids:
        _raise_bootstrap_source_invalid()
    for contract_id, invocation in invocation_by_id.items():
        if not contract_id.startswith("scheduler:") and (
            contract_id != invocation.get("entrypoint")
        ):
            _raise_bootstrap_source_invalid()


def validate_automation_project_configuration_evidence(
    *,
    automation_id: str,
    release_sha: str,
    config: Mapping[str, Any],
    automation_generation: int,
    project_configuration_version: int,
    scheduled_task_count: int,
    policy_events: Sequence[Mapping[str, Any]],
    evidence_rows: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    expected_request_id = automation_project_configuration_bootstrap_request_id(
        release_sha,
        automation_id,
    )
    matching = [
        row
        for row in evidence_rows
        if str(row.get("request_id") or "") == expected_request_id
    ]
    if len(matching) != 1:
        _raise_bootstrap_source_invalid()
    evidence = matching[0]
    expected_version = project_configuration_version - 1
    persisted_payloads = (
        ("config_json", "config_sha256", Mapping),
        ("account_bindings_json", "account_bindings_sha256", Mapping),
        ("resource_bindings_json", "resource_bindings_sha256", Mapping),
        ("enabled_entrypoints_json", "enabled_entrypoints_sha256", list),
        ("desired_schedule_json", "desired_schedule_sha256", Mapping),
        ("compiled_invocations_json", "compiled_invocations_sha256", Mapping),
    )
    if (
        expected_version <= 0
        or config.get("configured") not in {True, 1}
        or config.get("config_version") != project_configuration_version
        or any(
            not isinstance(config.get(json_field), expected_type)
            or _json_hash(config.get(json_field))
            != str(config.get(hash_field) or "")
            for json_field, hash_field, expected_type in persisted_payloads
        )
    ):
        _raise_bootstrap_source_invalid()
    request_payload_sha256 = _json_hash(
        {
            "config": dict(config["config_json"]),
            "account_bindings": dict(config["account_bindings_json"]),
            "resource_bindings": dict(config["resource_bindings_json"]),
            "enabled_entrypoints": list(config["enabled_entrypoints_json"]),
            "schedule": dict(config["desired_schedule_json"]),
            "compiled_invocations": dict(config["compiled_invocations_json"]),
            "device_id": config.get("device_id"),
            "expected_project_configuration_version": expected_version,
        }
    )
    metadata_sha256 = _validate_configuration_evidence_row(
        evidence,
        configuration_request_id=expected_request_id,
        automation_generation=automation_generation,
        project_configuration_version=project_configuration_version,
        scheduled_task_count=scheduled_task_count,
        schedule_sha256=config.get("desired_schedule_sha256"),
        expected_request_payload_sha256=request_payload_sha256,
        expected_metadata_sha256=None,
    )
    _validate_configuration_policy_event_history(
        policy_events,
        evidence=evidence,
        configuration_request_id=expected_request_id,
        require_latest=True,
    )
    return {
        "request_id": expected_request_id,
        "metadata_sha256": metadata_sha256,
    }


def validate_persisted_automation_project_configuration_evidence(
    *,
    source_snapshot: Mapping[str, Any],
    release_sha: str,
    policy_events: Sequence[Mapping[str, Any]],
    evidence_rows: Sequence[Mapping[str, Any]],
    generation_schedule_sha256: str,
) -> dict[str, str]:
    """Validate the immutable initial config event without current config rows."""

    snapshot = validate_automation_project_bootstrap_source_snapshot(
        source_snapshot
    )
    expected_request_id = automation_project_configuration_bootstrap_request_id(
        release_sha,
        snapshot["automation_id"],
    )
    if snapshot["configuration_request_id"] != expected_request_id:
        _raise_bootstrap_source_invalid()
    matching = [
        row
        for row in evidence_rows
        if str(row.get("request_id") or "") == expected_request_id
    ]
    if len(matching) != 1:
        _raise_bootstrap_source_invalid()
    evidence = matching[0]
    metadata_sha256 = _validate_configuration_evidence_row(
        evidence,
        configuration_request_id=expected_request_id,
        automation_generation=snapshot["automation_generation"],
        project_configuration_version=snapshot[
            "project_configuration_version"
        ],
        scheduled_task_count=len(snapshot["scheduled_tasks"]),
        schedule_sha256=generation_schedule_sha256,
        expected_request_payload_sha256=None,
        expected_metadata_sha256=snapshot[
            "configuration_event_metadata_sha256"
        ],
    )
    _validate_configuration_policy_event_history(
        policy_events,
        evidence=evidence,
        configuration_request_id=expected_request_id,
        require_latest=False,
    )
    return {
        "request_id": expected_request_id,
        "metadata_sha256": metadata_sha256,
    }


def _validate_configuration_evidence_row(
    evidence: Mapping[str, Any],
    *,
    configuration_request_id: str,
    automation_generation: int,
    project_configuration_version: int,
    scheduled_task_count: int,
    schedule_sha256: Any,
    expected_request_payload_sha256: str | None,
    expected_metadata_sha256: str | None,
) -> str:
    metadata = evidence.get("configuration_metadata_json")
    safe_schedule_sha256 = _bootstrap_sha256(schedule_sha256)
    if not isinstance(metadata, Mapping) or set(metadata) != {
        "request_payload_sha256",
        "from_project_configuration_version",
        "to_project_configuration_version",
        "schedule_sha256",
        "scheduled_task_count",
    }:
        _raise_bootstrap_source_invalid()
    request_payload_sha256 = _bootstrap_sha256(
        metadata.get("request_payload_sha256")
    )
    metadata_sha256 = _bootstrap_sha256(
        evidence.get("configuration_metadata_sha256")
    )
    expected_version = project_configuration_version - 1
    if (
        expected_version <= 0
        or (
            expected_request_payload_sha256 is not None
            and request_payload_sha256 != expected_request_payload_sha256
        )
        or str(metadata.get("schedule_sha256") or "") != safe_schedule_sha256
        or type(metadata.get("to_project_configuration_version")) is not int
        or metadata.get("to_project_configuration_version")
        != project_configuration_version
        or type(metadata.get("from_project_configuration_version")) is not int
        or metadata.get("from_project_configuration_version") != expected_version
        or type(metadata.get("scheduled_task_count")) is not int
        or metadata.get("scheduled_task_count") != scheduled_task_count
        or _json_hash(metadata) != metadata_sha256
        or (
            expected_metadata_sha256 is not None
            and metadata_sha256 != _bootstrap_sha256(expected_metadata_sha256)
        )
        or type(evidence.get("policy_event_id")) is not int
        or int(evidence.get("policy_event_id") or 0) <= 0
        or type(evidence.get("configuration_event_id")) is not int
        or int(evidence.get("configuration_event_id") or 0) <= 0
        or str(evidence.get("request_id") or "")
        != configuration_request_id
        or str(evidence.get("from_mode") or "") != "REQUIRE_EACH_RUN"
        or str(evidence.get("to_mode") or "") != "REQUIRE_EACH_RUN"
        or evidence.get("policy_contract_hash") is not None
        or evidence.get("policy_contract_snapshot_json") is not None
        or evidence.get("policy_tool_contract_hash") is not None
        or evidence.get("policy_plugin_contract_hash") is not None
        or evidence.get("policy_configuration_version")
        != project_configuration_version
        or evidence.get("policy_project_generation") != automation_generation
        or str(evidence.get("policy_actor_id") or "")
        != PLUGIN_CONFIGURATION_ACTOR_ID
        or str(evidence.get("policy_actor_role") or "")
        != PLUGIN_CONFIGURATION_ACTOR_ROLE
        or evidence.get("policy_actor_display_name") is not None
        or str(evidence.get("policy_reason") or "")
        != PLUGIN_CONFIGURATION_REASON
        or evidence.get("policy_comment") is not None
        or str(evidence.get("policy_correlation_id") or "")
        != configuration_request_id
        or str(evidence.get("configuration_event_type") or "")
        != "CONFIGURATION_UPDATED"
        or not str(evidence.get("configuration_from_state") or "")
        or evidence.get("configuration_from_state")
        != evidence.get("configuration_to_state")
        or str(evidence.get("configuration_actor_id") or "")
        != PLUGIN_CONFIGURATION_ACTOR_ID
        or str(evidence.get("configuration_actor_role") or "")
        != PLUGIN_CONFIGURATION_ACTOR_ROLE
    ):
        _raise_bootstrap_source_invalid()
    return metadata_sha256


def _validate_configuration_policy_event_history(
    policy_events: Sequence[Mapping[str, Any]],
    *,
    evidence: Mapping[str, Any],
    configuration_request_id: str,
    require_latest: bool,
) -> None:
    if isinstance(policy_events, (str, bytes)) or not isinstance(
        policy_events,
        Sequence,
    ):
        _raise_bootstrap_source_invalid()
    event_ids = [event.get("event_id") for event in policy_events]
    if (
        any(type(event_id) is not int or event_id <= 0 for event_id in event_ids)
        or len(event_ids) != len(set(event_ids))
    ):
        _raise_bootstrap_source_invalid()
    ordered_events = sorted(policy_events, key=lambda event: int(event["event_id"]))
    matching = [
        event
        for event in ordered_events
        if str(event.get("request_id") or "") == configuration_request_id
    ]
    if len(matching) != 1:
        _raise_bootstrap_source_invalid()
    event = matching[0]
    comparable_fields = {
        "event_id": "policy_event_id",
        "from_mode": "from_mode",
        "to_mode": "to_mode",
        "contract_hash": "policy_contract_hash",
        "contract_snapshot_json": "policy_contract_snapshot_json",
        "tool_contract_hash": "policy_tool_contract_hash",
        "plugin_contract_hash": "policy_plugin_contract_hash",
        "project_configuration_version": "policy_configuration_version",
        "project_generation": "policy_project_generation",
        "actor_id": "policy_actor_id",
        "actor_role": "policy_actor_role",
        "actor_display_name": "policy_actor_display_name",
        "reason": "policy_reason",
        "comment": "policy_comment",
        "correlation_id": "policy_correlation_id",
    }
    if (
        any(event.get(field) != evidence.get(alias) for field, alias in comparable_fields.items())
        or (
            require_latest
            and (
                not ordered_events
                or int(ordered_events[-1]["event_id"])
                != int(event["event_id"])
            )
        )
    ):
        _raise_bootstrap_source_invalid()


def validate_legacy_scheduled_grant_event(
    event: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
) -> None:
    snapshot = event.get("contract_snapshot_json")
    task_id = str(row.get("id") or "")
    if (
        str(event.get("request_id") or "")
        != legacy_scheduled_policy_grant_request_id(task_id)
        or str(event.get("from_mode") or "") != "REQUIRE_EACH_RUN"
        or str(event.get("to_mode") or "") != "EXACT_SCHEDULE_EXEMPT"
        or str(event.get("actor_id") or "") != LEGACY_SCHEDULE_GRANT_ACTOR_ID
        or str(event.get("actor_role") or "")
        != LEGACY_SCHEDULE_GRANT_ACTOR_ROLE
        or str(event.get("actor_display_name") or "")
        != "Control Plane v1 migration"
        or str(event.get("reason") or "") != LEGACY_SCHEDULE_GRANT_REASON
        or str(event.get("comment") or "")
        != "preserve previously authorized production automation"
        or not _bootstrap_is_uuid(event.get("correlation_id"))
        or not isinstance(snapshot, Mapping)
        or set(snapshot)
        != {
            "schema_version",
            "task_id",
            "tool_name",
            "tool_version",
            "operation_type",
            "risk_level",
            "approval_mode",
            "cron_expression",
            "enabled",
            "configuration_version",
            "arguments_hash",
            "dynamic_rules_hash",
            "postconditions_hash",
            "tool_contract_hash",
        }
        or type(snapshot.get("schema_version")) is not int
        or snapshot.get("schema_version") != 1
        or str(snapshot.get("task_id") or "") != task_id
        or str(snapshot.get("tool_name") or "")
        != str(row.get("tool_name") or "")
        or str(snapshot.get("cron_expression") or "")
        != str(row.get("cron_expression") or "")
        or snapshot.get("enabled") is not True
        or _bootstrap_boolean(row.get("enabled")) is not True
        or type(snapshot.get("configuration_version")) is not int
        or snapshot.get("configuration_version")
        != row.get("configuration_version")
        or snapshot.get("approval_mode") != "schedule_allowlist"
        or not _bootstrap_is_sha256(snapshot.get("arguments_hash"))
        or not _bootstrap_is_sha256(snapshot.get("dynamic_rules_hash"))
        or not _bootstrap_is_sha256(snapshot.get("postconditions_hash"))
        or not _bootstrap_is_sha256(snapshot.get("tool_contract_hash"))
        or str(snapshot.get("arguments_hash") or "")
        != canonical_sha256(row.get("tool_params"))
        or canonical_sha256(snapshot) != str(event.get("contract_hash") or "")
        or str(snapshot.get("tool_contract_hash") or "")
        != str(event.get("tool_contract_hash") or "")
    ):
        _raise_bootstrap_source_invalid()


def validate_plugin_configuration_retirement_event(
    event: Mapping[str, Any],
    *,
    configuration_request_id: str,
) -> None:
    if (
        str(event.get("request_id") or "") != configuration_request_id
        or str(event.get("from_mode") or "") != "EXACT_SCHEDULE_EXEMPT"
        or str(event.get("to_mode") or "") != "REQUIRE_EACH_RUN"
        or str(event.get("actor_id") or "") != PLUGIN_CONFIGURATION_ACTOR_ID
        or str(event.get("actor_role") or "")
        != PLUGIN_CONFIGURATION_ACTOR_ROLE
        or event.get("actor_display_name") is not None
        or str(event.get("reason") or "") != PLUGIN_CONFIGURATION_REASON
        or event.get("comment") is not None
        or str(event.get("correlation_id") or "")
        != configuration_request_id
        or event.get("contract_hash") is not None
        or event.get("contract_snapshot_json") is not None
        or event.get("tool_contract_hash") is not None
    ):
        _raise_bootstrap_source_invalid()


def _bootstrap_required_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        _raise_bootstrap_source_invalid()
    return text


def _bootstrap_positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        _raise_bootstrap_source_invalid()
    return value


def _bootstrap_boolean(value: Any) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in {0, 1}:
        return bool(value)
    _raise_bootstrap_source_invalid()


def _bootstrap_sha256(value: Any) -> str:
    digest = str(value or "").strip().lower()
    if not _bootstrap_is_sha256(digest):
        _raise_bootstrap_source_invalid()
    return digest


def _bootstrap_is_sha256(value: Any) -> bool:
    digest = str(value or "").strip().lower()
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def _bootstrap_uuid(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    if not _bootstrap_is_uuid(candidate):
        _raise_bootstrap_source_invalid()
    return candidate


def _bootstrap_is_uuid(value: Any) -> bool:
    candidate = str(value or "").strip().lower()
    try:
        return str(uuid.UUID(candidate)) == candidate
    except (ValueError, AttributeError):
        return False


def _raise_bootstrap_source_invalid() -> None:
    raise AutomationProjectBootstrapContractError(
        "PROJECT_POLICY_BOOTSTRAP_SOURCE_INVALID"
    )


def _raise_bootstrap_marker_invalid() -> None:
    raise AutomationProjectBootstrapContractError(
        "PROJECT_POLICY_BOOTSTRAP_MARKER_INVALID"
    )


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


def _positive_version(value: Any, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _sha256(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _release_sha(value: Any) -> str:
    try:
        return automation_project_bootstrap_release_sha(value)
    except AutomationProjectBootstrapContractError as exc:
        raise ValueError("release_sha must be a full lowercase Git SHA-1") from exc
