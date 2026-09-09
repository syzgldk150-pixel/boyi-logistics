"""Rollback must not reactivate a retired queue, even after source restoration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_automation_project_version_preflight import _load_preflight, _load_runner
from tests.test_legacy_execution_retirement_mysql import database, _apply
from tests.test_legacy_unknown_scope_migration_mysql import database as legacy_database
from tests.test_pending_run_timebase_migration_mysql import _connect
from tests.test_release_boundaries import _run_sourced_release_harness

ROOT = Path(__file__).resolve().parents[1]


def test_restored_direct_composition_is_inspected_without_import_or_start(tmp_path):
    check = _load_preflight()._restored_source_uses_direct_execution
    assert check(ROOT / "agent")
    (tmp_path / "agent/orchestration").mkdir(parents=True)
    main = (ROOT / "agent/main.py").read_text(encoding="utf-8")
    runner = (ROOT / "agent/agent/orchestration/workflow_runner.py").read_text(encoding="utf-8")
    (tmp_path / "main.py").write_text(main, encoding="utf-8")
    (tmp_path / "agent/orchestration/workflow_runner.py").write_text(runner, encoding="utf-8")
    assert check(tmp_path)
    for source in (
        main.replace("execution_enabled=False", "execution_enabled=True"),
        main.replace("await direct_invocations.startup()", "# unstarted Direct module"),
        "# An unused Direct module does not change the running architecture.\n",
    ):
        (tmp_path / "main.py").write_text(source, encoding="utf-8")
        assert not check(tmp_path)
    (tmp_path / "main.py").write_text(main, encoding="utf-8")
    (tmp_path / "agent/orchestration/workflow_runner.py").write_text(
        runner.replace("if not self._execution_enabled:", "if False:"), encoding="utf-8")
    assert not check(tmp_path)
    assert not check(None)


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL")
def test_real_046_marker_blocks_old_source_but_allows_direct_source(database, tmp_path, capsys):
    preflight = _load_preflight()
    manifest = tmp_path / "seeds.json"
    manifest.write_text(json.dumps({"seeds": [{"automation_id": "rollback_fixture",
        "plugin_id": "rollback_fixture", "version": "1.0.0"}]}), encoding="utf-8")
    connect = lambda: _connect(database, "+00:00")
    assert preflight.check_rollback_exact_seed_compatibility(connect, manifest) == 0
    _apply(database)
    for source in (None, tmp_path):
        assert preflight.check_rollback_exact_seed_compatibility(
            connect, manifest, restored_source_root=source) == 1
        assert "code=LEGACY_EXECUTION_RETIRED" in capsys.readouterr().out
    assert preflight.check_rollback_exact_seed_compatibility(
        connect, manifest, restored_source_root=ROOT / "agent") == 0
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS total FROM schema_migrations WHERE version='046'")
        assert cursor.fetchone()["total"] == 1


def test_existing_rollback_cli_passes_actual_restored_source_path():
    runner = _load_runner()
    with patch.object(runner, "check_rollback_exact_seed_compatibility", return_value=0) as check, patch(
        "sys.argv", ["run_migrations.py", "--check-rollback-exact-seed-compatibility",
                     "--rollback-exact-seed-manifest", "/fixture/seeds.json",
                     "--rollback-restored-source-root", "/fixture/restored-agent"]):
        assert runner.main() == 0
    check.assert_called_once_with(runner._connect, "/fixture/seeds.json",
                                  restored_source_root="/fixture/restored-agent")


@pytest.mark.parametrize("code", ["LEGACY_EXECUTION_RETIRED", "MIGRATION_STATE_INVALID"])
def test_actual_rollback_keeps_hold_and_never_starts_retired_execution(code):
    completed = _run_sourced_release_harness(r'''
        BACKUP_DIR="${stage_root}/_rollback"
        ROOTS[agent]="${stage_root}/restored-agent"
        mkdir -p "${BACKUP_DIR}"
        SCHEDULER_RELEASE_HOLD_FILE="${stage_root}/scheduler-release.pause"
        touch "${SCHEDULER_RELEASE_HOLD_FILE}"
        trap '[[ -f "${SCHEDULER_RELEASE_HOLD_FILE}" ]] && echo actual-hold-retained' EXIT
        MUTATION_STARTED=1
        AGENT_RELEASE=1
        FEISHU_NOTIFICATION_LEASE_PENDING_AT_APPLY=0
        AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY=0
        CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE=0
        SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY=0
        DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY=0
        CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY=0
        VENV_ACTIVATED=0
        stop_runtime_services_for_rollback() { echo actual-stop; }
        restore_managed_release_state() { echo actual-restore; }
        verify_runtime_virtualenvs() { return 0; }
        restart_runtime_services_for_rollback() { echo forbidden-restart; }
        clear_scheduler_release_hold_for_rollback() { echo forbidden-clear-hold; }
        cleanup_failed_release_stage() { echo forbidden-cleanup; }
        write_restored_first_party_seed_manifest() { printf '{}\n' >"$1"; }
        run_staged_migration_runner() {
          [[ "$*" == *"--rollback-restored-source-root ${ROOTS[agent]}"* ]] || return 2
          echo 'rollback_exact_seed_compatibility=blocked code=REJECTION_CODE'
          return 1
        }
        set +e
        false
        rollback
    '''.replace("REJECTION_CODE", code))
    assert completed.returncode != 0
    assert "actual-stop" in completed.stdout and "actual-restore" in completed.stdout
    assert "forbidden-" not in completed.stdout
    assert "actual-hold-retained" in completed.stdout
    assert "release_hold_preserved=1" in completed.stderr
    assert "recovery_material_preserved=1" in completed.stderr
