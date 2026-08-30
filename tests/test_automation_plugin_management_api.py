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


class _ContributionRegistry:
    def __init__(
        self,
        active_generation: int | None,
        records: tuple[object, ...] = (),
    ) -> None:
        self.generation = active_generation
        self.records = records

    def active_generation(self, automation_id: str) -> int | None:
        assert automation_id
        return self.generation

    def snapshot(self) -> tuple[object, ...]:
        return self.records


class _ProjectionCatalog:
    def __init__(self, instance: dict[str, Any], entry: object) -> None:
        self.instance = instance
        self.entry = entry

    def safe_projection(self) -> dict[str, Any]:
        return {
            "plugins": [],
            "instances": [json.loads(json.dumps(self.instance))],
        }

    def require(self, automation_id: str) -> object:
        assert automation_id == self.instance["automation_id"]
        return self.entry


def _committed_service_entry(
    *,
    automation_id: str,
    generation: int | None,
    enabled: bool,
    declared_kinds: dict[str, str],
    committed_enabled_entrypoints: tuple[str, ...],
) -> SimpleNamespace:
    contributions: dict[str, list[dict[str, str]]] = {
        "console": [],
        "scheduler": [],
        "webhook": [],
        "feishu": [],
        "events": [],
    }
    for contribution_id, contribution_kind in declared_kinds.items():
        contributions[contribution_kind].append(
            {
                "id": contribution_id,
                "service": "plugin.example.run@1",
                "operation": "run",
            }
        )
    snapshot = (
        SimpleNamespace(
            enabled_entrypoints=committed_enabled_entrypoints,
            execution_metadata={"contributions": contributions},
        )
        if generation is not None
        else None
    )
    return SimpleNamespace(
        automation_id=automation_id,
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        enabled=enabled,
        committed_generation=generation,
        committed_snapshot=snapshot,
    )


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

    def inspect_service_v2_upload(self, package: bytes, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("inspect-v2", {"package": package, **kwargs}))
        return {
            "plugin_id": "example_service",
            "name": "Example service",
            "version": "2.0.0",
            "host_api": {"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
            "permissions": [],
            "account_roles": [],
            "resource_roles": [],
            "config_schema": {"type": "object"},
            "contributions": [],
            "scheduling": {"supported": False, "default_schedule": {"kind": "none", "times": [], "enabled": False}},
        }

    def install_service_v2(self, package: bytes, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("install-v2", {"package": package, **kwargs}))
        return {"automation_id": "server-generated", "generation_ready": True}

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


def _service_v2_install_intent(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "instance_name": "Example service project",
        "config": {},
        "account_bindings": {},
        "resource_bindings": {},
        "enabled_entrypoints": [],
        "schedule": {"kind": "none", "times": [], "enabled": False},
        "permissions_confirmed": True,
    }
    value.update(overrides)
    return value


def test_service_v2_inspect_upload_is_exact_multipart_and_has_no_mutation() -> None:
    service = _ApiService()
    client = _api_client(service)
    package = b"PK\x03\x04service-v2"
    request_id = str(uuid.uuid4())

    response = client.post(
        "/internal/v1/automation/plugins/inspect-upload",
        data={
            "request_id": request_id,
            "package_sha256": hashlib.sha256(package).hexdigest(),
        },
        files={"package": ("service.zip", package, "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["data"]["plugin_id"] == "example_service"
    name, call = service.calls[-1]
    assert name == "inspect-v2"
    assert call["request_id"] == request_id
    assert call["package"] == package
    assert all(name not in {"install", "install-v2", "configuration", "state"} for name, _ in service.calls)

    rejected = client.post(
        "/internal/v1/automation/plugins/inspect-upload",
        data={
            "request_id": str(uuid.uuid4()),
            "package_sha256": hashlib.sha256(package).hexdigest(),
            "automation_id": "browser-authority",
        },
        files={"package": ("service.zip", package, "application/zip")},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "PLUGIN_MULTIPART_FIELDS_INVALID"


def test_service_v2_install_requires_exact_bounded_intent_multipart() -> None:
    service = _ApiService()
    client = _api_client(service)
    package = b"PK\x03\x04service-v2"
    request_id = str(uuid.uuid4())
    intent = _service_v2_install_intent()

    response = client.post(
        "/internal/v1/automation/plugins/install-v2",
        data={
            "request_id": request_id,
            "package_sha256": hashlib.sha256(package).hexdigest(),
            "intent": json.dumps(intent),
        },
        files={"package": ("service.zip", package, "application/zip")},
    )

    assert response.status_code == 200
    name, call = service.calls[-1]
    assert name == "install-v2"
    assert call["raw_intent"] == json.dumps(intent)
    assert call["request_id"] == request_id

    extra = client.post(
        "/internal/v1/automation/plugins/install-v2",
        data={
            "request_id": str(uuid.uuid4()),
            "package_sha256": hashlib.sha256(package).hexdigest(),
            "intent": json.dumps(intent),
            "automation_id": "browser-authority",
        },
        files={"package": ("service.zip", package, "application/zip")},
    )
    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "PLUGIN_MULTIPART_FIELDS_INVALID"

    oversized = client.post(
        "/internal/v1/automation/plugins/install-v2",
        data={
            "request_id": str(uuid.uuid4()),
            "package_sha256": hashlib.sha256(package).hexdigest(),
            "intent": "x" * (16 * 1024 + 1),
        },
        files={"package": ("service.zip", package, "application/zip")},
    )
    assert oversized.status_code == 422
    assert oversized.json()["error"]["code"] == "PLUGIN_MULTIPART_FIELDS_INVALID"


@pytest.mark.parametrize(
    "intent",
    [
        _service_v2_install_intent(automation_id="browser-selected"),
        _service_v2_install_intent(device_id="desktop-worker"),
        _service_v2_install_intent(permissions_confirmed=False),
        _service_v2_install_intent(package_sha256="a" * 64),
    ],
)
def test_service_v2_install_intent_rejects_browser_authority_and_unconfirmed_permissions(
    intent: dict[str, Any],
) -> None:
    with pytest.raises(PluginConflictError) as raised:
        AutomationPluginManagementService._parse_service_v2_install_intent(
            json.dumps(intent)
        )
    assert raised.value.code == "PLUGIN_INSTALL_INTENT_INVALID"


def test_service_v2_inspect_projection_excludes_service_operation_and_package_authority() -> None:
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="example_service",
            name="Example service",
            version="2.0.0",
            host_api={"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
            capabilities=({"name": "storage.kv", "operations": ("get",), "account_role": None, "resource_role": None},),
            account_roles=(),
            resource_roles=(),
            config_schema={"type": "object", "additionalProperties": False, "properties": {}, "required": []},
            contributes={
                "console": ({"id": "run_now", "title": "Run now", "service": "plugin.example_service.run@1", "operation": "run", "default_enabled": True},),
                "scheduler": ({"id": "nightly", "title": "Nightly", "service": "plugin.example_service.run@1", "operation": "run", "default_enabled": True, "schedule": {"kind": "cron", "expression": "5 18 * * *", "timezone": "Asia/Shanghai"}},),
                "webhook": (), "feishu": (), "events": (),
            },
        )
    )

    projection = AutomationPluginManagementService._service_v2_wizard_projection(verified)

    assert projection["scheduling"]["default_schedule"] == {
        "kind": "daily_times", "times": ["18:05"], "enabled": True,
    }
    assert set(projection) == {
        "plugin_id", "name", "version", "host_api", "permissions", "account_roles",
        "resource_roles", "config_schema", "contributions", "scheduling",
    }
    assert all("service" not in item and "operation" not in item for item in projection["contributions"])
    assert "package_sha256" not in projection


def test_legacy_install_keeps_action_v1_and_rejects_service_v2_without_intent() -> None:
    calls: list[str] = []
    active_version = SimpleNamespace(
        version="1.0.0",
        runtime_model=PluginRuntimeModel.ACTION_V1,
        plugin_api="1.0.0",
    )
    instance = SimpleNamespace(
        automation_id="action-v1-instance",
        plugin_id="example_action",
        display_name="Action v1 instance",
        active_version=active_version,
        enabled=False,
        state=PluginProjectState.INSTALLED,
        record_version=1,
        target_generation=1,
        committed_generation=None,
        reconcile_state=RuntimeReconcileState.PREPARING,
    )

    def inspect_action_v1(*_args: Any, **_kwargs: Any) -> None:
        calls.append("inspect-v1")
        raise PluginPackageError(
            "service v2 required",
            code="PLUGIN_SERVICE_V2_REQUIRED",
        )

    lifecycle = SimpleNamespace(
        inspect_service_v2_upload=inspect_action_v1,
        install_upload=lambda *_args, **_kwargs: calls.append("install-v1") or instance,
    )
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=lifecycle,
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    result = service.install(
        b"action-v1-package",
        instance_name="Action v1 instance",
        request_id=str(uuid.uuid4()),
        transport_package_sha256="a" * 64,
        actor=_console_actor(),
    )

    assert calls == ["inspect-v1", "install-v1"]
    assert result["automation_id"] == "action-v1-instance"

    lifecycle.inspect_service_v2_upload = lambda *_args, **_kwargs: object()
    with pytest.raises(PluginConflictError) as raised:
        service.install(
            b"service-v2-package",
            instance_name="Service v2 instance",
            request_id=str(uuid.uuid4()),
            transport_package_sha256="b" * 64,
            actor=_console_actor(),
        )
    assert raised.value.code == "PLUGIN_SERVICE_V2_INSTALL_INTENT_REQUIRED"
    assert calls == ["inspect-v1", "install-v1"]


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


def test_scheduler_refresh_rejects_explicit_invalid_tasks_for_lifecycle_and_configuration() -> None:
    service = _ApiService()

    def invalid_refresh() -> dict[str, Any]:
        return {
            "initialized": True,
            "invalid_tasks": [{"task_id": "invalid-task"}],
        }

    client = _api_client(service, scheduler_refresh_provider=invalid_refresh)
    package = b"PK\x03\x04invalid-refresh"
    installed = client.post(
        "/internal/v1/automation/plugins/install",
        data={
            "instance_name": "Invalid refresh",
            "request_id": str(uuid.uuid4()),
            "package_sha256": hashlib.sha256(package).hexdigest(),
        },
        files={"package": ("invalid-refresh.zip", package, "application/zip")},
    )
    configured = client.put(
        "/internal/v1/automation/instances/automation-1/configuration",
        json=_configuration_request_payload(),
    )

    assert installed.status_code == 200
    assert installed.json()["data"]["scheduler_refresh_completed"] is False
    assert installed.json()["data"]["schedule_runtime_state"] == "REFRESH_FAILED"
    assert configured.status_code == 200
    assert configured.json()["data"]["scheduler_refresh_completed"] is False
    assert configured.json()["data"]["schedule_runtime_state"] == "REFRESH_FAILED"


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


def test_uninstall_projection_refresh_error_is_explicitly_retryable_and_not_ready() -> None:
    service = _ApiService()

    def fail_uninstall(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise PluginConflictError(
            "runtime projection refresh failed",
            code="RUNTIME_PROJECTION_REFRESH_FAILED",
        )

    service.uninstall = fail_uninstall  # type: ignore[method-assign]
    refresh_calls: list[str] = []
    client = _api_client(
        service,
        scheduler_refresh_provider=lambda: refresh_calls.append("refresh")
        or {"initialized": True, "invalid_tasks": []},
    )
    response = client.post(
        "/internal/v1/automation/instances/automation-1/uninstall",
        json={
            "request_id": str(uuid.uuid4()),
            "expected_record_version": 3,
            "current_version": "1.0.0",
            "confirm": True,
        },
    )

    assert response.status_code == 409
    data = response.json()["data"]
    assert data["runtime_projection_pending"] is True
    assert data["runtime_projection_retryable"] is True
    assert data["contribution_projection_state"] == "STALE"
    assert data["generation_ready"] is False
    assert data["transition_state"] != "READY"
    assert refresh_calls == []


def test_service_v2_wizard_installs_unknown_package_without_registry_and_enables_only_after_ready() -> None:
    initial = _entry(
        automation_id="unregistered-v2",
        plugin_id="unknown_service",
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        configured=False,
        enabled=False,
        current_enabled_entrypoints=(),
        reconcile_state=RuntimeReconcileState.PREPARING,
        record_version=1,
    )
    configured = _entry(
        **{
            **vars(initial),
            "configured": True,
            "current_enabled_entrypoints": ("run_now",),
            "record_version": 2,
        }
    )
    ready = _entry(
        **{
            **vars(initial),
            "configured": True,
            "current_enabled_entrypoints": ("run_now",),
            "committed_generation": 1,
            "target_generation": 1,
            "reconcile_state": RuntimeReconcileState.STABLE,
            # Configuration staging, generation allocation and generation
            # commit each advance the project version before auto-enable.
            "record_version": 4,
        }
    )
    catalog = _Catalog(initial)
    calls: list[str] = []
    active_version = SimpleNamespace(
        version="2.0.0",
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )
    instance = SimpleNamespace(
        automation_id="unregistered-v2",
        plugin_id="unknown_service",
        display_name="Unknown service",
        active_version=active_version,
        enabled=False,
        state=PluginProjectState.INSTALLED,
        record_version=1,
        target_generation=1,
        committed_generation=None,
        reconcile_state=RuntimeReconcileState.PREPARING,
    )

    def configure(_automation_id: str, **_kwargs: Any) -> None:
        calls.append("configure")
        catalog.current = configured

    def install_upload(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append("install")
        assert kwargs["install_payload_sha256"]
        return instance

    def set_enabled(_automation_id: str, **_kwargs: Any) -> SimpleNamespace:
        calls.append("lifecycle-enable")
        catalog.current.enabled = True
        catalog.current.record_version += 1
        return instance

    def reconcile(_automation_id: str) -> None:
        calls.append("reconcile")
        if catalog.current is configured:
            # Mirror the production repository: target allocation and commit
            # advance record_version after configuration staging.
            catalog.current = ready

    def claim_enable_base(*_args: Any, **_kwargs: Any) -> int:
        assert catalog.current is ready
        assert catalog.current.record_version == 4
        return catalog.current.record_version

    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            inspect_service_v2_upload=lambda *_args, **_kwargs: SimpleNamespace(
                package_sha256="a" * 64
            ),
            install_upload=install_upload,
            claim_service_v2_install_enable_base=claim_enable_base,
            set_enabled=set_enabled,
        ),
        configuration=SimpleNamespace(save=configure),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(reconcile_project=reconcile),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    service._require_committed_ready = lambda _entry: None  # type: ignore[method-assign]

    result = service.install_service_v2(
        b"unknown-v2-package",
        request_id=str(uuid.uuid4()),
        transport_package_sha256="a" * 64,
        raw_intent=json.dumps(
            _service_v2_install_intent(enabled_entrypoints=["run_now"])
        ),
        actor=_console_actor(),
    )

    assert calls == [
        "install",
        "configure",
        "reconcile",
        "reconcile",
        "lifecycle-enable",
        "reconcile",
    ]
    assert result["automation_id"] == "unregistered-v2"
    assert catalog.current.enabled is True
    assert "unknown_service" not in str(calls)


def test_service_v2_install_replay_proves_config_and_exact_enable_child() -> None:
    ready = _entry(
        automation_id="replay-v2",
        plugin_id="replay_service",
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        configured=True,
        enabled=False,
        state=PluginProjectState.INSTALLED.value,
        record_version=4,
        target_generation=1,
        committed_generation=1,
        reconcile_state=RuntimeReconcileState.STABLE,
        current_enabled_entrypoints=("run_now",),
    )
    catalog = _Catalog(ready)
    config_calls: list[dict[str, Any]] = []
    state_calls: list[dict[str, Any]] = []
    active_version = SimpleNamespace(
        version="2.0.0",
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )

    def instance() -> SimpleNamespace:
        return SimpleNamespace(
            automation_id=ready.automation_id,
            plugin_id=ready.plugin_id,
            display_name="Replay service",
            active_version=active_version,
            enabled=ready.enabled,
            state=PluginProjectState(ready.state),
            record_version=ready.record_version,
            target_generation=1,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.STABLE,
        )

    def set_enabled(_automation_id: str, **kwargs: Any) -> SimpleNamespace:
        state_calls.append(dict(kwargs))
        if not ready.enabled:
            ready.enabled = True
            ready.state = PluginProjectState.ENABLED.value
            ready.record_version = 5
        return instance()

    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            inspect_service_v2_upload=lambda *_args, **_kwargs: SimpleNamespace(
                package_sha256="a" * 64
            ),
            install_upload=lambda *_args, **_kwargs: instance(),
            claim_service_v2_install_enable_base=(
                lambda *_args, **_kwargs: 4
            ),
            set_enabled=set_enabled,
        ),
        configuration=SimpleNamespace(
            save=lambda _automation_id, **kwargs: config_calls.append(dict(kwargs))
        ),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(reconcile_project=lambda _automation_id: None),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    service._require_committed_ready = lambda _entry: None  # type: ignore[method-assign]
    root_request_id = str(uuid.uuid4())
    arguments = {
        "package_bytes": b"replay-v2-package",
        "request_id": root_request_id,
        "transport_package_sha256": "a" * 64,
        "raw_intent": json.dumps(
            _service_v2_install_intent(enabled_entrypoints=["run_now"])
        ),
        "actor": _console_actor(),
    }

    first = service.install_service_v2(**arguments)
    second = service.install_service_v2(**arguments)

    assert first["enabled"] is True
    assert second == first
    assert len(config_calls) == 2
    assert {call["expected_project_configuration_version"] for call in config_calls} == {1}
    assert len(state_calls) == 2
    assert state_calls[0] == state_calls[1]
    assert state_calls[0]["expected_record_version"] == 4
    assert state_calls[0]["state_change_context"]["phase"] == "enable"


def test_service_v2_post_enable_failure_rolls_back_and_same_root_resumes_next_audited_attempt() -> None:
    ready = _entry(
        automation_id="recover-v2",
        plugin_id="recover_service",
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        configured=True,
        enabled=False,
        state=PluginProjectState.INSTALLED.value,
        record_version=4,
        target_generation=1,
        committed_generation=1,
        reconcile_state=RuntimeReconcileState.STABLE,
        current_enabled_entrypoints=("run_now",),
    )
    catalog = _Catalog(ready)
    active_version = SimpleNamespace(
        version="2.0.0",
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )
    witnesses: dict[str, dict[str, object]] = {}
    state_calls: list[dict[str, Any]] = []
    reconcile_calls = 0

    def instance() -> SimpleNamespace:
        return SimpleNamespace(
            automation_id=ready.automation_id,
            plugin_id=ready.plugin_id,
            display_name="Recover service",
            active_version=active_version,
            enabled=ready.enabled,
            state=PluginProjectState(ready.state),
            record_version=ready.record_version,
            target_generation=1,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.STABLE,
        )

    def set_enabled(_automation_id: str, **kwargs: Any) -> SimpleNamespace:
        state_calls.append(dict(kwargs))
        request_id = str(kwargs["request_id"])
        expected = int(kwargs["expected_record_version"])
        target = bool(kwargs["enabled"])
        context = dict(kwargs["state_change_context"])
        prior = witnesses.get(request_id)
        witness = {
            "enabled": target,
            "expected_record_version": expected,
            "actor_id": str(kwargs["actor_id"]),
            "actor_role": str(kwargs["actor_role"]),
            "state_change_context": context,
        }
        if prior is not None:
            assert prior == witness
        else:
            assert ready.record_version == expected
            ready.enabled = target
            ready.state = (
                PluginProjectState.ENABLED.value
                if target
                else PluginProjectState.DISABLED.value
            )
            ready.record_version += 1
            witnesses[request_id] = witness
        return instance()

    def reconcile(_automation_id: str) -> None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        if reconcile_calls == 3:
            raise RuntimeError("injected post-enable failure")

    lifecycle = SimpleNamespace(
        inspect_service_v2_upload=lambda *_args, **_kwargs: SimpleNamespace(
            package_sha256="a" * 64
        ),
        install_upload=lambda *_args, **_kwargs: instance(),
        claim_service_v2_install_enable_base=lambda *_args, **_kwargs: 4,
        set_enabled=set_enabled,
        state_change_witness=lambda _automation_id, request_id: witnesses.get(
            request_id
        ),
    )
    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=lifecycle,
        configuration=SimpleNamespace(save=lambda *_args, **_kwargs: None),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(reconcile_project=reconcile),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    service._require_committed_ready = lambda _entry: None  # type: ignore[method-assign]
    root_request_id = str(uuid.uuid4())
    arguments = {
        "package_bytes": b"recover-v2-package",
        "request_id": root_request_id,
        "transport_package_sha256": "a" * 64,
        "raw_intent": json.dumps(
            _service_v2_install_intent(enabled_entrypoints=["run_now"])
        ),
        "actor": _console_actor(),
    }

    with pytest.raises(PluginConflictError) as error:
        service.install_service_v2(**arguments)

    assert error.value.code == "PLUGIN_ENABLE_RECONCILE_FAILED"
    assert ready.enabled is False
    assert ready.state == PluginProjectState.DISABLED.value
    assert ready.record_version == 6
    assert [call["enabled"] for call in state_calls] == [True, False]
    assert state_calls[0]["state_change_context"]["attempt"] == 1
    assert state_calls[1]["state_change_context"]["phase"] == "rollback"

    resumed = service.install_service_v2(**arguments)

    assert resumed["enabled"] is True
    assert ready.record_version == 7
    assert state_calls[-1]["expected_record_version"] == 6
    assert state_calls[-1]["state_change_context"]["attempt"] == 2


def test_service_v2_install_replay_never_treats_manual_disable_as_its_rollback() -> None:
    root_request_id = str(uuid.uuid4())
    payload_sha256 = "b" * 64
    enabled_request_id = AutomationPluginManagementService._service_v2_install_enable_request_id(
        root_request_id,
        1,
    )
    enable_context = AutomationPluginManagementService._service_v2_install_state_context(
        root_request_id=root_request_id,
        install_payload_sha256=payload_sha256,
        attempt=1,
        phase="enable",
    )
    witnesses = {
        enabled_request_id: {
            "enabled": True,
            "expected_record_version": 4,
            "actor_id": "console-admin-1",
            "actor_role": "super_admin",
            "state_change_context": enable_context,
        }
    }
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(
            state_change_witness=lambda _automation_id, request_id: witnesses.get(
                request_id
            )
        ),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    manually_disabled = _entry(
        automation_id="automation-1",
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        enabled=False,
        state=PluginProjectState.DISABLED.value,
        record_version=6,
    )

    with pytest.raises(PluginConflictError) as error:
        service._service_v2_install_enable_claim(
            manually_disabled,
            base_record_version=4,
            root_request_id=root_request_id,
            install_payload_sha256=payload_sha256,
            actor_id="console-admin-1",
            actor_role="super_admin",
        )

    assert error.value.code == "PLUGIN_INSTALL_PROGRESS_CONFLICT"


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


def test_catalog_projects_exact_active_service_v2_contributions_without_leaking_records() -> None:
    instance = {
        "automation_id": "service-project",
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "enabled": True,
        "committed_generation": 7,
        "dependency_state": "READY",
        "blocking_reasons": [],
        "entrypoints": ["run_now", "daily_run"],
        "enabled_entrypoints": ["run_now", "daily_run"],
        "entrypoint_kinds": {
            "run_now": "console",
            "daily_run": "scheduler",
        },
    }
    registry = _ContributionRegistry(
        7,
        (
            SimpleNamespace(
                automation_id="service-project",
                generation=7,
                contribution_id="daily_run",
                contribution_kind="scheduler",
                phase="COMMITTED",
                backend_status="DISABLED",
                service="must-not-cross-boundary",
            ),
            SimpleNamespace(
                automation_id="service-project",
                generation=7,
                contribution_id="run_now",
                contribution_kind="console",
                phase="COMMITTED",
                backend_status="READY",
                declaration={"secret": "must-not-cross-boundary"},
            ),
        ),
    )
    catalog = _ProjectionCatalog(
        instance,
        _committed_service_entry(
            automation_id="service-project",
            generation=7,
            enabled=True,
            declared_kinds={"run_now": "console", "daily_run": "scheduler"},
            committed_enabled_entrypoints=("run_now", "daily_run"),
        ),
    )
    service = AutomationPluginManagementService(
        catalog=catalog,
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        contribution_registry=registry,
    )

    projection = service.catalog_projection(actor=_console_actor(super_admin=False))
    projected = projection["instances"][0]

    assert projected["contribution_projection_state"] == "ACTIVE"
    assert projected["dependency_state"] == "READY"
    assert projected["active_contributions"] == [
        {
            "contribution_id": "run_now",
            "contribution_kind": "console",
            "generation": 7,
            "phase": "COMMITTED",
            "backend_status": "READY",
        },
        {
            "contribution_id": "daily_run",
            "contribution_kind": "scheduler",
            "generation": 7,
            "phase": "COMMITTED",
            "backend_status": "DISABLED",
        },
    ]
    assert "must-not-cross-boundary" not in repr(projection)


def test_catalog_service_v2_projection_states_are_closed_and_fail_closed() -> None:
    managed = {
        "automation_id": "service-project",
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "enabled": True,
        "committed_generation": 7,
        "dependency_state": "READY",
        "blocking_reasons": [],
        "entrypoints": ["run_now"],
        "enabled_entrypoints": ["run_now"],
        "entrypoint_kinds": {"run_now": "console"},
    }
    stale_registry = _ContributionRegistry(
        6,
        (
            SimpleNamespace(
                automation_id="service-project",
                generation=6,
                contribution_id="run_now",
                contribution_kind="console",
                phase="COMMITTED",
                backend_status="READY",
            ),
        ),
    )
    service = AutomationPluginManagementService(
        catalog=_ProjectionCatalog(
            managed,
            _committed_service_entry(
                automation_id="service-project",
                generation=7,
                enabled=True,
                declared_kinds={"run_now": "console"},
                committed_enabled_entrypoints=("run_now",),
            ),
        ),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        contribution_registry=stale_registry,
    )

    stale = service.catalog_projection(actor=_console_actor())["instances"][0]

    assert stale["contribution_projection_state"] == "STALE"
    assert stale["dependency_state"] == "BLOCKED_DEPENDENCY"
    assert stale["blocking_reasons"][-1]["code"] == "RUNTIME_PROJECTION_STALE"
    assert stale["active_contributions"][0]["generation"] == 6

    service_only = {
        **managed,
        "automation_id": "service-only",
        "entrypoints": [],
        "enabled_entrypoints": [],
        "entrypoint_kinds": {},
    }
    compatible = AutomationPluginManagementService(
        catalog=_ProjectionCatalog(
            service_only,
            _committed_service_entry(
                automation_id="service-only",
                generation=7,
                enabled=True,
                declared_kinds={},
                committed_enabled_entrypoints=(),
            ),
        ),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    ).catalog_projection(actor=_console_actor())["instances"][0]
    assert compatible["contribution_projection_state"] == "ACTIVE"
    assert compatible["active_contributions"] == []

    inactive = {
        **managed,
        "committed_generation": None,
    }
    unavailable_registry = SimpleNamespace(
        active_generation=lambda _automation_id: (_ for _ in ()).throw(
            RuntimeError("registry unavailable")
        ),
        snapshot=lambda: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
    )
    inactive_projection = AutomationPluginManagementService(
        catalog=_ProjectionCatalog(
            inactive,
            _committed_service_entry(
                automation_id="service-project",
                generation=None,
                enabled=True,
                declared_kinds={"run_now": "console"},
                committed_enabled_entrypoints=(),
            ),
        ),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        contribution_registry=unavailable_registry,
    ).catalog_projection(actor=_console_actor())["instances"][0]
    assert inactive_projection["contribution_projection_state"] == "INACTIVE"
    assert inactive_projection["active_contributions"] == []


def test_catalog_projection_uses_only_enabled_managed_contributions() -> None:
    base = {
        "automation_id": "partially-enabled",
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "enabled": True,
        "committed_generation": 4,
        "dependency_state": "READY",
        "blocking_reasons": [],
        "entrypoints": ["run_now", "daily_run"],
        "enabled_entrypoints": ["run_now"],
        "entrypoint_kinds": {
            "run_now": "console",
            "daily_run": "scheduler",
        },
    }

    def project(
        instance: dict[str, Any],
        registry: object | None,
        *,
        committed_enabled: tuple[str, ...] = ("run_now",),
        project_enabled: bool = True,
    ) -> dict[str, Any]:
        return AutomationPluginManagementService(
            catalog=_ProjectionCatalog(
                instance,
                _committed_service_entry(
                    automation_id="partially-enabled",
                    generation=4,
                    enabled=project_enabled,
                    declared_kinds={
                        "run_now": "console",
                        "daily_run": "scheduler",
                    },
                    committed_enabled_entrypoints=committed_enabled,
                ),
            ),
            lifecycle=SimpleNamespace(),
            configuration=SimpleNamespace(),
            worker_repository=SimpleNamespace(),
            target_service=SimpleNamespace(),
            package_repository=SimpleNamespace(),
            storage=SimpleNamespace(),
            contribution_registry=registry,
        ).catalog_projection(actor=_console_actor())["instances"][0]

    partial = project(
        base,
        _ContributionRegistry(
            4,
            (
                SimpleNamespace(
                    automation_id="partially-enabled",
                    generation=4,
                    contribution_id="run_now",
                    contribution_kind="console",
                    phase="COMMITTED",
                    backend_status="READY",
                ),
            ),
        ),
    )
    assert partial["contribution_projection_state"] == "ACTIVE"
    assert [item["contribution_id"] for item in partial["active_contributions"]] == [
        "run_now"
    ]

    all_disabled = project(
        {**base, "enabled_entrypoints": []},
        SimpleNamespace(
            active_generation=lambda _automation_id: (_ for _ in ()).throw(
                AssertionError("all-disabled projection must not require a marker")
            ),
            snapshot=lambda: (_ for _ in ()).throw(
                AssertionError("all-disabled projection has no active records")
            ),
        ),
        committed_enabled=(),
    )
    assert all_disabled["contribution_projection_state"] == "ACTIVE"
    assert all_disabled["active_contributions"] == []

    malformed = project(
        {**base, "entrypoint_kinds": {"run_now": "console"}},
        _ContributionRegistry(
            4,
            (
                SimpleNamespace(
                    automation_id="partially-enabled",
                    generation=4,
                    contribution_id="run_now",
                    contribution_kind="console",
                    phase="COMMITTED",
                    backend_status="READY",
                ),
            ),
        ),
    )
    assert malformed["contribution_projection_state"] == "STALE"
    assert malformed["dependency_state"] == "BLOCKED_DEPENDENCY"

    malformed_disabled = project(
        {
            **base,
            "enabled": False,
            "entrypoint_kinds": {"run_now": "console"},
        },
        _ContributionRegistry(None),
        project_enabled=False,
    )
    assert malformed_disabled["contribution_projection_state"] == "STALE"

    unavailable = project(
        base,
        SimpleNamespace(
            active_generation=lambda _automation_id: (_ for _ in ()).throw(
                RuntimeError("registry unavailable")
            ),
            snapshot=lambda: (),
        ),
    )
    assert unavailable["contribution_projection_state"] == "STALE"
    assert unavailable["active_contributions"] == []

    desired_changed = project(
        {
            **base,
            "entrypoints": ["daily_run"],
            "enabled_entrypoints": ["daily_run"],
            "entrypoint_kinds": {"daily_run": "scheduler"},
        },
        _ContributionRegistry(
            4,
            (
                SimpleNamespace(
                    automation_id="partially-enabled",
                    generation=4,
                    contribution_id="run_now",
                    contribution_kind="console",
                    phase="COMMITTED",
                    backend_status="READY",
                ),
            ),
        ),
    )
    assert desired_changed["contribution_projection_state"] == "ACTIVE"
    assert desired_changed["active_contributions"][0]["contribution_id"] == "run_now"


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


def test_service_v2_transition_readiness_requires_exact_enabled_projection_generation() -> None:
    contributions = {
        "console": (
            {
                "id": "run_now",
                "service": "plugin.example.run@1",
                "operation": "run",
            },
        ),
        "scheduler": (
            {
                "id": "daily_run",
                "service": "plugin.example.run@1",
                "operation": "run",
            },
        ),
        "webhook": (),
        "feishu": (),
        "events": (),
    }

    def ready_entry(enabled_entrypoints: tuple[str, ...]) -> SimpleNamespace:
        snapshot = SimpleNamespace(
            generation=4,
            plugin_version="1.0.0",
            project_config_sha256="1" * 64,
            account_bindings_sha256="2" * 64,
            resource_bindings_sha256="3" * 64,
            device_binding_sha256="4" * 64,
            enabled_entrypoints=enabled_entrypoints,
            execution_metadata={
                "project_config_version": 2,
                "contributions": contributions,
            },
        )
        return _entry(
            runtime_model=PluginRuntimeModel.SERVICE_V2.value,
            target_generation=4,
            committed_generation=4,
            reconcile_state=RuntimeReconcileState.STABLE,
            current_enabled_entrypoints=enabled_entrypoints,
            committed_snapshot=snapshot,
        )

    def service(registry: object | None) -> AutomationPluginManagementService:
        return AutomationPluginManagementService(
            catalog=_Catalog(),  # type: ignore[arg-type]
            lifecycle=SimpleNamespace(),
            configuration=SimpleNamespace(),
            worker_repository=SimpleNamespace(),
            target_service=SimpleNamespace(),
            package_repository=SimpleNamespace(),
            storage=SimpleNamespace(),
            contribution_registry=registry,
        )

    partial = ready_entry(("run_now",))
    stale = service(_ContributionRegistry(3))._transition_projection(partial)
    assert stale["generation_ready"] is False
    assert stale["transition_state"] != "READY"

    active = service(_ContributionRegistry(4))._transition_projection(partial)
    assert active == {"generation_ready": True, "transition_state": "READY"}

    all_disabled = service(
        SimpleNamespace(
            active_generation=lambda _automation_id: (_ for _ in ()).throw(
                AssertionError("all-disabled readiness must not require a marker")
            )
        )
    )._transition_projection(ready_entry(()))
    assert all_disabled == {"generation_ready": True, "transition_state": "READY"}

    legacy_adapter = service(None)._transition_projection(partial)
    assert legacy_adapter == {"generation_ready": True, "transition_state": "READY"}


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


def test_disable_refresh_failure_returns_committed_pending_projection_for_api_retry() -> None:
    catalog = _Catalog(
        _entry(
            runtime_model=PluginRuntimeModel.SERVICE_V2.value,
            enabled=True,
            state=PluginProjectState.ENABLED.value,
            provided_services=(),
        )
    )
    lifecycle_calls: list[str] = []
    target_calls: list[str] = []
    refresh_calls: list[str] = []
    active_version = SimpleNamespace(
        version="1.0.0",
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )

    def set_enabled(automation_id: str, **_kwargs: Any) -> SimpleNamespace:
        lifecycle_calls.append(automation_id)
        return SimpleNamespace(
            automation_id=automation_id,
            plugin_id="example_action",
            display_name="Example action",
            active_version=active_version,
            enabled=False,
            state=PluginProjectState.DISABLED,
            record_version=4,
            target_generation=2,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.DRAINING,
        )

    def reconcile(automation_id: str) -> None:
        target_calls.append(automation_id)
        raise PluginConflictError(
            "scheduler refresh failed",
            code="RUNTIME_PROJECTION_REFRESH_FAILED",
        )

    service = AutomationPluginManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(set_enabled=set_enabled),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(reconcile_project=reconcile),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    client = _api_client(
        service,  # type: ignore[arg-type]
        scheduler_refresh_provider=lambda: (
            refresh_calls.append("refresh")
            or {"initialized": True, "invalid_tasks": []}
        ),
    )

    response = client.post(
        "/internal/v1/automation/instances/automation-1/state",
        json={
            "enabled": False,
            "request_id": str(uuid.uuid4()),
            "expected_record_version": 3,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["plugin_operation_committed"] is True
    assert data["scheduler_refresh_completed"] is True
    assert data["runtime_projection_pending"] is False
    assert data["contribution_projection_state"] == "INACTIVE"
    assert data["generation_ready"] is False
    assert data["transition_state"] != "READY"
    assert lifecycle_calls == ["automation-1"]
    assert target_calls == ["automation-1"]
    assert refresh_calls == ["refresh"]


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
