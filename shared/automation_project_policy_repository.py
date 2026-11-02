"""Transactional persistence for project policies and grouped approvals."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


PROJECT_POLICY_MODES = frozenset(
    {"PROJECT_FULL_AUTO", "REQUIRE_EACH_RUN", "LEGACY_SCHEDULE_ONLY"}
)
SUPER_ADMIN_PROJECT_POLICY_REASON = "SUPER_ADMIN_PROJECT_POLICY_CHANGED"
class AutomationProjectBootstrapContractError(ValueError):
    """A persisted 018 bootstrap input is incomplete or inconsistent."""

    def __init__(self, code: str) -> None:
        super().__init__(str(code))
        self.code = str(code)


def read_project_configuration_rows(cursor, automation_ids, *, for_update=False):
    """Read the same complete schedule contract for one or several projects."""
    identities = tuple(_required_text(value, "automation_id") for value in automation_ids)
    if not identities:
        return []
    suffix = " FOR UPDATE" if for_update else ""
    condition = "task.automation_id=%s" if len(identities) == 1 else (
        "task.automation_id IN (" + ",".join("%s" for _ in identities) + ")"
    )
    cursor.execute(
        f"""
        SELECT task.id, task.automation_id, task.name, task.tool_name,
               task.tool_params, task.cron_expression, task.enabled,
               task.automation_generation, task.configuration_version,
               task.updated_at
        FROM scheduled_tasks AS task
        WHERE {condition}
        ORDER BY task.id{suffix}
        """,
        identities,
    )
    return [_decode_row(row, ("tool_params",)) or {} for row in _rows(cursor)]


class AutomationProjectPolicyRepository(RepositoryBase):
    POLICY_JSON_FIELDS = ("contract_snapshot_json",)
    EVENT_JSON_FIELDS = ("contract_snapshot_json",)

    def list_policies(self, *, automation_ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
        selected = tuple(_required_text(value, "automation_id") for value in automation_ids) if automation_ids is not None else None
        if selected is not None and (not 1 <= len(selected) <= 1000 or len(set(selected)) != len(selected)):
            raise ValueError("policy display requires 1 to 1000 distinct instance IDs")
        where = " WHERE automation_id IN (" + ",".join(["%s"] * len(selected)) + ")" if selected is not None else ""
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_project_policies" + where + " ORDER BY automation_id",
                selected or (),
            )
            return [
                _decode_row(row, self.POLICY_JSON_FIELDS) or {}
                for row in _rows(cursor)
            ]

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
        with self.cursor() as cursor:
            return read_project_configuration_rows(cursor, (automation_id,), for_update=for_update)


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
