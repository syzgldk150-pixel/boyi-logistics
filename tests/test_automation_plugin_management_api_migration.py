from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.automation_plugins.errors import PluginConflictError, PluginPackageError
from agent.automation_plugins.management import (
    AutomationPluginManagementService,
    MigrationPreparationPersistedError,
)
from agent.automation_plugins.management_api import (
    create_automation_plugin_management_router,
)
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginProjectState,
    PluginRuntimeModel,
    RuntimeReconcileState,
)
from agent.orchestration.models import Actor, ActorType
from shared.orchestration_repository_support import IdempotencyConflict

from tests.automation_plugin_management_api_support import (
    _console_actor,
    _entry,
    _Catalog,
    _ContributionRegistry,
    _ProjectionCatalog,
    _committed_service_entry,
    _projection_service,
    _ApiService,
    _api_client,
    _configuration_request_payload,
    _service_v2_install_intent,
)


def _migration_operation_payload() -> dict[str, Any]:
    return {
        "expected_record_version": 3,
        "request_id": str(uuid.uuid4()),
        "reason": "验证迁移入口",
        "confirm": True,
    }


def test_migration_mutations_refresh_scheduler_after_commit() -> None:
    service = _ApiService()
    refresh_calls: list[str] = []
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: (
            refresh_calls.append("refresh") or {"initialized": True}
        ),
    )
    pair_id = str(uuid.uuid4())

    create = client.post(
        "/internal/v1/automation/migrations",
        json={
            "migration_pair_id": pair_id,
            "source_automation_id": "automation-1",
            "target_automation_id": "automation-2",
            "business_key_fields": ["target_date"],
            "request_id": str(uuid.uuid4()),
            "reason": "创建并行验证",
        },
    )
    assert create.status_code == 200
    assert create.json()["data"]["migration_operation_committed"] is True
    assert create.json()["data"]["schedule_runtime_state"] == "REFRESHED"
    assert "schedule_runtime_enabled" not in create.json()["data"]

    for suffix, expected_state in (
        ("ready", "READY"),
        ("cutover", "CUTOVER"),
        ("rollback", "ROLLED_BACK"),
        ("complete", "COMPLETED"),
    ):
        response = client.post(
            f"/internal/v1/automation/migrations/{pair_id}/{suffix}",
            json=_migration_operation_payload(),
        )
        assert response.status_code == 200
        assert response.json()["data"]["state"] == expected_state
        assert response.json()["data"]["scheduler_refresh_completed"] is True

    assert refresh_calls == ["refresh"] * 5


def test_migration_commit_reports_refresh_failure_without_implying_retry() -> None:
    service = _ApiService()
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("scheduler unavailable")
        ),
    )

    response = client.post(
        f"/internal/v1/automation/migrations/{uuid.uuid4()}/cutover",
        json=_migration_operation_payload(),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["migration_operation_committed"] is True
    assert data["scheduler_refresh_completed"] is False
    assert data["schedule_runtime_state"] == "REFRESH_FAILED"
    assert "已提交" in response.json()["message"]
    assert "不要重复提交迁移切换" in response.json()["message"]


def test_migration_create_copy_failure_refreshes_scheduler_after_preparing_hold() -> None:
    pair_id = str(uuid.uuid4())
    removed_jobs: list[str] = []

    class _PreparingService(_ApiService):
        def create_migration_pair(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("migration-create", dict(kwargs)))
            raise MigrationPreparationPersistedError(
                migration_pair_id=kwargs["migration_pair_id"],
                phase="TARGET_COPY",
            )

    service = _PreparingService()
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: (
            removed_jobs.append("stale-target-job") or {"initialized": True, "jobs": 0}
        ),
    )
    response = client.post(
        "/internal/v1/automation/migrations",
        json={
            "migration_pair_id": pair_id,
            "source_automation_id": "automation-1",
            "target_automation_id": "automation-2",
            "business_key_fields": ["target_date"],
            "request_id": str(uuid.uuid4()),
            "reason": "创建并行验证",
        },
    )

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["state"] == "PREPARING"
    assert data["migration_preparation_committed"] is True
    assert data["retry_with_same_request_id"] is True
    assert data["scheduler_refresh_completed"] is True
    assert removed_jobs == ["stale-target-job"]
    assert "不要创建新的迁移对" in response.json()["message"]

