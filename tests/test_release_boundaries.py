from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
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


def _service_identity_smoke_source() -> str:
    release = (REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh").read_text(
        encoding="utf-8"
    )
    function = release.split("check_service_identity_smoke() {", 1)[1].split(
        "\n}\n\nactivate_scheduler_after_release() {",
        1,
    )[0]
    marker = '"${console_python}" - <<\'PY\'\n'
    return function.split(marker, 1)[1].rsplit("\nPY", 1)[0]


def _healthy_service_identity_payload() -> dict[str, object]:
    return {
        "ok": True,
        "probe_marker": "SMOKE_BODY_MUST_NOT_APPEAR",
        "data": {
            "components": {
                "scheduler": {
                    "state": "paused",
                    "release_hold": True,
                },
                "workflow_runner": {
                    "state": "held",
                    "release_hold": True,
                    "active_runs": 0,
                },
                "automation_plugins": {
                    "ok": True,
                    "runnable": True,
                    "runtime_status": "READY",
                    "broker": {"state": "running"},
                    "sandbox": {
                        "state": "ready",
                        "code": "OK",
                        "checked_at": "2026-08-22T00:00:00+00:00",
                    },
                    "catalog": {
                        "ok": True,
                        "runnable": True,
                        "runtime_status": "READY",
                        "unsupported_automation_ids": [],
                        "invalid_enabled_trust": [],
                        "unstable_generations": [],
                        "invalid_enabled_runtime": [],
                    },
                    "generations": {"healthy": True, "blocked_projects": {}},
                },
            }
        },
    }


def _run_service_identity_smoke(
    payload: object,
    *,
    response_status: int = 200,
    service_identity_source: str | None = None,
) -> subprocess.CompletedProcess[str]:
    task_tmp_root = REPOSITORY_ROOT / ".task_tmp"
    task_tmp_preexisting = task_tmp_root.exists()
    task_tmp_root.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=task_tmp_root) as temporary:
            temp_root = Path(temporary)
            (temp_root / "shared").mkdir()
            (temp_root / "shared" / "__init__.py").write_text("", encoding="utf-8")
            (temp_root / "shared" / "release_identity.py").write_bytes(
                (REPOSITORY_ROOT / "shared" / "release_identity.py").read_bytes()
            )
            (temp_root / "dotenv.py").write_text(
                "def dotenv_values(_path):\n"
                "    return {\n"
                "        'AGENT_INTERNAL_API_TOKEN': 'test-internal-token',\n"
                "        'CONSOLE_AGENT_SIGNING_SECRET': 'test-signing-secret',\n"
                "    }\n",
                encoding="utf-8",
            )
            (temp_root / "shared" / "service_identity.py").write_text(
                service_identity_source
                or (
                    "def build_console_identity_headers(**_kwargs):\n"
                    "    return {}\n\n"
                    "def validate_service_identity_secrets(**_kwargs):\n"
                    "    return None\n"
                ),
                encoding="utf-8",
            )
            (temp_root / "sitecustomize.py").write_text(
                "import json\n"
                "import os\n"
                "from urllib.error import HTTPError\n"
                "import urllib.request\n\n"
                "class _Response:\n"
                "    def __init__(self, status, payload):\n"
                "        self.status = status\n"
                "        self._payload = payload\n"
                "    def __enter__(self):\n"
                "        return self\n"
                "    def __exit__(self, *_args):\n"
                "        return False\n"
                "    def read(self):\n"
                "        return self._payload.encode('utf-8')\n\n"
                "def _urlopen(request, timeout):\n"
                "    del timeout\n"
                "    status = int(os.environ['SMOKE_TEST_STATUS'])\n"
                "    if status != 200:\n"
                "        raise HTTPError(request.full_url, status, 'closed fixture', None, None)\n"
                "    return _Response(status, os.environ['SMOKE_TEST_PAYLOAD'])\n\n"
                "urllib.request.urlopen = _urlopen\n",
                encoding="utf-8",
            )
            identity_file = temp_root / "identity.env"
            identity_file.touch()
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(temp_root),
                "BOYI_IDENTITY_ENV_FILE": str(identity_file),
                "BOYI_DEPLOYED_ROOT": str(temp_root),
                "SMOKE_TEST_STATUS": str(response_status),
                "SMOKE_TEST_PAYLOAD": json.dumps(payload),
            }
            for name in ("SYSTEMROOT", "WINDIR"):
                if os.environ.get(name):
                    environment[name] = os.environ[name]
            return subprocess.run(
                [sys.executable, "-c", _service_identity_smoke_source()],
                cwd=REPOSITORY_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
    finally:
        if not task_tmp_preexisting:
            try:
                task_tmp_root.rmdir()
            except OSError:
                pass


def _run_rollback_fault_harness(
    *,
    fail_agent_restart: bool,
    fail_stage_cleanup: bool = False,
    fail_rollback_health: bool = False,
    fail_final_rollback_health: bool = False,
    migrations_attempted: bool = False,
    runtime_start_attempted: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str], bool, bool, bool]:
    release_script = REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh"
    task_tmp_root = REPOSITORY_ROOT / ".task_tmp"
    task_tmp_preexisting = task_tmp_root.exists()
    task_tmp_root.mkdir(exist_ok=True)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary = tempfile.TemporaryDirectory(dir=task_tmp_root)
        temp_root = Path(temporary.name)
        stage_root = temp_root / "release-aaaaaaaaaaaa-20260815192447"
        backup_root = stage_root / "_rollback"
        backup_root.mkdir(parents=True)
        (stage_root / "partial-delete-me").touch()
        (stage_root / "preserve-after-partial-delete").touch()
        (backup_root / "release_sha.absent").touch()
        (backup_root / "automation_plugin_release.env.absent").touch()
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
            fail_stage_cleanup="$4"
            migrations_attempted="$5"
            runtime_start_attempted="$6"
            fail_rollback_health="$7"
            fail_final_rollback_health="$8"
            stage_root="${temp_root}/release-aaaaaaaaaaaa-20260815192447"
            events_path="${temp_root}/events.log"
            source "${release_script}" "${stage_root}" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" shared 0 0

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
            DEPLOY_ROOT="${temp_root}"
            VENV_ROOT="${temp_root}/venvs"
            RELEASE_VENV="${VENV_ROOT}/runtime-deps-new"
            CREATED_VENV="${fail_agent_restart}"
            VENV_ACTIVATED=0
            SERVICES_QUIESCED=1
            MUTATION_STARTED=1
            MIGRATIONS_ATTEMPTED="${migrations_attempted}"
            NEW_RUNTIME_START_ATTEMPTED="${runtime_start_attempted}"
            RELEASE_STAGE="check_health"
            SCHEDULER_RELEASE_HOLD_FILE="${stage_root}/scheduler-release.pause"
            printf '%s\n' "${RELEASE_SHA}" >"${SCHEDULER_RELEASE_HOLD_FILE}"
            SCHEDULER_RELEASE_HOLD_CREATED=1
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

            rm() {
              if [[ "$#" == "2" && "$1" == "--" && \
                "$2" == "${SCHEDULER_RELEASE_HOLD_FILE}" ]]; then
                printf 'hold-cleared\n' >>"${events_path}"
              fi
              if [[ "${fail_stage_cleanup}" == "1" && "$#" == "3" && \
                "$1" == "-rf" && "$2" == "--" && "$3" == "${stage_root}" ]]; then
                command rm -f -- "${stage_root}/partial-delete-me"
                printf 'cleanup-partial\n' >>"${events_path}"
                return 1
              fi
              command rm "$@"
            }

            check_rollback_health() {
              rollback_health_calls="$((${rollback_health_calls:-0} + 1))"
              local hold_present=0
              [[ -f "${SCHEDULER_RELEASE_HOLD_FILE}" ]] && hold_present=1
              printf 'rollback-health:%s:hold=%s\n' \
                "${rollback_health_calls}" "${hold_present}" >>"${events_path}"
              if [[ "${rollback_health_calls}" == "1" && \
                "${fail_rollback_health}" == "1" ]]; then
                return 1
              fi
              if [[ "${rollback_health_calls}" == "2" && \
                "${fail_final_rollback_health}" == "1" ]]; then
                return 1
              fi
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
            "1" if fail_stage_cleanup else "0",
            "1" if migrations_attempted else "0",
            "1" if runtime_start_attempted else "0",
            "1" if fail_rollback_health else "0",
            "1" if fail_final_rollback_health else "0",
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
            (stage_root / "scheduler-release.pause").exists(),
        )
        return result
    finally:
        if temporary is not None:
            temporary.cleanup()
        if not task_tmp_preexisting and task_tmp_root.exists():
            task_tmp_root.rmdir()


