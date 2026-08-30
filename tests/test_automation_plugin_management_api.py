from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.errors import PluginConflictError, PluginPackageError
from agent.automation_plugins.management import (
    AutomationPluginManagementService,
    MigrationPreparationPersistedError,
)
from agent.automation_plugins.management_api import (
    create_automation_plugin_management_router,
)
from agent.automation_plugins.management_repository import (
    MySQLAutomationPluginManagementRepository,
)
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginProjectState,
    PluginRuntimeModel,
    PluginTrustSource,
    PluginVersionRecord,
    RuntimeReconcileState,
)
from agent.automation_plugins.storage import FilesystemPluginStorage
from agent.orchestration.models import Actor, ActorType
from shared.orchestration_repository_support import IdempotencyConflict


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


class _Catalog:
    def __init__(self, entry: SimpleNamespace | None = None) -> None:
        self.current = entry or _entry()

    def require(self, automation_id: str) -> SimpleNamespace:
        if automation_id != self.current.automation_id:
            raise AssertionError("unexpected automation id")
        return self.current

    @staticmethod
    def safe_projection() -> dict[str, Any]:
        return {"plugins": [{"plugin_id": "example_action"}], "instances": []}


class _ApiService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.catalog_result: dict[str, Any] = {"plugins": []}
        self.configuration_result: dict[str, Any] = {
            "automation_id": "automation-1",
            "project_configuration_version": 4,
            "generation_ready": True,
            "schedule": {
                "kind": "daily_times",
                "times": ["08:00"],
                "enabled": True,
            },
            "enabled_entrypoints": ["scheduler"],
        }

    def catalog_projection(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("catalog", kwargs))
        return self.catalog_result

    def worker_projection(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("workers", kwargs))
        return {"workers": []}

    def pair_worker(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("pair", kwargs))
        return {"device_id": kwargs["device_id"], "platform": "windows"}

    def install(self, package: bytes, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("install", {"package": package, **kwargs}))
        return {"automation_id": "server-generated"}

    def upgrade(self, automation_id: str, package: bytes, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(
            ("upgrade", {"automation_id": automation_id, "package": package, **kwargs})
        )
        return {"automation_id": automation_id, "transition_state": "PREPARING"}

    def set_enabled(self, automation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("state", {"automation_id": automation_id, **kwargs}))
        return {"automation_id": automation_id}

    def uninstall(self, automation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("uninstall", {"automation_id": automation_id, **kwargs}))
        return {"automation_id": automation_id}

    def save_configuration(self, automation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("configuration", {"automation_id": automation_id, **kwargs}))
        return dict(self.configuration_result)

    def create_migration_pair(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("migration-create", dict(kwargs)))
        return {
            "migration_pair_id": kwargs["migration_pair_id"],
            "state": "TESTING",
        }

    def mark_migration_ready(self, migration_pair_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("migration-ready", {"migration_pair_id": migration_pair_id, **kwargs}))
        return {"migration_pair_id": migration_pair_id, "state": "READY"}

    def cutover_migration_pair(self, migration_pair_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("migration-cutover", {"migration_pair_id": migration_pair_id, **kwargs}))
        return {"migration_pair_id": migration_pair_id, "state": "CUTOVER"}

    def rollback_migration_pair(self, migration_pair_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("migration-rollback", {"migration_pair_id": migration_pair_id, **kwargs}))
        return {"migration_pair_id": migration_pair_id, "state": "ROLLED_BACK"}

    def complete_migration_pair(self, migration_pair_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("migration-complete", {"migration_pair_id": migration_pair_id, **kwargs}))
        return {"migration_pair_id": migration_pair_id, "state": "COMPLETED"}

    def recover_current_unknown_write(
        self,
        automation_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(("recover-current", {"automation_id": automation_id, **kwargs}))
        return {
            "automation_id": automation_id,
            "recovery_status": "APPLIED",
            "transitioned": True,
        }


def _api_client(
    service: _ApiService,
    *,
    scheduler_refresh_provider: Any | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_automation_plugin_management_router(
            service_provider=lambda: service,  # type: ignore[arg-type]
            actor_provider=lambda _request: _console_actor(),
            scheduler_refresh_provider=scheduler_refresh_provider,
        )
    )
    return TestClient(app)


def _configuration_request_payload() -> dict[str, Any]:
    return {
        "config": {"mode": "daily"},
        "account_bindings": {},
        "resource_bindings": {},
        "enabled_entrypoints": ["scheduler"],
        "device_id": None,
        "schedule": {
            "kind": "daily_times",
            "times": ["08:00"],
            "enabled": True,
        },
        "request_id": str(uuid.uuid4()),
        "expected_project_configuration_version": 3,
    }


def test_management_router_is_closed_and_install_identity_is_server_owned() -> None:
    service = _ApiService()
    client = _api_client(service)
    package = b"PK\x03\x04signed-plugin"
    digest = hashlib.sha256(package).hexdigest()
    request_id = str(uuid.uuid4())

    assert client.get("/internal/v1/automation/plugins/catalog").status_code == 200
    assert client.get("/internal/v1/automation/workers").status_code == 200
    installed = client.post(
        "/internal/v1/automation/plugins/install",
        data={
            "instance_name": "Season account action",
            "request_id": request_id,
            "package_sha256": digest,
        },
        files={"package": ("action.zip", package, "application/zip")},
    )
    assert installed.status_code == 200
    name, call = service.calls[-1]
    assert name == "install"
    assert call["package"] == package
    assert call["instance_name"] == "Season account action"
    assert "automation_id" not in call
    assert "manifest" not in call

    unsupported = client.post(
        "/internal/v1/automation/plugins/install",
        data={
            "instance_name": "forged",
            "request_id": str(uuid.uuid4()),
            "package_sha256": digest,
            "automation_id": "browser-forged-id",
        },
        files={"package": ("action.zip", package, "application/zip")},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "PLUGIN_MULTIPART_FIELDS_INVALID"

    state = client.post(
        "/internal/v1/automation/instances/automation-1/state",
        json={
            "enabled": True,
            "request_id": str(uuid.uuid4()),
            "expected_record_version": 3,
            "actor_id": "browser-forged-admin",
        },
    )
    assert state.status_code == 422
    assert not any(item[0] == "state" for item in service.calls)


def test_lifecycle_mutations_refresh_scheduler_after_commit() -> None:
    service = _ApiService()
    refresh_calls: list[str] = []
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: (
            refresh_calls.append("refresh") or {"initialized": True}
        ),
    )
    package = b"PK\x03\x04lifecycle-plugin"
    digest = hashlib.sha256(package).hexdigest()

    install = client.post(
        "/internal/v1/automation/plugins/install",
        data={
            "instance_name": "Lifecycle plugin",
            "request_id": str(uuid.uuid4()),
            "package_sha256": digest,
        },
        files={"package": ("lifecycle.zip", package, "application/zip")},
    )
    upgrade_package = b"PK\x03\x04lifecycle-upgrade"
    upgrade_digest = hashlib.sha256(upgrade_package).hexdigest()
    upgrade = client.post(
        "/internal/v1/automation/instances/automation-1/upgrade",
        data={
            "request_id": str(uuid.uuid4()),
            "expected_record_version": "3",
            "package_sha256": upgrade_digest,
        },
        files={"package": ("lifecycle-upgrade.zip", upgrade_package, "application/zip")},
    )
    disable = client.post(
        "/internal/v1/automation/instances/automation-1/state",
        json={
            "enabled": False,
            "request_id": str(uuid.uuid4()),
            "expected_record_version": 3,
        },
    )
    uninstall = client.post(
        "/internal/v1/automation/instances/automation-1/uninstall",
        json={
            "request_id": str(uuid.uuid4()),
            "expected_record_version": 3,
            "current_version": "1.0.0",
            "confirm": True,
        },
    )

    for response in (install, upgrade, disable, uninstall):
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["plugin_operation_committed"] is True
        assert data["scheduler_refresh_completed"] is True
        assert data["schedule_runtime_state"] == "REFRESHED"
        assert "schedule_runtime_enabled" not in data
    assert refresh_calls == ["refresh"] * 4


def test_lifecycle_refresh_failure_is_explicit_and_does_not_retry_operation() -> None:
    service = _ApiService()
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("scheduler unavailable")
        ),
    )
    package = b"PK\x03\x04lifecycle-plugin"
    response = client.post(
        "/internal/v1/automation/plugins/install",
        data={
            "instance_name": "Lifecycle plugin",
            "request_id": str(uuid.uuid4()),
            "package_sha256": hashlib.sha256(package).hexdigest(),
        },
        files={"package": ("lifecycle.zip", package, "application/zip")},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["plugin_operation_committed"] is True
    assert data["scheduler_refresh_completed"] is False
    assert data["schedule_runtime_state"] == "REFRESH_FAILED"
    assert "已提交" in response.json()["message"]
    assert "不要重复提交安装操作" in response.json()["message"]


def test_failed_lifecycle_response_does_not_refresh_scheduler() -> None:
    service = _ApiService()

    def fail_state(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise PluginConflictError("state conflict", code="STATE_CONFLICT")

    service.set_enabled = fail_state  # type: ignore[method-assign]
    refresh_calls: list[str] = []
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: refresh_calls.append("refresh")
        or {"initialized": True},
    )
    response = client.post(
        "/internal/v1/automation/instances/automation-1/state",
        json={
            "enabled": False,
            "request_id": str(uuid.uuid4()),
            "expected_record_version": 3,
        },
    )

    assert response.status_code == 409
    assert refresh_calls == []


def test_service_v2_install_enables_only_manifest_default_contributions() -> None:
    entry = _entry(
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        plugin_api="2.0.0",
        active_runtime_model=None,
        active_version=None,
        configured=False,
        enabled=False,
        project_config_version=0,
        current_enabled_entrypoints=(),
        config_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        account_roles=(),
        resource_roles=(),
        allowed_entrypoints=("manual_run", "nightly_run"),
        contributions={
            "console": [
                {"id": "manual_run", "default_enabled": True},
            ],
            "scheduler": [
                {"id": "nightly_run", "default_enabled": False},
            ],
        },
    )
    catalog = _Catalog(entry)
    configuration_calls: list[dict[str, Any]] = []
    enable_calls: list[dict[str, Any]] = []
    reconcile_calls: list[str] = []
    active_version = SimpleNamespace(
        version="1.0.0",
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )
    instance = SimpleNamespace(
        automation_id="automation-1",
        plugin_id="example_action",
        display_name="Example action",
        active_version=active_version,
        enabled=False,
        state=PluginProjectState.INSTALLED,
        record_version=3,
        target_generation=1,
        committed_generation=None,
        reconcile_state=RuntimeReconcileState.PREPARING,
    )

    def save(automation_id: str, **kwargs: Any) -> None:
        configuration_calls.append({"automation_id": automation_id, **kwargs})
        catalog.current = _entry(
            **{
                **vars(entry),
                "configured": True,
                "project_config_version": 1,
                "current_enabled_entrypoints": tuple(kwargs["enabled_entrypoints"]),
            }
        )

    def set_enabled(automation_id: str, **kwargs: Any) -> SimpleNamespace:
        enable_calls.append({"automation_id": automation_id, **kwargs})
        catalog.current.enabled = True
        catalog.current.record_version += 1
        return instance

    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            install_upload=lambda *_args, **_kwargs: instance,
            set_enabled=set_enabled,
        ),
        configuration=SimpleNamespace(save=save),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda automation_id: reconcile_calls.append(automation_id)
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    result = service.install(
        b"v2-package",
        instance_name="",
        request_id=str(uuid.uuid4()),
        transport_package_sha256="a" * 64,
        actor=_console_actor(),
    )

    assert configuration_calls[0]["enabled_entrypoints"] == ("manual_run",)
    assert configuration_calls[0]["schedule"] == {
        "kind": "none",
        "times": [],
        "enabled": False,
    }
    assert len(enable_calls) == 1
    assert reconcile_calls == ["automation-1"]
    assert result["runtime_model"] == PluginRuntimeModel.SERVICE_V2.value


