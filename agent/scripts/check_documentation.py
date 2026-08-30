"""Validate tracked Markdown links, lifecycle metadata, and instruction mirrors."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INSTRUCTION_PAIRS = (
    ("AGENTS.md", "CLAUDE.md"),
    ("agent/AGENTS.md", "agent/CLAUDE.md"),
    ("console/AGENTS.md", "console/CLAUDE.md"),
)
REQUIRED_AGENT_DOC_FIELDS = {"module", "type", "status", "updated"}
HISTORICAL_STATUSES = {"historical", "implemented", "superseded"}
NON_CURRENT_STATUSES = HISTORICAL_STATUSES | {"aspirational", "snapshot"}
MARKDOWN_LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def tracked_markdown() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        REPOSITORY_ROOT / raw.decode("utf-8")
        for raw in completed.stdout.split(b"\0")
        if raw
    ]


def parse_frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip().strip('"\'')
    return fields


def related_targets(text: str) -> list[str]:
    if not text.startswith("---\n"):
        return []
    end = text.find("\n---\n", 4)
    if end < 0:
        return []
    lines = text[4:end].splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^related:\s*(.*?)\s*$", line)
        if not match:
            continue
        inline = match.group(1)
        if inline.startswith("[") and inline.endswith("]"):
            return [
                item.strip().strip('"\'')
                for item in inline[1:-1].split(",")
                if item.strip()
            ]
        targets: list[str] = []
        for nested in lines[index + 1 :]:
            nested_match = re.match(r"^\s+-\s+(.+?)\s*$", nested)
            if not nested_match:
                break
            targets.append(nested_match.group(1).strip().strip('"\''))
        return targets
    return []


def instruction_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def local_link_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    elif " " in target:
        target = target.split(" ", 1)[0]
    target = unquote(target.split("#", 1)[0])
    if not target or target.startswith(("/", "http://", "https://", "mailto:", "tel:")):
        return None
    return target


def main() -> int:
    problems: list[str] = []
    markdown_files = tracked_markdown()

    if not (REPOSITORY_ROOT / "docs" / "README.md").is_file():
        problems.append("missing repository documentation index: docs/README.md")

    for left_name, right_name in INSTRUCTION_PAIRS:
        left = REPOSITORY_ROOT / left_name
        right = REPOSITORY_ROOT / right_name
        if instruction_text(left) != instruction_text(right):
            problems.append(f"instruction mirror drift: {left_name} != {right_name}")

    for path in markdown_files:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(text)

        if relative.startswith("agent/docs/"):
            missing = REQUIRED_AGENT_DOC_FIELDS - set(frontmatter or {})
            if missing:
                problems.append(
                    f"missing agent doc metadata: {relative} ({', '.join(sorted(missing))})"
                )

        if relative.startswith("agent/tms_docs/"):
            required = {"type", "status", "captured_at", "verified_at"}
            missing = required - set(frontmatter or {})
            if missing:
                problems.append(
                    f"missing TMS snapshot metadata: {relative} ({', '.join(sorted(missing))})"
                )
            elif frontmatter and frontmatter.get("status") != "snapshot":
                problems.append(f"TMS document must be a snapshot: {relative}")

        if re.search(r"(?:^|/)docs/superpowers/(?:plans|specs)/", relative):
            status = (frontmatter or {}).get("status")
            if status not in HISTORICAL_STATUSES:
                problems.append(f"historical plan/spec lacks lifecycle status: {relative}")

        if relative.startswith("docs/ai-development/"):
            required = {"type", "status", "updated"}
            missing = required - set(frontmatter or {})
            if missing:
                problems.append(
                    f"missing AI-development lifecycle metadata: {relative} "
                    f"({', '.join(sorted(missing))})"
                )
            elif frontmatter and frontmatter.get("status") not in NON_CURRENT_STATUSES:
                problems.append(f"AI-development document looks current: {relative}")

        if relative.startswith("agent/docs/price_scripts/"):
            status = (frontmatter or {}).get("status")
            if status not in {"historical", "snapshot", "superseded"}:
                problems.append(f"retired price document looks current: {relative}")

        if relative == "agent/docs/ocr/ocr-self-learning-plan.md":
            status = (frontmatter or {}).get("status")
            if status not in HISTORICAL_STATUSES:
                problems.append(f"OCR learning plan lacks lifecycle status: {relative}")

        if relative == "console/known_issues.md":
            status = (frontmatter or {}).get("status")
            if status != "historical":
                problems.append(f"known-issues archive lacks historical status: {relative}")

        for target in related_targets(text):
            candidate = (path.parent / target.split("#", 1)[0]).resolve()
            if not candidate.exists():
                problems.append(f"broken frontmatter related path: {relative} -> {target}")

        for match in MARKDOWN_LINK_PATTERN.finditer(text):
            target = local_link_target(match.group(1))
            if target is None:
                continue
            candidate = (path.parent / target).resolve()
            if not candidate.exists():
                line_number = text.count("\n", 0, match.start()) + 1
                problems.append(f"broken Markdown link: {relative}:{line_number} -> {target}")

    if problems:
        raise SystemExit("\n".join(problems))
    print(f"documentation=ok tracked_markdown={len(markdown_files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
