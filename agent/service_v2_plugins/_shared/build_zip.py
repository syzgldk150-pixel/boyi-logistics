"""Build one deterministic, unsigned service-v2 ZIP using only the stdlib."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path


_SHARED_FILES = {
    "payload/main.py": "service_main.py",
    "payload/clock_runtime.py": "clock_runtime.py",
    "payload/boyi_plugin_sdk.py": "boyi_plugin_sdk.py",
}
_ARRIVAL_SHARED_FILES = {
    "payload/main.py": "arrival_service_main.py",
    "payload/boyi_plugin_sdk.py": "boyi_plugin_sdk.py",
}
_ARRIVAL_PLUGIN_ID = "sync_arrival_stats_v2"
_SOURCE_FILES = {
    "manifest.json": "manifest.json",
    "payload/plugin.py": "payload/plugin.py",
}
_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _read_manifest(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 2 or value.get("runtime_model") != "service_v2":
        raise ValueError("source manifest must declare schema-v2 service_v2")
    return value


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o444) << 16
    return info


def build_plugin_zip(source_directory: Path | str, output_path: Path | str) -> Path:
    """Create a byte-reproducible package at a caller-selected new path."""

    source = Path(source_directory).resolve()
    output = Path(output_path).resolve()
    if not source.is_dir():
        raise FileNotFoundError("plugin source directory does not exist")
    if output.exists() or output.is_symlink():
        raise FileExistsError("output ZIP already exists")
    manifest = _read_manifest(source / "manifest.json")
    shared = Path(__file__).resolve().parent
    entries = {package_path: (source / source_path).read_bytes() for package_path, source_path in _SOURCE_FILES.items()}
    shared_files = _SHARED_FILES
    if manifest.get("plugin_id") == _ARRIVAL_PLUGIN_ID:
        shared_files = _ARRIVAL_SHARED_FILES
        repository_root = source.parents[2]
        first_party = repository_root / "agent" / "first_party_automation_plugins"
        entries.update(
            {
                "payload/action.py": (
                    first_party / "sync_arrival_stats" / "payload" / "action.py"
                ).read_bytes(),
                "payload/boyi_plugin_result.py": (
                    first_party / "_runtime" / "result.py"
                ).read_bytes(),
            }
        )
    entries.update(
        {package_path: (shared / source_path).read_bytes() for package_path, source_path in shared_files.items()}
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=False) as archive:
            for name in sorted(entries):
                archive.writestr(_zip_info(name), entries[name])
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build_plugin_zip(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