def test_service_v2_default_scheduler_install_uses_a_real_daily_schedule() -> None:
    entry = _entry(
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        contributions={
            "console": [{"id": "manual_run", "default_enabled": True}],
            "scheduler": [{
                "id": "daily_run", "default_enabled": True,
                "schedule": {
                    "kind": "cron", "expression": "5 18 * * *", "timezone": "Asia/Shanghai",
                },
            }],
        },
        scheduling={"supported": True, "allowed_kinds": ["daily_times"]},
    )

    assert AutomationPluginManagementService._service_v2_default_schedule(
        entry, ("manual_run", "daily_run")
    ) == {"kind": "daily_times", "times": ["18:05"], "enabled": True}


def test_service_v2_default_scheduler_rejects_lossy_cron_mapping() -> None:
    entry = _entry(
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        contributions={
            "scheduler": [{
                "id": "weekday_run", "default_enabled": True,
                "schedule": {
                    "kind": "cron", "expression": "5 18 * * 1-5", "timezone": "Asia/Shanghai",
                },
            }],
        },
        scheduling={"supported": True, "allowed_kinds": ["daily_times"]},
    )

    with pytest.raises(PluginConflictError) as raised:
        AutomationPluginManagementService._service_v2_default_schedule(
            entry, ("weekday_run",)
        )

    assert raised.value.code == "PLUGIN_DEFAULT_SCHEDULE_UNSUPPORTED"


