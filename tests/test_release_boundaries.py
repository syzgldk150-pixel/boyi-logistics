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
        self.assertIn("verify_locked_environment.py agent/requirements.lock", workflow)
        self.assertIn("verify_locked_environment.py console/requirements.lock", workflow)

        release = (REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh").read_text(encoding="utf-8")
        execution = release.split("trap rollback ERR", 1)[1]
        self.assertLess(execution.index("build_release_virtualenvs"), execution.index("MUTATION_STARTED=1"))
        self.assertLess(execution.index("apply_migrations"), execution.index("activate_release_virtualenvs"))
        self.assertIn('release_venv="${VENV_ROOT}/${target}-${RELEASE_SHA}"', release)
        self.assertIn('bootstrap_python="$(readlink -f -- "${PYTHON_BINS[$target]}")"', release)
        self.assertIn('migration_python="${RELEASE_VENVS[agent]}/bin/python"', release)
        self.assertIn("restore_virtualenvs", release)
        self.assertIn("https://mirrors.aliyun.com/pypi/simple", release)
        self.assertIn('--retries "${PIP_RETRIES}"', release)
        self.assertIn('--timeout "${PIP_TIMEOUT_SECONDS}"', release)
        self.assertIn('release_error stage=${RELEASE_STAGE}', release)
        for stage in (
            "static_preflight",
            "build_release_virtualenvs",
            "sync_scope:${scope}",
            "apply_migrations",
            "activate_release_virtualenvs",
            "restart_services",
            "check_health",
        ):
            self.assertIn(f'RELEASE_STAGE="{stage}"', release)

    def test_release_keeps_ssh_verification_and_publishes_new_modules(self):
        publisher = (REPOSITORY_ROOT / "agent" / "deploy" / "publish_to_ecs.ps1").read_text(encoding="utf-8")
        self.assertNotIn("StrictHostKeyChecking=no", publisher)
        self.assertIn('"config", "routes", "services", "static", "templates"', publisher)


if __name__ == "__main__":
    unittest.main()
