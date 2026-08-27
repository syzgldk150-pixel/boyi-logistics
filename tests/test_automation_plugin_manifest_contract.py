from __future__ import annotations

import copy
import uuid
from unittest.mock import patch

import pytest

from agent.automation_plugins.errors import PluginConflictError, PluginManifestError
from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.invocation import compile_instance_arguments
from agent.automation_plugins.manifest import (
    AutomationPluginManifest,
    runtime_descriptor_matches_signed_installation,
)
from agent.automation_plugins.mysql_repository import (
    MySQLAutomationPluginRepositoryAdapter,
    _registration_rows,
    _legacy_project_config,
    _transient_entry,
)
from agent.automation_plugins.models import (
    FirstPartyInstanceSeed,
    PluginTrustSource,
    PluginVersionRecord,
)
from agent.tool_registry import ToolRegistry
from shared.automation_project_manifest import FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES
from shared.automation_plugin_repository import (
    AutomationPluginPreparedTargetOccupied,
)
from shared.orchestration_repository_support import ConcurrentUpdateError


def _manifest_mapping() -> dict:
    return resolve_first_party_manifests(ToolRegistry())["sync_scan_codes"].to_mapping()


def test_governance_anchor_is_signed_and_cannot_drift_from_action_contract() -> None:
    source = _manifest_mapping()
    source["governance_anchor"]["risk_level"] = "low"
    assert source["tool_contract"]["risk_level"] != "low"
    with pytest.raises(PluginManifestError, match="governance anchor"):
        AutomationPluginManifest.from_mapping(source)


def test_governance_anchor_rejects_unknown_or_missing_fields() -> None:
    source = _manifest_mapping()
    source["governance_anchor"]["unreviewed_policy"] = True
    with pytest.raises(PluginManifestError, match="unsupported fields"):
        AutomationPluginManifest.from_mapping(source)
    missing = _manifest_mapping()
    missing["governance_anchor"].pop("permissions")
    with pytest.raises(PluginManifestError, match="missing fields"):
        AutomationPluginManifest.from_mapping(missing)


def test_legacy_v1_broker_operation_without_effect_projects_conservatively_to_write() -> None:
    source = _manifest_mapping()
    source["runtime_permissions"]["broker_operations"][0].pop("effect")
    manifest = AutomationPluginManifest.from_mapping(source)
    assert manifest.runtime_permissions["broker_operations"][0]["effect"] == "write"
    invalid = _manifest_mapping()
    invalid["runtime_permissions"]["broker_operations"][0]["effect"] = "mutate"
    with pytest.raises(PluginManifestError, match="effect"):
        AutomationPluginManifest.from_mapping(invalid)


def test_legacy_runtime_descriptor_compatibility_is_exact_and_write_only() -> None:
    source = _manifest_mapping()
    legacy_operations = source["runtime_permissions"]["broker_operations"][:2]
    assert len(legacy_operations) == 2
    for operation in legacy_operations:
        operation.pop("effect")
    manifest = AutomationPluginManifest.from_mapping(source)
    signed = {
        "runtime": copy.deepcopy(dict(manifest.runtime)),
        "runtime_permissions": copy.deepcopy(
            manifest.to_signed_mapping()["runtime_permissions"]
        ),
        "account_roles": [copy.deepcopy(dict(item)) for item in manifest.account_roles],
        "resource_roles": [
            copy.deepcopy(dict(item)) for item in manifest.resource_roles
        ],
        "install_metadata": {
            "install_root": "/plugins/legacy",
            "python_relative": "venv/bin/python",
        },
    }
    normalized = copy.deepcopy(signed)
    for operation in normalized["runtime_permissions"]["broker_operations"][:2]:
        operation["effect"] = "write"

    assert runtime_descriptor_matches_signed_installation(
        normalized,
        signed,
        schema_version=1,
    )
    assert not runtime_descriptor_matches_signed_installation(
        normalized,
        signed,
        schema_version=2,
    )

    mixed_projection = copy.deepcopy(normalized)
    mixed_projection["runtime_permissions"]["broker_operations"][1].pop("effect")
    assert not runtime_descriptor_matches_signed_installation(
        mixed_projection,
        signed,
        schema_version=1,
    )

    wrong_effect = copy.deepcopy(normalized)
    wrong_effect["runtime_permissions"]["broker_operations"][0]["effect"] = "read"
    assert not runtime_descriptor_matches_signed_installation(
        wrong_effect,
        signed,
        schema_version=1,
    )

    wrong_action = copy.deepcopy(normalized)
    wrong_action["runtime_permissions"]["broker_operations"][0]["action"] += ".tampered"
    assert not runtime_descriptor_matches_signed_installation(
        wrong_action,
        signed,
        schema_version=1,
    )

    wrong_installation = copy.deepcopy(normalized)
    wrong_installation["install_metadata"]["python_relative"] = "venv/bin/other"
    assert not runtime_descriptor_matches_signed_installation(
        wrong_installation,
        signed,
        schema_version=1,
    )


