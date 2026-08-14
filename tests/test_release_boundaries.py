from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _path_for_bash(path: Path) -> str:
    raw = str(path)
    wsl_prefix = "\\\\wsl.localhost\\Ubuntu\\"
    if os.name == "nt" and raw.lower().startswith(wsl_prefix.lower()):
        return "/" + raw[len(wsl_prefix) :].replace("\\", "/")
    if os.name == "nt":
        return subprocess.check_output(
            ["wsl.exe", "-d", "Ubuntu", "--", "wslpath", "-a", raw],
            text=True,
            encoding="utf-8",
        ).strip()
    return raw


def _run_rollback_fault_harness(
    *, fail_agent_restart: bool, cutover_pending: bool = True
) -> tuple[subprocess.CompletedProcess[str], list[str], bool, bool]:
    release_script = REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh"
    task_tmp_root = REPOSITORY_ROOT / ".task_tmp"
    task_tmp_preexisting = task_tmp_root.exists()
    task_tmp_root.mkdir(exist_ok=True)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary = tempfile.TemporaryDirectory(dir=task_tmp_root)
        temp_root = Path(temporary.name)
        stage_root = temp_root / "stage"
        backup_root = stage_root / "_rollback"
        backup_root.mkdir(parents=True)
        (backup_root / "release_sha.absent").touch()
        events_path = temp_root / "events.log"
        for service in ("agent", "console"):
            python_path = temp_root / service / ".venv" / "bin" / "python"
            python_path.parent.mkdir(parents=True)
            with python_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    "#!/usr/bin/env bash\n"
                    f"printf 'venv:{service}\\n' >> {_path_for_bash(events_path)!r}\n"
                    "exit 0\n"
                )
            python_path.chmod(0o755)
        new_venv = temp_root / "venvs" / "runtime-deps-new"
        new_venv.mkdir(parents=True)
        (new_venv / "preserve-marker").touch()

        harness = textwrap.dedent(
            r"""
            release_script="$1"
            temp_root="$2"
            fail_agent_restart="$3"
            cutover_pending="$4"
            stage_root="${temp_root}/stage"
            events_path="${temp_root}/events.log"
            source "${release_script}" "${stage_root}" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" agent,console 0 0

            ROOTS[agent]="${temp_root}/agent"
            ROOTS[console]="${temp_root}/console"
            PYTHON_BINS[agent]="${ROOTS[agent]}/.venv/bin/python"
            PYTHON_BINS[console]="${ROOTS[console]}/.venv/bin/python"
            chmod 0755 "${PYTHON_BINS[agent]}" "${PYTHON_BINS[console]}"
            SERVICES[agent]="agent.service"
            SERVICES[console]="console.service"
            RUNTIME_TARGETS=(agent console)
            SCOPES=()
            REQUESTED_TARGETS=()
            BACKUP_DIR="${stage_root}/_rollback"
            BACKUP_TREE="${BACKUP_DIR}/tree"
            VENV_ROOT="${temp_root}/venvs"
            RELEASE_VENV="${VENV_ROOT}/runtime-deps-new"
            CREATED_VENV="${fail_agent_restart}"
            VENV_ACTIVATED=0
            SERVICES_QUIESCED=1
            MUTATION_STARTED=1
            CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY="${cutover_pending}"
            RELEASE_STAGE="check_health"
            declare -A ACTIVE_STATE=(
              [agent.service]=1
              [console.service]=1
            )

            sudo() {
              if [[ "$1" == "systemctl" ]]; then
                local action="$2"
                local service="${3:-}"
                case "${action}" in
                  stop)
                    printf 'stop:%s\n' "${service}" >>"${events_path}"
                    ACTIVE_STATE["${service}"]=0
                    return 0
                    ;;
                  restart)
                    printf 'restart:%s\n' "${service}" >>"${events_path}"
                    if [[ "${service}" == "agent.service" && "${fail_agent_restart}" == "1" ]]; then
                      return 1
                    fi
                    ACTIVE_STATE["${service}"]=1
                    return 0
                    ;;
                  daemon-reload)
                    printf 'daemon-reload\n' >>"${events_path}"
                    return 0
                    ;;
                esac
              fi
              if [[ "$1" == "install" ]]; then
                return 0
              fi
              return 1
            }

            systemctl() {
              if [[ "$1" == "is-active" && "$2" == "--quiet" ]]; then
                [[ "${ACTIVE_STATE[$3]:-0}" == "1" ]]
                return
              fi
              return 1
            }

            restore_control_plane_task_cutover_data() {
              printf 'migration-restore\n' >>"${events_path}"
              return 0
            }

            set +e
            false
            rollback
            """
        )
        harness_path = temp_root / "rollback_harness.sh"
        with harness_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(harness)
        harness_args = [
            "bash",
            _path_for_bash(harness_path),
            _path_for_bash(release_script),
            _path_for_bash(temp_root),
            "1" if fail_agent_restart else "0",
            "1" if cutover_pending else "0",
        ]
        if os.name == "nt":
            harness_args = ["wsl.exe", "-d", "Ubuntu", "--", *harness_args]
        completed = subprocess.run(
            harness_args,
            cwd=REPOSITORY_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        events = (
            events_path.read_text(encoding="utf-8").splitlines()
            if events_path.exists()
            else []
        )
        result = (
            completed,
            events,
            stage_root.exists(),
            (new_venv / "preserve-marker").exists(),
        )
        return result
    finally:
        if temporary is not None:
            temporary.cleanup()
        if not task_tmp_preexisting and task_tmp_root.exists():
            task_tmp_root.rmdir()


def _requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==[^\s;]+", line)
        if not match:
            raise AssertionError(f"non-exact requirement in {path}: {line}")
        names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;]+)", line)
        if not match:
            raise AssertionError(f"non-exact requirement in {path}: {line}")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        versions[name] = match.group(2)
    return versions


