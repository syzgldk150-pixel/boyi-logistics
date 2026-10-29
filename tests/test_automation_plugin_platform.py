from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.configuration import normalize_project_schedule
from agent.automation_plugins.core_adapter import (
    AccountManagerSessionResolver,
)
from agent.automation_plugins.errors import (
    PluginConflictError,
    PluginExecutionError,
    PluginPackageError,
)
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import (
    PluginInstanceRecord,
    PluginProjectState,
    PluginRuntimeModel,
    PluginTrustSource,
    PluginVersionRecord,
)
from agent.automation_plugins.sandbox import FailClosedPluginSandbox
from agent.automation_plugins.release_config import load_production_plugin_release_config
from agent.automation_plugins.storage import LockedVirtualEnvironmentBuilder


def test_copied_venv_normalization_is_unchanged_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv_root = tmp_path / "venv"
    alias = venv_root / "Scripts" / "python3.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"unchanged")
    monkeypatch.setattr("agent.automation_plugins.storage.os.name", "nt")

    LockedVirtualEnvironmentBuilder._normalize_copied_venv(venv_root)

    assert alias.read_bytes() == b"unchanged"


class _PartiallyCorruptCatalogRepository:
    def __init__(self, good: PluginInstanceRecord) -> None:
        self.good = good

    @staticmethod
    def list_instance_ids() -> tuple[str, ...]:
        return ("good-project", "bad-project")

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        if automation_id == "bad-project":
            raise ValueError("persisted snapshot is corrupt")
        return self.good if automation_id == self.good.automation_id else None

    @staticmethod
    def list_instances() -> list[PluginInstanceRecord]:
        raise AssertionError("raw identity discovery must not parse every project")


def test_catalog_quarantines_one_corrupt_persisted_project() -> None:
    source = Path(__file__).resolve().parents[1] / "agent/service_v2_plugins/sync_arrive_list_v2/manifest.json"
    manifest = AutomationPluginManifestV2.from_mapping(json.loads(source.read_text(encoding="utf-8")))
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="1" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD,
        install_root="/srv/plugins/arrive-list",
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )
    good = PluginInstanceRecord(
        automation_id="good-project",
        display_name="good",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.DISABLED,
        active_version=version,
        enabled=False,
        target_generation=0,
        committed_generation=0,
    )
    catalog = PluginCatalog(_PartiallyCorruptCatalogRepository(good))

    projection = catalog.safe_projection()
    health = catalog.production_health(("good-project", "bad-project"))

    assert catalog.persisted_automation_ids() == ("bad-project", "good-project")
    assert [item["automation_id"] for item in projection["instances"]] == [
        "good-project"
    ]
    assert projection["unavailable_projects"] == {
        "bad-project": {
            "runtime_status": "UNAVAILABLE",
            "error_code": "PLUGIN_PROJECT_DATA_INVALID",
        }
    }
    assert health["ok"] is True
    assert health["runnable"] is True
    assert health["unsupported_automation_ids"] == []


@pytest.mark.parametrize("raw_ids", [("",), ("same", "same")])
def test_catalog_identity_corruption_remains_global(raw_ids: tuple[str, ...]) -> None:
    repository = _PartiallyCorruptCatalogRepository.__new__(
        _PartiallyCorruptCatalogRepository
    )
    repository.list_instance_ids = lambda: raw_ids
    catalog = PluginCatalog(repository)

    with pytest.raises(PluginConflictError) as raised:
        catalog.safe_projection()

    assert raised.value.code == "PLUGIN_IDENTITY_CONFLICT"


def test_catalog_identity_query_error_remains_global() -> None:
    repository = _PartiallyCorruptCatalogRepository.__new__(
        _PartiallyCorruptCatalogRepository
    )

    def _failed_query() -> tuple[str, ...]:
        raise RuntimeError("database unavailable")

    repository.list_instance_ids = _failed_query

    with pytest.raises(RuntimeError, match="database unavailable"):
        PluginCatalog(repository).safe_projection()


def test_account_resolver_rejects_bound_account_system_mismatch() -> None:
    class Manager:
        @staticmethod
        def require_active_binding_descriptor(account_id: str) -> dict[str, str]:
            return {
                "account_id": account_id,
                "system": "yunda",
                "account_purpose": "general",
                "session_profile": "yunda",
            }

    resolver = AccountManagerSessionResolver(Manager())

    with pytest.raises(PluginExecutionError) as blocked:
        resolver.require_active_binding_descriptor(
            account_id="acct-1",
            allowed_systems=["ronghui"],
        )

    assert blocked.value.code == "BROKER_ACCOUNT_SYSTEM_MISMATCH"


def test_schedule_capability_is_closed() -> None:
    capability = {
        "supported": True,
        "allowed_kinds": ["daily_times"],
        "max_daily_times": 2,
    }
    assert normalize_project_schedule(
        {"kind": "daily_times", "times": ["09:30", "08:00"], "enabled": True},
        capability,
    ) == {"kind": "daily_times", "times": ["08:00", "09:30"], "enabled": True}
    with pytest.raises(PluginConflictError):
        normalize_project_schedule(
            {"kind": "daily_times", "times": ["08:00", "09:00", "10:00"], "enabled": True},
            capability,
        )


def test_uploaded_python_runtime_defaults_to_fail_closed_sandbox() -> None:
    with pytest.raises(PluginExecutionError) as exc_info:
        asyncio.run(FailClosedPluginSandbox().launch())
    assert exc_info.value.code == "PLUGIN_SANDBOX_UNAVAILABLE"


def test_trust_source_enum_matches_migration_check() -> None:
    sql = Path("agent/migrations/033_plugin_service_v2_foundation.sql").read_text(
        encoding="utf-8"
    )
    marker = "CONSTRAINT chk_automation_plugin_trust_source CHECK"
    clause = sql[sql.index(marker) : sql.index(")\n    )", sql.index(marker))]
    assert {f"'{item.value}'" for item in PluginTrustSource} <= {
        token.strip().rstrip(",") for token in clause.replace("(", " ").split()
    }


def test_production_release_configuration_has_no_fallback(tmp_path: Path) -> None:
    release_sha = "abcdef1"
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    values = {
        "BOYI_AUTOMATION_PLUGIN_ROOT": str(plugin_root),
        "BOYI_AUTOMATION_PLUGIN_RELEASE_SHA": release_sha,
    }
    config = load_production_plugin_release_config(
        values,
        runtime_release_sha=release_sha,
    )
    assert config.plugin_root == plugin_root.resolve()
    assert config.storage_root == plugin_root.resolve() / "installed"
    assert config.runtime_root == plugin_root.resolve() / "runtime"
    with pytest.raises(PluginPackageError):
        load_production_plugin_release_config({}, runtime_release_sha=release_sha)
    with pytest.raises(PluginPackageError):
        load_production_plugin_release_config(values, runtime_release_sha="1234567")
    link = tmp_path / "plugins-link"
    link.symlink_to(plugin_root, target_is_directory=True)
    with pytest.raises(PluginPackageError):
        load_production_plugin_release_config(
            {**values, "BOYI_AUTOMATION_PLUGIN_ROOT": str(link)},
            runtime_release_sha=release_sha,
        )