def test_current_unknown_write_recovery_accepts_only_request_identity() -> None:
    service = _ApiService()
    client = _api_client(service)
    request_id = str(uuid.uuid4())

    response = client.post(
        "/internal/v1/automation/instances/automation-1/generation/"
        "recover-current-unknown-write",
        json={"request_id": request_id},
    )

    assert response.status_code == 200
    name, call = service.calls[-1]
    assert name == "recover-current"
    assert call["automation_id"] == "automation-1"
    assert call["request_id"] == request_id
    assert "generation" not in call
    assert "lease_id" not in call

    rejected = client.post(
        "/internal/v1/automation/instances/automation-1/generation/"
        "recover-current-unknown-write",
        json={"request_id": str(uuid.uuid4()), "lease_id": "browser-lease"},
    )
    assert rejected.status_code == 422


def test_configuration_route_refreshes_scheduler_after_ready_commit() -> None:
    service = _ApiService()
    refresh_calls: list[str] = []
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: (
            refresh_calls.append("refresh")
            or {"initialized": True, "jobs": 1, "job_ids": ["server-only"]}
        ),
    )

    response = client.put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=_configuration_request_payload(),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["schedule_runtime_state"] == "ACTIVE"
    assert data["schedule_runtime_enabled"] is True
    assert data["scheduler_refresh_completed"] is True
    assert refresh_calls == ["refresh"]
    assert "job_ids" not in data


def test_configuration_route_reports_refresh_failure_and_blocked_generation() -> None:
    service = _ApiService()
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("scheduler unavailable")
        ),
    )
    failed = client.put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=_configuration_request_payload(),
    )
    assert failed.status_code == 200
    assert failed.json()["data"]["schedule_runtime_state"] == "REFRESH_FAILED"
    assert failed.json()["data"]["scheduler_refresh_completed"] is False

    refresh_calls: list[str] = []
    service.configuration_result["generation_ready"] = False
    blocked = _api_client(
        service,
        scheduler_refresh_provider=lambda: refresh_calls.append("refresh"),
    ).put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=_configuration_request_payload(),
    )
    assert blocked.status_code == 200
    assert blocked.json()["data"]["schedule_runtime_state"] == "BLOCKED_GENERATION"
    assert refresh_calls == []


def test_configuration_response_loss_retry_can_repeat_runtime_refresh() -> None:
    service = _ApiService()
    outcomes: list[object] = [
        RuntimeError("first refresh unavailable"),
        {"initialized": True, "jobs": 1, "job_ids": ["server-only"]},
    ]

    def refresh() -> dict[str, Any]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    client = _api_client(service, scheduler_refresh_provider=refresh)
    payload = _configuration_request_payload()
    first = client.put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=payload,
    )
    retried = client.put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=payload,
    )

    assert first.json()["data"]["schedule_runtime_state"] == "REFRESH_FAILED"
    assert retried.json()["data"]["schedule_runtime_state"] == "ACTIVE"
    calls = [item for item in service.calls if item[0] == "configuration"]
    assert len(calls) == 2
    assert {item[1]["request_id"] for item in calls} == {payload["request_id"]}


