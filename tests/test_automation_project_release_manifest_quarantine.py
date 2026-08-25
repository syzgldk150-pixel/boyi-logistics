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


@pytest.fixture(scope="module")
def preflight():
    spec = importlib.util.spec_from_file_location(
        "test_automation_project_release_manifest_quarantine_module",
        PREFLIGHT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current_unknown_write_project() -> dict[str, object]:
    return {
        "reconcile_state": "BLOCKED_UNKNOWN_WRITE",
        "generation": 3,
        "generation_state": "BLOCKED",
        "generation_error_code": "WRITE_OUTCOME_UNKNOWN",
        "unsafe_non_disposed_other_count": 0,
        "active_current_lease_count": 0,
        "unknown_write_count": 1,
    }


def test_archived_bootstrap_accepts_current_unknown_write_quarantine(preflight):
    assert preflight._bootstrap_generation_may_be_blocked(
        _current_unknown_write_project(),
        {"generation": 1, "generation_state": "BLOCKED"},
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("generation_error_code", None),
        ("unsafe_non_disposed_other_count", 1),
        ("active_current_lease_count", 1),
        ("unknown_write_count", 0),
    ),
)
def test_archived_bootstrap_rejects_incomplete_unknown_write_evidence(
    preflight,
    field,
    value,
):
    project = _current_unknown_write_project()
    project[field] = value

    assert not preflight._bootstrap_generation_may_be_blocked(
        project,
        {"generation": 1, "generation_state": "BLOCKED"},
    )