def test_registration_persists_signed_manifest_not_legacy_execution_projection() -> None:
    for legacy_missing_effect in (False, True):
        source = _manifest_mapping()
        if legacy_missing_effect:
            source["runtime_permissions"]["broker_operations"][0].pop("effect")
        manifest = AutomationPluginManifest.from_mapping(source)
        package_sha256 = "a" * 64
        version = PluginVersionRecord(
            plugin_id=manifest.plugin_id,
            version=manifest.version,
            package_sha256=package_sha256,
            manifest_sha256=manifest.manifest_sha256,
            manifest=manifest.to_signed_mapping(),
            trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
            install_root="/plugins/signed",
            install_metadata={
                "archive_relative": "package-archive.zip",
                "archive_sha256": package_sha256,
                "python_relative": "venv/bin/python",
            },
        )

        _, persisted = _registration_rows(version, actor_id="test:release")

        assert persisted["manifest_json"] == manifest.to_signed_mapping()
        if legacy_missing_effect:
            assert "effect" not in persisted["manifest_json"][
                "runtime_permissions"
            ]["broker_operations"][0]
        else:
            assert persisted["manifest_json"] == manifest.to_mapping()


def test_production_manifest_rejects_legacy_core_tool_ref_runtime() -> None:
    source = _manifest_mapping()
    source["runtime"] = {
        "kind": "core_tool_ref",
        "tool_name": source["governance_anchor"]["name"],
    }
    source["tool_contract"] = copy.deepcopy(source["tool_contract"])
    with pytest.raises(PluginManifestError, match="runtime.kind"):
        AutomationPluginManifest.from_mapping(source)


def test_scan_contract_keeps_ingress_routes_but_removes_unverifiable_outbound_flow() -> None:
    source = _manifest_mapping()

    assert source["resource_roles"] == [
        {
            "role": "webhook_route",
            "allowed_kinds": ["webhook_route"],
            "required": False,
        },
        {
            "role": "feishu_route",
            "allowed_kinds": ["feishu_route"],
            "required": False,
        },
    ]
    webhook = source["invocation_contracts"]["webhook"]
    assert webhook["dynamic_resolvers"] == {}
    assert "trigger_flow" not in source["tool_contract"]["input_schema"][
        "properties"
    ]
    assert all(
        "trigger_flow" not in contract["argument_template"]
        for contract in source["invocation_contracts"].values()
    )
    assert all(
        "account_id" not in str(field).lower()
        for field in source["tool_contract"]["input_schema"]["properties"]
    )
    broker_pairs = {
        (item["operation"], item["action"])
        for item in source["runtime_permissions"]["broker_operations"]
    }
    assert ("network.request", "feishu.webhook.invoke") not in broker_pairs


