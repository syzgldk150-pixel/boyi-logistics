from __future__ import annotations

import copy
import hashlib
import stat
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from Crypto.PublicKey import ECC

import agent.automation_plugins.first_party as first_party_module
import agent.automation_plugins.storage as plugin_storage
from agent.automation_plugins.errors import (
    PluginConflictError,
    PluginExecutionError,
    PluginPackageError,
    PluginUninstallBlocked,
)
from agent.automation_plugins.execution import FilesystemPluginIntegrityVerifier
from agent.automation_plugins.first_party import (
    SignedFirstPartyPackageProvider,
    bootstrap_first_party_plugins,
    resolve_first_party_manifests,
)
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.manifest import (
    AutomationPluginManifest,
    governance_anchor_from_tool_contract,
)
from agent.automation_plugins.models import (
    ExecutionBlock,
    ExecutionBlockKind,
    FirstPartyInstanceSeed,
    PluginInstanceRecord,
    PluginProjectState,
    PluginTrustSource,
    PluginUninstallStatus,
    PluginVersionRecord,
    WorkerCleanupRequest,
)
from agent.automation_plugins.package import (
    Ed25519PackageSigner,
    Ed25519TrustStore,
    build_signed_plugin_zip,
    verify_signed_plugin_zip,
)
from agent.automation_plugins.ports import (
    BootstrapPersistenceResult,
    HardUninstallPreparation,
)
from agent.automation_plugins.sdk import PLUGIN_SDK_SOURCE
from agent.automation_plugins.storage import FilesystemPluginStorage, LockedVirtualEnvironmentBuilder
from agent.tool_registry import ToolRegistry


def _uploaded_package(
    *,
    plugin_id: str = "lifecycle_test_action",
    execution_platform: str = "server",
) -> tuple[bytes, Ed25519TrustStore]:
    source = resolve_first_party_manifests(ToolRegistry())["sync_scan_codes"].to_mapping()
    contract = copy.deepcopy(source["invocation_contracts"]["console"])
    source.update(
        {
            "plugin_id": plugin_id,
            "name": "Lifecycle action",
            "description": "Lifecycle integration fixture",
            "runtime": {"kind": "python_subprocess", "entrypoint": "payload/main.py"},
            "allowed_entrypoints": ["console"],
            "invocation_contracts": {"console": contract},
            "scheduling": {"supported": False, "allowed_kinds": [], "max_daily_times": 0},
            "project_full_auto_allowed": False,
            "execution_platform": execution_platform,
        }
    )
    if execution_platform == "windows":
        source["worker_requirement"] = {
            "required": True,
            "interactive_session": True,
            "supported_os": ["windows"],
            "queue_deadline_seconds": 3600,
        }
    source["account_roles"] = [
        {**role, "argument_field": None} for role in source["account_roles"]
    ]
    role = source["account_roles"][0]["role"]
    tool = copy.deepcopy(source["tool_contract"])
    tool.update(
        {
            "name": plugin_id,
            "executor": "payload/main.py",
            "input_schema": copy.deepcopy(contract["input_schema"]),
            "project_full_auto_allowed": False,
        }
    )
    source["tool_contract"] = tool
    source["governance_anchor"] = governance_anchor_from_tool_contract(tool)
    source["runtime_permissions"] = {
        "network": False,
        "browser": True,
        "office": False,
        "file_roles": [],
        "broker_operations": [
            {
                "operation": "browser.invoke",
                "action": "scan.fetch",
                "roles": [role],
                "effect": "read",
            }
        ],
        "max_broker_calls": 2,
    }
    manifest = AutomationPluginManifest.from_mapping(source)
    private_key = ECC.generate(curve="Ed25519")
    package = build_signed_plugin_zip(
        manifest,
        {
            "payload/main.py": b"import json\nprint(json.dumps({'ok': True}))\n",
            "payload/boyi_plugin_sdk.py": PLUGIN_SDK_SOURCE.encode("utf-8"),
        },
        signer=Ed25519PackageSigner(key_id="lifecycle", private_key=private_key),
    )
    trust = Ed25519TrustStore(
        {"lifecycle": private_key.public_key().export_key(format="raw")}
    )
    return package, trust


