"""A failed held release can transfer its hold only while both services are stopped."""
from __future__ import annotations

import pytest

from tests.test_release_boundaries import (
    _run_remote_release_argument_harness,
    _run_sourced_release_harness,
)


SETUP = r'''
    BACKUP_DIR="${stage_root}/_rollback"
    mkdir -p "${BACKUP_DIR}"
    SCHEDULER_RELEASE_HOLD_FILE="${stage_root}/scheduler-release.pause"
    RECOVER_RELEASE_HOLD_SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    printf '%s\n' "${RECOVER_RELEASE_HOLD_SHA}" >"${SCHEDULER_RELEASE_HOLD_FILE}"
    systemctl() {
      [[ "$1" == show ]] || return 2
      case "$4" in
        ActiveState) echo inactive ;;
        MainPID|ControlPID) echo 0 ;;
        *) return 2 ;;
      esac
    }
'''


def test_recovery_argument_requires_exact_sha():
    accepted = _run_remote_release_argument_harness(
        "", "--recover-held-release=" + "b" * 40,
    )
    assert accepted.returncode == 0, accepted.stderr
    for value in ("extra", "--recover-held-release=", "--recover-held-release=" + "b" * 39):
        rejected = _run_remote_release_argument_harness("", value)
        assert rejected.returncode == 2
        assert "INVALID_RECOVERY_ARGUMENT" in rejected.stderr


def test_hold_is_transferred_atomically_and_failed_recovery_keeps_it():
    completed = _run_sourced_release_harness(SETUP + r'''
        mv() {
          [[ -f "${SCHEDULER_RELEASE_HOLD_FILE}" ]] || return 2
          [[ "$(cat "${SCHEDULER_RELEASE_HOLD_FILE}")" == "${RECOVER_RELEASE_HOLD_SHA}" ]] || return 2
          command mv "$@"
        }
        create_scheduler_release_hold
        [[ "$(cat "${SCHEDULER_RELEASE_HOLD_FILE}")" == "${RELEASE_SHA}" ]]
        [[ "$(cat "${BACKUP_DIR}/recovered_scheduler_release_hold.sha")" == "${RECOVER_RELEASE_HOLD_SHA}" ]]
        [[ "$(stat -c %a "${SCHEDULER_RELEASE_HOLD_FILE}")" == 600 ]]
        ensure_scheduler_release_hold
        if clear_scheduler_release_hold_for_rollback; then exit 3; fi
        [[ -f "${SCHEDULER_RELEASE_HOLD_FILE}" ]]
        echo hold-remained-closed
    ''')
    assert completed.returncode == 0, completed.stderr
    assert "scheduler_release_hold=adopted" in completed.stdout
    assert "hold-remained-closed" in completed.stdout
    assert "RECOVERED_HELD_RELEASE" in completed.stderr


@pytest.mark.parametrize("change", [
    'RECOVER_RELEASE_HOLD_SHA="cccccccccccccccccccccccccccccccccccccccc"',
    'RECOVER_RELEASE_HOLD_SHA=""',
    'COORDINATED_RELEASE=0',
    'systemctl() { [[ "$4" == ActiveState ]] && echo active || echo 0; }',
    'systemctl() { [[ "$4" == ActiveState ]] && echo inactive || echo 123; }',
    'systemctl() { [[ "$4" == ControlPID ]] && echo 123 || { [[ "$4" == ActiveState ]] && echo inactive || echo 0; }; }',
    'systemctl() { return 1; }',
])
def test_recovery_refuses_unverified_owner_or_service_state(change):
    completed = _run_sourced_release_harness(SETUP + change + r'''
        if create_scheduler_release_hold; then exit 3; fi
        [[ "$(cat "${SCHEDULER_RELEASE_HOLD_FILE}")" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]]
        [[ ! -e "${BACKUP_DIR}/recovered_scheduler_release_hold.sha" ]]
        echo original-hold-preserved
    ''')
    assert completed.returncode == 0, completed.stderr
    assert "original-hold-preserved" in completed.stdout


def test_recovery_refuses_symlink_hold():
    completed = _run_sourced_release_harness(SETUP + r'''
        command mv "${SCHEDULER_RELEASE_HOLD_FILE}" "${stage_root}/original-hold"
        ln -s "${stage_root}/original-hold" "${SCHEDULER_RELEASE_HOLD_FILE}"
        if create_scheduler_release_hold; then exit 3; fi
        [[ -L "${SCHEDULER_RELEASE_HOLD_FILE}" ]]
        [[ "$(cat "${stage_root}/original-hold")" == "${RECOVER_RELEASE_HOLD_SHA}" ]]
    ''')
    assert completed.returncode == 0, completed.stderr


def test_non_emergency_recovery_also_adopts_before_any_runtime_mutation():
    completed = _run_sourced_release_harness(SETUP + r'''
        acquire_release_lock() { return 0; }
        validate_environment() { validate_scheduler_release_hold_recovery; }
        preflight_service_identity_configuration() { return 0; }
        preflight_control_plane_task_cutover() { return 0; }
        preflight_automation_project_scheduled_task_identities() { return 0; }
        preflight_automation_project_required_resources() { return 0; }
        preflight_scheduled_write_window() { echo real-window-check-still-required; }
        backup_managed_sources() { return 0; }
        run_static_preflight() { return 0; }
        build_release_virtualenvs() { return 0; }
        preflight_signed_first_party_plugins() { return 0; }
        capture_preexisting_automation_plugin_db_ownership() { return 0; }
        capture_control_plane_release_state() { return 0; }
        preflight_running_protected_writes() { return 0; }
        quiesce_runtime_services() {
          [[ "${SCHEDULER_RELEASE_HOLD_ADOPTED}" == 1 ]]
          [[ "$(cat "${SCHEDULER_RELEASE_HOLD_FILE}")" == "${RELEASE_SHA}" ]]
          echo hold-already-adopted-before-mutation
          exit 0
        }
        [[ "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}" == 0 ]]
        run_release
    ''')
    assert completed.returncode == 0, completed.stderr
    assert "hold-already-adopted-before-mutation" in completed.stdout
    assert completed.stdout.count("real-window-check-still-required") == 2