def test_v2_scheduler_status_uses_contribution_kind_not_contribution_id() -> None:
    service = _ApiService()
    service.configuration_result.update(
        {
            "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
            "enabled_entrypoints": ["daily_clockin"],
            "entrypoint_kinds": {"daily_clockin": "scheduler"},
        }
    )
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: {"initialized": True},
    )

    enabled = client.put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=_configuration_request_payload(),
    )

    assert enabled.status_code == 200
    assert enabled.json()["data"]["schedule_runtime_state"] == "ACTIVE"

    service.configuration_result["entrypoint_kinds"] = {"daily_clockin": "console"}
    disabled = client.put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=_configuration_request_payload(),
    )

    assert disabled.status_code == 200
    assert disabled.json()["data"]["schedule_runtime_state"] == "ENTRYPOINT_DISABLED"


def test_v2_scheduler_status_can_read_catalog_invocation_contract() -> None:
    service = _ApiService()
    service.configuration_result.update(
        {
            "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
            "enabled_entrypoints": ["daily_clockin"],
        }
    )
    service.catalog_result = {
        "instances": [
            {
                "automation_id": "automation-1",
                "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
                "invocation_contracts": {
                    "daily_clockin": {"contribution_kind": "scheduler"},
                },
            }
        ]
    }
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: {"initialized": True},
    )

    response = client.put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=_configuration_request_payload(),
    )

    assert response.status_code == 200
    assert response.json()["data"]["schedule_runtime_state"] == "ACTIVE"


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


def test_worker_pair_dto_rejects_private_identity_material() -> None:
    service = _ApiService()
    client = _api_client(service)
    public_key = base64.b64encode(b"p" * 32).decode("ascii")
    payload = {
        "device_id": "windows-season-1",
        "display_name": "Season desktop",
        "platform": "windows",
        "agent_version": "1.0.0",
        "identity_json": {
            "device_key_id": "device-key-1",
            "ed25519_public_key_base64": public_key,
            "tls_client_certificate_sha256": "a" * 64,
        },
        "capabilities_json": {"interactive": True},
        "request_id": str(uuid.uuid4()),
    }
    accepted = client.post("/internal/v1/automation/workers/pair", json=payload)
    assert accepted.status_code == 200
    assert service.calls[-1][0] == "pair"

    payload["identity_json"]["ed25519_private_key_base64"] = public_key
    rejected = client.post("/internal/v1/automation/workers/pair", json=payload)
    assert rejected.status_code == 422
    assert public_key not in rejected.text
    assert len([item for item in service.calls if item[0] == "pair"]) == 1


def test_management_identity_and_worker_projection_are_fail_closed() -> None:
    workers = SimpleNamespace(
        list_worker_devices=lambda: (
            {
                "device_id": "worker-1",
                "display_name": "Dispatch desktop",
                "platform": "windows",
                "service_state": "ONLINE",
                "interactive_session_state": "UNLOCKED",
                "last_seen_at": datetime(2026, 8, 15, tzinfo=timezone.utc),
                "identity_json": {"device_key_id": "must-not-leak"},
                "paired_public_key_fingerprint": "f" * 64,
                "capabilities_json": {"interactive": True},
            },
        )
    )
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=workers,
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    projection = service.worker_projection(actor=_console_actor(super_admin=False))
    serialized = repr(projection)
    assert projection["workers"][0]["online"] is True
    assert "identity_json" not in serialized
    assert "fingerprint" not in serialized
    assert "capabilities" not in serialized

    forged = Actor(
        actor_type=ActorType.CONSOLE_ADMIN,
        actor_id="forged",
        roles=("super_admin",),
        authenticated_by="internal_service_token",
    )
    with pytest.raises(PluginConflictError) as error:
        service.catalog_projection(actor=forged)
    assert error.value.code == "PLUGIN_MANAGEMENT_FORBIDDEN"

    with pytest.raises(PluginConflictError) as error:
        service.install(
            b"package",
            instance_name="blocked",
            request_id=str(uuid.uuid4()),
            transport_package_sha256="0" * 64,
            actor=_console_actor(super_admin=False),
        )
    assert error.value.code == "PLUGIN_MANAGEMENT_FORBIDDEN"


def test_catalog_projects_only_closed_managed_resource_descriptors() -> None:
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        resource_catalog_provider=lambda: (
            {
                "resource_id": "phase7.input_sheet",
                "name": "输入表格",
                "kind": "feishu_sheet",
                "status": "available",
            },
        ),
    )

    projection = service.catalog_projection(actor=_console_actor(super_admin=False))

    assert projection["resource_pool_available"] is True
    assert projection["resources"] == [
        {
            "resource_id": "phase7.input_sheet",
            "name": "输入表格",
            "kind": "feishu_sheet",
            "status": "available",
        }
    ]
    assert "token" not in repr(projection)

    invalid = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        resource_catalog_provider=lambda: (
            {
                "resource_id": "phase7.input_sheet",
                "name": "输入表格",
                "kind": "feishu_sheet",
                "status": "available",
                "token": "must-not-cross-boundary",
            },
        ),
    )
    invalid_projection = invalid.catalog_projection(
        actor=_console_actor(super_admin=False)
    )
    assert invalid_projection["resource_pool_available"] is False
    assert invalid_projection["resources"] == []
    assert "must-not-cross-boundary" not in repr(invalid_projection)