class _MemoryPluginRepository:
    def __init__(self) -> None:
        self.versions: dict[tuple[str, str], PluginVersionRecord] = {}
        self.instances: dict[str, PluginInstanceRecord] = {}
        self.requests: dict[str, tuple[str, str, str]] = {}
        self.fail_install = False
        self.blocks: list[ExecutionBlock] = []
        self.preparations: dict[tuple[str, str], HardUninstallPreparation] = {}
        self.acked: set[str] = set()
        self.call_log: list[str] = []

    def get_package_version(self, plugin_id: str, version: str):
        return self.versions.get((plugin_id, version))

    def get_instance(self, automation_id: str):
        return self.instances.get(automation_id)

    def list_instances(self):
        return list(self.instances.values())

    def install_instance(
        self,
        version: PluginVersionRecord,
        *,
        instance_name: str,
        actor_id: str,
        actor_role: str,
        request_id: str,
    ) -> PluginInstanceRecord:
        assert actor_id and actor_role == "super_admin"
        identity = (version.package_sha256, instance_name)
        existing = self.requests.get(request_id)
        if existing is not None:
            if existing[:2] != identity:
                raise PluginConflictError("IDEMPOTENCY_CONFLICT")
            return self.instances[existing[2]]
        if self.fail_install:
            raise RuntimeError("fault injection before atomic commit")
        automation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{request_id}:{instance_name}"))
        instance = PluginInstanceRecord(
            automation_id=automation_id,
            display_name=instance_name,
            plugin_id=version.plugin_id,
            state=PluginProjectState.INSTALLED,
            active_version=version,
        )
        self.versions[(version.plugin_id, version.version)] = version
        self.instances[automation_id] = instance
        self.requests[request_id] = (version.package_sha256, instance_name, automation_id)
        return instance

    def upgrade_instance(self, *args, **kwargs):
        raise NotImplementedError

    def bootstrap_missing(self, *args, **kwargs):
        raise NotImplementedError

    def set_enabled(self, *args, **kwargs):
        raise NotImplementedError

    def list_execution_blocks(self, automation_id: str):
        return tuple(self.blocks)

    def prepare_hard_uninstall(
        self,
        automation_id: str,
        *,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_current_version: str,
        expected_record_version: int,
    ) -> HardUninstallPreparation:
        assert actor_id and actor_role == "super_admin"
        if self.blocks:
            raise PluginUninstallBlocked("protected state")
        instance = self.instances[automation_id]
        assert instance.active_version.version == expected_current_version
        assert instance.record_version == expected_record_version
        purge_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"purge:{request_id}"))
        request = WorkerCleanupRequest(
            command_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"cleanup:{request_id}")),
            automation_id=automation_id,
            version=expected_current_version,
            device_id="worker-1",
            requested_at=datetime.now(timezone.utc),
            package_sha256=instance.active_version.package_sha256,
        )
        preparation = HardUninstallPreparation(
            purge_id=purge_id,
            instance=replace(instance, state=PluginProjectState.UNINSTALLING),
            cleanup_requests=(request,),
            prepared_at=datetime.now(timezone.utc),
            delete_shared_package=True,
        )
        self.instances[automation_id] = preparation.instance
        self.preparations[(automation_id, purge_id)] = preparation
        self.call_log.append("prepare")
        return preparation

    def persist_cleanup_requests(self, preparation: HardUninstallPreparation) -> None:
        assert preparation.cleanup_requests
        self.call_log.append("persist_cleanup")

    def get_hard_uninstall_preparation(self, *, automation_id: str, purge_id: str):
        return self.preparations.get((automation_id, purge_id))

    def all_cleanup_acknowledged(self, preparation: HardUninstallPreparation) -> bool:
        return all(item.command_id in self.acked for item in preparation.cleanup_requests)

    def reserve_hard_uninstall_finalize(self, preparation: HardUninstallPreparation):
        if not self.all_cleanup_acknowledged(preparation):
            raise PluginConflictError("cleanup not acknowledged")
        if self.blocks:
            raise PluginUninstallBlocked("protected state")
        self.call_log.append("reserve")
        return preparation

    def hard_delete_application_state(self, preparation: HardUninstallPreparation) -> None:
        self.call_log.append("db_delete")
        self.instances.pop(preparation.instance.automation_id, None)

    def complete_hard_uninstall(self, preparation: HardUninstallPreparation) -> None:
        self.call_log.append("complete")
        self.versions.pop(
            (preparation.instance.plugin_id, preparation.instance.active_version.version),
            None,
        )
        self.preparations.pop((preparation.instance.automation_id, preparation.purge_id), None)

    def mark_purge_failed(
        self,
        preparation: HardUninstallPreparation,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        assert error_code and error_summary
        self.call_log.append("failed")


class _LoggingStorage(FilesystemPluginStorage):
    def __init__(self, root: Path, call_log: list[str]) -> None:
        super().__init__(root)
        self._call_log = call_log
        self.fail_remove = False

    def remove_version_root(self, install_root: Path) -> None:
        self._call_log.append("fs_delete")
        if self.fail_remove:
            raise OSError("fault injection during filesystem purge")
        super().remove_version_root(install_root)


def _service(tmp_path: Path):
    package, trust = _uploaded_package()
    repository = _MemoryPluginRepository()
    storage = _LoggingStorage(tmp_path / "plugins", repository.call_log)
    service = AutomationPluginService(
        repository=repository,
        storage=storage,
        environments=LockedVirtualEnvironmentBuilder(),
        upload_signature_verifier=trust,
    )
    return package, repository, storage, service


@pytest.mark.parametrize(
    ("package_options", "service_options", "error_code"),
    (
        (
            {"execution_platform": "windows"},
            {"allowed_execution_platforms": ("server",)},
            "PLUGIN_EXECUTION_PLATFORM_DISABLED",
        ),
        (
            {"plugin_id": "r7_arrival_checkin"},
            {"blocked_plugin_ids": ("r7_arrival_checkin",)},
            "PLUGIN_ACTION_DEFERRED",
        ),
    ),
)
def test_release_scope_rejects_windows_and_deferred_uploads_before_materialization(
    tmp_path: Path,
    package_options: dict[str, str],
    service_options: dict[str, tuple[str, ...]],
    error_code: str,
) -> None:
    package, trust = _uploaded_package(**package_options)
    repository = _MemoryPluginRepository()
    storage = _LoggingStorage(tmp_path / "plugins", repository.call_log)
    service = AutomationPluginService(
        repository=repository,
        storage=storage,
        environments=LockedVirtualEnvironmentBuilder(),
        upload_signature_verifier=trust,
        **service_options,
    )

    with pytest.raises(PluginPackageError) as raised:
        service.install_upload(
            package,
            instance_name="deferred instance",
            actor_id="admin-1",
            actor_role="super_admin",
            request_id=str(uuid.uuid4()),
            transport_package_sha256=hashlib.sha256(package).hexdigest(),
        )

    assert raised.value.code == error_code
    assert repository.instances == {}
    assert repository.versions == {}
    assert [path.name for path in (tmp_path / "plugins").iterdir()] == [".staging"]
    assert list((tmp_path / "plugins" / ".staging").iterdir()) == []


def test_upload_install_is_idempotent_and_shares_immutable_version(tmp_path: Path) -> None:
    package, repository, storage, service = _service(tmp_path)
    request_id = str(uuid.uuid4())
    first = service.install_upload(
        package,
        instance_name="first instance",
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=request_id,
        transport_package_sha256=hashlib.sha256(package).hexdigest(),
    )
    retried = service.install_upload(
        package,
        instance_name="first instance",
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=request_id,
    )
    assert retried.automation_id == first.automation_id
    second = service.install_upload(
        package,
        instance_name="second instance",
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=str(uuid.uuid4()),
    )
    assert second.automation_id != first.automation_id
    assert second.active_version.install_root == first.active_version.install_root
    assert len(repository.versions) == 1
    archive_relative = first.active_version.install_metadata["archive_relative"]
    archive_path = Path(first.active_version.install_root or "") / str(archive_relative)
    assert archive_relative == "package-archive.zip"
    assert first.active_version.install_metadata["archive_sha256"] == hashlib.sha256(
        package
    ).hexdigest()
    assert archive_path.stat().st_mode & 0o777 == 0o600
    assert storage.read_verified_archive(
        Path(first.active_version.install_root or ""),
        str(archive_relative),
        expected_sha256=hashlib.sha256(package).hexdigest(),
    ) == package
    with pytest.raises(PluginConflictError, match="IDEMPOTENCY_CONFLICT"):
        service.install_upload(
            package,
            instance_name="different name",
            actor_id="admin-1",
            actor_role="super_admin",
            request_id=request_id,
        )


def test_failed_install_removes_unreferenced_materialization(tmp_path: Path) -> None:
    package, repository, storage, service = _service(tmp_path)
    repository.fail_install = True
    with pytest.raises(RuntimeError, match="fault injection"):
        service.install_upload(
            package,
            instance_name="broken",
            actor_id="admin-1",
            actor_role="super_admin",
            request_id=str(uuid.uuid4()),
        )
    assert repository.versions == {}
    assert not any(path.name.startswith("1.0.0-") for path in storage.root.rglob("*"))


def test_signed_first_party_materialization_copies_exact_archive_out_of_release(
    tmp_path: Path,
) -> None:
    package, trust = _uploaded_package()
    verified = verify_signed_plugin_zip(package, verifier=trust)
    storage = FilesystemPluginStorage(tmp_path / "installed")
    provider = SignedFirstPartyPackageProvider(
        artifact_root=tmp_path / "release-artifacts",
        signature_verifier=trust,
        storage=storage,
        environments=LockedVirtualEnvironmentBuilder(),
    )
    provider._verified[(verified.manifest.plugin_id, verified.manifest.version)] = (  # noqa: SLF001
        verified
    )
    materialized = provider.materialize(
        PluginVersionRecord(
            plugin_id=verified.manifest.plugin_id,
            version=verified.manifest.version,
            package_sha256=verified.package_sha256,
            manifest_sha256=verified.manifest_sha256,
            manifest=verified.manifest.to_mapping(),
            trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
            install_root=None,
        )
    )
    archive_relative = str(materialized.install_metadata["archive_relative"])
    assert materialized.install_root is not None
    assert storage.read_verified_archive(
        Path(materialized.install_root),
        archive_relative,
        expected_sha256=verified.package_sha256,
    ) == package


def _signed_recovery_fixture(tmp_path: Path):
    package, trust = _uploaded_package()
    verified = verify_signed_plugin_zip(package, verifier=trust)
    storage = FilesystemPluginStorage(tmp_path / "installed")
    provider = SignedFirstPartyPackageProvider(
        artifact_root=tmp_path / "release-artifacts",
        signature_verifier=trust,
        storage=storage,
        environments=LockedVirtualEnvironmentBuilder(),
    )
    provider._verified[(verified.manifest.plugin_id, verified.manifest.version)] = (  # noqa: SLF001
        verified
    )
    descriptor = PluginVersionRecord(
        plugin_id=verified.manifest.plugin_id,
        version=verified.manifest.version,
        package_sha256=verified.package_sha256,
        manifest_sha256=verified.manifest_sha256,
        manifest=verified.manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root=None,
        install_metadata={"signing_key_id": verified.signing_key_id},
    )
    materialized = provider.materialize(descriptor)
    persisted = replace(
        materialized,
        install_metadata={
            **dict(materialized.install_metadata),
            "install_root": materialized.install_root,
        },
    )
    return package, verified, storage, provider, descriptor, persisted


def test_signed_first_party_missing_root_recovery_is_exact_and_idempotent(
    tmp_path: Path,
) -> None:
    package, verified, storage, provider, descriptor, persisted = (
        _signed_recovery_fixture(tmp_path)
    )
    persisted_before = copy.deepcopy(persisted)
    expected_root = Path(str(persisted.install_root))
    storage.remove_version_root(expected_root)
    assert not expected_root.exists()

    rebuilt = provider.recover_missing(
        persisted=persisted,
        descriptor=descriptor,
    )

    assert rebuilt is not None
    assert persisted == persisted_before
    assert rebuilt.install_root == persisted.install_root
    assert {
        **dict(rebuilt.install_metadata),
        "install_root": rebuilt.install_root,
    } == persisted.install_metadata
    assert storage.read_verified_archive(
        expected_root,
        str(persisted.install_metadata["archive_relative"]),
        expected_sha256=verified.package_sha256,
    ) == package
    assert provider.recover_missing(
        persisted=persisted,
        descriptor=descriptor,
    ) is None


def test_signed_first_party_recovery_rejects_wrong_or_unsafe_roots(
    tmp_path: Path,
) -> None:
    _, _, storage, provider, descriptor, persisted = _signed_recovery_fixture(
        tmp_path
    )
    expected_root = Path(str(persisted.install_root))
    storage.remove_version_root(expected_root)
    wrong_root = tmp_path / "wrong" / "missing"
    wrong = replace(
        persisted,
        install_root=str(wrong_root),
        install_metadata={
            **dict(persisted.install_metadata),
            "install_root": str(wrong_root),
        },
    )
    with pytest.raises(PluginPackageError, match="deterministic target"):
        provider.recover_missing(persisted=wrong, descriptor=descriptor)
    assert not expected_root.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    expected_root.parent.mkdir(parents=True, exist_ok=True)
    expected_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PluginPackageError, match="symbolic links|unsafe"):
        provider.recover_missing(persisted=persisted, descriptor=descriptor)
    assert expected_root.is_symlink()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("corrupt_target", ("package", "archive"))
