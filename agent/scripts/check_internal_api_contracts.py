"""Enforce versioned Agent APIs and reject removed legacy callers."""

from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_MAIN = REPOSITORY_ROOT / "agent" / "main.py"
CALLER_ROOTS = (
    REPOSITORY_ROOT / "console",
    REPOSITORY_ROOT / "agent" / "feishu",
    REPOSITORY_ROOT / "agent" / "tools",
)
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
LEGACY_LOCAL_URLS = (
    "http://127.0.0.1:9000/tms",
    "http://localhost:9000/tms",
)


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _is_legacy_path(value: str) -> bool:
    return any(value == prefix or value.startswith(f"{prefix}/") for prefix in LEGACY_INTERNAL_PREFIXES)


def _caller_problems() -> list[str]:
    problems: list[str] = []
    checked_calls = {"_agent_request", "_get_admin", "_post_admin", "call_http_service"}
    for root in CALLER_ROOTS:
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    call_name = node.func.id
                else:
                    continue
                if call_name not in checked_calls:
                    continue
                candidates = list(node.args)
                candidates.extend(keyword.value for keyword in node.keywords if keyword.arg == "endpoint")
                for candidate in candidates:
                    value = _literal_string(candidate)
                    if value and (_is_legacy_path(value) or any(url in value for url in LEGACY_LOCAL_URLS)):
                        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
                        problems.append(f"legacy Agent caller: {relative}:{node.lineno} {value}")

            source = path.read_text(encoding="utf-8")
            for legacy_url in LEGACY_LOCAL_URLS:
                if legacy_url in source:
                    relative = path.relative_to(REPOSITORY_ROOT).as_posix()
                    problems.append(f"legacy Agent base URL: {relative} {legacy_url}")
    return problems


def _agent_route_problems() -> list[str]:
    problems: list[str] = []
    source = AGENT_MAIN.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(AGENT_MAIN))
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr not in {"delete", "get", "patch", "post", "put"} or not decorator.args:
                continue
            route = _literal_string(decorator.args[0])
            if route and _is_legacy_path(route):
                problems.append(f"legacy Agent route remains: agent/main.py:{node.lineno} {route}")

    if 'app.include_router(tms_router, deprecated=True)' in source:
        problems.append("deprecated root TMS router must be removed")
    if source.count('app.include_router(tms_router, prefix="/internal/v1")') != 1:
        problems.append("Agent must include the versioned TMS router exactly once")
    return problems


def main() -> int:
    problems = _caller_problems() + _agent_route_problems()
    if problems:
        raise SystemExit("\n".join(problems))
    print("internal_api_contracts=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
