from __future__ import annotations

import re
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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
        self.assertLess(execution.index("build_release_virtualenvs"), execution.index("MUTATION_STARTED=1"))
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
        self.assertIn("record_active_dependency_hashes", release)
        self.assertNotIn('${target}-${RELEASE_SHA}', release)
        self.assertIn('bootstrap_python="$(readlink -f -- "${PYTHON_BINS[agent]}")"', release)
        self.assertIn('migration_python="${RELEASE_VENV}/bin/python"', release)
        self.assertIn("restore_virtualenvs", release)
        self.assertIn("https://mirrors.aliyun.com/pypi/simple", release)
        self.assertIn('--retries "${PIP_RETRIES}"', release)
        self.assertIn('--timeout "${PIP_TIMEOUT_SECONDS}"', release)
        self.assertIn('release_error stage=${RELEASE_STAGE}', release)
        self.assertNotIn('\nBACKUP_ROOT=', release)
        self.assertIn('BACKUP_DIR="${STAGE_ROOT}/_rollback"', release)
        self.assertIn('LEGACY_BACKUP_ROOT="/home/boyce/.boyi-backups"', release)
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
        self.assertIn("prune_inactive_virtualenvs", release)
        self.assertIn('find "${VENV_ROOT}" -mindepth 1 -maxdepth 1 -type d', release)
        self.assertIn('rm -rf -- "${LEGACY_BACKUP_ROOT}"', release)
        self.assertLess(execution.index("check_health"), execution.index("cleanup_successful_release"))
        self.assertLess(execution.index("MUTATION_STARTED=0"), execution.index("cleanup_successful_release"))
        self.assertLess(
            execution.index('RELEASE_STAGE="retire_legacy_finance_etl"'),
            execution.index('RELEASE_STAGE="sync_scope:${scope}"'),
        )
        for stage in (
            "static_preflight",
            "build_release_virtualenvs",
            "retire_legacy_finance_etl",
            "sync_scope:${scope}",
            "apply_migrations",
            "activate_release_virtualenvs",
            "restart_services",
            "check_health",
            "record_dependency_hashes",
            "cleanup_successful_release",
        ):
            self.assertIn(f'RELEASE_STAGE="{stage}"', release)

    def test_release_keeps_ssh_verification_and_publishes_new_modules(self):
        publisher = (REPOSITORY_ROOT / "agent" / "deploy" / "publish_to_ecs.ps1").read_text(encoding="utf-8")
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