def test_signed_first_party_recovery_never_overwrites_corrupt_existing_tree(
    tmp_path: Path,
    corrupt_target: str,
) -> None:
    _, verified, _, provider, descriptor, persisted = _signed_recovery_fixture(
        tmp_path
    )
    root = Path(str(persisted.install_root))
    target = (
        root / "package" / verified.files[0].path
        if corrupt_target == "package"
        else root / str(persisted.install_metadata["archive_relative"])
    )
    target.chmod(0o600)
    target.write_bytes(b"corrupt-existing-tree")

    with pytest.raises(PluginPackageError, match="integrity|archive"):
        provider.recover_missing(persisted=persisted, descriptor=descriptor)

    assert target.read_bytes() == b"corrupt-existing-tree"


def test_signed_first_party_recovery_rejects_extra_package_module(
    tmp_path: Path,
) -> None:
    _, _, _, provider, descriptor, persisted = _signed_recovery_fixture(tmp_path)
    root = Path(str(persisted.install_root))
    extra = root / "package" / "payload" / "json.py"
    original_mode = extra.parent.stat().st_mode & 0o777
    extra.parent.chmod(original_mode | stat.S_IWUSR)
    extra.write_text("raise RuntimeError('shadow')\n", encoding="utf-8")
    extra.parent.chmod(original_mode)

    with pytest.raises(PluginPackageError, match="differs from signed files"):
        provider.recover_missing(persisted=persisted, descriptor=descriptor)

    assert extra.is_file()