def test_open_migration_pair_blocks_ordinary_project_state_mutation() -> None:
    lifecycle_calls: list[dict[str, Any]] = []
    service = AutomationPluginManagementService(
        catalog=_Catalog(_entry()),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            set_enabled=lambda *args, **kwargs: lifecycle_calls.append(kwargs)
        ),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(
            get_active_plugin_migration_pair_for_automation=lambda _automation_id: {
                "migration_pair_id": str(uuid.uuid4()),
                "state": "CUTOVER",
            }
        ),
        storage=SimpleNamespace(),
    )

    with pytest.raises(PluginConflictError) as raised:
        service.set_enabled(
            "automation-1",
            enabled=True,
            request_id=str(uuid.uuid4()),
            expected_record_version=3,
            actor=_console_actor(),
        )

    assert raised.value.code == "PLUGIN_MIGRATION_PROJECT_MUTATION_BLOCKED"
    assert lifecycle_calls == []


def test_create_migration_pair_copies_closed_bindings_without_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent.automation_plugins.management.reviewed_migration_binding_mapping",
        lambda **_kwargs: SimpleNamespace(
            account_roles={"legacy_operator": "operator"},
            resource_roles={},
        ),
    )
    source = _entry(
        automation_id="legacy-clock",
        runtime_model=PluginRuntimeModel.ACTION_V1.value,
        account_roles=(
            {
                "role": "legacy_operator",
                "allowed_systems": ["ronghui"],
                "required": True,
            },
        ),
        resource_roles=(),
    )
    target = _entry(
        automation_id="clock-v2",
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        project_config_version=1,
        account_roles=(
            {
                "role": "operator",
                "allowed_systems": ["ronghui"],
                "required": True,
            },
        ),
        resource_roles=(),
        contributions={
            "console": [{"id": "manual_run"}],
            "scheduler": [{"id": "daily_clockin"}],
        },
    )
    source_record = AutomationProjectConfigRecord(
        automation_id="legacy-clock",
        config={"sitecode": "A"},
        account_bindings={"legacy_operator": "account-1"},
        resource_bindings={},
        schedule={"kind": "none", "times": [], "enabled": False},
        config_version=7,
        configured=True,
        config_sha256="1" * 64,
        account_bindings_sha256="2" * 64,
        resource_bindings_sha256="3" * 64,
        device_binding_sha256="4" * 64,
        enabled_entrypoints=("console",),
    )
    saves: list[dict[str, Any]] = []
    reconciled: list[str] = []
    prepared_pairs: list[dict[str, Any]] = []
    package = SimpleNamespace(
        get_plugin_migration_pair=lambda _pair_id: None,
        get_active_plugin_migration_pair_for_automation=lambda _automation_id: None,
        begin_plugin_migration_pair_preparation=lambda **kwargs: prepared_pairs.append(
            {
                "migration_pair_id": kwargs["migration_pair_id"],
                "state": "PREPARING",
                "record_version": 1,
                "entrypoint_snapshot_json": {
                    "business_key_contract": kwargs["business_key_contract"]
                },
            }
        ) or prepared_pairs[-1],
        finalize_plugin_migration_pair_preparation=lambda _pair_id, **kwargs: {
            "migration_pair_id": _pair_id,
            "state": "TESTING",
            "record_version": 2,
            "entrypoint_snapshot_json": {
                "business_key_contract": {"fields": ["__host_business_date"], "namespace": "clockin"}
            },
        },
    )
    service = AutomationPluginManagementService(
        catalog=SimpleNamespace(require=lambda automation_id: {  # type: ignore[arg-type]
            "legacy-clock": source,
            "clock-v2": target,
        }[automation_id]),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(
            read=lambda automation_id: source_record
            if automation_id == "legacy-clock"
            else None,
            save=lambda automation_id, **kwargs: saves.append(
                {"automation_id": automation_id, **kwargs}
            ),
        ),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda automation_id: reconciled.append(automation_id)
        ),
        package_repository=package,
        storage=SimpleNamespace(),
    )

    result = service.create_migration_pair(
        migration_pair_id=str(uuid.uuid4()),
        source_automation_id="legacy-clock",
        target_automation_id="clock-v2",
        business_key_fields=("__host_business_date",),
        business_key_namespace="clockin",
        request_id=str(uuid.uuid4()),
        reason="copy and manually verify",
        actor=_console_actor(),
    )

    assert saves[0]["automation_id"] == "clock-v2"
    assert saves[0]["account_bindings"] == {"operator": "account-1"}
    assert saves[0]["enabled_entrypoints"] == ("manual_run",)
    assert saves[0]["schedule"] == source_record.schedule
    assert prepared_pairs and prepared_pairs[0]["state"] == "PREPARING"
    assert reconciled == ["clock-v2"]
    assert result["target_preparation_state"] == "PREPARING"
    assert result["copied_configuration"]["source_config_version"] == 7


