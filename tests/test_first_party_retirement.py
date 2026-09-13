"""Retirement removes executable dependencies, never historical evidence."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.automation_plugins.errors import PluginPackageError
from agent.automation_plugins.first_party import bootstrap_first_party_plugins, release_first_party_automation_ids
from agent.automation_plugins.first_party_retirement import (
    read_retired_release, require_completed_migrations, retired_release_index,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from scripts.build_first_party_plugin_release import main as build_release

SHA = "a" * 40


def test_package_free_release_build_and_exact_validation(tmp_path):
    root = tmp_path / "release"
    assert build_release(["--service-v2-only", "--release-sha", SHA, "--output-root", str(root)]) == 0
    result = read_retired_release(root, SHA)
    assert result.package_count == 0
    assert result.instance_count == len(release_first_party_automation_ids())
    with pytest.raises(PluginPackageError):
        read_retired_release(root, "b" * 40)
    (root / "old.zip").write_bytes(b"retired executable must not be shipped")
    with pytest.raises(PluginPackageError):
        read_retired_release(root, SHA)


def test_empty_legacy_index_cannot_claim_retirement(tmp_path):
    (tmp_path / "release-index.json").write_bytes(canonical_json_bytes({
        "schema_version": 1, "release_sha": SHA, "plugins": {},
    }))
    assert read_retired_release(tmp_path, SHA) is None
    index = retired_release_index(SHA)
    index["retired_instance_ids"].pop()
    (tmp_path / "release-index.json").write_bytes(canonical_json_bytes(index))
    with pytest.raises(PluginPackageError):
        read_retired_release(tmp_path, SHA)


def test_completed_bootstrap_does_not_load_or_rebuild_legacy_packages():
    class MustNotLoad:
        def __getattr__(self, name):
            raise AssertionError("retired bootstrap accessed " + name)
    result = bootstrap_first_party_plugins(MustNotLoad(), core_catalog=MustNotLoad(),
        package_provider=MustNotLoad(), current_release_sha=SHA, expected_release_sha=SHA,
        superseded_automation_ids=tuple(release_first_party_automation_ids()))
    assert result.ok and not result.created and not result.existing
    assert set(result.superseded) == release_first_party_automation_ids()
    partial = bootstrap_first_party_plugins(MustNotLoad(), core_catalog=MustNotLoad(),
        current_release_sha=SHA, expected_release_sha=SHA,
        superseded_automation_ids=tuple(sorted(release_first_party_automation_ids()))[:-1])
    assert not partial.ok


class RetirementRows:
    state = "COMPLETED"
    source_enabled = False
    target_removed = False
    target_runtime = "SERVICE_V2"

    @contextmanager
    def unit_of_work(self):
        yield SimpleNamespace(automation_plugins=self)

    def get_authoritative_plugin_migration_pair_for_automation(self, identity, *, for_update):
        assert for_update is False
        return {"source_automation_id": identity, "target_automation_id": "target-" + identity,
                "state": self.state, "rolled_back_at": None}

    def get_project(self, identity, *, for_update):
        assert not for_update
        if identity.startswith("target-"):
            return None if self.target_removed else {"plugin_id": "v2", "plugin_version": "2.0.1"}
        return {"enabled": self.source_enabled, "state": "ENABLED" if self.source_enabled else "DISABLED"}

    def get_version(self, plugin_id, version):
        return {"runtime_model": self.target_runtime}


@pytest.mark.parametrize("field,value", [
    ("state", "TESTING"), ("state", "READY"), ("state", "CUTOVER"),
    ("state", "ROLLED_BACK"), ("source_enabled", True), ("target_runtime", "ACTION_V1"),
])
def test_retirement_rejects_unfinished_or_conflicting_database_ownership(field, value):
    rows = RetirementRows()
    setattr(rows, field, value)
    with pytest.raises(PluginPackageError):
        require_completed_migrations(rows)


def test_retired_source_stays_retired_after_intentional_target_uninstall():
    rows = RetirementRows()
    require_completed_migrations(rows)
    rows.target_removed = True
    require_completed_migrations(rows)