def test_signed_first_party_recovery_concurrent_commit_only_revalidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, storage, provider, descriptor, persisted = _signed_recovery_fixture(
        tmp_path
    )
    root = Path(str(persisted.install_root))
    storage.remove_version_root(root)
    original_materialize = provider.materialize

    def raced_materialize(version: PluginVersionRecord) -> PluginVersionRecord:
        original_materialize(version)
        raise PluginConflictError("simulated concurrent immutable commit")

    monkeypatch.setattr(provider, "materialize", raced_materialize)
    assert provider.recover_missing(
        persisted=persisted,
        descriptor=descriptor,
    ) is None
    assert root.is_dir()


def test_signed_first_party_recovery_rejects_corrupt_concurrent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, storage, provider, descriptor, persisted = _signed_recovery_fixture(
        tmp_path
    )
    root = Path(str(persisted.install_root))
    storage.remove_version_root(root)

    def raced_materialize(_version: PluginVersionRecord) -> PluginVersionRecord:
        root.mkdir(parents=True)
        marker = root / "unverified-race"
        marker.write_bytes(b"must-not-be-adopted-or-overwritten")
        raise PluginConflictError("simulated corrupt concurrent commit")

    monkeypatch.setattr(provider, "materialize", raced_materialize)
    with pytest.raises(PluginPackageError, match="integrity|without valid bytes"):
        provider.recover_missing(
            persisted=persisted,
            descriptor=descriptor,
        )
    assert (root / "unverified-race").read_bytes() == (
        b"must-not-be-adopted-or-overwritten"
    )


