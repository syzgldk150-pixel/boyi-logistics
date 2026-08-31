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


def test_service_v2_inspect_projection_preserves_signed_action_call_limits() -> None:
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="example_service",
            name="Example service",
            version="2.0.0",
            host_api={"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
            capabilities=(
                {
                    "name": "service.invoke",
                    "operations": ("preview", "execute"),
                    "account_role": None,
                    "resource_role": None,
                    "action_call_limits": {"preview": 1, "execute": 250},
                },
            ),
            account_roles=(),
            resource_roles=(),
            config_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
            contributes={
                "console": (),
                "scheduler": (),
                "webhook": (),
                "feishu": (),
                "events": (),
            },
        )
    )

    projection = AutomationPluginManagementService._service_v2_wizard_projection(
        verified
    )

    assert projection["permissions"] == [
        {
            "name": "service.invoke",
            "operations": ["preview", "execute"],
            "account_role": None,
            "resource_role": None,
            "action_call_limits": {"preview": 1, "execute": 250},
        }
    ]


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
