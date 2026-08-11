"""Repository-wide checks for current Markdown and runtime prompt contracts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import yaml


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _tracked_markdown() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def test_tracked_markdown_has_balanced_fences_and_valid_local_links() -> None:
    errors: list[str] = []
    for path in _tracked_markdown():
        text = path.read_text(encoding="utf-8")
        fence: str | None = None
        for line_number, line in enumerate(text.splitlines(), start=1):
            marker = line.lstrip()
            if marker.startswith("```"):
                current = "```"
            elif marker.startswith("~~~"):
                current = "~~~"
            else:
                continue
            if fence is None:
                fence = current
            elif fence == current:
                fence = None
        if fence is not None:
            errors.append(f"{path.relative_to(ROOT)}: unmatched {fence} fence")

        for match in MARKDOWN_LINK_RE.finditer(text):
            target = match.group(1).strip().strip("<>")
            if not target or target.startswith(("#", "/")):
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                continue
            target = target.split("#", 1)[0].split("?", 1)[0]
            if not target:
                continue
            candidate = (path.parent / unquote(target)).resolve()
            if not candidate.exists():
                errors.append(
                    f"{path.relative_to(ROOT)}: broken local link {match.group(1)}"
                )
    assert not errors, "\n".join(errors)


def test_runtime_prompts_only_reference_current_tools_and_endpoints() -> None:
    registry = yaml.safe_load((ROOT / "agent" / "tools" / "registry.yaml").read_text(encoding="utf-8"))
    tool_names = {tool["name"] for tool in registry["tools"]}
    prompt_dir = ROOT / "agent" / "prompts"
    tool_selection = (prompt_dir / "tool_selection.md").read_text(encoding="utf-8")
    business_rules = (prompt_dir / "business_rules.md").read_text(encoding="utf-8")

    mapped_tools = {
        columns[1]
        for line in tool_selection.splitlines()
        if line.startswith("|")
        for columns in ([cell.strip() for cell in line.strip("|").split("|")],)
        if len(columns) >= 3
        and columns[1] != "使用工具"
        and columns[1].strip("-")
    }
    assert mapped_tools <= tool_names
    combined = tool_selection + business_rules
    assert "trigger_n8n" not in combined
    assert "localhost:8080" not in combined
    assert "http-service" not in combined
    assert "财务 ETL" not in combined


def test_current_docs_do_not_contain_double_prefixed_admin_routes() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _tracked_markdown()
        if "/internal/v1/admin/internal/v1/tms/" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


def test_console_syntax_check_is_independent_of_current_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "console" / "check_syntax.py")],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Syntax OK" in result.stdout