class ReleaseBoundaryTests(unittest.TestCase):
    def test_direct_dependencies_are_covered_by_exact_locks(self):
        for service in ("agent", "console"):
            direct = _requirement_names(REPOSITORY_ROOT / service / "requirements.txt")
            locked = _requirement_names(REPOSITORY_ROOT / service / "requirements.lock")
            self.assertTrue(direct)
            self.assertTrue(direct <= locked, direct - locked)

    def test_ci_and_production_use_python_310_locked_environments(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertEqual(2, workflow.count('python-version: "3.10"'))
        self.assertIn(
            "python -m pip install -r agent/requirements.lock -r console/requirements.lock",
            workflow,
        )
        self.assertIn("verify_locked_environment.py agent/requirements.lock", workflow)
        self.assertIn("verify_locked_environment.py console/requirements.lock", workflow)

        release = (REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh").read_text(encoding="utf-8")
        execution = release.split("trap rollback ERR", 1)[1]
        quiesce_function = release.split("quiesce_runtime_services() {", 1)[1].split("\n}", 1)[0]
        activate_function = release.split("activate_release_virtualenvs() {", 1)[1].split("\n}", 1)[0]
        rollback_function = release.split("\nrollback() {", 1)[1].split("\n}", 1)[0]
        self.assertLess(
            execution.index("preflight_service_identity_configuration"),
            execution.index("backup_managed_sources"),
        )
        self.assertEqual(2, execution.count("preflight_scheduled_write_window\n"))
        self.assertLess(
            execution.index('RELEASE_STAGE="preflight_scheduled_write_window_before_mutation"'),
            execution.index("MUTATION_STARTED=1"),
        )
        self.assertLess(execution.index("build_release_virtualenvs"), execution.index("MUTATION_STARTED=1"))
        self.assertLess(execution.index("quiesce_runtime_services"), execution.index("retire_legacy_finance_etl"))
        self.assertLess(execution.index("quiesce_runtime_services"), execution.index('sync_scope "${scope}"'))
        self.assertLess(execution.index("quiesce_runtime_services"), execution.index("apply_migrations"))
        self.assertLess(
            execution.index("capture_control_plane_task_cutover_state"),
            execution.index("apply_migrations"),
        )
        self.assertLess(execution.index("apply_migrations"), execution.index("activate_release_virtualenvs"))
        self.assertIn('agent_hash="$(sha256sum "${agent_lock}"', release)
        self.assertIn('console_hash="$(sha256sum "${console_lock}"', release)
        self.assertIn("printf 'agent=%s\\nconsole=%s\\n'", release)
        self.assertIn('release_venv="${VENV_ROOT}/runtime-deps-${lock_hash}"', release)
        self.assertIn('Reusing shared virtual environment for dependency lock', release)
        self.assertIn('--requirement "${agent_lock}"', release)
        self.assertIn('--requirement "${console_lock}"', release)
        self.assertIn('CREATED_VENV=1', release)
        self.assertIn('[[ "${CREATED_VENV}" == "1" ]] || return 0', release)
        self.assertIn('[[ "${VENV_SWITCHED[$target]:-}" == "1" ]] || continue', release)
        self.assertIn('RUNTIME_TARGETS=(agent console)', release)
        self.assertLess(
            quiesce_function.index("SERVICES_QUIESCED=1"),
            quiesce_function.index('sudo systemctl stop "${SERVICES[$target]}"'),
        )
        self.assertLess(
            activate_function.index('VENV_SWITCHED[$target]="1"'),
            activate_function.index('rm -- "${active_venv}"'),
        )
        self.assertIn("record_active_dependency_hashes", release)
        self.assertNotIn('${target}-${RELEASE_SHA}', release)
        self.assertIn('bootstrap_python="$(readlink -f -- "${PYTHON_BINS[agent]}")"', release)
        self.assertIn('migration_python="${RELEASE_VENV}/bin/python"', release)
        self.assertIn("restore_virtualenvs", release)
        self.assertIn("verify_runtime_virtualenvs", rollback_function)
        self.assertLess(
            rollback_function.index("stop_runtime_services_for_rollback"),
            rollback_function.index("restore_control_plane_task_cutover_data"),
        )
        self.assertIn('CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY}" == "1', rollback_function)
        self.assertLess(
            rollback_function.index("restore_control_plane_task_cutover_data"),
            rollback_function.index("restore_managed_release_state"),
        )
        self.assertIn("restart_runtime_services_for_rollback", rollback_function)
        self.assertIn("rollback_incomplete", rollback_function)
        self.assertIn("recovery_material_preserved=1", rollback_function)
        self.assertIn("dotenv_values", release)
        self.assertIn("build_console_identity_headers", release)
        self.assertIn('request_target = "/internal/v1/health"', release)
        self.assertIn('scheduled_task_approval_bootstrap', release)
        self.assertIn('completed != 1', release)
        self.assertIn('created + existing + configured != reviewed', release)
        self.assertIn("https://mirrors.aliyun.com/pypi/simple", release)
        self.assertIn('--retries "${PIP_RETRIES}"', release)
        self.assertIn('--timeout "${PIP_TIMEOUT_SECONDS}"', release)
        self.assertIn('release_error stage=${RELEASE_STAGE}', release)
        self.assertNotIn('\nBACKUP_ROOT=', release)
        self.assertIn('BACKUP_DIR="${STAGE_ROOT}/_rollback"', release)
        self.assertNotIn('LEGACY_BACKUP_ROOT=', release)
        self.assertIn(
            'LEGACY_FINANCE_ETL_ROOT="/home/boyce/agent/finance_reconciliation"',
            release,
        )
        self.assertIn(
            'mv -- "${LEGACY_FINANCE_ETL_ROOT}" "${retired_path}"',
            release,
        )
        self.assertIn(
            'mv -- "${retired_path}" "${LEGACY_FINANCE_ETL_ROOT}"',
            release,
        )
        self.assertNotIn('rm -rf -- "${LEGACY_FINANCE_ETL_ROOT}"', release)
        cleanup_function = release.split("cleanup_successful_release() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("pending_business_validation", cleanup_function)
        self.assertNotIn("rm -rf", cleanup_function)
        self.assertNotIn("prune_inactive_virtualenvs", release)
        self.assertNotIn('rm -rf -- "${LEGACY_BACKUP_ROOT}"', release)
        self.assertLess(execution.index("check_health"), execution.index("cleanup_successful_release"))
        self.assertLess(
            execution.index('RELEASE_STAGE="check_health"'),
            execution.index('RELEASE_STAGE="check_service_identity_smoke"'),
        )
        self.assertLess(execution.index("MUTATION_STARTED=0"), execution.index("cleanup_successful_release"))
        self.assertLess(
            execution.index('RELEASE_STAGE="retire_legacy_finance_etl"'),
            execution.index('RELEASE_STAGE="sync_scope:${scope}"'),
        )
        for stage in (
            "preflight_service_identity_configuration",
            "static_preflight",
            "build_release_virtualenvs",
            "quiesce_runtime_services",
            "retire_legacy_finance_etl",
            "sync_scope:${scope}",
            "capture_control_plane_task_cutover_state",
            "apply_migrations",
            "activate_release_virtualenvs",
            "restart_services",
            "check_health",
            "check_service_identity_smoke",
            "record_dependency_hashes",
            "cleanup_successful_release",
        ):
            self.assertIn(f'RELEASE_STAGE="{stage}"', release)

    def test_health_failure_with_reused_venv_stops_and_recovers_both_services(self):
        completed, events, stage_exists, _new_venv_exists = _run_rollback_fault_harness(
            fail_agent_restart=False
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("release_error stage=check_health", completed.stderr)
        self.assertNotIn("rollback_incomplete", completed.stderr)
        self.assertFalse(stage_exists)
        self.assertEqual(["stop:agent.service", "stop:console.service"], events[:2])
        self.assertIn("venv:agent", events)
        self.assertIn("venv:console", events)
        self.assertLess(events.index("migration-restore"), events.index("venv:agent"))
        self.assertLess(events.index("restart:agent.service"), events.index("restart:console.service"))

    def test_incomplete_rollback_attempts_both_restarts_and_preserves_recovery_material(self):
        completed, events, stage_exists, new_venv_exists = _run_rollback_fault_harness(
            fail_agent_restart=True
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("rollback_incomplete", completed.stderr)
        self.assertIn("recovery_material_preserved=1", completed.stderr)
        self.assertTrue(stage_exists)
        self.assertTrue(new_venv_exists)
        self.assertIn("restart:agent.service", events)
        self.assertIn("restart:console.service", events)

    def test_rollback_does_not_revert_cutover_applied_before_this_release(self):
        completed, events, stage_exists, _new_venv_exists = _run_rollback_fault_harness(
            fail_agent_restart=False,
            cutover_pending=False,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("rollback_incomplete", completed.stderr)
        self.assertNotIn("migration-restore", events)
        self.assertFalse(stage_exists)
        self.assertIn("restart:agent.service", events)
        self.assertIn("restart:console.service", events)

    def test_release_keeps_ssh_verification_and_publishes_new_modules(self):
        publisher = (REPOSITORY_ROOT / "agent" / "deploy" / "publish_to_ecs.ps1").read_text(encoding="utf-8")
        publisher_finally = publisher.split("\nfinally {", 1)[1]
        self.assertNotIn("rm -rf", publisher_finally)
        self.assertIn("Remote release stage preserved for recovery", publisher_finally)
        blocked_extensions = publisher[
            publisher.index("$BlockedExtensions = @(") : publisher.index("function Assert-Command")
        ]
        self.assertNotIn("StrictHostKeyChecking=no", publisher)
        self.assertIn('"app_support.py"', publisher)
        self.assertIn('"navigation.py"', publisher)
        self.assertIn('"config", "routes", "services", "static", "templates"', publisher)
        self.assertNotIn('".webp"', blocked_extensions)
        self.assertIn('if ($extension -in @(".png", ".jpg", ".jpeg", ".webp"))', publisher)

    def test_shared_runtime_uses_headless_opencv_for_both_services(self):
        agent_lock = (REPOSITORY_ROOT / "agent" / "requirements.lock").read_text(encoding="utf-8")
        console_lock = (REPOSITORY_ROOT / "console" / "requirements.lock").read_text(encoding="utf-8")
        self.assertIn("opencv-python-headless==", agent_lock)
        self.assertIn("opencv-python-headless==", console_lock)
        self.assertNotIn("opencv-python==", console_lock)

        agent_versions = _locked_versions(REPOSITORY_ROOT / "agent" / "requirements.lock")
        console_versions = _locked_versions(REPOSITORY_ROOT / "console" / "requirements.lock")
        overlap = agent_versions.keys() & console_versions.keys()
        self.assertTrue(overlap)
        self.assertEqual(
            {},
            {
                name: (agent_versions[name], console_versions[name])
                for name in overlap
                if agent_versions[name] != console_versions[name]
            },
        )


if __name__ == "__main__":
    unittest.main()