def test_arrival_contract_uses_instance_sheet_roles_and_no_outbound_flow() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())[
        "sync_arrival_stats"
    ]
    source = manifest.to_mapping()

    roles = {item["role"]: item for item in source["resource_roles"]}
    for role, required in {
        "arrival_stats_primary_sheet": True,
        "arrival_stats_secondary_sheet": True,
        "arrival_stats_pending_sheet": False,
        "arrival_stats_archive_sheet": True,
        "arrival_stats_split_pending_sheet": True,
    }.items():
        assert roles[role] == {
            "role": role,
            "allowed_kinds": ["feishu_sheet"],
            "required": required,
        }
    assert roles["webhook_route"]["required"] is False
    assert roles["feishu_route"]["required"] is False
    assert "trigger_flow" not in source["tool_contract"]["input_schema"][
        "properties"
    ]
    assert source["config_schema"]["properties"]["pending_sheet_disabled"] == {
        "type": "boolean",
        "description": "是否跳过写入「未齐货物」飞书表",
    }
    assert all(
        "trigger_flow" not in contract["argument_template"]
        for contract in source["invocation_contracts"].values()
    )
    assert all(
        contract["argument_template"]["pending_sheet_disabled"]
        == {
            "source": "project_config",
            "key": "pending_sheet_disabled",
        }
        for contract in source["invocation_contracts"].values()
    )
    broker = {
        (item["operation"], item["action"]): item["roles"]
        for item in source["runtime_permissions"]["broker_operations"]
    }
    assert broker[("network.request", "feishu.sheet.replace")] == [
        "arrival_stats_primary_sheet",
        "arrival_stats_secondary_sheet",
        "arrival_stats_pending_sheet",
        "arrival_stats_split_pending_sheet",
    ]
    assert broker[("network.request", "feishu.sheet.add")] == [
        "arrival_stats_archive_sheet"
    ]
    assert ("network.request", "feishu.webhook.invoke") not in broker
    migration = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES["arrival_stats"]
    assert "trigger_flow" not in migration.legacy_arguments
    assert migration.legacy_arguments == {
        "account_id": "ronghui_default",
        "pending_sheet_disabled": True,
    }
    assert migration.resource_bindings == {
        "webhook_route": "phase7.stats_webhook",
        "feishu_route": "automation.feishu_route.arrival_stats",
        "arrival_stats_primary_sheet": "phase7.arrive_primary_sheet",
        "arrival_stats_secondary_sheet": "phase7.arrive_secondary_sheet",
        "arrival_stats_archive_sheet": "phase7.stats_archive_sheet",
        "arrival_stats_split_pending_sheet": "phase7.split_pending_target_sheet",
    }
    migrated_config = _legacy_project_config(migration, manifest)
    assert migrated_config == {"pending_sheet_disabled": True}
    compiled = compile_instance_arguments(
        _transient_entry(migration.automation_id, manifest),
        config=migrated_config,
        account_bindings=migration.legacy_account_bindings,
        resource_bindings=dict(migration.resource_bindings),
        entrypoint="console",
        resolve_dynamic=False,
    )
    assert compiled.arguments["pending_sheet_disabled"] is True
    assert "arrival_stats_pending_sheet" not in compiled.resource_bindings


def test_split_contract_is_human_triggered_with_verified_selection_fields() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())[
        "split_pending_problem_upload"
    ]
    source = manifest.to_mapping()
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[
        "split_pending_problem_upload"
    ]

    assert source["version"] == "1.0.23"
    assert source["allowed_entrypoints"] == ["console", "feishu"]
    assert source["scheduling"] == {
        "supported": False,
        "allowed_kinds": [],
        "max_daily_times": 0,
    }
    expected_dynamic = {
        "dry_run": "verified_{entrypoint}_dry_run",
        "preview_fingerprint": "verified_{entrypoint}_preview_fingerprint",
        "selected_bill_codes": "verified_{entrypoint}_selected_bill_codes",
    }
    for entrypoint in ("console", "feishu"):
        assert source["invocation_contracts"][entrypoint]["dynamic_resolvers"] == {
            field: resolver.format(entrypoint=entrypoint)
            for field, resolver in expected_dynamic.items()
        }
        assert source["invocation_contracts"][entrypoint]["argument_template"] == {}
    assert set(template.allowed_entrypoints) == {"console", "feishu"}
    assert template.legacy_arguments["dry_run"] is True


