from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.errors import PluginConflictError, PluginPackageError
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.management_repository import (
    MySQLAutomationPluginManagementRepository,
)
from agent.automation_plugins.models import PluginTrustSource, PluginVersionRecord
from agent.automation_plugins.storage import FilesystemPluginStorage
from agent.orchestration.models import Actor, ActorType


def _console_actor() -> Actor:
    return Actor(
        actor_type=ActorType.CONSOLE_ADMIN,
        actor_id="console-admin-1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )


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
        catalog=SimpleNamespace(),
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
        catalog=SimpleNamespace(),
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
