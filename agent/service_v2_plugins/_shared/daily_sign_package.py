"""Build the reviewed daily-sign sources with only package-local I/O imports.

The workflow/formulas/renderers are the exact maintained business sources.
The subprocess has no Host Python modules, SQL client or network connection.
"""
from pathlib import Path


def daily_sign_business_files(repository: Path) -> dict[str, bytes]:
    paths = {
        "daily_sign_sync_tool": "agent/tools/daily_sign_sync_tool.py",
        "daily_sign_pipeline": "agent/tools/daily_sign_pipeline.py",
        "daily_sign_rules": "agent/tools/daily_sign_rules.py",
        "daily_sign_readback": "agent/tools/daily_sign_readback.py",
        "daily_sign_material": "agent/tools/daily_sign_material.py",
        "phase7_sync_common": "agent/tools/phase7_sync_common.py",
        "feishu_readback": "agent/agent/feishu_readback.py",
    }
    replacements = {
        "from agent import feishu_readback": "from business import feishu_readback",
        "from agent.tms_runtime.account_manager import": "from daily_sign_io import",
        "from agent.workflow_resource_store import": "from daily_sign_io import",
        "from agent.automation_plugins.errors import": "from daily_sign_io import",
        "from tools.daily_sign_store import": "from daily_sign_io import",
        "from tools.feishu_cli_tool import": "from daily_sign_io import",
        "from tools.phase7_mysql_store import": "from daily_sign_io import",
        "from tools.tms_tool import": "from daily_sign_io import",
    }
    replacements.update({f"from tools.{name} import": f"from business.{name} import" for name in paths})
    entries = {"payload/business/__init__.py": b""}
    for name, path in paths.items():
        text = (repository/path).read_text(encoding="utf-8")
        for original, packaged in replacements.items():
            text = text.replace(original, packaged)
        if name == "daily_sign_pipeline":
            original = '    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12]\n    return f"{prefix}:{digest}"'
            if text.count(original) != 1:
                raise ValueError("Daily-sign account scope adapter changed; review packaging")
            text = text.replace(original, '    from daily_sign_io import source_scope\n    return source_scope(prefix, account_id)')
        entries[f"payload/business/{name}.py"] = text.encode("utf-8")
    # Tracking normalization is a shared data identity rule, shipped verbatim.
    import ast
    source = (repository/"agent/tools/phase7_mysql_store.py").read_text(encoding="utf-8")
    names = {"_R_CHILD_TRACKING_RE", "_RONGHUI_NUMERIC_CHILD_TRACKING_RE", "_clean_text", "is_receipt_like_tracking", "is_child_like_tracking", "main_tracking_from_scan_code"}
    parts = []
    found = set()
    for node in ast.parse(source).body:
        identifiers = {node.name} if isinstance(node, ast.FunctionDef) else {target.id for target in node.targets if isinstance(target, ast.Name)} if isinstance(node, ast.Assign) else set()
        if identifiers & names:
            parts.append(ast.get_source_segment(source, node))
            found.update(identifiers & names)
    if found != names:
        raise ValueError("Daily-sign tracking identity rules changed; review packaging")
    entries["payload/business/tracking_ids.py"] = ("from typing import Any\nimport re\n\n" + "\n\n".join(parts) + "\n").encode()
    return entries
