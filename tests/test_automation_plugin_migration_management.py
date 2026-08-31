"""MIG001 migration and arrival-project management coverage."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.migration_binding_mapping import (
    reviewed_migration_binding_mapping,
)
from agent.automation_plugins.migration_entrypoint_ownership import (
    migration_target_entrypoints_and_ownership,
)
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginProjectState,
    PluginRuntimeModel,
    RuntimeReconcileState,
)
from agent.orchestration.models import Actor, ActorType
from shared.automation_plugin_migration_ownership import MIGRATION_OWNERSHIP_STATES


def _console_actor(*, super_admin: bool = True) -> Actor:
    return Actor(
        actor_type=ActorType.CONSOLE_ADMIN,
        actor_id="console-admin-1",
        roles=("super_admin" if super_admin else "admin",),
        authenticated_by="mysql_admin_session",
    )


def _entry(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "automation_id": "automation-1",
        "plugin_id": "example_action",
        "display_name": "Example action",
        "installed_version": "1.0.0",
        "record_version": 3,
        "enabled": False,
        "state": PluginProjectState.DISABLED.value,
        "target_generation": 2,
        "committed_generation": 1,
        "reconcile_state": RuntimeReconcileState.WAITING_COEFFECTS,
        "configured": True,
        "committed_snapshot": None,
        "project_config_version": 2,
        "project_config_sha256": "1" * 64,
        "account_bindings_sha256": "2" * 64,
        "resource_bindings_sha256": "3" * 64,
        "device_binding_sha256": "4" * 64,
        "current_enabled_entrypoints": ("console",),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_create_migration_pair_with_enabled_scheduler_is_production_gated() -> None:
    source, target, source_record = _arrival_migration_fixture(
        enabled_entrypoints=("console", "feishu"),
    )
    service = AutomationPluginManagementService(
        catalog=SimpleNamespace(
            require=lambda automation_id: {
                "arrival_stats": source,
                "arrival-stats-v2": target,
            }[automation_id]
        ),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(
            read=lambda _automation_id: replace(
                source_record,
                schedule={
                    "kind": "daily_times",
                    "times": ["18:30"],
                    "enabled": True,
                },
                enabled_entrypoints=("console", "scheduler", "feishu"),
            )
        ),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    with pytest.raises(PluginConflictError) as raised:
        service.create_migration_pair(
            migration_pair_id=str(uuid.uuid4()),
            source_automation_id="arrival_stats",
            target_automation_id="arrival-stats-v2",
            business_key_fields=("__host_business_date",),
            business_key_namespace="arrival-stats",
            request_id=str(uuid.uuid4()),
            reason="scheduler requires production reload authority",
            actor=_console_actor(),
        )

    assert raised.value.code == "PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED"


def test_migration_binding_mapping_uses_only_the_reviewed_bijection() -> None:
    roles = (
        {"role": "primary", "allowed_kinds": ["feishu_sheet"], "required": True},
        {"role": "secondary", "allowed_kinds": ["feishu_sheet"], "required": True},
    )

    mapped = AutomationPluginManagementService._map_migration_bindings(
        source_bindings={"primary": "sheet-1", "secondary": "sheet-2"},
        source_roles=roles,
        target_roles=tuple(reversed(roles)),
        source_to_target_roles={
            "primary": "primary",
            "secondary": "secondary",
        },
        kind="resource",
    )

    assert mapped == {"secondary": "sheet-2", "primary": "sheet-1"}


def test_migration_exact_name_does_not_hide_a_role_contract_mismatch() -> None:
    with pytest.raises(PluginConflictError) as raised:
        AutomationPluginManagementService._map_migration_bindings(
            source_bindings={"operator": "account-1"},
            source_roles=(
                {
                    "role": "operator",
                    "argument_field": "source_account_id",
                    "allowed_systems": ["ronghui"],
                    "required": True,
                    "collection": False,
                },
            ),
            target_roles=(
                {
                    "role": "operator",
                    "argument_field": "target_account_id",
                    "allowed_systems": ["ronghui"],
                    "required": True,
                    "collection": False,
                },
            ),
            source_to_target_roles={"operator": "operator"},
            kind="account",
        )

    assert raised.value.code == "PLUGIN_MIGRATION_BINDING_MAPPING_UNAVAILABLE"


def test_self_pickup_migration_maps_both_identical_account_contracts_explicitly() -> None:
    mapping = reviewed_migration_binding_mapping(
        source_automation_id="self_pickup_problem_upload",
        source_plugin_id="self_pickup_problem_upload",
        target_plugin_id="self_pickup_problem_upload_v2",
    )
    assert mapping is not None
    roles = (
        {
            "role": "account_id",
            "argument_field": None,
            "allowed_systems": ["ronghui"],
            "required": True,
            "collection": False,
        },
        {
            "role": "daxiang_s_account_id",
            "argument_field": None,
            "allowed_systems": ["ronghui"],
            "required": True,
            "collection": False,
        },
    )

    copied = AutomationPluginManagementService._map_migration_bindings(
        source_bindings={
            "account_id": "primary-account",
            "daxiang_s_account_id": "daxiang-s-account",
        },
        source_roles=roles,
        target_roles=tuple(reversed(roles)),
        source_to_target_roles=mapping.account_roles,
        kind="account",
    )

    assert copied == {
        "account_id": "primary-account",
        "daxiang_s_account_id": "daxiang-s-account",
    }
    assert (
        reviewed_migration_binding_mapping(
            source_automation_id="unknown",
            source_plugin_id="self_pickup_problem_upload",
            target_plugin_id="self_pickup_problem_upload_v2",
        )
        is None
    )


def test_split_pending_migration_uses_only_its_reviewed_account_and_sheet_roles() -> None:
    mapping = reviewed_migration_binding_mapping(
        source_automation_id="split_pending_problem_upload",
        source_plugin_id="split_pending_problem_upload",
        target_plugin_id="split_pending_problem_upload_v2",
    )

    assert mapping is not None
    assert dict(mapping.account_roles) == {"account_id": "account_id"}
    assert dict(mapping.resource_roles) == {
        "split_pending_source_sheet": "split_pending_source_sheet",
        "split_pending_target_sheet": "split_pending_target_sheet",
    }
    assert (
        reviewed_migration_binding_mapping(
            source_automation_id="split_pending_problem_upload",
            source_plugin_id="split_pending_problem_upload",
            target_plugin_id="unknown_target",
        )
        is None
    )


def _self_pickup_migration_fixture() -> tuple[SimpleNamespace, SimpleNamespace]:
    source = _entry(
        automation_id="self_pickup_problem_upload",
        plugin_id="self_pickup_problem_upload",
    )
    target = _entry(
        automation_id="self-pickup-problem-upload-v2",
        plugin_id="self_pickup_problem_upload_v2",
        contributions={
            "console": (
                {
                    "id": "execute_console",
                    "service": "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1",
                    "operation": "execute",
                    "selection_preview_operation": "preview",
                },
            ),
            "scheduler": (),
            "webhook": (),
            "feishu": (
                {
                    "id": "execute_feishu",
                    "service": "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1",
                    "operation": "execute",
                    "commands": ("自提到货问题件",),
                    "selection_preview_operation": "preview",
                },
            ),
            "events": (),
        },
    )
    return source, target


def test_self_pickup_enabled_feishu_selection_migration_is_production_gated() -> None:
    source, target = _self_pickup_migration_fixture()

    with pytest.raises(PluginConflictError) as raised:
        migration_target_entrypoints_and_ownership(
            source=source,
            target=target,
            source_enabled_entrypoints=("console", "feishu"),
            source_schedule={"kind": "none", "times": [], "enabled": False},
            source_resource_bindings={
                "feishu_route": "automation.feishu_route.self_pickup_problem_upload",
                "self_pickup_source_sheet": "phase7.self_pickup_source_sheet",
            },
        )

    assert (
        raised.value.code
        == "PLUGIN_MIGRATION_FEISHU_SELECTION_PREVIEW_PRODUCTION_GATED"
    )


def test_self_pickup_console_only_migration_keeps_feishu_unowned() -> None:
    source, target = _self_pickup_migration_fixture()

    entrypoints, ownership, consumed = migration_target_entrypoints_and_ownership(
        source=source,
        target=target,
        source_enabled_entrypoints=("console",),
        source_schedule={"kind": "none", "times": [], "enabled": False},
        source_resource_bindings={
            "feishu_route": "automation.feishu_route.self_pickup_problem_upload",
            "self_pickup_source_sheet": "phase7.self_pickup_source_sheet",
        },
    )

    assert entrypoints == ("execute_console",)
    assert consumed == frozenset({"feishu_route"})
    assert ownership["feishu"] == {
        "source_enabled": False,
        "source_tool_name": None,
        "source_route_key": None,
        "source_resource_id": None,
        "target_contribution_id": None,
        "commands": [],
    }
    assert {
        state: owners["feishu"]
        for state, owners in ownership["owners"].items()
    } == {state: "NONE" for state in MIGRATION_OWNERSHIP_STATES}


def _arrival_migration_fixture(*, enabled_entrypoints: tuple[str, ...]):
    sheet_roles = (
        {
            "role": "arrival_stats_primary_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": "arrival_stats_secondary_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": "arrival_stats_pending_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": False,
        },
        {
            "role": "arrival_stats_archive_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": "arrival_stats_split_pending_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    )
    source = _entry(
        automation_id="arrival_stats",
        plugin_id="sync_arrival_stats",
        runtime_model=PluginRuntimeModel.ACTION_V1.value,
        account_roles=(
            {
                "role": "account_id",
                "argument_field": None,
                "allowed_systems": ["ronghui"],
                "required": True,
                "collection": False,
            },
        ),
        resource_roles=(
            *sheet_roles,
            {
                "role": "feishu_route",
                "allowed_kinds": ["feishu_route"],
                "required": False,
            },
            {
                "role": "webhook_route",
                "allowed_kinds": ["webhook_route"],
                "required": False,
            },
        ),
    )
    target = _entry(
        automation_id="arrival-stats-v2",
        plugin_id="sync_arrival_stats_v2",
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        project_config_version=1,
        account_roles=(
            {
                "role": "arrival_stats_tms",
                "argument_field": None,
                "allowed_systems": ["ronghui"],
                "required": True,
                "collection": False,
            },
        ),
        resource_roles=sheet_roles,
        contributions={
            "console": ({"id": "manual_run"},),
            "scheduler": ({"id": "daily_arrival_stats"},),
            "feishu": (
                {
                    "id": "arrival_stats_command",
                    "commands": ["统计到货数据"],
                },
            ),
        },
    )
    resource_bindings = {
        "arrival_stats_primary_sheet": "sheet-primary",
        "arrival_stats_secondary_sheet": "sheet-secondary",
        # The optional pending Sheet is deliberately not configured.
        "arrival_stats_archive_sheet": "sheet-archive",
        "arrival_stats_split_pending_sheet": "sheet-split-pending",
        "feishu_route": "automation.feishu_route.arrival_stats",
        "webhook_route": "phase7.stats_webhook",
    }
    source_record = AutomationProjectConfigRecord(
        automation_id="arrival_stats",
        config={"pending_sheet_disabled": True},
        account_bindings={"account_id": "ronghui-default"},
        resource_bindings=resource_bindings,
        schedule={"kind": "none", "times": [], "enabled": False},
        config_version=9,
        configured=True,
        config_sha256="1" * 64,
        account_bindings_sha256="2" * 64,
        resource_bindings_sha256="3" * 64,
        device_binding_sha256="4" * 64,
        enabled_entrypoints=enabled_entrypoints,
    )
    return source, target, source_record


def test_arrival_migration_exactly_maps_business_roles_and_consumes_route_resources() -> None:
    source, target, source_record = _arrival_migration_fixture(
        enabled_entrypoints=("console", "feishu"),
    )
    saves: list[dict[str, Any]] = []
    beginnings: list[dict[str, Any]] = []
    package = SimpleNamespace(
        begin_plugin_migration_pair_preparation=lambda **kwargs: beginnings.append(
            kwargs
        )
        or {
            "migration_pair_id": kwargs["migration_pair_id"],
            "state": "PREPARING",
            "record_version": 1,
        },
        finalize_plugin_migration_pair_preparation=lambda pair_id, **_kwargs: {
            "migration_pair_id": pair_id,
            "state": "TESTING",
            "record_version": 2,
        },
    )
    service = AutomationPluginManagementService(
        catalog=SimpleNamespace(
            require=lambda automation_id: {
                "arrival_stats": source,
                "arrival-stats-v2": target,
            }[automation_id]
        ),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(
            read=lambda _automation_id: source_record,
            save=lambda automation_id, **kwargs: saves.append(
                {"automation_id": automation_id, **kwargs}
            ),
        ),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(reconcile_project=lambda _automation_id: None),
        package_repository=package,
        storage=SimpleNamespace(),
    )

    service.create_migration_pair(
        migration_pair_id=str(uuid.uuid4()),
        source_automation_id="arrival_stats",
        target_automation_id="arrival-stats-v2",
        business_key_fields=("__host_business_date",),
        business_key_namespace="arrival-stats",
        request_id=str(uuid.uuid4()),
        reason="offline exact pair preparation",
        actor=_console_actor(),
    )

    assert saves[0]["account_bindings"] == {"arrival_stats_tms": "ronghui-default"}
    assert saves[0]["resource_bindings"] == {
        "arrival_stats_primary_sheet": "sheet-primary",
        "arrival_stats_secondary_sheet": "sheet-secondary",
        "arrival_stats_archive_sheet": "sheet-archive",
        "arrival_stats_split_pending_sheet": "sheet-split-pending",
    }
    assert saves[0]["enabled_entrypoints"] == (
        "manual_run",
        "arrival_stats_command",
    )
    ownership = beginnings[0]["entrypoint_ownership"]
    assert ownership["feishu"] == {
        "source_enabled": True,
        "target_contribution_id": "arrival_stats_command",
        "source_tool_name": "sync_arrival_stats",
        "source_route_key": "builtin.arrival_stats",
        "source_resource_id": "automation.feishu_route.arrival_stats",
        "commands": ["统计到货数据"],
    }


def test_arrival_migration_enabled_webhook_is_explicitly_production_gated() -> None:
    source, target, source_record = _arrival_migration_fixture(
        enabled_entrypoints=("console", "feishu", "webhook"),
    )
    service = AutomationPluginManagementService(
        catalog=SimpleNamespace(
            require=lambda automation_id: {
                "arrival_stats": source,
                "arrival-stats-v2": target,
            }[automation_id]
        ),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(read=lambda _automation_id: source_record),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    with pytest.raises(PluginConflictError) as raised:
        service.create_migration_pair(
            migration_pair_id=str(uuid.uuid4()),
            source_automation_id="arrival_stats",
            target_automation_id="arrival-stats-v2",
            business_key_fields=("__host_business_date",),
            business_key_namespace="arrival-stats",
            request_id=str(uuid.uuid4()),
            reason="must not silently drop webhook ownership",
            actor=_console_actor(),
        )

    assert raised.value.code == "PLUGIN_MIGRATION_WEBHOOK_PRODUCTION_GATED"


def test_migration_ownership_contract_keeps_unmigrated_scheduler_disabled() -> None:
    source, target, source_record = _arrival_migration_fixture(
        enabled_entrypoints=("console", "feishu"),
    )
    entrypoints, ownership, _consumed = migration_target_entrypoints_and_ownership(
        source=source,
        target=target,
        source_enabled_entrypoints=("console", "feishu"),
        source_schedule={"kind": "none", "times": [], "enabled": False},
        source_resource_bindings=source_record.resource_bindings,
    )

    assert entrypoints == (
        "manual_run",
        "arrival_stats_command",
    )
    for kind in ("console", "feishu"):
        assert ownership["owners"]["TESTING"][kind] == "ACTION_V1"
        assert ownership["owners"]["CUTOVER"][kind] == "SERVICE_V2"
        assert ownership["owners"]["ROLLED_BACK"][kind] == "ACTION_V1"
    assert ownership["scheduler"] == {
        "source_enabled": False,
        "target_contribution_id": None,
        "schedule_mode": "NONE",
    }
    assert {
        state: owners["scheduler"]
        for state, owners in ownership["owners"].items()
    } == {state: "NONE" for state in MIGRATION_OWNERSHIP_STATES}