def test_self_pickup_contract_has_verified_human_selection_fields() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())[
        "self_pickup_problem_upload"
    ]
    source = manifest.to_mapping()
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[
        "self_pickup_problem_upload"
    ]

    assert source["version"] == "1.0.23"
    assert source["allowed_entrypoints"] == ["console", "feishu"]
    assert source["scheduling"] == {
        "supported": False,
        "allowed_kinds": [],
        "max_daily_times": 0,
    }
    for entrypoint in ("console", "feishu"):
        assert source["invocation_contracts"][entrypoint]["dynamic_resolvers"] == {
            "dry_run": f"verified_{entrypoint}_dry_run",
            "preview_fingerprint": f"verified_{entrypoint}_preview_fingerprint",
            "selected_bill_codes": f"verified_{entrypoint}_selected_bill_codes",
        }
    assert set(template.allowed_entrypoints) == {"console", "feishu"}
    assert template.legacy_arguments["dry_run"] is True


def test_arrival_bootstrap_persists_disabled_pending_sheet_invocations() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())[
        "sync_arrival_stats"
    ]
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES["arrival_stats"]

    class AutomationPlugins:
        def __init__(self) -> None:
            self.project = None
            self.saved = None

        def get_project(self, _automation_id, *, for_update):
            assert for_update is True
            return self.project

        def install_project_instance(self, row):
            self.project = {**row, "record_version": 1}
            return self.project

        def initialize_project_config(self, _automation_id, *, enabled_entrypoints):
            assert tuple(enabled_entrypoints) == tuple(template.allowed_entrypoints)
            return {"config_version": 1}

        def get_project_config(self, _automation_id, *, for_update):
            assert for_update is True
            return {
                "config_version": 1,
                "committed_schedule": {
                    "kind": "none",
                    "times": [],
                    "enabled": False,
                },
            }

        def save_project_config(self, _automation_id, **payload):
            self.saved = copy.deepcopy(payload)

        def set_project_enabled(
            self,
            _automation_id,
            *,
            enabled,
            expected_record_version,
        ):
            assert enabled is True
            assert expected_record_version == 1

    class UnitOfWork:
        def __init__(self) -> None:
            self.automation_plugins = AutomationPlugins()
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def commit(self):
            self.committed = True

    class Orchestration:
        def __init__(self) -> None:
            self.uow = UnitOfWork()

        def unit_of_work(self):
            return self.uow

    orchestration = Orchestration()
    repository = MySQLAutomationPluginRepositoryAdapter(orchestration)
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root=None,
    )
    seed = FirstPartyInstanceSeed(
        automation_id=template.automation_id,
        plugin_id=template.tool_name,
        version=manifest.version,
        display_name=template.automation_id,
        allowed_entrypoints=tuple(template.allowed_entrypoints),
    )

    with patch.object(repository, "_register"):
        result = repository.bootstrap_missing(
            (version,),
            (seed,),
            release_sha="bootstrap-test-release",
        )

    assert result.created == ("arrival_stats",)
    assert orchestration.uow.committed is True
    saved = orchestration.uow.automation_plugins.saved
    assert saved is not None
    assert saved["config"] == {"pending_sheet_disabled": True}
    assert "arrival_stats_pending_sheet" not in saved["resource_bindings"]
    assert saved["compiled_invocations"]
    assert all(
        invocation["arguments"]["pending_sheet_disabled"] is True
        for invocation in saved["compiled_invocations"].values()
    )


