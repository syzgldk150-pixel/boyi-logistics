from __future__ import annotations

from pathlib import Path

import pytest

import agent.automation_plugins.storage as plugin_storage
from agent.automation_plugins.errors import (
    PluginConflictError,
    PluginPackageError,
)
from agent.automation_plugins.storage import FilesystemPluginStorage


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_storage_refuses_symlink_or_path_escape_during_materialization(tmp_path: Path) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    stage = storage.create_staging_root("safe_plugin", "1.0.0")
    (stage / "payload").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (stage / "payload" / "escape").symlink_to(outside)
    with pytest.raises(PluginPackageError, match="symbolic links"):
        storage.commit_staging_root(
            stage,
            plugin_id="safe_plugin",
            version="1.0.0",
            manifest_sha256="1" * 64,
        )
    assert outside.read_text(encoding="utf-8") == "keep"
    with pytest.raises(PluginPackageError, match="outside"):
        storage.remove_version_root(outside)


def test_storage_commit_and_recovery_share_one_expected_root_identity(
    tmp_path: Path,
) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    identity = {
        "plugin_id": "expected_root_plugin",
        "version": "1.2.3",
        "manifest_sha256": "a" * 64,
    }
    expected, exists = storage.inspect_expected_version_root(**identity)
    assert not exists
    stage = storage.create_staging_root(
        identity["plugin_id"],
        identity["version"],
    )
    (stage / "payload.py").write_text("pass\n", encoding="utf-8")
    assert storage.commit_staging_root(stage, **identity) == expected

    second_stage = storage.create_staging_root(
        identity["plugin_id"],
        identity["version"],
    )
    (second_stage / "payload.py").write_text("different\n", encoding="utf-8")
    with pytest.raises(PluginConflictError, match="already exists"):
        storage.commit_staging_root(second_stage, **identity)
    assert (expected / "payload.py").read_text(encoding="utf-8") == "pass\n"

    storage.remove_version_root(expected)
    outside = tmp_path / "outside-root"
    outside.mkdir()
    expected.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PluginPackageError, match="symbolic links"):
        storage.inspect_expected_version_root(**identity)
    assert expected.is_symlink()
    assert list(outside.iterdir()) == []


def test_storage_atomic_publish_never_replaces_racing_empty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    identity = {
        "plugin_id": "atomic_publish_plugin",
        "version": "1.2.3",
        "manifest_sha256": "b" * 64,
    }
    stage = storage.create_staging_root(
        identity["plugin_id"],
        identity["version"],
    )
    (stage / "payload.py").write_text("candidate\n", encoding="utf-8")
    original_inspect = storage.inspect_expected_version_root
    inspect_count = 0
    racing_inode: int | None = None
    racing_marker: Path | None = None

    def inspect_with_race(**kwargs):
        nonlocal inspect_count, racing_inode, racing_marker
        target, exists = original_inspect(**kwargs)
        inspect_count += 1
        if inspect_count == 2:
            assert not exists
            target.mkdir()
            racing_inode = target.lstat().st_ino
            racing_marker = target.parent / "concurrent-owner.marker"
            racing_marker.write_text(str(racing_inode), encoding="ascii")
        return target, exists

    monkeypatch.setattr(
        storage,
        "inspect_expected_version_root",
        inspect_with_race,
    )
    with pytest.raises(PluginConflictError, match="appeared during commit"):
        storage.commit_staging_root(stage, **identity)

    expected, exists = original_inspect(**identity)
    assert exists
    assert racing_inode is not None
    assert expected.lstat().st_ino == racing_inode
    assert list(expected.iterdir()) == []
    assert racing_marker is not None
    assert racing_marker.read_text(encoding="ascii") == str(racing_inode)
    assert (stage / "payload.py").read_text(encoding="utf-8") == "candidate\n"