def test_management_mutations_fail_closed_during_release_hold() -> None:
    lifecycle_calls: list[dict[str, Any]] = []
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            install_upload=lambda *args, **kwargs: lifecycle_calls.append(kwargs)
        ),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        release_hold_provider=lambda: True,
    )
    assert service.catalog_projection(actor=_console_actor())["plugins"]
    with pytest.raises(PluginConflictError) as error:
        service.install(
            b"signed-package",
            instance_name="blocked during release",
            request_id=str(uuid.uuid4()),
            transport_package_sha256="a" * 64,
            actor=_console_actor(),
        )
    assert error.value.code == "PLUGIN_RELEASE_HOLD"
    assert lifecycle_calls == []

    unavailable = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        release_hold_provider=lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    with pytest.raises(PluginConflictError) as error:
        unavailable.set_enabled(
            "automation-1",
            enabled=False,
            request_id=str(uuid.uuid4()),
            expected_record_version=3,
            actor=_console_actor(),
        )
    assert error.value.code == "PLUGIN_RELEASE_HOLD_STATE_UNAVAILABLE"


def test_enable_requires_exact_committed_generation_before_lifecycle_call() -> None:
    lifecycle_calls: list[dict[str, Any]] = []
    target_calls: list[str] = []
    catalog = _Catalog(_entry())
    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            set_enabled=lambda *args, **kwargs: lifecycle_calls.append(kwargs)
        ),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda automation_id: target_calls.append(automation_id)
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    with pytest.raises(PluginConflictError) as error:
        service.set_enabled(
            "automation-1",
            enabled=True,
            request_id=str(uuid.uuid4()),
            expected_record_version=3,
            actor=_console_actor(),
        )
    assert error.value.code == "PLUGIN_GENERATION_NOT_READY"
    assert target_calls == ["automation-1"]
    assert lifecycle_calls == []


def test_disable_revokes_authority_while_generation_is_reconciling() -> None:
    lifecycle_calls: list[dict[str, Any]] = []
    target_calls: list[str] = []
    catalog = _Catalog(
        _entry(
            enabled=True,
            state=PluginProjectState.ENABLED.value,
            reconcile_state=RuntimeReconcileState.PREPARING,
        )
    )

    def set_enabled(automation_id: str, **kwargs: Any) -> SimpleNamespace:
        lifecycle_calls.append({"automation_id": automation_id, **kwargs})
        return SimpleNamespace(
            automation_id=automation_id,
            plugin_id="example_action",
            display_name="Example action",
            active_version=SimpleNamespace(version="1.0.0"),
            enabled=False,
            state=PluginProjectState.DISABLED,
            record_version=4,
            target_generation=2,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.PREPARING,
        )

    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(set_enabled=set_enabled),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda automation_id: target_calls.append(automation_id)
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    result = service.set_enabled(
        "automation-1",
        enabled=False,
        request_id=str(uuid.uuid4()),
        expected_record_version=3,
        actor=_console_actor(),
    )

    assert result["enabled"] is False
    assert result["state"] == "DISABLED"
    assert target_calls == []
    assert lifecycle_calls[0]["expected_record_version"] == 3


def test_state_response_loss_retry_reaches_audited_lifecycle_without_reconcile() -> None:
    catalog = _Catalog(
        _entry(
            enabled=True,
            state=PluginProjectState.ENABLED.value,
            reconcile_state=RuntimeReconcileState.PREPARING,
        )
    )
    target_calls: list[str] = []
    lifecycle_calls: list[dict[str, Any]] = []
    request_id = str(uuid.uuid4())

    def set_enabled(automation_id: str, **kwargs: Any) -> SimpleNamespace:
        lifecycle_calls.append({"automation_id": automation_id, **kwargs})
        if len(lifecycle_calls) == 1:
            catalog.current = _entry(
                enabled=False,
                state=PluginProjectState.DISABLED.value,
                record_version=4,
                reconcile_state=RuntimeReconcileState.PREPARING,
            )
        elif lifecycle_calls[-1] != lifecycle_calls[0]:
            raise IdempotencyConflict("plugin state request was reused")
        return SimpleNamespace(
            automation_id=automation_id,
            plugin_id="example_action",
            display_name="Example action",
            active_version=SimpleNamespace(version="1.0.0"),
            enabled=False,
            state=PluginProjectState.DISABLED,
            record_version=4,
            target_generation=2,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.PREPARING,
        )

    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(set_enabled=set_enabled),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda automation_id: target_calls.append(automation_id)
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    first = service.set_enabled(
        "automation-1",
        enabled=False,
        request_id=request_id,
        expected_record_version=3,
        actor=_console_actor(),
    )
    retried = service.set_enabled(
        "automation-1",
        enabled=False,
        request_id=request_id,
        expected_record_version=3,
        actor=_console_actor(),
    )

    assert first == retried
    assert first["enabled"] is False
    assert target_calls == []
    assert len(lifecycle_calls) == 2
    assert {call["request_id"] for call in lifecycle_calls} == {request_id}
    assert {call["expected_record_version"] for call in lifecycle_calls} == {3}


