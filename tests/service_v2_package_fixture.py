"""Build real production plugin fixtures without importing local bytecode caches."""

import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from agent.automation_plugins.developer_v2 import build_service_v2_package
from service_v2_plugins._shared.build_zip import build_plugin_zip


def build_production_plugin_fixture(source: Path, output: Path) -> Path:
    """Use the declared source entrypoint, or the migrated package assembler."""
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if not (source / manifest["runtime"]["entrypoint"]).is_file():
        # Migrated packages have plugin.py; their declared main.py is supplied
        # by the existing first-party assembler, together with shared modules.
        return build_plugin_zip(source, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="src-", dir=output.parent) as directory:
        clean_source = Path(directory)
        shutil.copy2(source / "manifest.json", clean_source / "manifest.json")
        shutil.copytree(source / "payload", clean_source / "payload",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if manifest.get("settings_ui") is not None:
            shutil.copytree(source / "settings", clean_source / "settings",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return build_service_v2_package(clean_source, output)
