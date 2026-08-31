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


__all__ = [
    "_console_actor",
    "_entry",
    "_Catalog",
    "_ContributionRegistry",
    "_ProjectionCatalog",
    "_committed_service_entry",
    "_projection_service",
    "_ApiService",
    "_api_client",
    "_configuration_request_payload",
    "_service_v2_install_intent",
]


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


def _projection_service(
    catalog: object, contribution_registry: object,
) -> AutomationPluginManagementService:
    return AutomationPluginManagementService(
        catalog=catalog,
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        worker_repository=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        contribution_registry=contribution_registry,
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