def test_first_party_bootstrap_stages_existing_older_instance_to_release_version() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_arrival_stats"]
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES["arrival_stats"]

    class AutomationPlugins:
        def get_project(self, automation_id, *, for_update):
            assert automation_id == template.automation_id
            assert for_update is True
            return {
                "automation_id": automation_id,
                "plugin_id": manifest.plugin_id,
                "plugin_version": "1.0.0",
                "record_version": 4,
                "target_generation": 1,
                "committed_generation": 1,
                "reconcile_state": "STABLE",
            }

    class UnitOfWork:
        def __init__(self) -> None:
            self.automation_plugins = AutomationPlugins()
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def commit(self):
            self.committed = True

    class Orchestration:
        def __init__(self) -> None:
            self.uow = UnitOfWork()

        def unit_of_work(self):
            return self.uow

    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root=None,
    )
    seed = FirstPartyInstanceSeed(
        automation_id=template.automation_id,
        plugin_id=template.tool_name,
        version=manifest.version,
        display_name=template.automation_id,
        allowed_entrypoints=tuple(template.allowed_entrypoints),
    )
    repository = MySQLAutomationPluginRepositoryAdapter(Orchestration())

    with (
        patch.object(repository, "_register"),
        patch.object(
            repository,
            "_prepare_first_party_upgrade_configuration",
            return_value=("1.0.0", 5, None),
        ) as prepare,
        patch.object(repository, "upgrade_instance") as upgrade,
    ):
        result = repository.bootstrap_missing(
            (version,),
            (seed,),
            release_sha="a" * 40,
        )

    assert result.created == ()
    assert result.existing == (template.automation_id,)
    prepare.assert_called_once_with(
        seed=seed,
        version=version,
        release_sha="a" * 40,
        expected_current_version="1.0.0",
        allow_blocked_unknown_write_archive=False,
    )
    upgrade.assert_called_once()
    call = upgrade.call_args
    assert call.args == (template.automation_id, version)
    assert call.kwargs["actor_role"] == "super_admin"
    assert call.kwargs["expected_current_version"] == "1.0.0"
    assert call.kwargs["expected_record_version"] == 5
    assert "prepared_configuration_request_id" not in call.kwargs
    assert "allow_blocked_unknown_write_archive" not in call.kwargs
    uuid.UUID(call.kwargs["request_id"])

    with (
        patch.object(repository, "_register"),
        patch.object(
            repository,
            "_prepare_first_party_upgrade_configuration",
            return_value=(version.version, 6, None),
        ),
        patch.object(repository, "upgrade_instance") as replayed_upgrade,
    ):
        replayed = repository.bootstrap_missing(
            (version,),
            (seed,),
            release_sha="a" * 40,
        )

    assert replayed.existing == (template.automation_id,)
    replayed_upgrade.assert_not_called()

    with (
        patch.object(repository, "_register"),
        patch.object(
            repository,
            "_prepare_first_party_upgrade_configuration",
            return_value=("1.0.0", 5, str(uuid.uuid4())),
        ),
        patch.object(
            repository,
            "upgrade_instance",
            side_effect=AutomationPluginPreparedTargetOccupied(
                "prepared target is no longer empty"
            ),
        ),
    ):
        deferred = repository.bootstrap_missing(
            (version,),
            (seed,),
            release_sha="a" * 40,
        )

    assert deferred.existing == (template.automation_id,)

    with (
        patch.object(repository, "_register"),
        patch.object(
            repository,
            "_prepare_first_party_upgrade_configuration",
            return_value=("1.0.0", 5, str(uuid.uuid4())),
        ),
        patch.object(
            repository,
            "upgrade_instance",
            side_effect=ConcurrentUpdateError("policy lineage changed"),
        ),
        pytest.raises(ConcurrentUpdateError, match="policy lineage changed"),
    ):
        repository.bootstrap_missing(
            (version,),
            (seed,),
            release_sha="a" * 40,
        )


def test_first_party_bootstrap_leaves_same_release_version_stable() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_arrival_stats"]
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES["arrival_stats"]

    class AutomationPlugins:
        def get_project(self, automation_id, *, for_update):
            assert automation_id == template.automation_id
            assert for_update is True
            return {
                "automation_id": automation_id,
                "plugin_id": manifest.plugin_id,
                "plugin_version": manifest.version,
                "record_version": 7,
                "target_generation": 3,
                "committed_generation": 3,
                "reconcile_state": "STABLE",
            }

    class UnitOfWork:
        automation_plugins = AutomationPlugins()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def commit(self):
            return None

    class Orchestration:
        @staticmethod
        def unit_of_work():
            return UnitOfWork()

    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
        manifest=manifest.to_signed_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root=None,
    )
    seed = FirstPartyInstanceSeed(
        automation_id=template.automation_id,
        plugin_id=template.tool_name,
        version=manifest.version,
        display_name=template.automation_id,
        allowed_entrypoints=tuple(template.allowed_entrypoints),
    )
    repository = MySQLAutomationPluginRepositoryAdapter(Orchestration())

    with (
        patch.object(repository, "_register"),
        patch.object(
            repository,
            "_prepare_first_party_upgrade_configuration",
        ) as prepare,
        patch.object(repository, "upgrade_instance") as upgrade,
    ):
        result = repository.bootstrap_missing(
            (version,),
            (seed,),
            release_sha="b" * 40,
        )

    assert result.created == ()
    assert result.existing == (template.automation_id,)
    prepare.assert_not_called()
    upgrade.assert_not_called()


