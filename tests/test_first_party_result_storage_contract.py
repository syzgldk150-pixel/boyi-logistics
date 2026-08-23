from __future__ import annotations

import ast
import runpy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY_ROOT = ROOT / "agent" / "first_party_automation_plugins"


def test_first_party_source_system_literals_fit_evidence_storage() -> None:
    violations: list[tuple[str, int, str]] = []
    for path in sorted(FIRST_PARTY_ROOT.glob("*/payload/action.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        candidates: list[ast.AST] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(
                    isinstance(target, ast.Name) and target.id == "source_system"
                    for target in targets
                ):
                    candidates.append(node.value)
            elif isinstance(node, ast.Call):
                candidates.extend(
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "source_system"
                )
        for candidate in candidates:
            for value in ast.walk(candidate):
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and len(value.value) > 32
                ):
                    violations.append(
                        (str(path.relative_to(ROOT)), value.lineno, value.value)
                    )

    assert violations == []


def test_first_party_result_rejects_source_system_beyond_storage_limit() -> None:
    namespace = runpy.run_path(str(FIRST_PARTY_ROOT / "_runtime" / "result.py"))

    with pytest.raises(ValueError, match="source_system is invalid"):
        namespace["success_result"](
            data={},
            source_system="x" * 33,
            record_count=0,
            pagination_complete=True,
            evidence_refs=["evidence:test"],
        )