def test_stale_state_request_fails_closed_when_audited_lifecycle_rejects_it() -> None:
    lifecycle_calls: list[dict[str, Any]] = []

    def reject_state_change(automation_id: str, **kwargs: Any) -> None:
        lifecycle_calls.append({"automation_id": automation_id, **kwargs})
        raise IdempotencyConflict("plugin state request was reused")

    service = AutomationPluginManagementService(
        catalog=_Catalog(
            _entry(
                enabled=True,
                state=PluginProjectState.ENABLED.value,
                record_version=4,
            )
        ),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(set_enabled=reject_state_change),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda _automation_id: (_ for _ in ()).throw(
                AssertionError("stale retries must not reconcile")
            )
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    with pytest.raises(PluginConflictError) as error:
        service.set_enabled(
            "automation-1",
            enabled=True,
            request_id=str(uuid.uuid4()),
            expected_record_version=3,
            actor=_console_actor(),
        )

    assert error.value.code == "PLUGIN_INSTANCE_VERSION_CONFLICT"
    assert len(lifecycle_calls) == 1


@pytest.mark.parametrize(
    "current_entry",
    (
        _entry(record_version=2),
        _entry(
            enabled=False,
            state=PluginProjectState.DISABLED.value,
            record_version=4,
        ),
    ),
)
def test_stale_state_request_with_non_replay_shape_never_calls_lifecycle(
    current_entry: SimpleNamespace,
) -> None:
    lifecycle_calls: list[dict[str, Any]] = []
    service = AutomationPluginManagementService(
        catalog=_Catalog(current_entry),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            set_enabled=lambda *args, **kwargs: lifecycle_calls.append(kwargs)
        ),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    with pytest.raises(PluginConflictError) as error:
        service.set_enabled(
            "automation-1",
            enabled=True,
            request_id=str(uuid.uuid4()),
            expected_record_version=3,
            actor=_console_actor(),
        )

    assert error.value.code == "PLUGIN_INSTANCE_VERSION_CONFLICT"
    assert lifecycle_calls == []


def test_upgrade_response_loss_retry_keeps_non_runnable_transition_idempotent() -> None:
    catalog = _Catalog(_entry(installed_version="1.0.0"))
    lifecycle_calls: list[dict[str, Any]] = []
    request_id = str(uuid.uuid4())

    def upgrade_upload(automation_id: str, package: bytes, **kwargs: Any) -> None:
        assert automation_id == "automation-1"
        assert package == b"package-v2"
        lifecycle_calls.append(kwargs)
        catalog.current = _entry(
            installed_version="2.0.0",
            record_version=4,
            target_generation=3,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.WAITING_COEFFECTS,
        )

    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(upgrade_upload=upgrade_upload),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda _automation_id: (_ for _ in ()).throw(
                RuntimeError("worker offline")
            )
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    first = service.upgrade(
        "automation-1",
        b"package-v2",
        request_id=request_id,
        expected_record_version=3,
        transport_package_sha256="a" * 64,
        actor=_console_actor(),
    )
    retried = service.upgrade(
        "automation-1",
        b"package-v2",
        request_id=request_id,
        expected_record_version=3,
        transport_package_sha256="a" * 64,
        actor=_console_actor(),
    )
    assert first == retried
    assert first["version"] == "2.0.0"
    assert first["generation_ready"] is False
    assert first["transition_state"] == "BLOCKED_DEPENDENCY"
    assert len(lifecycle_calls) == 2
    assert {call["request_id"] for call in lifecycle_calls} == {request_id}
    assert {call["expected_record_version"] for call in lifecycle_calls} == {3}


def test_configuration_persists_exact_payload_and_projects_reconcile_block() -> None:
    catalog = _Catalog(_entry())
    saved: list[dict[str, Any]] = []
    record = AutomationProjectConfigRecord(
        automation_id="automation-1",
        config={"mode": "daily"},
        account_bindings={"season": "account-season"},
        resource_bindings={"input": "resource-1"},
        schedule={"kind": "daily_times", "times": ["08:00"], "enabled": True},
        config_version=2,
        configured=True,
        config_sha256="1" * 64,
        account_bindings_sha256="2" * 64,
        resource_bindings_sha256="3" * 64,
        device_binding_sha256="4" * 64,
        enabled_entrypoints=("scheduler",),
    )

    def save(*args: Any, **kwargs: Any) -> AutomationProjectConfigRecord:
        saved.append({"args": args, **kwargs})
        return record

    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(save=save),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(
            reconcile_project=lambda _automation_id: (_ for _ in ()).throw(
                RuntimeError("resource offline")
            )
        ),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    result = service.save_configuration(
        "automation-1",
        config={"mode": "daily"},
        account_bindings={"season": "account-season"},
        resource_bindings={"input": "resource-1"},
        enabled_entrypoints=("scheduler",),
        schedule={"kind": "daily_times", "times": ["08:00"], "enabled": True},
        device_id=None,
        request_id=str(uuid.uuid4()),
        expected_project_configuration_version=1,
        actor=_console_actor(),
    )
    assert saved[0]["account_bindings"] == {"season": "account-season"}
    assert saved[0]["schedule"] == {
        "kind": "daily_times",
        "times": ["08:00"],
        "enabled": True,
    }
    assert result["generation_ready"] is False
    assert result["transition_state"] == "BLOCKED_DEPENDENCY"
    assert result["schedule"] == {
        "kind": "daily_times",
        "times": ["08:00"],
        "enabled": True,
    }
    assert result["enabled_entrypoints"] == ["scheduler"]


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


def test_create_migration_pair_copies_only_uniquely_compatible_closed_bindings() -> None:
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
        schedule={"kind": "daily_times", "times": ["18:30"], "enabled": True},
        config_version=7,
        configured=True,
        config_sha256="1" * 64,
        account_bindings_sha256="2" * 64,
        resource_bindings_sha256="3" * 64,
        device_binding_sha256="4" * 64,
        enabled_entrypoints=("scheduler",),
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
    assert saves[0]["enabled_entrypoints"] == ("manual_run", "daily_clockin")
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


def test_create_migration_pair_copy_failure_leaves_durable_preparing_hold() -> None:
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


class _AccountManager:
    @staticmethod
    def list_accounts(*, include_status: bool, validate: bool) -> list[dict[str, Any]]:
        assert include_status is False and validate is False
        return [
            {
                "account_id": "default-account",
                "system": "ronghui",
                "is_active": True,
                "is_default": True,
            },
            {
                "account_id": "season",
                "system": "ronghui",
                "is_active": True,
                "is_default": False,
            },
        ]


def test_binding_resolver_never_uses_default_or_first_item() -> None:
    worker = {
        "device_id": "worker-season",
        "display_name": "Season desktop",
        "platform": "windows",
        "service_state": "OFFLINE",
        "interactive_session_state": "LOCKED",
        "capabilities_json": {"interactive": True},
        "paired_public_key_fingerprint": "a" * 64,
        "capabilities_sha256": "b" * 64,
        "record_version": 1,
    }
    resolver = ProductionProjectBindingResolver(
        account_manager=_AccountManager(),
        resource_provider=lambda resource_id: (
            {
                "resource_id": resource_id,
                "resource_kind": "input_file",
                "_meta": {
                    "configuration_version": 3,
                    "config_sha256": "c" * 64,
                    "source": "managed-resource-pool",
                },
            }
            if resource_id == "resource-exact"
            else None
        ),
        worker_repository=SimpleNamespace(
            get_worker_device=lambda device_id: worker
            if device_id == "worker-season"
            else None
        ),
    )
    account_role = {"allowed_systems": ["ronghui"]}
    assert resolver.describe_account_binding(
        automation_id="automation-1",
        role=account_role,
        account_id="season",
    )["account_id"] == "season"
    with pytest.raises(PluginConflictError) as error:
        resolver.validate_account_binding(
            automation_id="automation-1",
            role=account_role,
            account_id="missing-account",
        )
    assert error.value.code == "PLUGIN_ACCOUNT_BINDING_NOT_FOUND"

    resource_role = {"allowed_kinds": ["input_file"]}
    assert resolver.describe_resource_binding(
        automation_id="automation-1",
        role=resource_role,
        resource_id="resource-exact",
    )["resource_kind"] == "input_file"
    with pytest.raises(PluginConflictError) as error:
        resolver.validate_resource_binding(
            automation_id="automation-1",
            role=resource_role,
            resource_id="missing-resource",
        )
    assert error.value.code == "PLUGIN_RESOURCE_BINDING_NOT_FOUND"


    binding = resolver.resolve_device_binding(
        automation_id="automation-1",
        device_id="worker-season",
        worker_requirement={"supported_os": ["windows"], "interactive_session": True},
    )
    assert binding.device_id == "worker-season"
    with pytest.raises(PluginConflictError) as error:
        resolver.resolve_device_binding(
            automation_id="automation-1",
            device_id="worker-missing",
            worker_requirement={
                "supported_os": ["windows"],
                "interactive_session": True,
            },
        )
    assert error.value.code == "PLUGIN_WORKER_BINDING_NOT_FOUND"


def test_binding_resolver_broker_resource_requires_exact_complete_revision() -> None:
    resources = {
        "bitable-exact": {
            "resource_kind": "feishu_bitable",
            "_meta": {
                "configuration_version": 4,
                "config_sha256": "d" * 64,
                "source": "automation-settings",
                "updated_at": "2026-08-15T12:00:00+08:00",
            },
        },
        "source-missing": {
            "resource_kind": "feishu_bitable",
            "_meta": {
                "configuration_version": 4,
                "config_sha256": "e" * 64,
                "source": "",
            },
        },
    }
    resolver = ProductionProjectBindingResolver(
        account_manager=_AccountManager(),
        resource_provider=resources.get,
        worker_repository=SimpleNamespace(get_worker_device=lambda _device_id: None),
    )

    descriptor = resolver.require_active(
        resource_id="bitable-exact",
        allowed_kinds=["feishu_bitable"],
    )
    assert descriptor == {
        "resource_id": "bitable-exact",
        "resource_kind": "feishu_bitable",
        "source": "automation-settings",
        "configuration_version": "4",
        "config_sha256": "d" * 64,
        "updated_at": "2026-08-15T12:00:00+08:00",
    }
    for resource_id, kinds in (
        ("missing", ["feishu_bitable"]),
        ("bitable-exact", ["feishu_sheet"]),
        ("source-missing", ["feishu_bitable"]),
    ):
        with pytest.raises(PluginConflictError):
            resolver.require_active(resource_id=resource_id, allowed_kinds=kinds)


def test_pair_worker_validates_public_identity_and_repository_audit_contract() -> None:
    rows: dict[str, dict[str, Any]] = {}

    def pair_worker_device(row: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert kwargs["request_id"]
        current = rows.get(row["device_id"])
        if current is not None and current["identity_json"] != row["identity_json"]:
            raise PluginConflictError("device identity cannot be replaced")
        persisted = {
            **row,
            "service_state": "OFFLINE",
            "interactive_session_state": "LOGGED_OUT",
            "last_seen_at": None,
        }
        rows[row["device_id"]] = persisted
        return persisted

    workers = SimpleNamespace(pair_worker_device=pair_worker_device)
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=workers,
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    public_bytes = b"k" * 32
    identity = {
        "device_key_id": "worker-key-1",
        "ed25519_public_key_base64": base64.b64encode(public_bytes).decode("ascii"),
        "tls_client_certificate_sha256": "c" * 64,
    }
    result = service.pair_worker(
        device_id="worker-season",
        display_name="Season desktop",
        platform="windows",
        agent_version="1.0.0",
        identity=identity,
        capabilities={"interactive": True},
        request_id=str(uuid.uuid4()),
        actor=_console_actor(),
    )
    assert result["device_id"] == "worker-season"
    assert rows["worker-season"]["paired_public_key_fingerprint"] == hashlib.sha256(
        public_bytes
    ).hexdigest()
    assert "identity_json" not in result

    with pytest.raises(PluginConflictError):
        service.pair_worker(
            device_id="worker-season",
            display_name="Season desktop",
            platform="windows",
            agent_version="1.0.0",
            identity={
                **identity,
                "ed25519_public_key_base64": base64.b64encode(b"z" * 32).decode(
                    "ascii"
                ),
            },
            capabilities={"interactive": True},
            request_id=str(uuid.uuid4()),
            actor=_console_actor(),
        )


def test_mysql_pairing_adapter_does_not_call_unaudited_pair_device() -> None:
    calls: list[str] = []

    class _LowLevel:
        def pair_device(self, _row: dict[str, Any]) -> None:
            calls.append("unaudited")

    class _UnitOfWork:
        automation_plugins = _LowLevel()

        def __enter__(self) -> _UnitOfWork:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        @staticmethod
        def commit() -> None:
            calls.append("commit")

    repository = MySQLAutomationPluginManagementRepository(
        SimpleNamespace(unit_of_work=lambda: _UnitOfWork())
    )
    with pytest.raises(PluginConflictError) as error:
        repository.pair_worker_device(
            {"device_id": "worker-1"},
            request_id=str(uuid.uuid4()),
            actor_id="console-admin-1",
            actor_role="super_admin",
        )
    assert error.value.code == "PLUGIN_WORKER_PAIRING_AUDIT_UNAVAILABLE"
    assert calls == []


def test_mysql_pairing_adapter_writes_domain_outbox_in_same_uow() -> None:
    calls: list[tuple[str, Any]] = []
    request_id = str(uuid.uuid4())

    class _LowLevel:
        @staticmethod
        def pair_device_with_audit(row, **kwargs):
            calls.append(("pair", (dict(row), dict(kwargs))))
            return {
                **row,
                "identity_sha256": "1" * 64,
                "capabilities_sha256": "2" * 64,
            }

    class _Events:
        @staticmethod
        def append_with_outbox(event, outbox):
            calls.append(("event", (dict(event), tuple(outbox))))

    class _UnitOfWork:
        automation_plugins = _LowLevel()
        events = _Events()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def commit():
            calls.append(("commit", None))

    repository = MySQLAutomationPluginManagementRepository(
        SimpleNamespace(unit_of_work=lambda: _UnitOfWork())
    )
    row = {
        "device_id": "worker-1",
        "paired_public_key_fingerprint": "f" * 64,
    }

    persisted = repository.pair_worker_device(
        row,
        request_id=request_id,
        actor_id="admin-one",
        actor_role="super_admin",
    )

    assert persisted["device_id"] == "worker-1"
    assert [name for name, _value in calls] == ["pair", "event", "commit"]
    paired_row, paired_context = calls[0][1]
    assert paired_row == row
    assert paired_context == {
        "request_id": request_id,
        "actor_id": "admin-one",
        "actor_role": "super_admin",
    }
    event, outbox = calls[1][1]
    assert event["event_type"] == "automation_worker.paired"
    assert event["correlation_id"] == request_id
    assert "identity_json" not in event["payload"]
    assert outbox[0]["consumer_name"] == "orchestration.audit"


def test_management_package_reader_returns_only_exact_installed_signed_archive(
    tmp_path: Path,
) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    archive = b"PK\x03\x04worker-package"
    digest = hashlib.sha256(archive).hexdigest()
    stage = storage.create_staging_root("worker_action", "1.0.0")
    relative = storage.persist_verified_archive(
        stage,
        archive,
        expected_sha256=digest,
    )
    install_root = storage.commit_staging_root(
        stage,
        plugin_id="worker_action",
        version="1.0.0",
        manifest_sha256="d" * 64,
    )
    version = PluginVersionRecord(
        plugin_id="worker_action",
        version="1.0.0",
        package_sha256=digest,
        manifest_sha256="d" * 64,
        manifest={},
        trust_source=PluginTrustSource.ED25519_UPLOAD,
        install_root=str(install_root),
        install_metadata={
            "archive_relative": relative,
            "archive_sha256": digest,
        },
    )
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(
            get_package_version=lambda plugin_id, plugin_version: version
            if (plugin_id, plugin_version) == ("worker_action", "1.0.0")
            else None
        ),
        storage=storage,
    )
    assert service.package_bytes(
        "worker_action",
        "1.0.0",
        expected_sha256=digest,
    ) == archive
    with pytest.raises(PluginConflictError) as error:
        service.package_bytes(
            "worker_action",
            "1.0.0",
            expected_sha256="e" * 64,
        )
    assert error.value.code == "PLUGIN_PACKAGE_DIGEST_MISMATCH"

    (install_root / relative).write_bytes(archive + b"tampered")
    with pytest.raises(PluginPackageError):
        service.package_bytes(
            "worker_action",
            "1.0.0",
            expected_sha256=digest,
        )