def test_first_party_bootstrap_never_downgrades_existing_instance() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_arrival_stats"]
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES["arrival_stats"]

    class AutomationPlugins:
        def get_project(self, _automation_id, *, for_update):
            assert for_update is True
            return {
                "plugin_id": manifest.plugin_id,
                "plugin_version": "9.0.0",
                "record_version": 2,
            }

    class UnitOfWork:
        automation_plugins = AutomationPlugins()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def commit(self):
            raise AssertionError("downgrade candidate must abort before commit")

    class Orchestration:
        def unit_of_work(self):
            return UnitOfWork()

    repository = MySQLAutomationPluginRepositoryAdapter(Orchestration())
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root=None,
    )
    seed = FirstPartyInstanceSeed(
        automation_id=template.automation_id,
        plugin_id=template.tool_name,
        version=manifest.version,
        display_name=template.automation_id,
        allowed_entrypoints=tuple(template.allowed_entrypoints),
    )

    with patch.object(repository, "_register"):
        with pytest.raises(PluginConflictError) as raised:
            repository.bootstrap_missing(
                (version,),
                (seed,),
                release_sha="b" * 40,
            )

    assert raised.value.code == "PLUGIN_UPGRADE_VERSION_INVALID"


def test_arrive_list_contract_uses_two_exact_instance_sheet_roles() -> None:
    source = resolve_first_party_manifests(ToolRegistry())[
        "sync_arrive_list"
    ].to_mapping()

    roles = {item["role"]: item for item in source["resource_roles"]}
    for role in ("arrive_primary_sheet", "arrive_secondary_sheet"):
        assert roles[role] == {
            "role": role,
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        }
    broker = {
        (item["operation"], item["action"]): item["roles"]
        for item in source["runtime_permissions"]["broker_operations"]
    }
    assert broker[("network.request", "feishu.sheet.replace")] == [
        "arrive_primary_sheet",
        "arrive_secondary_sheet",
    ]
    assert FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[
        "arrive_list"
    ].resource_bindings == {
        "feishu_route": "automation.feishu_route.arrive_list",
        "arrive_primary_sheet": "phase7.arrive_primary_sheet",
        "arrive_secondary_sheet": "phase7.arrive_secondary_sheet",
    }


def test_daily_send_orders_uses_bound_bitable_and_global_lock_contract() -> None:
    source = resolve_first_party_manifests(ToolRegistry())[
        "sync_daily_send_orders"
    ].to_mapping()

    assert {
        item["role"]: item for item in source["resource_roles"]
    }["send_order_bitable"] == {
        "role": "send_order_bitable",
        "allowed_kinds": ["feishu_bitable"],
        "required": True,
    }
    assert {
        (item["operation"], item["action"]): item["roles"]
        for item in source["runtime_permissions"]["broker_operations"]
    } == {
        ("ledger.invoke", "sync_daily_send_orders.lock.acquire"): ["account_id"],
        ("ledger.invoke", "sync_daily_send_orders.lock.release"): ["account_id"],
        ("browser.invoke", "ronghui.send_order.read_page"): ["account_id"],
        ("network.request", "feishu.bitable.list_records"): [
            "send_order_bitable"
        ],
        ("network.request", "feishu.bitable.delete_records"): [
            "send_order_bitable"
        ],
        ("network.request", "feishu.bitable.write_records"): [
            "send_order_bitable"
        ],
        ("projection.invoke", "waybill.ronghui.replace_date"): ["account_id"],
    }
    assert source["runtime_permissions"]["max_broker_calls"] == 1000
    for contract in source["invocation_contracts"].values():
        assert not {"request_body", "base_token", "table_id"} & set(
            contract["input_schema"]["properties"]
        )