def _run_sourced_release_harness(
    body: str,
    *,
    emergency_override: bool = False,
    target_csv: str = "shared",
) -> subprocess.CompletedProcess[str]:
    release_script = REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh"
    task_tmp_root = REPOSITORY_ROOT / ".task_tmp"
    task_tmp_preexisting = task_tmp_root.exists()
    task_tmp_root.mkdir(exist_ok=True)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary = tempfile.TemporaryDirectory(dir=task_tmp_root)
        temp_root = Path(temporary.name)
        stage_root = temp_root / "stage"
        (stage_root / "agent" / "migrations").mkdir(parents=True)
        harness_path = temp_root / "release_function_harness.sh"
        emergency_argument = (
            " '--emergency-scheduled-window-override=emergency_user_authorized'"
            if emergency_override
            else ""
        )
        harness_path.write_text(
            textwrap.dedent(
                f"""
                release_script="$1"
                stage_root="$2"
                source "${{release_script}}" "${{stage_root}}" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" {target_csv} 0 0{emergency_argument}
                {body}
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        command = [
            "bash",
            _path_for_bash(harness_path),
            _path_for_bash(release_script),
            _path_for_bash(stage_root),
        ]
        if os.name == "nt":
            command = ["wsl.exe", "-d", "Ubuntu", "--", *command]
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
        if not task_tmp_preexisting and task_tmp_root.exists():
            task_tmp_root.rmdir()


def _run_remote_release_argument_harness(
    *remote_args: str,
) -> subprocess.CompletedProcess[str]:
    release_script = REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh"
    task_tmp_root = REPOSITORY_ROOT / ".task_tmp"
    task_tmp_preexisting = task_tmp_root.exists()
    task_tmp_root.mkdir(exist_ok=True)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary = tempfile.TemporaryDirectory(dir=task_tmp_root)
        stage_root = Path(temporary.name) / "stage"
        stage_root.mkdir()
        command = [
            "bash",
            "-c",
            (
                'release_script="$1"; stage_root="$2"; shift 2; '
                'source "${release_script}" "${stage_root}" '
                '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" shared 0 0 "$@"; '
                'printf "override=%s\\n" "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}"'
            ),
            "bash",
            _path_for_bash(release_script),
            _path_for_bash(stage_root),
            *remote_args,
        ]
        if os.name == "nt":
            command = ["wsl.exe", "-d", "Ubuntu", "--", *command]
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
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
    def test_console_only_release_has_no_agent_or_shared_runtime_scope(self):
        completed = _run_sourced_release_harness(
            r"""
            printf 'targets=%s\n' "${RUNTIME_TARGETS[*]}"
            printf 'scopes=%s\n' "${SCOPES[*]}"
            printf 'agent=%s coordinated=%s\n' "${AGENT_RELEASE}" "${COORDINATED_RELEASE}"
            """,
            target_csv="console",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("targets=console", completed.stdout)
        self.assertIn("scopes=console", completed.stdout)
        self.assertIn("agent=0 coordinated=0", completed.stdout)

    def test_agent_only_release_has_no_console_or_shared_runtime_scope(self):
        completed = _run_sourced_release_harness(
            r"""
            printf 'targets=%s\n' "${RUNTIME_TARGETS[*]}"
            printf 'scopes=%s\n' "${SCOPES[*]}"
            printf 'agent=%s coordinated=%s\n' "${AGENT_RELEASE}" "${COORDINATED_RELEASE}"
            """,
            target_csv="agent",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("targets=agent", completed.stdout)
        self.assertIn("scopes=agent", completed.stdout)
        self.assertIn("agent=1 coordinated=0", completed.stdout)

    def test_shared_release_coordinates_both_services_and_all_source_scopes(self):
        completed = _run_sourced_release_harness(
            r"""
            printf 'targets=%s\n' "${RUNTIME_TARGETS[*]}"
            printf 'scopes=%s\n' "${SCOPES[*]}"
            printf 'agent=%s coordinated=%s\n' "${AGENT_RELEASE}" "${COORDINATED_RELEASE}"
            """,
            target_csv="shared",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("targets=agent console", completed.stdout)
        self.assertIn("scopes=shared agent console", completed.stdout)
        self.assertIn("agent=1 coordinated=1", completed.stdout)

    def test_running_plugin_call_is_waited_for_before_shutdown_only(self):
        completed = _run_sourced_release_harness(
            r"""
            PROTECTED_WRITE_POLL_SECONDS=1
            sleep() { :; }
            counter="${stage_root}/quiesce_calls"
            echo 0 >"${counter}"
            run_staged_migration_runner() {
              local calls=$(( $(cat "${counter}") + 1 ))
              echo "${calls}" >"${counter}"
              if (( calls < 3 )); then
                echo "protected_write_quiesce=blocked reason=PROTECTED_WRITE_RUNNING count=1" >&2
                return 1
              fi
              echo "protected_write_quiesce=ok running_writes=0"
            }
            preflight_scheduled_write_window() { echo "window_rechecked"; }
            preflight_running_protected_writes || exit 9
            echo "calls=$(cat "${counter}")"
            MUTATION_STARTED=1
            echo 0 >"${counter}"
            run_staged_migration_runner() {
              echo $(( $(cat "${counter}") + 1 )) >"${counter}"
              echo "protected_write_quiesce=blocked reason=PROTECTED_WRITE_RUNNING count=1" >&2
              return 1
            }
            if preflight_running_protected_writes; then echo "post_mutation=passed"; else echo "post_mutation=blocked calls=$(cat "${counter}")"; fi
            """,
            target_csv="agent",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("protected_write_quiesce=waiting waited_seconds=0", completed.stdout)
        self.assertIn("protected_write_quiesce=waiting waited_seconds=1", completed.stdout)
        self.assertIn("window_rechecked", completed.stdout)
        self.assertIn("calls=3", completed.stdout)
        self.assertIn("post_mutation=blocked calls=1", completed.stdout)

    def test_unexpected_quiesce_failures_are_never_waited_for(self):
        completed = _run_sourced_release_harness(
            r"""
            counter="${stage_root}/quiesce_calls"
            echo 0 >"${counter}"
            run_staged_migration_runner() {
              echo $(( $(cat "${counter}") + 1 )) >"${counter}"
              echo "protected_write_quiesce=blocked reason=QUIESCE_RUNTIME_ERROR count=1" >&2
              return 1
            }
            if preflight_running_protected_writes; then echo passed; else echo "blocked calls=$(cat "${counter}")"; fi
            PROTECTED_WRITE_WAIT_SECONDS=2
            PROTECTED_WRITE_POLL_SECONDS=1
            sleep() { :; }
            echo 0 >"${counter}"
            run_staged_migration_runner() {
              echo $(( $(cat "${counter}") + 1 )) >"${counter}"
              echo "protected_write_quiesce=blocked reason=PROTECTED_WRITE_RUNNING count=1" >&2
              return 1
            }
            if preflight_running_protected_writes; then echo passed; else echo "bounded calls=$(cat "${counter}")"; fi
            """,
            target_csv="agent",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("blocked calls=1", completed.stdout)
        self.assertIn("bounded calls=3", completed.stdout)

    def test_console_only_execution_skips_agent_gates_and_shared_mutations(self):
        completed = _run_sourced_release_harness(
            r"""
            ROOTS[agent]="${stage_root}/production-agent"
            mkdir -p "${ROOTS[agent]}/runtime"
            for name in \
              acquire_release_lock validate_environment backup_managed_sources \
              run_static_preflight quiesce_runtime_services install_service_units \
              restart_services check_health cleanup_successful_release; do
              eval "${name}() { printf '%s\\n' '${name}'; }"
            done
            sync_scope() { printf 'sync_scope:%s\n' "$1"; }
            run_release
            """,
            target_csv="console",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("sync_scope:console", completed.stdout)
        for forbidden in (
            "preflight_running_protected_writes",
            "preflight_scheduled_write_window",
            "build_release_virtualenvs",
            "apply_migrations",
            "activate_release_virtualenvs",
            "write_automation_plugin_runtime_environment",
            "activate_scheduler_after_release",
        ):
            self.assertNotIn(forbidden, completed.stdout)

    def test_agent_only_execution_keeps_agent_gates_but_skips_shared_mutations(self):
        completed = _run_sourced_release_harness(
            r"""
            ROOTS[agent]="${stage_root}/production-agent"
            mkdir -p "${ROOTS[agent]}/runtime"
            for name in \
              acquire_release_lock validate_environment \
              preflight_service_identity_configuration preflight_scheduled_write_window \
              backup_managed_sources run_static_preflight \
              preflight_running_protected_writes capture_domain_database_split_state \
              quiesce_runtime_services install_service_units \
              write_automation_plugin_runtime_environment ensure_scheduler_release_hold \
              restart_services check_health activate_scheduler_after_release \
              cleanup_successful_release; do
              eval "${name}() { printf '%s\\n' '${name}'; }"
            done
            sync_scope() { printf 'sync_scope:%s\n' "$1"; }
            run_release
            """,
            target_csv="agent",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("sync_scope:agent", completed.stdout)
        for required in (
            "preflight_scheduled_write_window",
            "preflight_running_protected_writes",
            "write_automation_plugin_runtime_environment",
            "ensure_scheduler_release_hold",
        ):
            self.assertIn(required, completed.stdout)
        for forbidden in (
            "build_release_virtualenvs",
            "apply_migrations",
            "activate_release_virtualenvs",
            "check_service_identity_smoke",
            "sync_scope:console",
        ):
            self.assertNotIn(forbidden, completed.stdout)

    def test_ci_and_production_use_python_310_locked_environments(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "python -m pip install -r agent/requirements.lock -r console/requirements.lock",
            workflow,
        )
        self.assertIn("verify_locked_environment.py agent/requirements.lock", workflow)
        self.assertIn("verify_locked_environment.py console/requirements.lock", workflow)
        self.assertNotIn("windows-worker-quality:", workflow)
        agent_gate = workflow.split("agent-quality:", 1)[1].split("console-quality:", 1)[0]
        self.assertIn("runs-on: ubuntu-22.04", agent_gate)
        self.assertIn("python3.10 python3.10-venv", agent_gate)
        self.assertIn('/usr/bin/python3.10 -m venv --copies "$ci_runtime"', agent_gate)
        self.assertIn("assert sys.version_info[:2] == (3, 10)", agent_gate)
        self.assertIn("release_scope.py quality-files --null", agent_gate)
        self.assertIn("release_scope.py test-files --suite root --null", agent_gate)

        release = (REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh").read_text(encoding="utf-8")
        execution = release.split("trap rollback ERR", 1)[1]
        run_release_prefix = release.split("run_release() {", 1)[1].split("trap rollback ERR", 1)[0]
        self.assertIn('RELEASE_STAGE="acquire_release_lock"', run_release_prefix)
        self.assertIn('RELEASE_LOCK_FILE="${DEPLOY_ROOT}/release.lock"', release)
        self.assertIn('SCHEDULER_RELEASE_HOLD_FILE="${DEPLOY_ROOT}/scheduler-release.pause"', release)
        self.assertIn("flock -n 9", release)
        for retired in ("WINDOWS_WORKER", "first_party", "preflight_signed", "capture_control_plane_release_state"):
            self.assertNotIn(retired, release)
        mutation = execution.index("MUTATION_STARTED=1")
        self.assertLess(execution.index("preflight_service_identity_configuration"),
                        execution.index("backup_managed_sources"))
        self.assertEqual(2, execution.count("preflight_scheduled_write_window\n"))
        self.assertLess(execution.index('RELEASE_STAGE="preflight_scheduled_write_window_before_mutation"'), mutation)
        self.assertLess(execution.index("build_release_virtualenvs"), mutation)
        self.assertLess(execution.index("capture_domain_database_split_state"), mutation)
        self.assertEqual(4, execution.count("preflight_running_protected_writes\n"))
        self.assertLess(mutation, execution.index('RELEASE_STAGE="quiesce_runtime_services"'))
        self.assertLess(execution.index('RELEASE_STAGE="quiesce_runtime_services"'),
                        execution.index('RELEASE_STAGE="verify_protected_writes_quiesced"'))
        self.assertLess(execution.index("write_automation_plugin_runtime_environment"),
                        execution.index('RELEASE_STAGE="restart_services"'))

    def test_single_service_restart_targets_only_the_selected_unit(self):
        for target, expected, forbidden in (
            ("agent", "agent.service", "console.service"),
            ("console", "console.service", "agent.service"),
        ):
            with self.subTest(target=target):
                completed = _run_sourced_release_harness(
                    r"""
                    sudo() { printf 'sudo:%s\n' "$*"; }
                    systemctl() {
                      printf 'systemctl:%s\n' "$*"
                      [[ "$1" == "is-active" ]]
                    }
                    restart_services
                    """,
                    target_csv=target,
                )

                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn(expected, completed.stdout)
                self.assertNotIn(forbidden, completed.stdout)

    def test_publish_scope_auto_prioritizes_shared_and_explicit_narrow_targets_fail_closed(self):
        source = (
            REPOSITORY_ROOT / "agent" / "deploy" / "publish_to_ecs.ps1"
        ).read_text(encoding="utf-8")
        resolver = source.split("function Resolve-Targets", 1)[1].split(
            "\n}\n\nAssert-Command", 1
        )[0]

        self.assertIn('if ($sharedChanged) {\n                return @("shared")', resolver)
        self.assertEqual(
            2,
            resolver.count(
                'conflicts with changed shared/dependency/migration scope'
            ),
        )
        self.assertIn('"all" { return @("shared") }', resolver)
        self.assertIn('"shared" { return @("shared") }', resolver)

    def test_agent_and_console_are_uploaded_released_and_recorded_independently(self):
        publisher = (
            REPOSITORY_ROOT / "agent" / "deploy" / "publish_to_ecs.ps1"
        ).read_text(encoding="utf-8")
        publish_loop = publisher.split(
            "foreach ($publishTarget in $targetsToPublish) {",
            1,
        )[1]

        self.assertNotIn("$targetCsv", publisher)
        self.assertIn("$remoteStageBaseTime = Get-Date", publisher)
        self.assertIn("$remoteStageSequence = 0", publisher)
        self.assertIn(
            '$remoteStageTimestamp = $remoteStageBaseTime.AddSeconds($remoteStageSequence).ToString("yyyyMMddHHmmss")',
            publish_loop,
        )
        self.assertIn("$remoteStageSequence += 1", publish_loop)
        self.assertIn(
            '$remoteStageReleaseId = "release-$($releaseSha.Substring(0, 12))-$remoteStageTimestamp"',
            publish_loop,
        )
        self.assertIn(
            '$remoteStage = "${RemoteDeployRoot}/${remoteStageReleaseId}"',
            publish_loop,
        )
        self.assertNotIn(
            '$remoteStage = "${RemoteDeployRoot}/${releaseId}"',
            publish_loop,
        )
        self.assertIn('& scp @scpArgs -r $PayloadRoot "${remoteSpec}:${remoteStage}"', publish_loop)
        self.assertIn("'$publishTarget' '$skipRestartValue' '$skipHealthValue'", publish_loop)
        self.assertLess(
            publish_loop.index("Invoke-Remote $remoteReleaseCommand"),
            publish_loop.index("Save-State $state"),
        )
        self.assertIn('$state["lastPublishedTarget"] = $publishTarget', publish_loop)

    def test_remote_release_rejects_joint_agent_console_target(self):
        completed = _run_sourced_release_harness("", target_csv="agent,console")

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "Agent and Console releases require separate remote invocations",
            completed.stderr,
        )

    def test_publish_shared_scope_contains_every_migration_runner_helper(self):
        publisher = (
            REPOSITORY_ROOT / "agent" / "deploy" / "publish_to_ecs.ps1"
        ).read_text(encoding="utf-8")
        shared_paths_source = publisher.split("$sharedAgentPaths = @(", 1)[1].split(
            "\n    )",
            1,
        )[0]
        shared_paths = set(re.findall(r'"([^"]+)"', shared_paths_source))
        migration_runner = (
            REPOSITORY_ROOT / "agent" / "scripts" / "run_migrations.py"
        ).read_text(encoding="utf-8")
        helper_filenames = set(
            re.findall(
                r'(?:with_name|_load_script_helper)\(\s*"([^"]+\.py)"',
                migration_runner,
            )
        )
        expected_helpers = {
            "business_module_migration_contract.py",
            "migration_055_domain_databases.py",
            "migration_064_retire_pre_v2.py",
            "migration_connection.py",
            "scheduled_write_window.py",
        }

        self.assertEqual(expected_helpers, helper_filenames)
        self.assertTrue(
            {f"agent/scripts/{filename}" for filename in helper_filenames}
            <= shared_paths
        )
        self.assertNotIn("agent/scripts/", shared_paths)

    def test_nginx_allows_the_signed_plugin_limit_plus_multipart_overhead(self):
        nginx = (
            REPOSITORY_ROOT / "agent" / "deploy" / "nginx" / "boyi.homes.conf"
        ).read_text(encoding="utf-8")

        self.assertEqual(2, nginx.count("client_max_body_size 34m;"))
        self.assertNotIn("client_max_body_size 25m;", nginx)

    def test_service_identity_smoke_closes_project_import_failure(self):
        sensitive_marker = "SENSITIVE_SERVICE_IDENTITY_IMPORT_MARKER"
        completed = _run_service_identity_smoke(
            _healthy_service_identity_payload(),
            service_identity_source=(
                f"raise RuntimeError({sensitive_marker!r})\n"
            ),
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertEqual(
            "service_identity_smoke=failed reason=identity_configuration\n",
            completed.stderr,
        )
        self.assertNotIn(sensitive_marker, completed.stdout)
        self.assertNotIn(sensitive_marker, completed.stderr)

    def test_service_identity_smoke_reports_closed_http_gates(self):
        for status, expected in (
            (401, "http_401"),
            (403, "http_403"),
            (503, "http_5xx"),
            (429, "http_other"),
        ):
            with self.subTest(status=status):
                completed = _run_service_identity_smoke(
                    _healthy_service_identity_payload(),
                    response_status=status,
                )
                self.assertEqual(1, completed.returncode)
                self.assertEqual(
                    f"service_identity_smoke=failed reason={expected}",
                    completed.stderr.strip(),
                )
                self.assertNotIn("SMOKE_BODY_MUST_NOT_APPEAR", completed.stdout)
                self.assertNotIn("SMOKE_BODY_MUST_NOT_APPEAR", completed.stderr)

    def test_service_identity_smoke_reports_closed_component_gates(self):
        cases = (
            ("response_contract", ("ok",), False),
            ("scheduler_state", ("scheduler", "state"), "running"),
            ("scheduler_hold", ("scheduler", "release_hold"), False),
            ("runner_state", ("workflow_runner", "state"), "running"),
            ("runner_hold", ("workflow_runner", "release_hold"), False),
            ("runner_active", ("workflow_runner", "active_runs"), 1),
            ("plugin_broker", ("automation_plugins", "broker", "state"), "stopped"),
            ("plugin_sandbox", ("automation_plugins", "sandbox", "state"), "unavailable"),
            (
                "plugin_catalog_aggregate_or_shape",
                ("automation_plugins", "catalog", "ok"),
                False,
            ),
            (
                "plugin_generations",
                ("automation_plugins", "generations", "healthy"),
                "invalid",
            ),
            ("plugin_aggregate", ("automation_plugins", "ok"), False),
        )
        for expected, path, value in cases:
            with self.subTest(gate=expected):
                payload = _healthy_service_identity_payload()
                target = payload
                if path[0] != "ok":
                    target = payload["data"]["components"]
                for field in path[:-1]:
                    target = target[field]
                target[path[-1]] = value
                completed = _run_service_identity_smoke(payload)
                self.assertEqual(1, completed.returncode)
                self.assertEqual(
                    f"service_identity_smoke=failed reason={expected}",
                    completed.stderr.strip(),
                )
                self.assertNotIn("SMOKE_BODY_MUST_NOT_APPEAR", completed.stdout)
                self.assertNotIn("SMOKE_BODY_MUST_NOT_APPEAR", completed.stderr)

    def test_service_identity_smoke_reports_closed_plugin_catalog_subgates(self):
        sensitive_marker = "SENSITIVE_PLUGIN_CATALOG_MARKER"
        cases = (
            ("plugin_catalog_unsupported", "unsupported_automation_ids"),
            ("plugin_catalog_invalid_trust", "invalid_enabled_trust"),
            ("plugin_catalog_invalid_runtime", "invalid_enabled_runtime"),
        )
        for expected, field_name in cases:
            with self.subTest(gate=expected):
                payload = _healthy_service_identity_payload()
                catalog = payload["data"]["components"]["automation_plugins"]["catalog"]
                catalog[field_name] = [sensitive_marker]
                completed = _run_service_identity_smoke(payload)

                self.assertEqual(1, completed.returncode)
                self.assertEqual("", completed.stdout)
                self.assertEqual(
                    f"service_identity_smoke=failed reason={expected}\n",
                    completed.stderr,
                )
                self.assertNotIn(sensitive_marker, completed.stdout)
                self.assertNotIn(sensitive_marker, completed.stderr)

    def test_service_identity_smoke_accepts_explicitly_degraded_plugins(self):
        payload = _healthy_service_identity_payload()
        plugins = payload["data"]["components"]["automation_plugins"]
        plugins["runnable"] = False
        plugins["runtime_status"] = "UNAVAILABLE"
        plugins["catalog"]["runnable"] = False
        plugins["catalog"]["runtime_status"] = "UNAVAILABLE"
        plugins["catalog"]["unstable_generations"] = ["blocked-project"]
        plugins["generations"]["healthy"] = False
        plugins["generations"]["blocked_projects"] = {
            "blocked-project": ["WRITE_OUTCOME_UNKNOWN"]
        }

        completed = _run_service_identity_smoke(payload)

        self.assertEqual(0, completed.returncode)
        self.assertEqual("service_identity_smoke=ok", completed.stdout.strip())

    def test_service_identity_smoke_closes_plugin_catalog_shape_without_leakage(self):
        sensitive_marker = "SENSITIVE_PLUGIN_CATALOG_SHAPE_MARKER"
        payload = _healthy_service_identity_payload()
        payload["data"]["components"]["automation_plugins"]["catalog"] = {
            "ok": False,
            "diagnostic": sensitive_marker,
        }

        completed = _run_service_identity_smoke(payload)

        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertEqual(
            "service_identity_smoke=failed "
            "reason=plugin_catalog_aggregate_or_shape\n",
            completed.stderr,
        )
        self.assertNotIn(sensitive_marker, completed.stdout)
        self.assertNotIn(sensitive_marker, completed.stderr)

    def test_service_identity_smoke_success_contract_is_unchanged(self):
        completed = _run_service_identity_smoke(_healthy_service_identity_payload())

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("service_identity_smoke=ok", completed.stdout.strip())
        self.assertEqual("", completed.stderr)

    def test_direct_dependencies_are_covered_by_exact_locks(self):
        for service in ("agent", "console"):
            direct = _requirement_names(REPOSITORY_ROOT / service / "requirements.txt")
            locked = _requirement_names(REPOSITORY_ROOT / service / "requirements.lock")
            self.assertTrue(direct)
            self.assertTrue(direct <= locked, direct - locked)

    def test_health_failure_with_reused_venv_stops_and_recovers_both_services(self):
        completed, events, stage_exists, _new_venv_exists, hold_exists = (
            _run_rollback_fault_harness(fail_agent_restart=False)
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("release_error stage=check_health", completed.stderr)
        self.assertNotIn("rollback_incomplete", completed.stderr)
        self.assertFalse(stage_exists)
        self.assertFalse(hold_exists)
        self.assertEqual(["stop:agent.service", "stop:console.service"], events[:2])
        self.assertIn("venv:agent", events)
        self.assertIn("venv:console", events)
        self.assertNotIn("restore-014", events)
        self.assertLess(events.index("restart:agent.service"), events.index("restart:console.service"))
        self.assertIn("rollback-health:1:hold=1", events)
        self.assertIn("rollback-health:2:hold=0", events)
        self.assertLess(
            events.index("rollback-health:1:hold=1"),
            events.index("hold-cleared"),
        )
        self.assertLess(
            events.index("hold-cleared"),
            events.index("rollback-health:2:hold=0"),
        )
        self.assertEqual(2, events.count("restart:agent.service"))
        self.assertEqual(2, events.count("restart:console.service"))

    def test_unhealthy_restored_runtime_is_stopped_and_stage_is_preserved(self):
        completed, events, stage_exists, _new_venv_exists, hold_exists = (
            _run_rollback_fault_harness(
                fail_agent_restart=False,
                fail_rollback_health=True,
            )
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("Rollback runtime failed its held stable health gate", completed.stderr)
        self.assertIn("rollback_incomplete", completed.stderr)
        self.assertTrue(stage_exists)
        self.assertTrue(hold_exists)
        self.assertIn("rollback-health:1:hold=1", events)
        self.assertNotIn("hold-cleared", events)
        self.assertIn("restart:agent.service", events)
        self.assertIn("restart:console.service", events)
        self.assertGreaterEqual(events.count("stop:agent.service"), 2)
        self.assertGreaterEqual(events.count("stop:console.service"), 2)

    def test_unhealthy_unheld_rollback_runtime_is_stopped_and_stage_is_preserved(self):
        completed, events, stage_exists, _new_venv_exists, hold_exists = (
            _run_rollback_fault_harness(
                fail_agent_restart=False,
                fail_final_rollback_health=True,
            )
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("Rollback runtime failed its final stable health gate", completed.stderr)
        self.assertIn("rollback_incomplete", completed.stderr)
        self.assertTrue(stage_exists)
        self.assertFalse(hold_exists)
        self.assertIn("rollback-health:1:hold=1", events)
        self.assertIn("rollback-health:2:hold=0", events)
        self.assertEqual(2, events.count("restart:agent.service"))
        self.assertEqual(2, events.count("restart:console.service"))
        self.assertGreaterEqual(events.count("stop:agent.service"), 3)
        self.assertGreaterEqual(events.count("stop:console.service"), 3)

    def test_rollback_health_requires_exact_restored_sha_and_two_stable_checks(self):
        completed = _run_sourced_release_harness(
            r"""
            ROOTS[agent]="${stage_root}/restored-agent"
            mkdir -p "${ROOTS[agent]}/runtime"
            printf '%s\n' 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
              >"${ROOTS[agent]}/runtime/release_sha"
            REQUESTED_TARGETS=(agent console)
            RUNTIME_TARGETS=(agent console)
            SERVICES_QUIESCED=1
            SKIP_HEALTH=1
            health_calls=0
            check_health() {
              [[ "${RELEASE_SHA}" == \
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' ]]
              [[ "${SKIP_HEALTH}" == "0" ]]
              health_calls="$((health_calls + 1))"
            }
            systemctl() {
              if [[ "$1" == "show" && "$2" == "agent.service" && \
                "$3" == "-p" && "$4" == "MainPID" ]]; then
                printf '101\n'
              elif [[ "$1" == "show" && "$2" == "console.service" && \
                "$3" == "-p" && "$4" == "MainPID" ]]; then
                printf '202\n'
              elif [[ "$1" == "show" && "$3" == "-p" && \
                "$4" == "NRestarts" ]]; then
                printf '0\n'
              else
                return 1
              fi
            }
            sleep() { :; }
            check_rollback_health
            printf 'health_calls=%s\n' "${health_calls}"
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "rollback_health=ok "
            "release_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            completed.stdout,
        )
        self.assertIn("health_calls=2", completed.stdout)

    def test_rollback_health_rejects_noncanonical_restored_sha(self):
        completed = _run_sourced_release_harness(
            r"""
            ROOTS[agent]="${stage_root}/restored-agent"
            mkdir -p "${ROOTS[agent]}/runtime"
            printf '%s\n' 'bbbbbbb' >"${ROOTS[agent]}/runtime/release_sha"
            check_health() {
              printf 'health-must-not-run\n'
              return 0
            }
            check_rollback_health
            """
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "rollback_health=failed reason=RESTORED_RELEASE_SHA_INVALID",
            completed.stderr,
        )
        self.assertNotIn("health-must-not-run", completed.stdout)

    def test_incomplete_rollback_attempts_both_restarts_and_preserves_recovery_material(self):
        completed, events, stage_exists, new_venv_exists, _hold_exists = (
            _run_rollback_fault_harness(fail_agent_restart=True)
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("rollback_incomplete", completed.stderr)
        self.assertIn("recovery_material_preserved=1", completed.stderr)
        self.assertTrue(stage_exists)
        self.assertTrue(new_venv_exists)
        self.assertIn("restart:agent.service", events)
        self.assertIn("restart:console.service", events)

    def test_partial_stage_cleanup_failure_reports_unknown_recovery_material_state(self):
        completed, events, stage_exists, _new_venv_exists, _hold_exists = (
            _run_rollback_fault_harness(
                fail_agent_restart=False,
                fail_stage_cleanup=True,
            )
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("cleanup-partial", events)
        self.assertTrue(stage_exists)
        self.assertIn("rollback_cleanup_incomplete", completed.stderr)
        self.assertIn("recovery_material_state=unknown", completed.stderr)
        self.assertIn("verify_required=1", completed.stderr)
        self.assertNotIn("recovery_material_preserved=1", completed.stderr)

    def test_nonmutating_partial_stage_cleanup_failure_reports_unknown_state(self):
        completed = _run_sourced_release_harness(
            r"""
            DEPLOY_ROOT="$(dirname "${stage_root}")/deploy"
            STAGE_ROOT="${DEPLOY_ROOT}/release-aaaaaaaaaaaa-20260815192447"
            BACKUP_DIR="${STAGE_ROOT}/_rollback"
            mkdir -p "${BACKUP_DIR}"
            printf 'delete\n' >"${STAGE_ROOT}/partial-delete-me"
            printf 'preserve\n' >"${STAGE_ROOT}/preserve-after-partial-delete"
            MUTATION_STARTED=0
            RELEASE_STAGE="preflight"
            clear_scheduler_release_hold_for_rollback() { return 0; }
            remove_new_virtualenvs() { return 0; }
            rm() {
              if [[ "$#" == "3" && "$1" == "-rf" && "$2" == "--" && \
                "$3" == "${STAGE_ROOT}" ]]; then
                command rm -f -- "${STAGE_ROOT}/partial-delete-me"
                return 1
              fi
              command rm "$@"
            }
            set +e
            false
            rollback
            """
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("release_cleanup_incomplete", completed.stderr)
        self.assertIn("recovery_material_state=unknown", completed.stderr)
        self.assertIn("verify_required=1", completed.stderr)
        self.assertNotIn("recovery_material_preserved=1", completed.stderr)

    def test_rollback_does_not_revert_cutover_applied_before_this_release(self):
        completed, events, stage_exists, _new_venv_exists, hold_exists = (
            _run_rollback_fault_harness(
                fail_agent_restart=False,
            )
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("rollback_incomplete", completed.stderr)
        self.assertNotIn("restore-014", events)
        self.assertFalse(stage_exists)
        self.assertFalse(hold_exists)
        self.assertIn("restart:agent.service", events)
        self.assertIn("restart:console.service", events)

    def test_release_includes_local_modules_imported_by_agent_entrypoint(self):
        publisher = (REPOSITORY_ROOT / "agent/deploy/publish_to_ecs.ps1").read_text(encoding="utf-8")
        allowed = set(re.findall(r'"([^"\n]+)"', publisher.split("$AgentFiles = @(", 1)[1].split("\n)", 1)[0]))
        agent_root = REPOSITORY_ROOT / "agent"
        pending = ["main.py"]
        checked = set()
        while pending:
            filename = pending.pop()
            if filename in checked:
                continue
            checked.add(filename)
            self.assertIn(filename, allowed, f"Agent startup module missing from deployment: {filename}")
            tree = ast.parse((agent_root / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                for module in modules:
                    sibling = module.split(".", 1)[0] + ".py"
                    if (agent_root / sibling).is_file():
                        pending.append(sibling)

    def test_release_keeps_ssh_verification_and_publishes_new_modules(self):
        publisher = (REPOSITORY_ROOT / "agent" / "deploy" / "publish_to_ecs.ps1").read_text(encoding="utf-8")
        publisher_finally = publisher.split("\nfinally {", 1)[1]
        self.assertNotIn("rm -rf", publisher_finally)
        self.assertIn(
            "Remote release failed; verify whether recovery material remains at",
            publisher_finally,
        )
        blocked_extensions = publisher[
            publisher.index("$BlockedExtensions = @(") : publisher.index("function Assert-Command")
        ]
        self.assertNotIn("StrictHostKeyChecking=no", publisher)
        self.assertIn('"app_support.py"', publisher)
        self.assertIn('"navigation.py"', publisher)
        agent_files = publisher.split("$AgentFiles = @(", 1)[1].split("\n)", 1)[0]
        blocked_files = publisher.split("$BlockedFileNames = @(", 1)[1].split(
            "\n)", 1
        )[0]
        blocked_dirs = publisher.split("$BlockedDirNames = @(", 1)[1].split(
            "\n)", 1
        )[0]
        self.assertIn('"harness_composition.py"', agent_files)
        self.assertIn('".env"', blocked_files)
        self.assertIn('"runtime"', blocked_dirs)
        self.assertIn('"service_v2_plugins"', publisher)
        agent_dirs = publisher.split("$AgentDirs = @(", 1)[1].split(")", 1)[0]
        for retired in ("first_party_automation_plugins", "legacy", "price_scripts", "windows_worker"):
            self.assertNotIn(f'"{retired}"', agent_dirs)
        for retired in ("AutomationPluginArtifactRoot", "AutomationPluginTrustRoot", "Copy-AutomationPluginReleaseInputs"):
            self.assertNotIn(retired, publisher)
        self.assertIn(
            "[switch]$EmergencyUserAuthorizedScheduledWindowOverride",
            publisher,
        )
        self.assertIn(
            "--emergency-scheduled-window-override=emergency_user_authorized",
            publisher,
        )
        self.assertNotIn("_plugin_artifacts", publisher)
        self.assertNotIn("_plugin_trust", publisher)
        self.assertNotIn('".key"', publisher)
        self.assertNotIn("BEGIN PRIVATE KEY", publisher)
        self.assertIn('"config", "routes", "services", "static", "templates"', publisher)
        self.assertNotIn('".webp"', blocked_extensions)
        self.assertIn('if ($extension -in @(".png", ".jpg", ".jpeg", ".webp"))', publisher)
        agent_unit = (REPOSITORY_ROOT / "agent" / "agent.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "EnvironmentFile=/home/boyce/agent/runtime/automation_plugin_release.env",
            agent_unit,
        )

    def test_emergency_scheduled_window_override_requires_exact_remote_argument(self):
        without_override = _run_remote_release_argument_harness()
        exact_override = _run_remote_release_argument_harness(
            "--emergency-scheduled-window-override=emergency_user_authorized"
        )

        self.assertEqual(0, without_override.returncode, without_override.stderr)
        self.assertEqual("override=0\n", without_override.stdout)
        self.assertEqual(0, exact_override.returncode, exact_override.stderr)
        self.assertEqual("override=1\n", exact_override.stdout)

        invalid_arguments = (
            "--emergency-scheduled-window-override=",
            "--emergency-scheduled-window-override=not_authorized",
            "--emergency-scheduled-window-override=emergency_user_authorized\n"
            "forged_log=true",
        )
        for invalid_argument in invalid_arguments:
            with self.subTest(invalid_argument=invalid_argument):
                completed = _run_remote_release_argument_harness(invalid_argument)

                self.assertEqual(2, completed.returncode)
                self.assertEqual("", completed.stdout)
                self.assertEqual(
                    "emergency_scheduled_window_override=blocked "
                    "reason=INVALID_AUTHORIZATION_ARGUMENT\n",
                    completed.stderr,
                )
                self.assertNotIn(invalid_argument, completed.stderr)

        unexpected_extra = _run_remote_release_argument_harness(
            "--emergency-scheduled-window-override=emergency_user_authorized",
            "extra",
            "extra",
        )
        self.assertEqual(2, unexpected_extra.returncode)
        self.assertEqual("", unexpected_extra.stdout)
        self.assertEqual(
            "emergency_scheduled_window_override=blocked "
            "reason=UNEXPECTED_ARGUMENT_COUNT\n",
            unexpected_extra.stderr,
        )

    def test_emergency_scheduled_window_override_audits_both_skipped_checks(self):
        completed = _run_sourced_release_harness(
            r"""
            RELEASE_STAGE="preflight_scheduled_write_window"
            preflight_scheduled_write_window
            RELEASE_STAGE="preflight_scheduled_write_window_before_mutation"
            preflight_scheduled_write_window
            """,
            emergency_override=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            "scheduled_write_window=skipped emergency_user_authorized=true "
            "stage=preflight_scheduled_write_window\n"
            "scheduled_write_window=skipped emergency_user_authorized=true "
            "stage=preflight_scheduled_write_window_before_mutation\n",
            completed.stdout,
        )
        self.assertEqual("", completed.stderr)

    def test_scheduled_window_without_emergency_override_remains_fail_closed(self):
        completed = _run_sourced_release_harness(
            r"""
            RELEASE_STAGE="preflight_scheduled_write_window"
            preflight_scheduled_write_window
            """
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("scheduled_write_window=skipped", completed.stdout)
        self.assertIn(
            "scheduled_write_window=blocked reason=PREFLIGHT_RUNNER_MISSING count=1",
            completed.stderr,
        )

    def test_scheduler_release_hold_reuses_and_cleans_only_current_sha(self):
        completed = _run_sourced_release_harness(
            r"""
            SCHEDULER_RELEASE_HOLD_FILE="${stage_root}/scheduler-release.pause"
            SCHEDULER_RELEASE_HOLD_CREATED=0
            create_scheduler_release_hold
            ensure_scheduler_release_hold
            [[ "$(tr -d '[:space:]' <"${SCHEDULER_RELEASE_HOLD_FILE}")" == "${RELEASE_SHA}" ]]
            clear_scheduler_release_hold_for_rollback
            [[ ! -e "${SCHEDULER_RELEASE_HOLD_FILE}" ]]
            [[ "${SCHEDULER_RELEASE_HOLD_CREATED}" == "0" ]]
            echo "scheduler_release_hold_cleanup=ok"
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            "scheduler_release_hold=created\n"
            "scheduler_release_hold=retained\n"
            "scheduler_release_hold_cleanup=ok\n",
            completed.stdout,
        )
        self.assertEqual("", completed.stderr)

    def test_scheduler_release_hold_preserves_owner_mismatch(self):
        completed = _run_sourced_release_harness(
            r"""
            SCHEDULER_RELEASE_HOLD_FILE="${stage_root}/scheduler-release.pause"
            SCHEDULER_RELEASE_HOLD_CREATED=0
            create_scheduler_release_hold
            printf '%s\n' 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' >"${SCHEDULER_RELEASE_HOLD_FILE}"
            if ensure_scheduler_release_hold; then
              exit 91
            fi
            if clear_scheduler_release_hold_for_rollback; then
              exit 92
            fi
            [[ -f "${SCHEDULER_RELEASE_HOLD_FILE}" ]]
            echo "scheduler_release_hold_owner_mismatch=preserved"
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            "scheduler_release_hold=created\n"
            "scheduler_release_hold_owner_mismatch=preserved\n",
            completed.stdout,
        )
        self.assertEqual(
            "scheduler_release_hold=blocked "
            "reason=CURRENT_RELEASE_HOLD_OWNER_MISMATCH\n"
            "Current release scheduler hold has an unexpected owner\n",
            completed.stderr,
        )

    def test_release_writes_closed_plugin_runtime_environment_atomically(self):
        completed = _run_sourced_release_harness(
            r"""
            PLUGIN_RUNTIME_ENV_FILE="${stage_root}/runtime/automation_plugin_release.env"
            AUTOMATION_PLUGIN_ROOT="${stage_root}/plugin-root"
            mkdir -p "${AUTOMATION_PLUGIN_ROOT}/installed"
            write_automation_plugin_runtime_environment
            cat "${PLUGIN_RUNTIME_ENV_FILE}"
            stat -c 'mode=%a' "${PLUGIN_RUNTIME_ENV_FILE}"
            find "$(dirname "${PLUGIN_RUNTIME_ENV_FILE}")" \
              -maxdepth 1 -name '.automation_plugin_release.*' -print
            ln -s "${AUTOMATION_PLUGIN_ROOT}" "${stage_root}/linked-root"
            AUTOMATION_PLUGIN_ROOT="${stage_root}/linked-root"
            if write_automation_plugin_runtime_environment; then echo linked=accepted; else echo linked=refused; fi
            AUTOMATION_PLUGIN_ROOT="${stage_root}/missing-root"
            if write_automation_plugin_runtime_environment; then echo missing=accepted; else echo missing=refused; fi
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual("BOYI_AUTOMATION_PLUGIN_ROOT=" + lines[0].split("=", 1)[1], lines[0])
        self.assertTrue(lines[0].endswith("/plugin-root"))
        self.assertEqual(
            "BOYI_AUTOMATION_PLUGIN_RELEASE_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", lines[1])
        self.assertEqual("mode=600", lines[2])
        self.assertEqual(["linked=refused", "missing=refused"], lines[3:])

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
