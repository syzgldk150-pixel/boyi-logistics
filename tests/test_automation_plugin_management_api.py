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
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.management_api import (
    create_automation_plugin_management_router,
)
from agent.automation_plugins.management_repository import (
    MySQLAutomationPluginManagementRepository,
)
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginProjectState,
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
        return {"plugins": []}

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