def test_create_migration_pair_rejects_ambiguous_binding_mapping() -> None:
    source = _entry(
        automation_id="legacy-clock",
        runtime_model=PluginRuntimeModel.ACTION_V1.value,
        account_roles=(
            {"role": "one", "allowed_systems": ["ronghui"], "required": True},
            {"role": "two", "allowed_systems": ["ronghui"], "required": True},
        ),
        resource_roles=(),
    )
    target = _entry(
        automation_id="clock-v2",
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        account_roles=(
            {"role": "operator", "allowed_systems": ["ronghui"], "required": True},
        ),
        resource_roles=(),
        contributions={"console": [{"id": "manual_run"}], "scheduler": []},
    )
    source_record = AutomationProjectConfigRecord(
        automation_id="legacy-clock",
        config={},
        account_bindings={"one": "account-1", "two": "account-2"},
        resource_bindings={},
        schedule={"kind": "none", "times": [], "enabled": False},
        config_version=1,
        configured=True,
        config_sha256="1" * 64,
        account_bindings_sha256="2" * 64,
        resource_bindings_sha256="3" * 64,
        device_binding_sha256="4" * 64,
        enabled_entrypoints=("console",),
    )
    service = AutomationPluginManagementService(
        catalog=SimpleNamespace(require=lambda automation_id: {  # type: ignore[arg-type]
            "legacy-clock": source,
            "clock-v2": target,
        }[automation_id]),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(read=lambda _automation_id: source_record),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(
            get_plugin_migration_pair=lambda _pair_id: None,
            get_active_plugin_migration_pair_for_automation=lambda _automation_id: None,
        ),
        storage=SimpleNamespace(),
    )

    with pytest.raises(PluginConflictError) as raised:
        service.create_migration_pair(
            migration_pair_id=str(uuid.uuid4()),
            source_automation_id="legacy-clock",
            target_automation_id="clock-v2",
            business_key_fields=("__host_business_date",),
            business_key_namespace="clockin",
            request_id=str(uuid.uuid4()),
            reason="must not guess roles",
            actor=_console_actor(),
        )

    assert raised.value.code == "PLUGIN_MIGRATION_BINDING_MAPPING_UNAVAILABLE"


def test_create_migration_pair_copy_failure_leaves_durable_preparing_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent.automation_plugins.management.reviewed_migration_binding_mapping",
        lambda **_kwargs: SimpleNamespace(account_roles={}, resource_roles={}),
    )
    source = _entry(
        automation_id="legacy-clock",
        runtime_model=PluginRuntimeModel.ACTION_V1.value,
        account_roles=(),
        resource_roles=(),
    )
    target = _entry(
        automation_id="clock-v2",
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        account_roles=(),
        resource_roles=(),
        contributions={"console": [{"id": "manual_run"}], "scheduler": []},
    )
    source_record = AutomationProjectConfigRecord(
        automation_id="legacy-clock",
        config={}, account_bindings={}, resource_bindings={},
        schedule={"kind": "none", "times": [], "enabled": False},
        config_version=1, configured=True,
        config_sha256="1" * 64, account_bindings_sha256="2" * 64,
        resource_bindings_sha256="3" * 64, device_binding_sha256="4" * 64,
        enabled_entrypoints=("console",),
    )
    begun: list[dict[str, Any]] = []
    finalized: list[str] = []
    package = SimpleNamespace(
        begin_plugin_migration_pair_preparation=lambda **kwargs: begun.append(kwargs) or {
            "migration_pair_id": kwargs["migration_pair_id"], "state": "PREPARING",
            "record_version": 1,
            "entrypoint_snapshot_json": {"business_key_contract": kwargs["business_key_contract"]},
        },
        finalize_plugin_migration_pair_preparation=lambda _pair_id, **_kwargs: finalized.append(_pair_id),
    )
    service = AutomationPluginManagementService(
        catalog=SimpleNamespace(require=lambda automation_id: {"legacy-clock": source, "clock-v2": target}[automation_id]),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(
            read=lambda _automation_id: source_record,
            save=lambda _automation_id, **_kwargs: (_ for _ in ()).throw(RuntimeError("copy failed")),
        ),
        worker_repository=SimpleNamespace(), target_service=SimpleNamespace(),
        package_repository=package, storage=SimpleNamespace(),
    )

    with pytest.raises(MigrationPreparationPersistedError) as raised:
        service.create_migration_pair(
            migration_pair_id=str(uuid.uuid4()), source_automation_id="legacy-clock",
            target_automation_id="clock-v2", business_key_fields=("business_day",),
            business_key_namespace=None, request_id=str(uuid.uuid4()),
            reason="copy target configuration", actor=_console_actor(),
        )

    assert raised.value.phase == "TARGET_COPY"
    assert len(begun) == 1
    assert finalized == []


