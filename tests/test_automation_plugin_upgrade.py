from __future__ import annotations

from typing import Any, Mapping

from shared.automation_plugin_repository import AutomationPluginRepository


class _AbortRevertCursor:
    def __init__(
        self,
        *,
        project: Mapping[str, Any],
        committed_generation: Mapping[str, Any],
    ) -> None:
        self.project = dict(project)
        self.committed_generation = dict(committed_generation)
        self.result: Mapping[str, Any] | list[Mapping[str, Any]] | None = None
        self.rowcount = 0
        self.executions: list[tuple[str, object]] = []
        self.project_updates: list[tuple[str, object]] = []
        self.policy_updates: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        normalized = " ".join(str(sql).split())
        self.executions.append((normalized, params))
        self.rowcount = 0
        if normalized.startswith("SELECT") and "FROM automation_projects" in normalized:
            self.result = self.project
            return
        if (
            normalized.startswith("SELECT")
            and "FROM automation_project_generation_effects" in normalized
        ):
            self.result = []
            return
        if (
            normalized.startswith("SELECT")
            and "FROM automation_project_generation_leases" in normalized
        ):
            self.result = []
            return
        if (
            normalized.startswith("SELECT")
            and "FROM automation_project_policies" in normalized
        ):
            self.result = {"mode": "REQUIRE_EACH_RUN"}
            return
        if (
            normalized.startswith("SELECT")
            and "FROM automation_project_generations" in normalized
            and "COUNT(*)" not in normalized
        ):
            self.result = self.committed_generation
            return
        if normalized.startswith("SELECT COUNT(*) AS draining_count"):
            self.result = {"draining_count": 0}
            return
        if normalized.startswith("UPDATE automation_project_generations"):
            self.result = None
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE automation_projects"):
            self.result = None
            self.rowcount = 1
            self.project_updates.append((normalized, params))
            return
        if normalized.startswith("UPDATE automation_project_policies"):
            self.result = None
            self.rowcount = 1
            self.policy_updates.append((normalized, params))
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self) -> Mapping[str, Any] | None:
        return self.result if isinstance(self.result, Mapping) else None

    def fetchall(self) -> list[Mapping[str, Any]]:
        return self.result if isinstance(self.result, list) else []

    def close(self) -> None:
        return None


class _AbortRevertConnection:
    def __init__(self, cursor: _AbortRevertCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _AbortRevertCursor:
        return self._cursor


def test_failed_uncommitted_target_dispose_atomically_restores_committed_desired() -> None:
    cursor = _AbortRevertCursor(
        project={
            "automation_id": "upgrade-instance",
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "UPGRADING",
            "enabled": True,
            "target_generation": 2,
            "committed_generation": 1,
            "record_version": 4,
        },
        committed_generation={
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "1.0.0",
            "state": "COMMITTED",
        },
    )
    repository = AutomationPluginRepository(_AbortRevertConnection(cursor))

    repository.complete_generation_dispose_row("upgrade-instance", 2)

    assert len(cursor.project_updates) == 1
    update_sql, params = cursor.project_updates[0]
    assert "plugin_version=%s" in update_sql
    assert "target_generation=%s" in update_sql
    assert "CASE WHEN enabled THEN 'ENABLED' ELSE 'DISABLED' END" in update_sql
    assert "reconcile_state=%s" in update_sql
    assert params is not None
    assert "1.0.0" in params
    assert 1 in params
    assert "STABLE" in params
    assert len(cursor.policy_updates) == 1
    policy_sql, policy_params = cursor.policy_updates[0]
    assert "project_generation=%s" in policy_sql
    assert "mode=" not in policy_sql
    assert policy_params is not None
    assert 1 in policy_params


def test_postcommit_old_generation_dispose_never_reverts_committed_b() -> None:
    cursor = _AbortRevertCursor(
        project={
            "automation_id": "upgrade-instance",
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "ENABLED",
            "enabled": True,
            "target_generation": 2,
            "committed_generation": 2,
            "record_version": 5,
        },
        committed_generation={
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "COMMITTED",
        },
    )
    repository = AutomationPluginRepository(_AbortRevertConnection(cursor))

    repository.complete_generation_dispose_row("upgrade-instance", 1)

    assert len(cursor.project_updates) == 1
    update_sql, params = cursor.project_updates[0]
    assert "plugin_version=%s" not in update_sql
    assert "target_generation=%s" not in update_sql
    assert params == ("STABLE", "upgrade-instance")
    assert cursor.policy_updates == []


def test_postcommit_old_generation_failure_keeps_project_draining_not_error() -> None:
    cursor = _AbortRevertCursor(
        project={
            "automation_id": "upgrade-instance",
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "ENABLED",
            "enabled": True,
            "target_generation": 2,
            "committed_generation": 2,
            "record_version": 5,
        },
        committed_generation={
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "COMMITTED",
        },
    )
    repository = AutomationPluginRepository(_AbortRevertConnection(cursor))

    repository.fail_generation_row(
        "upgrade-instance",
        1,
        error_code="DISPOSE_FAILED",
        error_summary="synthetic cleanup failure",
    )

    assert len(cursor.project_updates) == 1
    update_sql, params = cursor.project_updates[0]
    assert "CASE" in update_sql
    assert "committed_generation<>%s" in update_sql
    assert "target_generation<>%s" in update_sql
    assert "'DRAINING'" in update_sql
    assert "'ERROR'" in update_sql
    assert params == (1, 1, "upgrade-instance")

