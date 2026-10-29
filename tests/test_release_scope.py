"""The release ships exactly the current Service V2 packages."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts import release_scope


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "agent" / "service_v2_plugins"


def _package_directories() -> set[str]:
    return {path.name for path in PLUGIN_ROOT.iterdir()
            if path.is_dir() and path.name not in {"_shared", "__pycache__"}}


def test_every_package_directory_is_listed_exactly_once():
    assert release_scope.SERVICE_V2_PLUGIN_IDS == _package_directories()
    assert not (release_scope.STANDALONE_SERVICE_V2_PLUGIN_IDS
                & release_scope.SHARED_BUILDER_SERVICE_V2_PLUGIN_IDS)


def test_release_sources_cover_each_package_and_the_shared_builder():
    sources = {path.relative_to(PLUGIN_ROOT).parts[0] for path in release_scope.release_source_files(ROOT)}
    assert sources == release_scope.SERVICE_V2_PLUGIN_IDS | {"_shared"}


def _staged_copy(tmp_path: Path) -> Path:
    staged = tmp_path / "stage"
    shutil.copytree(PLUGIN_ROOT, staged / "agent" / "service_v2_plugins",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return staged


def test_staged_tree_with_exact_packages_passes(tmp_path):
    release_scope.verify_staged_tree(_staged_copy(tmp_path))


@pytest.mark.parametrize("mutation", ["unknown_package", "missing_payload"])
def test_staged_tree_rejects_unknown_or_incomplete_packages(tmp_path, mutation):
    staged = _staged_copy(tmp_path)
    plugins = staged / "agent" / "service_v2_plugins"
    if mutation == "unknown_package":
        (plugins / "retired_plugin_v1").mkdir()
    else:
        (plugins / "sync_scan_codes_v2" / "payload" / "plugin.py").unlink()
    with pytest.raises(release_scope.ReleaseScopeError):
        release_scope.verify_staged_tree(staged)
