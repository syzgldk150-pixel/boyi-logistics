"""Static guard for package-safe online runtime imports."""

from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = PROJECT_ROOT / "agent" / "tms_runtime" / "scripts"
RUNTIME_ROOTS = (PROJECT_ROOT / "agent", PROJECT_ROOT / "tools", PROJECT_ROOT.parent / "console", PROJECT_ROOT.parent / "shared")
ALLOWED_LEGACY_ISOLATION = {
    PROJECT_ROOT / "tools" / "price_tool.py",
    PROJECT_ROOT / "tools" / "finance_tool.py",
}


def _python_files(root: Path):
    yield from (
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and "tests" not in path.parts
    )


def main() -> int:
    problems: list[str] = []
    for root in RUNTIME_ROOTS:
        if not root.is_dir():
            continue
        for path in _python_files(root):
            source = path.read_text(encoding="utf-8-sig")
            if "sys.path.insert" in source and path not in ALLOWED_LEGACY_ISOLATION:
                problems.append(f"global sys.path mutation: {path.relative_to(PROJECT_ROOT.parent)}")
            if path.is_relative_to(SCRIPT_ROOT):
                tree = ast.parse(source, filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module in {
                        "browser_manager",
                        "shared_login",
                        "r7_login",
                        "r7_login_manager",
                        "r13_login_manager",
                        "mysql_sink",
                        "auto_checkin_r7",
                        "waybill_tracking",
                        "get_infor",
                    }:
                        problems.append(f"bare runtime import: {path.relative_to(PROJECT_ROOT.parent)}:{node.lineno}")
    if problems:
        raise SystemExit("\n".join(problems))
    print("runtime_import_boundaries=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