def test_signed_first_party_recovery_discards_rebuilt_metadata_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, storage, provider, descriptor, persisted = _signed_recovery_fixture(
        tmp_path
    )
    root = Path(str(persisted.install_root))
    storage.remove_version_root(root)
    original_materialize = provider.materialize

    def drifted_materialize(version: PluginVersionRecord) -> PluginVersionRecord:
        rebuilt = original_materialize(version)
        return replace(
            rebuilt,
            install_metadata={
                **dict(rebuilt.install_metadata),
                "python_relative": "venv/bin/not-the-persisted-python",
            },
        )

    monkeypatch.setattr(provider, "materialize", drifted_materialize)
    with pytest.raises(PluginPackageError, match="did not verify"):
        provider.recover_missing(persisted=persisted, descriptor=descriptor)
    assert not root.exists()


def test_bootstrap_uses_original_database_record_after_root_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_scan_codes"]
    descriptor = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="a" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root=None,
    )
    persisted = replace(
        descriptor,
        install_root="/immutable/sync_scan_codes/1.0.0-exact",
        install_metadata={
            "install_root": "/immutable/sync_scan_codes/1.0.0-exact"
        },
    )
    rebuilt = replace(persisted)
    seed = FirstPartyInstanceSeed(
        automation_id="scan_codes",
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        display_name="scan",
        allowed_entrypoints=("console",),
    )

    class RecoveryProvider:
        def load_versions(self, **_kwargs):
            return (descriptor,)

        def materialize(self, _version):
            raise AssertionError("existing package must use recovery")

        def discard(self, _version):
            raise AssertionError("persisted recovery root must not be discarded")

        def recover_missing(self, *, persisted: PluginVersionRecord, descriptor):
            assert persisted is database_record
            assert descriptor is not persisted
            return rebuilt

    class Repository:
        def get_package_version(self, _plugin_id, _version):
            return database_record

        def bootstrap_missing(self, versions, instances, *, release_sha):
            assert versions == (database_record,)
            assert instances == (seed,)
            assert release_sha == "b" * 40
            return BootstrapPersistenceResult(created=(), existing=("scan_codes",))

    database_record = persisted
    monkeypatch.setattr(
        first_party_module,
        "release_first_party_plugin_ids",
        lambda: frozenset({manifest.plugin_id}),
    )
    monkeypatch.setattr(
        first_party_module,
        "release_first_party_instance_seeds",
        lambda: (seed,),
    )
    monkeypatch.setattr(
        first_party_module,
        "release_first_party_automation_ids",
        lambda: frozenset({"scan_codes"}),
    )

    result = bootstrap_first_party_plugins(
        Repository(),
        core_catalog=ToolRegistry(),
        current_release_sha="b" * 40,
        expected_release_sha="b" * 40,
        package_provider=RecoveryProvider(),
        package_materializer=RecoveryProvider(),
    )

    assert result.ok
    assert result.existing == ("scan_codes",)
    assert database_record is persisted


