from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PREFLIGHT_PATH = (
    Path(__file__).resolve().parents[1]
    / "agent"
    / "scripts"
    / "automation_project_release_manifest_preflight.py"
)


@pytest.fixture
def preflight():
    spec = importlib.util.spec_from_file_location(
        "historical_unknown_write_preflight",
        PREFLIGHT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stable_successor():
    return {
        "generation": 3,
        "generation_state": "COMMITTED",
        "reconcile_state": "STABLE",
        "unsafe_non_disposed_other_count": 0,
        "active_current_lease_count": 0,
    }


def test_archived_bootstrap_unknown_write_is_allowed_after_stable_successor(
    preflight,
):
    assert preflight._bootstrap_generation_may_be_blocked(
        _stable_successor(),
        {"generation": 1, "generation_state": "BLOCKED"},
    )


@pytest.mark.parametrize(
    ("project_change", "generation_change"),
    (
        ({"unsafe_non_disposed_other_count": 1}, {}),
        ({"active_current_lease_count": 1}, {}),
        ({"reconcile_state": "PREPARING"}, {}),
        ({"generation_state": "BLOCKED"}, {}),
        ({}, {"generation": 3}),
        ({}, {"generation_state": "COMMITTED"}),
    ),
)
def test_archived_bootstrap_unknown_write_fails_closed_on_drift(
    preflight,
    project_change,
    generation_change,
):
    project = _stable_successor()
    project.update(project_change)
    generation = {"generation": 1, "generation_state": "BLOCKED"}
    generation.update(generation_change)

    assert not preflight._bootstrap_generation_may_be_blocked(
        project,
        generation,
    )


def test_archive_query_rejects_active_historical_leases(preflight):
    class Cursor:
        def execute(self, sql, params):
            self.sql = " ".join(str(sql).split())

        def fetchall(self):
            return [{"automation_id": "arrival_stats"}]

    cursor = Cursor()
    preflight._read_release_projects(
        cursor,
        {"release_projects": frozenset({"arrival_stats"})},
    )

    assert "active_archive_lease.outcome IN ('RUNNING', 'VERIFYING')" in cursor.sql
