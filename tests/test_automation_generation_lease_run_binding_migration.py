from __future__ import annotations

from pathlib import Path

import pytest

from shared.automation_plugin_repository import AutomationPluginRepository
from shared.orchestration_schema import REQUIRED_COLUMNS
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
)

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "agent"
    / "migrations"
    / "019_automation_generation_lease_run_binding.sql"
)


def test_generation_lease_run_binding_preserves_unbound_legacy_rows() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ADD COLUMN orchestration_run_id CHAR(36) NULL" in sql
    assert "idx_automation_generation_lease_run (orchestration_run_id)" in sql
    assert "fk_automation_generation_lease_run" in sql
    assert "REFERENCES agent_runs (run_id) ON DELETE RESTRICT" in sql
    assert (
        "automation_project_generation_leases",
        "orchestration_run_id",
    ) in REQUIRED_COLUMNS


class _LeaseCursor:
    def __init__(self, lease: dict[str, object]) -> None:
        self.lease = lease
        self.sql: list[str] = []

    def __enter__(self) -> _LeaseCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, statement: str, _params: object) -> None:
        self.sql.append(statement)

    def fetchone(self) -> dict[str, object]:
        return self.lease


class _LeaseRepository(AutomationPluginRepository):
    def __init__(self, orchestration_run_id: str | None) -> None:
        self.cursor_instance = _LeaseCursor(
            {
                "lease_id": "lease-1",
                "automation_id": "arrival_stats",
                "generation": 1,
                "orchestration_run_id": orchestration_run_id,
                "outcome": "WRITE_OUTCOME_UNKNOWN",
            }
        )

    def cursor(self) -> _LeaseCursor:
        return self.cursor_instance


@pytest.mark.parametrize(
    ("bound_run_id", "error_type"),
    (
        (None, ConcurrentUpdateError),
        ("11111111-1111-4111-8111-111111111111", IdempotencyConflict),
    ),
)
def test_recovery_rejects_unbound_or_mismatched_run_before_mutating_lease(
    bound_run_id: str | None,
    error_type: type[Exception],
) -> None:
    repository = _LeaseRepository(bound_run_id)

    with pytest.raises(error_type):
        repository.resolve_unknown_generation_write_not_applied_row(
            "arrival_stats",
            1,
            "lease-1",
            expected_orchestration_run_id="22222222-2222-4222-8222-222222222222",
            evidence_sha256="a" * 64,
        )

    assert len(repository.cursor_instance.sql) == 1
    assert "FOR UPDATE" in repository.cursor_instance.sql[0]
