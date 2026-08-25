"""Validate an automation plugin source tree before any signing step."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.tool_registry import validate_registry


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "agent",
        "console",
        "feishu",
        "first_party_automation_plugins",
        "plugin_core_adapters",
        "shared",
        "tools",
    }
)


def _regular_source_root(value: Path) -> Path:
    if value.is_symlink():
        raise ValueError("source root must not be a symbolic link")
    root = value.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("source root must be a directory")
    for item in root.rglob("*"):
        if item.is_symlink() or (not item.is_dir() and not item.is_file()):
            raise ValueError("source tree contains an unsupported filesystem object")
    return root


def _load_manifest(path: Path) -> AutomationPluginManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest must be readable UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("manifest must contain one JSON object")
    return AutomationPluginManifest.from_mapping(raw)


def _python_tree(path: Path) -> ast.Module:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"payload source is unreadable: {path.name}") from exc
    try:
        compile(source, str(path), "exec", dont_inherit=True)
        return ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"payload source is invalid Python: {path.name}") from exc


def _validate_imports(tree: ast.Module, path: Path) -> None:
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    forbidden = imported_roots & _FORBIDDEN_IMPORT_ROOTS
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        dynamic_module = None
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            dynamic_module = node.args[0]
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
        ):
            dynamic_module = node.args[0]
        if (
            isinstance(dynamic_module, ast.Constant)
            and isinstance(dynamic_module.value, str)
        ):
            root = dynamic_module.value.split(".", 1)[0]
            if root in _FORBIDDEN_IMPORT_ROOTS:
                forbidden.add(root)
    if forbidden:
        raise ValueError(
            f"payload imports forbidden runtime modules in {path.name}: {sorted(forbidden)}"
        )


def _literal_action_id(tree: ast.Module) -> str:
    values: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "ACTION_ID" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.append(value.value)
    if len(values) != 1:
        raise ValueError("payload/action.py must define one literal ACTION_ID")
    return values[0]


def validate_source(root_value: Path) -> AutomationPluginManifest:
    root = _regular_source_root(root_value)
    manifest = _load_manifest(root / "manifest.json")
    payload_root = (root / "payload").resolve()
    entrypoint = (root / str(manifest.runtime["entrypoint"])).resolve()
    action_path = root / "payload" / "action.py"
    if (
        not entrypoint.is_relative_to(payload_root)
        or not entrypoint.is_file()
        or not action_path.is_file()
    ):
        raise ValueError("source tree must contain the declared entrypoint and payload/action.py")
    validated_tools = validate_registry(
        {"tools": [manifest.to_mapping()["tool_contract"]]},
        project_root=root,
    )
    if len(validated_tools) != 1 or validated_tools[0]["name"] != manifest.plugin_id:
        raise ValueError("manifest and tool contract identity do not match")
    for path in sorted((root / "payload").rglob("*.py")):
        tree = _python_tree(path)
        _validate_imports(tree, path)
    if _literal_action_id(_python_tree(action_path)) != manifest.plugin_id:
        raise ValueError("payload ACTION_ID must equal manifest plugin_id")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = validate_source(args.source_root)
    print(f"plugin_source=ok plugin_id={manifest.plugin_id} version={manifest.version}")
    print(f"manifest_sha256={manifest.manifest_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