def test_delivery_status_uses_bound_bitable_without_resource_locator_arguments() -> None:
    source = resolve_first_party_manifests(ToolRegistry())[
        "sync_delivery_status"
    ].to_mapping()

    assert {
        item["role"]: item for item in source["resource_roles"]
    }["delivery_status_bitable"] == {
        "role": "delivery_status_bitable",
        "allowed_kinds": ["feishu_bitable"],
        "required": True,
    }
    assert {
        (item["operation"], item["action"]): item["roles"]
        for item in source["runtime_permissions"]["broker_operations"]
    } == {
        (
            "network.request",
            "feishu.bitable.list_views",
        ): ["delivery_status_bitable"],
        (
            "network.request",
            "feishu.bitable.list_records",
        ): ["delivery_status_bitable"],
        ("browser.invoke", "ronghui.delivery_status.read"): ["account_id"],
        (
            "network.request",
            "feishu.bitable.write_records",
        ): ["delivery_status_bitable"],
        ("projection.invoke", "waybill.delivery_status.update"): ["account_id"],
    }
    for contract in source["invocation_contracts"].values():
        assert not {
            "base_token",
            "table_id",
            "view_id",
            "view_name",
        } & set(contract["input_schema"]["properties"])


def test_site_send_uses_two_required_bound_resources() -> None:
    source = resolve_first_party_manifests(ToolRegistry())[
        "sync_site_send_list"
    ].to_mapping()

    assert {
        item["role"]: item for item in source["resource_roles"]
    } == {
        "site_send_bitable": {
            "role": "site_send_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
        "site_send_sheet": {
            "role": "site_send_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    }
    assert {
        (item["operation"], item["action"]): item["roles"]
        for item in source["runtime_permissions"]["broker_operations"]
    } == {
        ("browser.invoke", "ronghui.site_send.read_page"): ["account_id"],
        ("network.request", "feishu.bitable.replace_snapshot"): [
            "site_send_bitable"
        ],
        ("network.request", "feishu.sheet.replace"): ["site_send_sheet"],
    }
    hidden = {
        "request_body",
        "base_token",
        "table_id",
        "spreadsheet_token",
        "range",
    }
    assert "target_date" not in source["config_schema"]["properties"]
    assert "target_date" not in source["config_schema"]["required"]
    for entrypoint, contract in source["invocation_contracts"].items():
        assert not hidden & set(contract["input_schema"]["properties"])
        assert "target_date" in contract["input_schema"]["required"]
        assert "target_date" not in contract["argument_template"]
        assert contract["dynamic_resolvers"] == {
            "target_date": "current_business_day"
        }, entrypoint


def test_yunda_dispatch_requires_and_migrates_reviewed_destination_config() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())[
        "sync_yunda_dispatch_forecast"
    ]
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[
        "yunda_dispatch_forecast"
    ]

    assert manifest.config_schema["required"] == ["dest_brch"]
    assert manifest.config_schema["properties"]["dest_brch"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "description": (
            "必填，项目实例显式保存的目的网点编码；插件和运行时均不提供默认值"
        ),
    }
    assert all(
        contract["argument_template"]["dest_brch"]
        == {"source": "project_config", "key": "dest_brch"}
        for contract in manifest.invocation_contracts.values()
    )

    migrated_config = _legacy_project_config(template, manifest)
    assert migrated_config == {"dest_brch": "56739382"}
    compiled = compile_instance_arguments(
        _transient_entry(template.automation_id, manifest),
        config=migrated_config,
        account_bindings=template.legacy_account_bindings,
        resource_bindings=dict(template.resource_bindings),
        entrypoint="scheduler",
        resolve_dynamic=False,
    )
    assert compiled.arguments == {"dest_brch": "56739382"}

    with pytest.raises(ValueError, match="missing required properties.*dest_brch"):
        compile_instance_arguments(
            _transient_entry(template.automation_id, manifest),
            config={},
            account_bindings=template.legacy_account_bindings,
            resource_bindings=dict(template.resource_bindings),
            entrypoint="scheduler",
            resolve_dynamic=False,
        )
