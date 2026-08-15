from __future__ import annotations

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
                    "broker": {"state": "running"},
                    "catalog": {
                        "ok": True,
                        "unsupported_automation_ids": [],
                        "enabled_builtin_release": [],
                        "invalid_enabled_trust": [],
                        "unstable_generations": [],
                        "invalid_enabled_runtime": [],
                    },
                    "generations": {"healthy": True},
                },
                "automation_workers": {
                    "enabled": False,
                    "state": "disabled",
                    "release_hold": False,
                    "active_jobs": 0,
                },
                "scheduled_task_approval_bootstrap": {
                    "reviewed_candidates": 0,
                    "created": 0,
                    "already_present": 0,
                    "explicitly_configured": 0,
                    "rejected": 0,
                    "completed": 1,
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
    cutover_pending: bool = True,
    daily_sign_pending: bool = False,
    contract_upgrade_pending: bool = False,
    automation_project_pending: bool = False,
    bootstrap_absent: bool = False,
    migrations_attempted: bool = False,
    runtime_start_attempted: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str], bool, bool]:
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
            cutover_pending="$5"
            daily_sign_pending="$6"
            contract_upgrade_pending="$7"
            automation_project_pending="$8"
            bootstrap_absent="$9"
            migrations_attempted="${10}"
            runtime_start_attempted="${11}"
            stage_root="${temp_root}/release-aaaaaaaaaaaa-20260815192447"
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
            DEPLOY_ROOT="${temp_root}"
            VENV_ROOT="${temp_root}/venvs"
            RELEASE_VENV="${VENV_ROOT}/runtime-deps-new"
            CREATED_VENV="${fail_agent_restart}"
            VENV_ACTIVATED=0
            SERVICES_QUIESCED=1
            MUTATION_STARTED=1
            CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY="${cutover_pending}"
            DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY="${daily_sign_pending}"
            SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY="${contract_upgrade_pending}"
            AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY="${automation_project_pending}"
            CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE="${bootstrap_absent}"
            MIGRATIONS_ATTEMPTED="${migrations_attempted}"
            NEW_RUNTIME_START_ATTEMPTED="${runtime_start_attempted}"
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

            rm() {
              if [[ "${fail_stage_cleanup}" == "1" && "$#" == "3" && \
                "$1" == "-rf" && "$2" == "--" && "$3" == "${stage_root}" ]]; then
                command rm -f -- "${stage_root}/partial-delete-me"
                printf 'cleanup-partial\n' >>"${events_path}"
                return 1
              fi
              command rm "$@"
            }

            restore_control_plane_task_cutover_data() {
              printf 'restore-014\n' >>"${events_path}"
              return 0
            }

            restore_daily_sign_single_tms_data() {
              printf 'restore-016\n' >>"${events_path}"
              return 0
            }

            restore_scheduled_task_contract_upgrade_data() {
              printf 'restore-017\n' >>"${events_path}"
              return 0
            }

            restore_automation_project_authorization_data() {
              printf 'restore-018\n' >>"${events_path}"
              return 0
            }

            restore_control_plane_policy_bootstrap_data() {
              printf 'restore-bootstrap\n' >>"${events_path}"
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
            "1" if cutover_pending else "0",
            "1" if daily_sign_pending else "0",
            "1" if contract_upgrade_pending else "0",
            "1" if automation_project_pending else "0",
            "1" if bootstrap_absent else "0",
            "1" if migrations_attempted else "0",
            "1" if runtime_start_attempted else "0",
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


def _run_sourced_release_harness(
    body: str,
    *,
    emergency_override: bool = False,
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
                source "${{release_script}}" "${{stage_root}}" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" agent,console 0 0{emergency_argument}
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
                '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" agent,console 0 0 "$@"; '
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
            (
                "plugin_catalog_aggregate_or_shape",
                ("automation_plugins", "catalog", "ok"),
                False,
            ),
            (
                "plugin_generations",
                ("automation_plugins", "generations", "healthy"),
                False,
            ),
            ("plugin_aggregate", ("automation_plugins", "ok"), False),
            ("worker", ("automation_workers", "enabled"), True),
            (
                "bootstrap_shape",
                ("scheduled_task_approval_bootstrap",),
                None,
            ),
            (
                "bootstrap_incomplete",
                ("scheduled_task_approval_bootstrap", "completed"),
                0,
            ),
            (
                "bootstrap_rejected",
                ("scheduled_task_approval_bootstrap", "rejected"),
                1,
            ),
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
            ("plugin_catalog_enabled_builtin", "enabled_builtin_release"),
            ("plugin_catalog_invalid_trust", "invalid_enabled_trust"),
            ("plugin_catalog_unstable_generations", "unstable_generations"),
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

    def test_ci_and_production_use_python_310_locked_environments(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertEqual(3, workflow.count('python-version: "3.10"'))
        self.assertIn(
            "python -m pip install -r agent/requirements.lock -r console/requirements.lock",
            workflow,
        )
        self.assertIn("verify_locked_environment.py agent/requirements.lock", workflow)
        self.assertIn("verify_locked_environment.py console/requirements.lock", workflow)
        self.assertIn("windows-worker-quality:", workflow)
        self.assertIn('if: ${{ false }}', workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("verify_locked_environment.py agent/windows_worker_requirements.lock", workflow)
        self.assertIn("agent/tests/test_windows_worker_service_loop.py", workflow)
        agent_gate = workflow.split("agent-quality:", 1)[1].split(
            "console-quality:", 1
        )[0]
        self.assertIn("windows_worker($|/)", agent_gate)
        self.assertIn("--exclude agent/agent/windows_worker", agent_gate)
        self.assertIn("--ignore-glob='tests/test_windows_worker_*.py'", agent_gate)
        self.assertIn("--ignore-glob='agent/tests/test_windows_worker_*.py'", agent_gate)

        release = (REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh").read_text(encoding="utf-8")
        execution = release.split("trap rollback ERR", 1)[1]
        worker_scope_guard = execution.split(
            'if [[ "${WINDOWS_WORKER_RELEASE_ENABLED}" == "1" ]]; then', 1
        )[1].split("\n  fi", 1)[0]
        run_release_prefix = release.split("run_release() {", 1)[1].split("trap rollback ERR", 1)[0]
        quiesce_function = release.split("quiesce_runtime_services() {", 1)[1].split("\n}", 1)[0]
        activate_function = release.split("activate_release_virtualenvs() {", 1)[1].split("\n}", 1)[0]
        rollback_function = release.split("\nrollback() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('RELEASE_STAGE="acquire_release_lock"', run_release_prefix)
        self.assertIn("acquire_release_lock", run_release_prefix)
        self.assertIn('RELEASE_LOCK_FILE="${DEPLOY_ROOT}/release.lock"', release)
        self.assertIn(
            'SCHEDULER_RELEASE_HOLD_FILE="${DEPLOY_ROOT}/scheduler-release.pause"',
            release,
        )
        self.assertIn("flock -n 9", release)
        self.assertLess(
            execution.index("preflight_service_identity_configuration"),
            execution.index("backup_managed_sources"),
        )
        self.assertIn(
            "--check-automation-project-required-resources",
            release,
        )
        self.assertIn(
            "--check-automation-project-scheduled-task-identities",
            release,
        )
        self.assertLess(
            execution.index(
                'RELEASE_STAGE="preflight_automation_project_scheduled_task_identities"'
            ),
            execution.index("backup_managed_sources"),
        )
        self.assertLess(
            execution.index(
                "preflight_automation_project_scheduled_task_identities\n"
            ),
            execution.index("MUTATION_STARTED=1"),
        )
        self.assertLess(
            execution.index(
                'RELEASE_STAGE="preflight_automation_project_required_resources"'
            ),
            execution.index("backup_managed_sources"),
        )
        self.assertLess(
            execution.index("preflight_automation_project_required_resources\n"),
            execution.index("MUTATION_STARTED=1"),
        )
        self.assertIn("WINDOWS_WORKER_RELEASE_ENABLED=0", release)
        self.assertIn("preflight_worker_mtls_proxy", worker_scope_guard)
        self.assertIn('echo "windows_worker_release_scope=disabled"', worker_scope_guard)
        self.assertLess(
            execution.index("WINDOWS_WORKER_RELEASE_ENABLED"),
            execution.index("backup_managed_sources"),
        )
        self.assertEqual(2, execution.count("preflight_scheduled_write_window\n"))
        self.assertLess(
            execution.index('RELEASE_STAGE="preflight_scheduled_write_window_before_mutation"'),
            execution.index("MUTATION_STARTED=1"),
        )
        self.assertEqual(4, execution.count("preflight_running_protected_writes\n"))
        pre_mutation = execution.split(
            'RELEASE_STAGE="preflight_scheduled_write_window_before_mutation"', 1
        )[1]
        emergency_and_later = pre_mutation.split(
            'if [[ "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}" == "1" ]]; then', 1
        )[1]
        emergency_branch, normal_and_later = emergency_and_later.split(
            "\n  else", 1
        )
        normal_branch, after_preflight_branch = normal_and_later.split(
            "\n  fi", 1
        )
        self.assertLess(
            emergency_branch.index('RELEASE_STAGE="capture_control_plane_release_state"'),
            emergency_branch.index(
                'RELEASE_STAGE="create_emergency_scheduler_release_hold"'
            ),
        )
        self.assertLess(
            emergency_branch.index(
                'RELEASE_STAGE="create_emergency_scheduler_release_hold"'
            ),
            emergency_branch.index(
                'RELEASE_STAGE="preflight_running_protected_writes_immediately_before_quiesce"'
            ),
        )
        self.assertTrue(
            emergency_branch.rstrip().endswith("preflight_running_protected_writes")
        )
        self.assertLess(
            normal_branch.index('RELEASE_STAGE="preflight_running_protected_writes"'),
            normal_branch.index('RELEASE_STAGE="capture_control_plane_release_state"'),
        )
        self.assertTrue(normal_branch.rstrip().endswith("capture_control_plane_release_state"))
        self.assertTrue(
            after_preflight_branch.lstrip().startswith(
                "MUTATION_STARTED=1\n  "
                'RELEASE_STAGE="quiesce_runtime_services"\n  '
                "quiesce_runtime_services\n  "
                'RELEASE_STAGE="verify_protected_writes_quiesced"\n  '
                "preflight_running_protected_writes"
            )
        )
        self.assertLess(execution.index("build_release_virtualenvs"), execution.index("MUTATION_STARTED=1"))
        self.assertLess(
            execution.index("preflight_signed_first_party_plugins"),
            execution.index("MUTATION_STARTED=1"),
        )
        self.assertLess(
            execution.index("preflight_worker_server_identity"),
            execution.index("MUTATION_STARTED=1"),
        )
        self.assertGreater(
            execution.index("install_verified_first_party_plugin_artifacts"),
            execution.index("MUTATION_STARTED=1"),
        )
        self.assertLess(
            execution.index("quiesce_runtime_services"),
            execution.index("capture_automation_plugin_installation_state"),
        )
        self.assertLess(
            execution.index("capture_automation_plugin_installation_state"),
            execution.index("install_verified_first_party_plugin_artifacts"),
        )
        self.assertLess(
            execution.index("install_verified_first_party_plugin_artifacts"),
            execution.index("apply_migrations"),
        )
        self.assertLess(
            execution.index("preflight_signed_first_party_plugins"),
            execution.index("install_verified_first_party_plugin_artifacts"),
        )
        self.assertIn(
            'STAGED_FIRST_PARTY_PLUGIN_RELEASE_ROOT="${STAGE_ROOT}/_plugin_artifacts"',
            release,
        )
        self.assertIn(
            'STAGED_FIRST_PARTY_PLUGIN_TRUST_ROOT="${STAGE_ROOT}/_plugin_trust"',
            release,
        )
        self.assertIn(
            '[[ "${temp_release}" == '
            '"${FIRST_PARTY_PLUGIN_RELEASES_ROOT}/.${RELEASE_SHA}."*',
            release,
        )
        self.assertIn("^package_count=[1-9][0-9]*$", release)
        self.assertIn("^instance_count=[1-9][0-9]*$", release)
        self.assertIn("BOYI_AUTOMATION_PLUGIN_VERIFIED_RELEASE_SHA", release)
        self.assertIn(
            '[[ -f "${verifier}" && ! -L "${verifier}" ]] || {',
            release,
        )
        self.assertNotIn('[[ -f "${verifier}" ]] || return 0', release)
        self.assertLess(execution.index("quiesce_runtime_services"), execution.index("retire_legacy_finance_etl"))
        self.assertLess(
            execution.index('RELEASE_STAGE="quiesce_runtime_services"'),
            execution.index('RELEASE_STAGE="verify_protected_writes_quiesced"'),
        )
        self.assertLess(
            execution.index('RELEASE_STAGE="verify_protected_writes_quiesced"'),
            execution.index('RELEASE_STAGE="retire_legacy_finance_etl"'),
        )
        self.assertLess(execution.index("quiesce_runtime_services"), execution.index('sync_scope "${scope}"'))
        self.assertLess(execution.index("quiesce_runtime_services"), execution.index("apply_migrations"))
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
            rollback_function.index("restore_automation_project_authorization_data"),
        )
        self.assertLess(
            rollback_function.index("restore_automation_project_authorization_data"),
            rollback_function.index("restore_control_plane_policy_bootstrap_data"),
        )
        self.assertLess(
            rollback_function.index("restore_control_plane_policy_bootstrap_data"),
            rollback_function.index("restore_scheduled_task_contract_upgrade_data"),
        )
        self.assertLess(
            rollback_function.index("restore_scheduled_task_contract_upgrade_data"),
            rollback_function.index("restore_daily_sign_single_tms_data"),
        )
        self.assertLess(
            rollback_function.index("restore_daily_sign_single_tms_data"),
            rollback_function.index("restore_control_plane_task_cutover_data"),
        )
        self.assertIn('CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY}" == "1', rollback_function)
        self.assertIn('SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY}" == "1', rollback_function)
        self.assertIn('AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY}" == "1', rollback_function)
        self.assertIn('DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY}" == "1', rollback_function)
        self.assertIn('CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE}" == "1', rollback_function)
        self.assertIn('MIGRATIONS_ATTEMPTED}" == "1', rollback_function)
        self.assertIn('NEW_RUNTIME_START_ATTEMPTED}" == "1', rollback_function)
        self.assertLess(
            rollback_function.index("restore_control_plane_task_cutover_data"),
            rollback_function.index("restore_managed_release_state"),
        )
        self.assertIn("restart_runtime_services_for_rollback", rollback_function)
        self.assertLess(
            rollback_function.index("restart_runtime_services_for_rollback"),
            rollback_function.index("cleanup_failed_release_stage"),
        )
        self.assertLess(
            rollback_function.index("remove_new_virtualenvs"),
            rollback_function.index("cleanup_failed_release_stage"),
        )
        self.assertIn("rollback_incomplete", rollback_function)
        self.assertIn("recovery_material_preserved=1", rollback_function)
        self.assertIn(
            "rollback_cleanup_incomplete stage_root=${STAGE_ROOT} "
            "recovery_material_state=unknown verify_required=1",
            rollback_function,
        )
        self.assertIn(
            "release_cleanup_incomplete stage_root=${STAGE_ROOT} "
            "recovery_material_state=unknown verify_required=1",
            rollback_function,
        )
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
        self.assertLess(
            execution.index('RELEASE_STAGE="check_service_identity_smoke"'),
            execution.index('RELEASE_STAGE="check_control_plane_release_manifest"'),
        )
        self.assertLess(
            execution.index('RELEASE_STAGE="check_control_plane_release_manifest"'),
            execution.index('RELEASE_STAGE="record_dependency_hashes"'),
        )
        self.assertLess(
            execution.index('RELEASE_STAGE="create_scheduler_release_hold"'),
            execution.index('RELEASE_STAGE="restart_services"'),
        )
        self.assertIn("ensure_scheduler_release_hold", execution)
        self.assertLess(
            execution.index('RELEASE_STAGE="record_dependency_hashes"'),
            execution.index('RELEASE_STAGE="activate_scheduler_after_release"'),
        )
        self.assertLess(
            execution.index("MUTATION_STARTED=0"),
            execution.index('RELEASE_STAGE="activate_scheduler_after_release"'),
        )
        self.assertIn('scheduler.get("state") != "paused"', release)
        self.assertIn('scheduler.get("release_hold") is not True', release)
        self.assertIn('workflow_runner.get("state") != "held"', release)
        self.assertIn('workflow_runner.get("release_hold") is not True', release)
        self.assertIn('workflow_runner.get("active_runs") != 0', release)
        self.assertIn('automation_plugins.get("ok") is not True', release)
        self.assertIn('automation_workers.get("enabled") is not False', release)
        self.assertIn('automation_workers.get("state") != "disabled"', release)
        self.assertIn('automation_workers.get("release_hold") is not False', release)
        self.assertIn('data["automation_plugins"].get("ok") is not True', release)
        self.assertIn('data["automation_workers"].get("enabled") is not False', release)
        self.assertIn('data["automation_workers"].get("state") != "disabled"', release)
        self.assertIn('data["automation_workers"].get("release_hold") is not False', release)
        self.assertIn('data["automation_workers"].get("active_jobs") or 0', release)
        self.assertIn("for _attempt in range(3):", release)
        self.assertIn(
            'request_target = "/internal/v1/admin/scheduler/activate-after-release"',
            release,
        )
        self.assertLess(
            rollback_function.index("clear_scheduler_release_hold_for_rollback"),
            rollback_function.index("restart_runtime_services_for_rollback"),
        )
        self.assertGreaterEqual(
            rollback_function.count("clear_scheduler_release_hold_for_rollback"),
            2,
        )
        self.assertIn("--expect-initial-production-manifest", release)
        self.assertIn("--check-control-plane-release-manifest", release)
        self.assertIn("--check-running-protected-writes", release)
        self.assertLess(execution.index("MUTATION_STARTED=0"), execution.index("cleanup_successful_release"))
        self.assertLess(
            execution.index('RELEASE_STAGE="retire_legacy_finance_etl"'),
            execution.index('RELEASE_STAGE="sync_scope:${scope}"'),
        )
        for stage in (
            "preflight_service_identity_configuration",
            "static_preflight",
            "build_release_virtualenvs",
            "preflight_running_protected_writes",
            "capture_control_plane_release_state",
            "quiesce_runtime_services",
            "verify_protected_writes_quiesced",
            "retire_legacy_finance_etl",
            "sync_scope:${scope}",
            "apply_migrations",
            "activate_release_virtualenvs",
            "create_scheduler_release_hold",
            "restart_services",
            "check_health",
            "check_service_identity_smoke",
            "check_control_plane_release_manifest",
            "record_dependency_hashes",
            "activate_scheduler_after_release",
            "cleanup_successful_release",
        ):
            self.assertIn(f'RELEASE_STAGE="{stage}"', release)

        main_source = (REPOSITORY_ROOT / "agent" / "main.py").read_text(encoding="utf-8")
        activation_source = main_source.split(
            "async def internal_activate_scheduler_after_release", 1
        )[1].split("\ndef _llm_settings_repository", 1)[0]
        scheduler_source = (
            REPOSITORY_ROOT / "agent" / "agent" / "scheduler.py"
        ).read_text(encoding="utf-8")
        self.assertIn("scheduler.start(paused=release_hold)", main_source)
        self.assertIn("await runner.start(held_for_release=release_hold)", main_source)
        self.assertIn('"scheduler": scheduler_runtime_status()', main_source)
        self.assertIn('"workflow_runner": (', main_source)
        self.assertIn(
            '@app.post("/internal/v1/admin/scheduler/activate-after-release")',
            main_source,
        )
        self.assertLess(
            activation_source.index("begin_scheduler_release_activation"),
            activation_source.index("runner.resume_after_release"),
        )
        self.assertLess(
            activation_source.index("runner.resume_after_release"),
            activation_source.index("consume_scheduler_release_hold"),
        )
        begin_scheduler = scheduler_source.split(
            "def begin_scheduler_release_activation", 1
        )[1].split("\ndef pause_scheduler_for_release", 1)[0]
        consume_hold = scheduler_source.split(
            "def consume_scheduler_release_hold", 1
        )[1].split("\ndef reload_scheduler", 1)[0]
        self.assertNotIn(".unlink()", begin_scheduler)
        self.assertIn("scheduler_release_hold_path().unlink()", consume_hold)

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
        self.assertNotIn("restore-014", events)
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

    def test_partial_stage_cleanup_failure_reports_unknown_recovery_material_state(self):
        completed, events, stage_exists, _new_venv_exists = _run_rollback_fault_harness(
            fail_agent_restart=False,
            fail_stage_cleanup=True,
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
        completed, events, stage_exists, _new_venv_exists = _run_rollback_fault_harness(
            fail_agent_restart=False,
            cutover_pending=False,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("rollback_incomplete", completed.stderr)
        self.assertNotIn("restore-014", events)
        self.assertFalse(stage_exists)
        self.assertIn("restart:agent.service", events)
        self.assertIn("restart:console.service", events)

    def test_rollback_restores_only_current_release_database_state_in_reverse_order(self):
        completed, events, stage_exists, _new_venv_exists = _run_rollback_fault_harness(
            fail_agent_restart=False,
            cutover_pending=True,
            daily_sign_pending=True,
            contract_upgrade_pending=True,
            automation_project_pending=True,
            bootstrap_absent=True,
            migrations_attempted=True,
            runtime_start_attempted=True,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertFalse(stage_exists)
        restore_events = [event for event in events if event.startswith("restore-")]
        self.assertEqual(
            [
                "restore-018",
                "restore-bootstrap",
                "restore-017",
                "restore-016",
                "restore-014",
            ],
            restore_events,
        )
        self.assertLess(events.index("restore-014"), events.index("venv:agent"))

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
        self.assertNotIn('"windows_worker_requirements.txt"', agent_files)
        self.assertNotIn('"windows_worker_requirements.lock"', agent_files)
        self.assertIn('"windows_worker_requirements.txt"', blocked_files)
        self.assertIn('"windows_worker_requirements.lock"', blocked_files)
        self.assertIn('"windows_worker_host.py"', blocked_files)
        self.assertIn('"windows_worker"', blocked_dirs)
        self.assertIn('"first_party_automation_plugins"', publisher)
        self.assertIn("[string]$AutomationPluginArtifactRoot", publisher)
        self.assertIn("[string]$AutomationPluginTrustRoot", publisher)
        self.assertIn("function Copy-AutomationPluginReleaseInputs", publisher)
        self.assertIn(
            "[switch]$EmergencyUserAuthorizedScheduledWindowOverride",
            publisher,
        )
        self.assertIn(
            "--emergency-scheduled-window-override=emergency_user_authorized",
            publisher,
        )
        self.assertIn("one release-index.json and only its ZIP packages", publisher)
        self.assertIn('$_.Extension -cne ".pub"', publisher)
        self.assertIn('Join-Path $DestinationRoot "_plugin_artifacts"', publisher)
        self.assertIn('Join-Path $DestinationRoot "_plugin_trust"', publisher)
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

    def test_release_captures_every_database_prestate_before_mutation(self):
        completed = _run_sourced_release_harness(
            r"""
            run_staged_migration_runner() {
              case "$1" in
                --control-plane-task-cutover-status)
                  echo 'control_plane_task_cutover_status=applied'
                  ;;
                --daily-sign-single-tms-status)
                  echo 'daily_sign_single_tms_status=pending_clean'
                  ;;
                --scheduled-task-contract-upgrade-status)
                  echo 'scheduled_task_contract_upgrade_status=pending_clean'
                  ;;
                --automation-project-authorization-status)
                  echo 'automation_project_authorization_status=pending_clean'
                  ;;
                --control-plane-policy-bootstrap-marker-status)
                  echo 'control_plane_policy_bootstrap_marker_status=absent'
                  ;;
                *) return 91 ;;
              esac
            }
            capture_control_plane_release_state
            printf 'states=%s,%s,%s,%s,%s\n' \
              "${CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY}" \
              "${DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY}" \
              "${SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY}" \
              "${AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY}" \
              "${CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE}"
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("states=0,1,1,1,1", completed.stdout)

    def test_schedule_identity_preflight_accepts_only_exact_success_states(self):
        for state in ("pending", "applied"):
            with self.subTest(state=state):
                completed = _run_sourced_release_harness(
                    f"""
                    run_staged_migration_runner() {{
                      [[ "$1" == "--check-automation-project-scheduled-task-identities" ]] || return 92
                      echo 'automation_project_scheduled_task_identities=ok state={state} allowed_count=71'
                    }}
                    preflight_automation_project_scheduled_task_identities
                    """
                )

                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(
                    "automation_project_scheduled_task_identities=ok "
                    f"state={state} allowed_count=71\n",
                    completed.stdout,
                )

    def test_schedule_identity_preflight_preserves_only_valid_hex_findings(self):
        completed = _run_sourced_release_harness(
            r"""
            run_staged_migration_runner() {
              echo 'automation_project_scheduled_task_identities=blocked count=2'
              echo 'automation_project_scheduled_task_identity task_id_hex=756e6b6e6f776e tool_name_hex=746f6f6c reason=UNKNOWN_TASK_ID field=id'
              echo 'automation_project_scheduled_task_identity_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa reason=INVALID_IDENTITY field=tool_name'
              return 1
            }
            preflight_automation_project_scheduled_task_identities
            """
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn("task_id_hex=756e6b6e6f776e", completed.stderr)
        self.assertIn("identity_sha256=" + "a" * 64, completed.stderr)

    def test_schedule_identity_preflight_suppresses_untrusted_or_mismatched_output(self):
        cases = (
            """echo 'password=must-not-be-printed'; return 1""",
            """
              echo 'automation_project_scheduled_task_identities=blocked count=1'
              echo 'automation_project_scheduled_task_identity task_id_hex=61 tool_name_hex=62 reason=TOOL_NAME_MISMATCH field=id'
              return 1
            """,
            """echo 'automation_project_scheduled_task_identities=ok state=pending allowed_count=70'""",
        )
        for body in cases:
            with self.subTest(body=body):
                completed = _run_sourced_release_harness(
                    f"""
                    run_staged_migration_runner() {{
                      {body}
                    }}
                    preflight_automation_project_scheduled_task_identities
                    """
                )

                self.assertNotEqual(0, completed.returncode)
                self.assertNotIn("must-not-be-printed", completed.stdout)
                self.assertNotIn("must-not-be-printed", completed.stderr)
                self.assertIn(
                    "automation_project_scheduled_task_identities=blocked "
                    "reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1",
                    completed.stderr,
                )

    def test_required_resource_preflight_accepts_only_exact_success(self):
        completed = _run_sourced_release_harness(
            r"""
            run_staged_migration_runner() {
              [[ "$1" == "--check-automation-project-required-resources" ]] || return 92
              echo 'automation_project_required_resources=ok count=8'
            }
            preflight_automation_project_required_resources
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            "automation_project_required_resources=ok count=8\n",
            completed.stdout,
        )

    def test_required_resource_preflight_preserves_safe_failure_details(self):
        completed = _run_sourced_release_harness(
            r"""
            run_staged_migration_runner() {
              echo 'automation_project_required_resources=blocked count=2'
              echo 'automation_project_required_resource=phase7.site_send_sheet reason=MISSING_FIELD field=range'
              echo 'automation_project_required_resource=phase7.daily_sign_sheet reason=MISSING_ROW field=resource_key'
              return 1
            }
            preflight_automation_project_required_resources
            """
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn(
            "automation_project_required_resource=phase7.site_send_sheet "
            "reason=MISSING_FIELD field=range",
            completed.stderr,
        )
        self.assertIn(
            "automation_project_required_resource=phase7.daily_sign_sheet "
            "reason=MISSING_ROW field=resource_key",
            completed.stderr,
        )

    def test_required_resource_preflight_suppresses_untrusted_failure_output(self):
        completed = _run_sourced_release_harness(
            r"""
            run_staged_migration_runner() {
              echo 'password=must-not-be-printed'
              return 1
            }
            preflight_automation_project_required_resources
            """
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("must-not-be-printed", completed.stdout)
        self.assertNotIn("must-not-be-printed", completed.stderr)
        self.assertIn(
            "automation_project_required_resources=blocked "
            "reason=UNEXPECTED_PREFLIGHT_RESPONSE",
            completed.stderr,
        )

    def test_release_rejects_dirty_contract_upgrade_before_mutation(self):
        completed = _run_sourced_release_harness(
            r"""
            run_staged_migration_runner() {
              case "$1" in
                --control-plane-task-cutover-status)
                  echo 'control_plane_task_cutover_status=applied'
                  ;;
                --daily-sign-single-tms-status)
                  echo 'daily_sign_single_tms_status=applied'
                  ;;
                --scheduled-task-contract-upgrade-status)
                  echo 'scheduled_task_contract_upgrade_status=pending_dirty'
                  ;;
                *) return 91 ;;
              esac
            }
            capture_control_plane_release_state
            """
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("migration 017 attempt left unrecovered", completed.stderr)

    def test_release_rejects_dirty_project_authorization_before_mutation(self):
        completed = _run_sourced_release_harness(
            r"""
            run_staged_migration_runner() {
              case "$1" in
                --control-plane-task-cutover-status)
                  echo 'control_plane_task_cutover_status=applied'
                  ;;
                --daily-sign-single-tms-status)
                  echo 'daily_sign_single_tms_status=applied'
                  ;;
                --scheduled-task-contract-upgrade-status)
                  echo 'scheduled_task_contract_upgrade_status=applied'
                  ;;
                --automation-project-authorization-status)
                  echo 'automation_project_authorization_status=pending_dirty'
                  ;;
                *) return 91 ;;
              esac
            }
            capture_control_plane_release_state
            """
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("migration 018 attempt left unrecovered", completed.stderr)

    def test_release_writes_closed_plugin_runtime_environment_atomically(self):
        completed = _run_sourced_release_harness(
            r"""
            PLUGIN_RUNTIME_ENV_FILE="${stage_root}/runtime/automation_plugin_release.env"
            FIRST_PARTY_PLUGIN_RELEASE_ROOT="/trusted/releases/${RELEASE_SHA}"
            FIRST_PARTY_PLUGIN_TRUST_ROOT="/trusted/public-keys"
            write_automation_plugin_runtime_environment
            cat "${PLUGIN_RUNTIME_ENV_FILE}"
            stat -c 'mode=%a' "${PLUGIN_RUNTIME_ENV_FILE}"
            find "$(dirname "${PLUGIN_RUNTIME_ENV_FILE}")" \
              -maxdepth 1 -name '.automation_plugin_release.*' -print
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "BOYI_AUTOMATION_PLUGIN_ARTIFACT_ROOT=/trusted/releases/"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            completed.stdout,
        )
        self.assertIn(
            "BOYI_AUTOMATION_PLUGIN_TRUST_ROOT=/trusted/public-keys",
            completed.stdout,
        )
        self.assertIn(
            "BOYI_AUTOMATION_PLUGIN_VERIFIED_RELEASE_SHA="
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            completed.stdout,
        )
        self.assertIn("mode=600", completed.stdout)

    def test_plugin_artifact_rollback_removes_only_this_release_additions(self):
        completed = _run_sourced_release_harness(
            r"""
            AUTOMATION_PLUGIN_ROOT="${stage_root}/production-plugins"
            FIRST_PARTY_PLUGIN_RELEASES_ROOT="${AUTOMATION_PLUGIN_ROOT}/releases"
            FIRST_PARTY_PLUGIN_RELEASE_ROOT="${FIRST_PARTY_PLUGIN_RELEASES_ROOT}/${RELEASE_SHA}"
            FIRST_PARTY_PLUGIN_TRUST_ROOT="${AUTOMATION_PLUGIN_ROOT}/trust"
            BACKUP_DIR="${stage_root}/_rollback"
            PLUGIN_TRUST_ADDITIONS_FILE="${BACKUP_DIR}/automation_plugin_trust.added"
            mkdir -p "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" "${FIRST_PARTY_PLUGIN_TRUST_ROOT}" "${BACKUP_DIR}"
            printf 'verified-package\n' >"${FIRST_PARTY_PLUGIN_RELEASE_ROOT}/release-index.json"
            printf 'public-key\n' >"${FIRST_PARTY_PLUGIN_TRUST_ROOT}/release.pub"
            key_sha="$(sha256sum "${FIRST_PARTY_PLUGIN_TRUST_ROOT}/release.pub" | awk '{print $1}')"
            printf '%s %s\n' "${key_sha}" release.pub >"${PLUGIN_TRUST_ADDITIONS_FILE}"
            touch "${BACKUP_DIR}/first_party_plugin_release.absent"
            touch "${BACKUP_DIR}/plugin_root.absent"
            touch "${BACKUP_DIR}/plugin_releases_root.absent"
            touch "${BACKUP_DIR}/plugin_trust_root.absent"
            verify_installed_first_party_plugin_artifacts() { return 0; }
            restore_first_party_plugin_artifacts
            [[ ! -e "${AUTOMATION_PLUGIN_ROOT}" ]]
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_plugin_artifact_rollback_refuses_changed_trust_key_without_deleting_release(self):
        completed = _run_sourced_release_harness(
            r"""
            AUTOMATION_PLUGIN_ROOT="${stage_root}/production-plugins"
            FIRST_PARTY_PLUGIN_RELEASES_ROOT="${AUTOMATION_PLUGIN_ROOT}/releases"
            FIRST_PARTY_PLUGIN_RELEASE_ROOT="${FIRST_PARTY_PLUGIN_RELEASES_ROOT}/${RELEASE_SHA}"
            FIRST_PARTY_PLUGIN_TRUST_ROOT="${AUTOMATION_PLUGIN_ROOT}/trust"
            BACKUP_DIR="${stage_root}/_rollback"
            PLUGIN_TRUST_ADDITIONS_FILE="${BACKUP_DIR}/automation_plugin_trust.added"
            mkdir -p "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" "${FIRST_PARTY_PLUGIN_TRUST_ROOT}" "${BACKUP_DIR}"
            printf 'verified-package\n' >"${FIRST_PARTY_PLUGIN_RELEASE_ROOT}/release-index.json"
            printf 'expected-key\n' >"${stage_root}/expected.pub"
            expected_sha="$(sha256sum "${stage_root}/expected.pub" | awk '{print $1}')"
            printf '%s %s\n' "${expected_sha}" release.pub >"${PLUGIN_TRUST_ADDITIONS_FILE}"
            printf 'changed-key\n' >"${FIRST_PARTY_PLUGIN_TRUST_ROOT}/release.pub"
            touch "${BACKUP_DIR}/first_party_plugin_release.absent"
            touch "${BACKUP_DIR}/plugin_root.existing"
            touch "${BACKUP_DIR}/plugin_releases_root.existing"
            touch "${BACKUP_DIR}/plugin_trust_root.existing"
            verify_installed_first_party_plugin_artifacts() { return 0; }
            if restore_first_party_plugin_artifacts; then
              exit 91
            fi
            [[ -f "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}/release-index.json" ]]
            [[ -f "${FIRST_PARTY_PLUGIN_TRUST_ROOT}/release.pub" ]]
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "Refusing to delete changed automation plugin trust key",
            completed.stderr,
        )

    def test_plugin_install_rollback_quarantines_only_release_created_versions(self):
        completed = _run_sourced_release_harness(
            r"""
            AUTOMATION_PLUGIN_ROOT="${stage_root}/production-plugins"
            AUTOMATION_PLUGIN_INSTALL_ROOT="${AUTOMATION_PLUGIN_ROOT}/installed"
            BACKUP_DIR="${stage_root}/_rollback"
            PLUGIN_INSTALL_INVENTORY_FILE="${BACKUP_DIR}/automation_plugin_install.paths"
            mkdir -p "${AUTOMATION_PLUGIN_INSTALL_ROOT}/existing_action/1.0.0-111111111111"
            capture_automation_plugin_installation_state
            mkdir -p \
              "${AUTOMATION_PLUGIN_INSTALL_ROOT}/new_action/1.0.0-222222222222/package" \
              "${AUTOMATION_PLUGIN_INSTALL_ROOT}/.staging/new_action-1.0.0-abcdef123456abcdef123456abcdef12"
            printf 'old\n' >"${AUTOMATION_PLUGIN_INSTALL_ROOT}/existing_action/1.0.0-111111111111/kept"
            printf 'new\n' >"${AUTOMATION_PLUGIN_INSTALL_ROOT}/new_action/1.0.0-222222222222/package/moved"
            restore_automation_plugin_installations
            [[ -f "${AUTOMATION_PLUGIN_INSTALL_ROOT}/existing_action/1.0.0-111111111111/kept" ]]
            [[ ! -e "${AUTOMATION_PLUGIN_INSTALL_ROOT}/new_action" ]]
            [[ -f "${BACKUP_DIR}/retired/automation_plugin_installed/new_action/1.0.0-222222222222/package/moved" ]]
            [[ -d "${BACKUP_DIR}/retired/automation_plugin_installed/.staging/new_action-1.0.0-abcdef123456abcdef123456abcdef12" ]]
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_plugin_install_rollback_uses_c_locale_and_checks_comm_order(self):
        release = (REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh").read_text(
            encoding="utf-8"
        )
        restore = release.split("restore_automation_plugin_installations() {", 1)[1].split(
            "\n}\n\nwrite_automation_plugin_runtime_environment() {",
            1,
        )[0]
        self.assertEqual(restore.count("LC_ALL=C comm --check-order"), 2)
        self.assertNotIn("\n  comm -", restore)
        self.assertEqual(
            restore.count("Automation plugin rollback inventory comparison failed"),
            2,
        )

        completed = _run_sourced_release_harness(
            r"""
            AUTOMATION_PLUGIN_ROOT="${stage_root}/production-plugins"
            AUTOMATION_PLUGIN_INSTALL_ROOT="${AUTOMATION_PLUGIN_ROOT}/installed"
            BACKUP_DIR="${stage_root}/_rollback"
            PLUGIN_INSTALL_INVENTORY_FILE="${BACKUP_DIR}/automation_plugin_install.paths"
            mkdir -p "${AUTOMATION_PLUGIN_INSTALL_ROOT}/existing_action/1.0.0-111111111111"
            capture_automation_plugin_installation_state
            mkdir -p "${AUTOMATION_PLUGIN_INSTALL_ROOT}/new_action/1.0.0-222222222222/package"
            comm() {
              [[ "${LC_ALL:-}" == "C" ]] || return 97
              command comm "$@"
            }
            restore_automation_plugin_installations
            [[ -d "${AUTOMATION_PLUGIN_INSTALL_ROOT}/existing_action/1.0.0-111111111111" ]]
            [[ ! -e "${AUTOMATION_PLUGIN_INSTALL_ROOT}/new_action" ]]
            """
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_plugin_install_rollback_refuses_to_hide_missing_preexisting_version(self):
        completed = _run_sourced_release_harness(
            r"""
            AUTOMATION_PLUGIN_ROOT="${stage_root}/production-plugins"
            AUTOMATION_PLUGIN_INSTALL_ROOT="${AUTOMATION_PLUGIN_ROOT}/installed"
            BACKUP_DIR="${stage_root}/_rollback"
            PLUGIN_INSTALL_INVENTORY_FILE="${BACKUP_DIR}/automation_plugin_install.paths"
            mkdir -p "${AUTOMATION_PLUGIN_INSTALL_ROOT}/existing_action/1.0.0-111111111111"
            capture_automation_plugin_installation_state
            rmdir "${AUTOMATION_PLUGIN_INSTALL_ROOT}/existing_action/1.0.0-111111111111"
            if restore_automation_plugin_installations; then
              exit 91
            fi
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "A pre-release automation plugin installation disappeared",
            completed.stderr,
        )

    def test_plugin_install_rollback_removes_new_empty_root_after_quarantine(self):
        completed = _run_sourced_release_harness(
            r"""
            AUTOMATION_PLUGIN_ROOT="${stage_root}/production-plugins"
            AUTOMATION_PLUGIN_INSTALL_ROOT="${AUTOMATION_PLUGIN_ROOT}/installed"
            BACKUP_DIR="${stage_root}/_rollback"
            PLUGIN_INSTALL_INVENTORY_FILE="${BACKUP_DIR}/automation_plugin_install.paths"
            capture_automation_plugin_installation_state
            mkdir -p \
              "${AUTOMATION_PLUGIN_INSTALL_ROOT}/new_action/1.0.0-222222222222/package" \
              "${AUTOMATION_PLUGIN_INSTALL_ROOT}/.staging"
            restore_automation_plugin_installations
            [[ ! -e "${AUTOMATION_PLUGIN_INSTALL_ROOT}" ]]
            [[ -d "${BACKUP_DIR}/retired/automation_plugin_installed/new_action/1.0.0-222222222222/package" ]]
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_failed_release_cleanup_removes_immutable_plugin_quarantine(self):
        release = (REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh").read_text(
            encoding="utf-8"
        )
        cleanup = release.split(
            "prepare_retired_automation_plugins_for_stage_cleanup() {", 1
        )[1].split("\n}\n\nwrite_automation_plugin_runtime_environment() {", 1)[0]
        self.assertIn('chmod u+rwx -- "${path}"', cleanup)
        self.assertNotIn("sudo", cleanup)
        self.assertNotIn("chown", cleanup)

        completed = _run_sourced_release_harness(
            r"""
            DEPLOY_ROOT="$(dirname "${stage_root}")/deploy"
            STAGE_ROOT="${DEPLOY_ROOT}/release-aaaaaaaaaaaa-20260815192447"
            BACKUP_DIR="${STAGE_ROOT}/_rollback"
            quarantine="${BACKUP_DIR}/retired/automation_plugin_installed"
            outside="${DEPLOY_ROOT}/outside-readonly"
            mkdir -p \
              "${quarantine}/new_action/1.0.0-222222222222/package/payload" \
              "${outside}"
            printf 'immutable\n' \
              >"${quarantine}/new_action/1.0.0-222222222222/package/payload/action.py"
            chmod 0444 \
              "${quarantine}/new_action/1.0.0-222222222222/package/payload/action.py"
            chmod 0555 \
              "${quarantine}/new_action/1.0.0-222222222222/package" \
              "${quarantine}/new_action/1.0.0-222222222222/package/payload" \
              "${outside}"

            cleanup_failed_release_stage
            [[ ! -e "${STAGE_ROOT}" && ! -L "${STAGE_ROOT}" ]]
            [[ "$(stat -c '%a' -- "${outside}")" == "555" ]]
            chmod 0755 "${outside}"
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_failed_release_cleanup_rejects_quarantine_symlink(self):
        completed = _run_sourced_release_harness(
            r"""
            DEPLOY_ROOT="$(dirname "${stage_root}")/deploy"
            STAGE_ROOT="${DEPLOY_ROOT}/release-aaaaaaaaaaaa-20260815192447"
            BACKUP_DIR="${STAGE_ROOT}/_rollback"
            quarantine="${BACKUP_DIR}/retired/automation_plugin_installed"
            outside="$(dirname "${stage_root}")/outside"
            mkdir -p "${quarantine}" "${outside}"
            ln -s "${outside}" "${quarantine}/escape"

            if cleanup_failed_release_stage; then
              exit 91
            fi
            [[ -L "${quarantine}/escape" && -d "${STAGE_ROOT}" ]]
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "cleanup inventory contains an unsafe entry",
            completed.stderr,
        )

    def test_failed_release_cleanup_rejects_owner_change_before_chmod(self):
        completed = _run_sourced_release_harness(
            r"""
            DEPLOY_ROOT="$(dirname "${stage_root}")/deploy"
            STAGE_ROOT="${DEPLOY_ROOT}/release-aaaaaaaaaaaa-20260815192447"
            BACKUP_DIR="${STAGE_ROOT}/_rollback"
            quarantine="${BACKUP_DIR}/retired/automation_plugin_installed"
            package="${quarantine}/new_action/1.0.0-222222222222/package"
            mkdir -p "${package}"
            printf 'foreign\n' >"${package}/foreign-owner"
            chmod 0555 "${package}"
            stat() {
              local last="${*: -1}"
              if [[ "$1" == "-c" && "$2" == "%u" && \
                "${last}" == "${package}/foreign-owner" ]]; then
                printf '999999\n'
                return 0
              fi
              command stat "$@"
            }

            if cleanup_failed_release_stage; then
              exit 91
            fi
            [[ "$(command stat -c '%a' -- "${package}")" == "555" ]]
            command chmod 0755 "${package}"
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "cleanup inventory ownership or device changed",
            completed.stderr,
        )

    def test_failed_release_cleanup_rejects_device_change_before_chmod(self):
        completed = _run_sourced_release_harness(
            r"""
            DEPLOY_ROOT="$(dirname "${stage_root}")/deploy"
            STAGE_ROOT="${DEPLOY_ROOT}/release-aaaaaaaaaaaa-20260815192447"
            BACKUP_DIR="${STAGE_ROOT}/_rollback"
            quarantine="${BACKUP_DIR}/retired/automation_plugin_installed"
            package="${quarantine}/new_action/1.0.0-222222222222/package"
            mkdir -p "${package}"
            printf 'foreign\n' >"${package}/foreign-device"
            chmod 0555 "${package}"
            stat() {
              local last="${*: -1}"
              if [[ "$1" == "-c" && "$2" == "%d" && \
                "${last}" == "${package}/foreign-device" ]]; then
                printf '999999\n'
                return 0
              fi
              command stat "$@"
            }

            if cleanup_failed_release_stage; then
              exit 91
            fi
            [[ "$(command stat -c '%a' -- "${package}")" == "555" ]]
            command chmod 0755 "${package}"
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "cleanup inventory ownership or device changed",
            completed.stderr,
        )

    def test_failed_release_cleanup_rejects_escaped_quarantine_target(self):
        completed = _run_sourced_release_harness(
            r"""
            DEPLOY_ROOT="$(dirname "${stage_root}")/deploy"
            STAGE_ROOT="${DEPLOY_ROOT}/release-aaaaaaaaaaaa-20260815192447"
            BACKUP_DIR="$(dirname "${stage_root}")/escaped-rollback"
            quarantine="${BACKUP_DIR}/retired/automation_plugin_installed"
            mkdir -p "${STAGE_ROOT}" "${quarantine}"

            if cleanup_failed_release_stage; then
              exit 91
            fi
            [[ -d "${STAGE_ROOT}" && -d "${quarantine}" ]]
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "cleanup path escaped the current release stage",
            completed.stderr,
        )

    def test_release_manifest_uses_bootstrap_prestate_to_select_initial_gate(self):
        completed = _run_sourced_release_harness(
            r"""
            run_staged_migration_runner() {
              if [[ "$*" == *'--expect-initial-production-manifest'* ]]; then
                echo 'control_plane_release_manifest=ok reviewed_rows=69 enabled_rows=67 policies=69 marker=1 initial=1'
              else
                echo 'control_plane_release_manifest=ok reviewed_rows=69 enabled_rows=61 policies=69 marker=1 initial=0'
              fi
            }
            CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE=1
            check_control_plane_release_manifest
            CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE=0
            check_control_plane_release_manifest
            """
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("enabled_rows=67", completed.stdout)
        self.assertIn("enabled_rows=61", completed.stdout)

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