def test_storage_atomic_publish_never_follows_racing_project_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    identity = {
        "plugin_id": "parent_race_plugin",
        "version": "1.2.3",
        "manifest_sha256": "c" * 64,
    }
    stage = storage.create_staging_root(
        identity["plugin_id"],
        identity["version"],
    )
    (stage / "payload.py").write_text("candidate\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_inspect = storage.inspect_expected_version_root
    inspect_count = 0
    displaced_project: Path | None = None

    def inspect_with_parent_race(**kwargs):
        nonlocal inspect_count, displaced_project
        target, exists = original_inspect(**kwargs)
        inspect_count += 1
        if inspect_count == 2:
            assert not exists
            displaced_project = target.parent.with_name(
                f"{target.parent.name}.displaced"
            )
            target.parent.rename(displaced_project)
            target.parent.symlink_to(outside, target_is_directory=True)
        return target, exists

    monkeypatch.setattr(
        storage,
        "inspect_expected_version_root",
        inspect_with_parent_race,
    )
    with pytest.raises(PluginPackageError, match="unsafe|opened safely"):
        storage.commit_staging_root(stage, **identity)

    expected_project = storage.root / identity["plugin_id"]
    assert expected_project.is_symlink()
    assert list(outside.iterdir()) == []
    assert displaced_project is not None and displaced_project.is_dir()
    assert list(displaced_project.iterdir()) == []
    assert (stage / "payload.py").read_text(encoding="utf-8") == "candidate\n"


def test_storage_refuses_hardlinks_before_materialize_or_remove(tmp_path: Path) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    outside = tmp_path / "outside.txt"
    outside.write_text("immutable", encoding="utf-8")
    original_mode = outside.stat().st_mode

    unsafe_stage = storage.create_staging_root("hardlink_plugin", "1.0.0")
    (unsafe_stage / "payload").mkdir()
    (unsafe_stage / "payload" / "linked.txt").hardlink_to(outside)
    with pytest.raises(PluginPackageError, match="hard-linked"):
        storage.commit_staging_root(
            unsafe_stage,
            plugin_id="hardlink_plugin",
            version="1.0.0",
            manifest_sha256="2" * 64,
        )

    safe_stage = storage.create_staging_root("hardlink_plugin", "1.0.1")
    (safe_stage / "payload").mkdir()
    installed_file = safe_stage / "payload" / "linked.txt"
    installed_file.write_text("private", encoding="utf-8")
    root = storage.commit_staging_root(
        safe_stage,
        plugin_id="hardlink_plugin",
        version="1.0.1",
        manifest_sha256="3" * 64,
    )
    installed_file = root / "payload" / "linked.txt"
    installed_file.unlink()
    installed_file.hardlink_to(outside)
    with pytest.raises(PluginPackageError, match="hard-linked"):
        storage.remove_version_root(root)
    assert outside.read_text(encoding="utf-8") == "immutable"
    assert outside.stat().st_mode == original_mode


def test_storage_refuses_windows_reparse_points(monkeypatch, tmp_path: Path) -> None:
    assert plugin_storage._is_reparse_point(  # noqa: SLF001 - explicit Windows flag vector
        Path("unused"),
        stat_result=type("WindowsStat", (), {"st_file_attributes": 0x0400})(),
    )
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    stage = storage.create_staging_root("reparse_plugin", "1.0.0")
    (stage / "payload").mkdir()
    simulated_junction = stage / "payload" / "junction"
    simulated_junction.mkdir()
    original = plugin_storage._is_reparse_point  # noqa: SLF001

    def is_reparse(path: Path, *, stat_result=None) -> bool:
        return Path(path) == simulated_junction or original(path, stat_result=stat_result)

    monkeypatch.setattr(plugin_storage, "_is_reparse_point", is_reparse)
    with pytest.raises(PluginPackageError, match="reparse points or junctions"):
        storage.commit_staging_root(
            stage,
            plugin_id="reparse_plugin",
            version="1.0.0",
            manifest_sha256="4" * 64,
        )