def test_hard_uninstall_waits_for_exact_cleanup_ack_and_deletes_db_before_fs(
    tmp_path: Path,
) -> None:
    package, repository, _, service = _service(tmp_path)
    instance = service.install_upload(
        package,
        instance_name="to remove",
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=str(uuid.uuid4()),
    )
    result = service.hard_uninstall(
        instance.automation_id,
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=str(uuid.uuid4()),
        expected_current_version=instance.active_version.version,
        expected_record_version=instance.record_version,
    )
    assert result.status == PluginUninstallStatus.PENDING
    assert repository.get_instance(instance.automation_id).state == PluginProjectState.UNINSTALLING
    assert Path(instance.active_version.install_root or "").is_dir()
    pending = service.finalize_hard_uninstall(
        instance.automation_id,
        purge_id=result.purge_id,
    )
    assert pending.status == PluginUninstallStatus.PENDING
    repository.acked.update(result.pending_cleanup_commands)
    complete = service.finalize_hard_uninstall(
        instance.automation_id,
        purge_id=result.purge_id,
    )
    assert complete.status == PluginUninstallStatus.COMPLETED
    assert repository.call_log.index("db_delete") < repository.call_log.index("fs_delete")
    assert repository.call_log.index("fs_delete") < repository.call_log.index("complete")
    assert repository.get_instance(instance.automation_id) is None
    assert not Path(instance.active_version.install_root or "").exists()


def test_unknown_write_blocks_uninstall_without_revocation(tmp_path: Path) -> None:
    package, repository, _, service = _service(tmp_path)
    instance = service.install_upload(
        package,
        instance_name="protected",
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=str(uuid.uuid4()),
    )
    repository.blocks = [
        ExecutionBlock(
            kind=ExecutionBlockKind.WRITE_OUTCOME_UNKNOWN,
            run_id="run-1",
        )
    ]
    with pytest.raises(PluginUninstallBlocked):
        service.hard_uninstall(
            instance.automation_id,
            actor_id="admin-1",
            actor_role="super_admin",
            request_id=str(uuid.uuid4()),
            expected_current_version=instance.active_version.version,
            expected_record_version=instance.record_version,
        )
    assert repository.get_instance(instance.automation_id).state == PluginProjectState.INSTALLED
    assert repository.preparations == {}


def test_filesystem_purge_failure_keeps_journal_and_is_retryable(tmp_path: Path) -> None:
    package, repository, storage, service = _service(tmp_path)
    instance = service.install_upload(
        package,
        instance_name="retry purge",
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=str(uuid.uuid4()),
    )
    result = service.hard_uninstall(
        instance.automation_id,
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=str(uuid.uuid4()),
        expected_current_version=instance.active_version.version,
        expected_record_version=instance.record_version,
    )
    repository.acked.update(result.pending_cleanup_commands)
    storage.fail_remove = True
    with pytest.raises(OSError, match="fault injection"):
        service.finalize_hard_uninstall(instance.automation_id, purge_id=result.purge_id)
    assert (instance.automation_id, result.purge_id) in repository.preparations
    assert "failed" in repository.call_log
    storage.fail_remove = False
    completed = service.finalize_hard_uninstall(
        instance.automation_id,
        purge_id=result.purge_id,
    )
    assert completed.status == PluginUninstallStatus.COMPLETED


def test_storage_refuses_symlink_or_path_escape_during_materialization(tmp_path: Path) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    stage = storage.create_staging_root("safe_plugin", "1.0.0")
    (stage / "payload").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (stage / "payload" / "escape").symlink_to(outside)
    with pytest.raises(PluginPackageError, match="symbolic links"):
        storage.commit_staging_root(
            stage,
            plugin_id="safe_plugin",
            version="1.0.0",
            manifest_sha256="1" * 64,
        )
    assert outside.read_text(encoding="utf-8") == "keep"
    with pytest.raises(PluginPackageError, match="outside"):
        storage.remove_version_root(outside)