def test_provider_uninstall_reconciles_consumers_before_finalize() -> None:
    calls: list[str] = []
    service = AutomationPluginManagementService(
        catalog=SimpleNamespace(), lifecycle=SimpleNamespace(), configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda automation_id: calls.append(f"project:{automation_id}"),
            reconcile_provider_dependency_tree=lambda automation_id, **_kwargs: (
                calls.append(f"tree:{automation_id}")
            ),
        ),
        package_repository=SimpleNamespace(), storage=SimpleNamespace(),
    )

    service._reconcile_before_uninstall(
        "provider-v2", provided_services=("plugin.provider-v2.api@1",)
    )

    assert calls == ["tree:provider-v2"]


def test_v2_provider_state_change_requires_consumer_reconcile() -> None:
    calls: list[str] = []
    entry = _entry(
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        provided_services=("plugin.provider-v2.api@1",),
    )
    service = AutomationPluginManagementService(
        catalog=SimpleNamespace(), lifecycle=SimpleNamespace(), configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_provider_dependency_tree=lambda automation_id, **_kwargs: (
                calls.append(f"tree:{automation_id}")
            ),
        ),
        package_repository=SimpleNamespace(), storage=SimpleNamespace(),
    )

    service._reconcile_v2_after_enabled_change(entry, enabled=False)

    assert calls == ["tree:automation-1"]


def test_v2_provider_disable_closes_consumers_before_state_and_route_change() -> None:
    calls: list[str] = []
    entry = _entry(
        enabled=True,
        state=PluginProjectState.ENABLED.value,
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        provided_services=("plugin.provider-v2.api@1",),
    )

    def set_enabled(automation_id: str, **_kwargs: Any) -> SimpleNamespace:
        calls.append("state-disabled")
        return SimpleNamespace(
            automation_id=automation_id,
            plugin_id="provider-v2",
            display_name="Provider v2",
            active_version=SimpleNamespace(version="1.0.0"),
            enabled=False,
            state=PluginProjectState.DISABLED,
            record_version=4,
            target_generation=1,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.STABLE,
        )

    target = SimpleNamespace(
        suspend_provider_consumers=lambda *_args, **_kwargs: (
            calls.append("consumer-schedules-closed")
            or ("consumer-v2",)
        ),
        reconcile_provider_dependency_tree=lambda *_args, **_kwargs: calls.append(
            "provider-route-withdrawn"
        ),
    )
    service = AutomationPluginManagementService(
        catalog=_Catalog(entry),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(set_enabled=set_enabled),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=target,
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    service.set_enabled(
        entry.automation_id,
        enabled=False,
        request_id=str(uuid.uuid4()),
        expected_record_version=entry.record_version,
        actor=_console_actor(),
    )

    assert calls == [
        "consumer-schedules-closed",
        "state-disabled",
        "provider-route-withdrawn",
    ]


def test_v2_provider_disable_keeps_provider_unchanged_if_consumer_close_fails() -> None:
    lifecycle_calls: list[str] = []
    entry = _entry(
        enabled=True,
        state=PluginProjectState.ENABLED.value,
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        provided_services=("plugin.provider-v2.api@1",),
    )
    service = AutomationPluginManagementService(
        catalog=_Catalog(entry),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            set_enabled=lambda *_args, **_kwargs: lifecycle_calls.append("changed")
        ),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            suspend_provider_consumers=lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("scheduler store unavailable"))
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="scheduler store unavailable"):
        service.set_enabled(
            entry.automation_id,
            enabled=False,
            request_id=str(uuid.uuid4()),
            expected_record_version=entry.record_version,
            actor=_console_actor(),
        )

    assert lifecycle_calls == []


def test_v2_provider_uninstall_closes_consumers_before_uninstall_preparation() -> None:
    calls: list[str] = []
    entry = _entry(
        enabled=True,
        state=PluginProjectState.ENABLED.value,
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        provided_services=("plugin.provider-v2.api@1",),
    )

    def hard_uninstall(automation_id: str, **kwargs: Any) -> SimpleNamespace:
        calls.append("uninstall-prepared")
        kwargs["before_finalize"](automation_id)
        return SimpleNamespace(
            automation_id=automation_id,
            status=SimpleNamespace(value="COMPLETED"),
            purge_id="purge-1",
            pending_cleanup_commands=(),
        )

    service = AutomationPluginManagementService(
        catalog=_Catalog(entry),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(hard_uninstall=hard_uninstall),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            suspend_provider_consumers=lambda *_args, **_kwargs: (
                calls.append("consumer-schedules-closed")
                or ("consumer-v2",)
            ),
            reconcile_project=lambda _automation_id: None,
            reconcile_provider_dependency_tree=lambda *_args, **_kwargs: calls.append(
                "provider-route-withdrawn"
            ),
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    service.uninstall(
        entry.automation_id,
        request_id=str(uuid.uuid4()),
        expected_record_version=entry.record_version,
        current_version=entry.installed_version,
        actor=_console_actor(),
    )

    assert calls == [
        "consumer-schedules-closed",
        "uninstall-prepared",
        "provider-route-withdrawn",
    ]
