"""Guard repository encoding, size, and sensitive-file boundaries."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_BASENAMES = {
    ".env",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
FORBIDDEN_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
MAX_PYTHON_LINES = 3_000
LEGACY_PYTHON_LINE_LIMITS = {
    "agent/agent/automation_plugins/production.py": 3_236,
    "agent/agent/orchestration/automation_project_policy_service.py": 3_005,
    "agent/main.py": 3_065,
    "console/services/automation_projects.py": 3_170,
    "shared/automation_plugin_generation_repository.py": 3_152,
    "shared/orchestration_repository.py": 3_002,
}
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [REPOSITORY_ROOT / raw.decode("utf-8") for raw in completed.stdout.split(b"\0") if raw]


def main() -> int:
    problems: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        basename = path.name.lower()
        credential_name = basename.startswith("credentials") and basename.endswith(".json")
        secret_name = basename.startswith("secrets.")
        if (
            basename in FORBIDDEN_BASENAMES
            or credential_name
            or secret_name
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
        ):
            # Deliberately do not read files whose names indicate credentials.
            problems.append(f"forbidden tracked file: {relative}")
            continue
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        content = path.read_bytes()
        if content.startswith(b"\xef\xbb\xbf"):
            problems.append(f"UTF-8 BOM: {relative}")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            problems.append(f"not UTF-8: {relative}")
            continue
        if path.suffix.lower() == ".py":
            line_limit = LEGACY_PYTHON_LINE_LIMITS.get(relative, MAX_PYTHON_LINES)
            if len(text.splitlines()) > line_limit:
                problems.append(
                    f"oversized Python module: {relative} (> {line_limit} lines)"
                )
        if path.name != Path(__file__).name:
            for line_number, line in enumerate(text.splitlines(), 1):
                if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                    problems.append(f"potential secret: {relative}:{line_number}")

    duplicate_overview = REPOSITORY_ROOT / "agent" / "project_overview.md"
    if duplicate_overview.exists():
        problems.append("duplicate overview: use agent/docs/project_overview.md only")

    if problems:
        raise SystemExit("\n".join(problems))
    print("repository_hygiene=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