def test_storage_commit_and_recovery_share_one_expected_root_identity(
    tmp_path: Path,
) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    identity = {
        "plugin_id": "expected_root_plugin",
        "version": "1.2.3",
        "manifest_sha256": "a" * 64,
    }
    expected, exists = storage.inspect_expected_version_root(**identity)
    assert not exists
    stage = storage.create_staging_root(
        identity["plugin_id"],
        identity["version"],
    )
    (stage / "payload.py").write_text("pass\n", encoding="utf-8")
    assert storage.commit_staging_root(stage, **identity) == expected

    second_stage = storage.create_staging_root(
        identity["plugin_id"],
        identity["version"],
    )
    (second_stage / "payload.py").write_text("different\n", encoding="utf-8")
    with pytest.raises(PluginConflictError, match="already exists"):
        storage.commit_staging_root(second_stage, **identity)
    assert (expected / "payload.py").read_text(encoding="utf-8") == "pass\n"

    storage.remove_version_root(expected)
    outside = tmp_path / "outside-root"
    outside.mkdir()
    expected.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PluginPackageError, match="symbolic links"):
        storage.inspect_expected_version_root(**identity)
    assert expected.is_symlink()
    assert list(outside.iterdir()) == []


def test_storage_atomic_publish_never_replaces_racing_empty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    identity = {
        "plugin_id": "atomic_publish_plugin",
        "version": "1.2.3",
        "manifest_sha256": "b" * 64,
    }
    stage = storage.create_staging_root(
        identity["plugin_id"],
        identity["version"],
    )
    (stage / "payload.py").write_text("candidate\n", encoding="utf-8")
    original_inspect = storage.inspect_expected_version_root
    inspect_count = 0
    racing_inode: int | None = None
    racing_marker: Path | None = None

    def inspect_with_race(**kwargs):
        nonlocal inspect_count, racing_inode, racing_marker
        target, exists = original_inspect(**kwargs)
        inspect_count += 1
        if inspect_count == 2:
            assert not exists
            target.mkdir()
            racing_inode = target.lstat().st_ino
            racing_marker = target.parent / "concurrent-owner.marker"
            racing_marker.write_text(str(racing_inode), encoding="ascii")
        return target, exists

    monkeypatch.setattr(
        storage,
        "inspect_expected_version_root",
        inspect_with_race,
    )
    with pytest.raises(PluginConflictError, match="appeared during commit"):
        storage.commit_staging_root(stage, **identity)

    expected, exists = original_inspect(**identity)
    assert exists
    assert racing_inode is not None
    assert expected.lstat().st_ino == racing_inode
    assert list(expected.iterdir()) == []
    assert racing_marker is not None
    assert racing_marker.read_text(encoding="ascii") == str(racing_inode)
    assert (stage / "payload.py").read_text(encoding="utf-8") == "candidate\n"


