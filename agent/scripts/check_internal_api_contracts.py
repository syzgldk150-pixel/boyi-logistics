"""Keep Console calls on versioned Agent APIs and legacy routes deprecated."""

from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_ROOT = REPOSITORY_ROOT / "console"
AGENT_MAIN = REPOSITORY_ROOT / "agent" / "main.py"
LEGACY_INTERNAL_PREFIXES = (
    "/admin",
    "/cancel-tool",
    "/chat",
    "/knowledge",
    "/run-tool",
    "/scheduled-tasks",
    "/tms",
    "/tool-logs",
    "/tool-output",
    "/tools",
    "/workflow-resources",
)


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _console_problems() -> list[str]:
    problems: list[str] = []
    for path in CONSOLE_ROOT.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "_agent_request":
                continue
            candidates = list(node.args[1:])
            candidates.extend(keyword.value for keyword in node.keywords if keyword.arg == "endpoint")
            for candidate in candidates:
                endpoint = _literal_string(candidate)
                if endpoint and endpoint.startswith(LEGACY_INTERNAL_PREFIXES):
                    relative = path.relative_to(REPOSITORY_ROOT).as_posix()
                    problems.append(f"Console uses legacy Agent endpoint: {relative}:{node.lineno} {endpoint}")
    return problems


def _agent_problems() -> list[str]:
    problems: list[str] = []
    tree = ast.parse(AGENT_MAIN.read_text(encoding="utf-8"), filename=str(AGENT_MAIN))
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr not in {"delete", "get", "patch", "post", "put"} or not decorator.args:
                continue
            route = _literal_string(decorator.args[0])
            if not route or not route.startswith(LEGACY_INTERNAL_PREFIXES):
                continue
            deprecated = any(
                keyword.arg == "deprecated"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in decorator.keywords
            )
            if not deprecated:
                problems.append(f"legacy Agent endpoint is not deprecated: agent/main.py:{node.lineno} {route}")

    source = AGENT_MAIN.read_text(encoding="utf-8")
    if 'app.include_router(tms_router, deprecated=True)' not in source:
        problems.append("legacy TMS router must remain explicitly deprecated")
    if 'app.include_router(tms_router, prefix="/internal/v1")' not in source:
        problems.append("versioned TMS router is missing")
    return problems


def main() -> int:
    problems = _console_problems() + _agent_problems()
    if problems:
        raise SystemExit("\n".join(problems))
    print("internal_api_contracts=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