def test_storage_atomic_publish_never_follows_racing_project_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    identity = {
        "plugin_id": "parent_race_plugin",
        "version": "1.2.3",
        "manifest_sha256": "c" * 64,
    }
    stage = storage.create_staging_root(
        identity["plugin_id"],
        identity["version"],
    )
    (stage / "payload.py").write_text("candidate\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_inspect = storage.inspect_expected_version_root
    inspect_count = 0
    displaced_project: Path | None = None

    def inspect_with_parent_race(**kwargs):
        nonlocal inspect_count, displaced_project
        target, exists = original_inspect(**kwargs)
        inspect_count += 1
        if inspect_count == 2:
            assert not exists
            displaced_project = target.parent.with_name(
                f"{target.parent.name}.displaced"
            )
            target.parent.rename(displaced_project)
            target.parent.symlink_to(outside, target_is_directory=True)
        return target, exists

    monkeypatch.setattr(
        storage,
        "inspect_expected_version_root",
        inspect_with_parent_race,
    )
    with pytest.raises(PluginPackageError, match="unsafe|opened safely"):
        storage.commit_staging_root(stage, **identity)

    expected_project = storage.root / identity["plugin_id"]
    assert expected_project.is_symlink()
    assert list(outside.iterdir()) == []
    assert displaced_project is not None and displaced_project.is_dir()
    assert list(displaced_project.iterdir()) == []
    assert (stage / "payload.py").read_text(encoding="utf-8") == "candidate\n"


def test_verified_archive_reader_rejects_tamper_hardlink_and_symlink(
    tmp_path: Path,
) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    package = b"PK\x03\x04immutable-signed-package"
    digest = hashlib.sha256(package).hexdigest()

    def committed(version: str, marker: str) -> Path:
        stage = storage.create_staging_root("archive_plugin", version)
        assert storage.persist_verified_archive(
            stage,
            package,
            expected_sha256=digest,
        ) == "package-archive.zip"
        return storage.commit_staging_root(
            stage,
            plugin_id="archive_plugin",
            version=version,
            manifest_sha256=marker * 64,
        )

    exact_root = committed("1.0.0", "a")
    assert storage.read_verified_archive(
        exact_root,
        "package-archive.zip",
        expected_sha256=digest,
    ) == package
    (exact_root / "package-archive.zip").write_bytes(package + b"tampered")
    with pytest.raises(PluginPackageError, match="digest verification"):
        storage.read_verified_archive(
            exact_root,
            "package-archive.zip",
            expected_sha256=digest,
        )

    hardlink_root = committed("1.0.1", "b")
    (tmp_path / "archive-hardlink.zip").hardlink_to(
        hardlink_root / "package-archive.zip"
    )
    with pytest.raises(PluginPackageError, match="hard-linked"):
        storage.read_verified_archive(
            hardlink_root,
            "package-archive.zip",
            expected_sha256=digest,
        )

    symlink_root = committed("1.0.2", "c")
    archive_path = symlink_root / "package-archive.zip"
    archive_path.unlink()
    archive_path.symlink_to(tmp_path / "archive-hardlink.zip")
    with pytest.raises(PluginPackageError, match="symbolic links"):
        storage.read_verified_archive(
            symlink_root,
            "package-archive.zip",
            expected_sha256=digest,
        )


def test_storage_refuses_hardlinks_before_materialize_or_remove(tmp_path: Path) -> None:
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    outside = tmp_path / "outside.txt"
    outside.write_text("immutable", encoding="utf-8")
    original_mode = outside.stat().st_mode

    unsafe_stage = storage.create_staging_root("hardlink_plugin", "1.0.0")
    (unsafe_stage / "payload").mkdir()
    (unsafe_stage / "payload" / "linked.txt").hardlink_to(outside)
    with pytest.raises(PluginPackageError, match="hard-linked"):
        storage.commit_staging_root(
            unsafe_stage,
            plugin_id="hardlink_plugin",
            version="1.0.0",
            manifest_sha256="2" * 64,
        )

    safe_stage = storage.create_staging_root("hardlink_plugin", "1.0.1")
    (safe_stage / "payload").mkdir()
    installed_file = safe_stage / "payload" / "linked.txt"
    installed_file.write_text("private", encoding="utf-8")
    root = storage.commit_staging_root(
        safe_stage,
        plugin_id="hardlink_plugin",
        version="1.0.1",
        manifest_sha256="3" * 64,
    )
    installed_file = root / "payload" / "linked.txt"
    installed_file.unlink()
    installed_file.hardlink_to(outside)
    with pytest.raises(PluginPackageError, match="hard-linked"):
        storage.remove_version_root(root)
    assert outside.read_text(encoding="utf-8") == "immutable"
    assert outside.stat().st_mode == original_mode


def test_storage_refuses_windows_reparse_points(monkeypatch, tmp_path: Path) -> None:
    assert plugin_storage._is_reparse_point(  # noqa: SLF001 - explicit Windows flag vector
        Path("unused"),
        stat_result=type("WindowsStat", (), {"st_file_attributes": 0x0400})(),
    )
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    stage = storage.create_staging_root("reparse_plugin", "1.0.0")
    (stage / "payload").mkdir()
    simulated_junction = stage / "payload" / "junction"
    simulated_junction.mkdir()
    original = plugin_storage._is_reparse_point  # noqa: SLF001

    def is_reparse(path: Path, *, stat_result=None) -> bool:
        return Path(path) == simulated_junction or original(path, stat_result=stat_result)

    monkeypatch.setattr(plugin_storage, "_is_reparse_point", is_reparse)
    with pytest.raises(PluginPackageError, match="reparse points or junctions"):
        storage.commit_staging_root(
            stage,
            plugin_id="reparse_plugin",
            version="1.0.0",
            manifest_sha256="4" * 64,
        )


def test_integrity_verifier_rejects_identical_hardlinked_bytes(tmp_path: Path) -> None:
    root = tmp_path / "version"
    payload = root / "package" / "payload"
    payload.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"print('same bytes')\n")
    signed_file = payload / "main.py"
    signed_file.hardlink_to(outside)
    metadata = {
        "install_root": str(root),
        "install_metadata": {
            "package_files": [
                {
                    "path": "payload/main.py",
                    "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                    "size": outside.stat().st_size,
                }
            ]
        },
    }
    with pytest.raises(PluginExecutionError, match="unsafe filesystem entry"):
        FilesystemPluginIntegrityVerifier().verify_install_root(metadata)
