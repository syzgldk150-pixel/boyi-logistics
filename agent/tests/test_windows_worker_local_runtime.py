from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import first_party_payload_files, resolve_first_party_manifests
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.package import (
    Ed25519PackageSigner,
    Ed25519TrustStore,
    build_signed_plugin_zip,
    verify_signed_plugin_zip,
)
from agent.automation_plugins.storage import FilesystemPluginStorage, LockedVirtualEnvironmentBuilder
from agent.tool_registry import ToolRegistry
from agent.windows_worker.local_runtime import WindowsLocalPluginRuntime
from agent.windows_worker.models import WorkerJob, WorkerJobStatus, WorkerJobType
from agent.windows_worker.state import WindowsWorkerStateStore


DISPATCH_AUTHORIZATION_ID = "f6d9dc71-b197-4800-bad3-4efe484406df"


class _Fetcher:
    def __init__(self, package: bytes, *, plugin_version: str) -> None:
        self._package = package
        self._plugin_version = plugin_version
        self.calls = 0

    def fetch_package(self, url: str, *, expected_sha256: str) -> bytes:
        assert url == (
            f"/internal/v1/automation/worker/packages/sync_arrive_list/{self._plugin_version}/"
            f"{expected_sha256}/{DISPATCH_AUTHORIZATION_ID}"
        )
        assert hashlib.sha256(self._package).hexdigest() == expected_sha256
        self.calls += 1
        return self._package


def _windows_package() -> tuple[AutomationPluginManifest, bytes, Ed25519TrustStore]:
    original = resolve_first_party_manifests(ToolRegistry())["sync_arrive_list"]
    mapping = copy.deepcopy(original.to_mapping())
    mapping["execution_platform"] = "windows"
    mapping["worker_requirement"] = {
        "required": True,
        "interactive_session": False,
        "supported_os": ["windows"],
        "queue_deadline_seconds": 300,
    }
    manifest = AutomationPluginManifest.from_mapping(mapping)
    private_key = ECC.generate(curve="Ed25519")
    signer = Ed25519PackageSigner(key_id="worker-test", private_key=private_key)
    trust = Ed25519TrustStore(
        {"worker-test": private_key.public_key().export_key(format="raw")}
    )
    package = build_signed_plugin_zip(
        manifest,
        first_party_payload_files(manifest),
        signer=signer,
    )
    return manifest, package, trust


def _job(
    *,
    automation_id: str,
    job_type: WorkerJobType,
    generation: int,
    package_sha256: str,
    plugin_version: str,
    cleanup_scope: str | None = None,
) -> WorkerJob:
    now = datetime.now(timezone.utc)
    payload = (
        {
            "package_url": (
                f"/internal/v1/automation/worker/packages/sync_arrive_list/{plugin_version}/"
                f"{package_sha256}/{DISPATCH_AUTHORIZATION_ID}"
            ),
            "package_sha256": package_sha256,
        }
        if job_type in {WorkerJobType.INSTALL, WorkerJobType.UPGRADE}
        else {"purge_id": str(uuid.uuid4()), "generation": generation}
    )
    return WorkerJob(
        job_id=str(uuid.uuid4()),
        automation_id=automation_id,
        automation_generation=generation,
        plugin_id="sync_arrive_list",
        plugin_version=plugin_version,
        job_type=job_type,
        status=WorkerJobStatus.CLAIMED,
        payload=payload,
        target_device_id="office_pc_one",
        available_at=now,
        deadline_at=now + timedelta(minutes=5),
        requires_interactive_session=False,
        operation_type="read" if job_type != WorkerJobType.INVOKE else "external_write",
        cleanup_scope=cleanup_scope,
    )


def test_signed_package_is_shared_but_instances_and_generations_are_isolated(tmp_path: Path) -> None:
    manifest, package, trust = _windows_package()
    verified = verify_signed_plugin_zip(package, verifier=trust)
    fetcher = _Fetcher(package, plugin_version=manifest.version)
    state = WindowsWorkerStateStore(tmp_path / "state")
    runtime = WindowsLocalPluginRuntime(
        state=state,
        package_storage=FilesystemPluginStorage(tmp_path / "packages"),
        signature_verifier=trust,
        package_fetcher=fetcher,
        environment_builder=LockedVirtualEnvironmentBuilder(),
    )
    first = _job(
        automation_id="arrive_instance_one",
        job_type=WorkerJobType.INSTALL,
        generation=1,
        package_sha256=verified.package_sha256,
        plugin_version=manifest.version,
    )
    second = _job(
        automation_id="arrive_instance_two",
        job_type=WorkerJobType.INSTALL,
        generation=1,
        package_sha256=verified.package_sha256,
        plugin_version=manifest.version,
    )
    upgraded = _job(
        automation_id="arrive_instance_one",
        job_type=WorkerJobType.UPGRADE,
        generation=2,
        package_sha256=verified.package_sha256,
        plugin_version=manifest.version,
    )
    assert runtime.install_or_upgrade(first)["installed"] is True
    assert runtime.install_or_upgrade(second)["installed"] is True
    assert runtime.install_or_upgrade(upgraded)["generation"] == 2
    assert fetcher.calls == 1
    package_record = state.get_package(manifest.plugin_id, manifest.version)
    assert package_record is not None and package_record["reference_count"] == 3
    first_one = state.get_deployment("arrive_instance_one", 1)
    first_two = state.get_deployment("arrive_instance_one", 2)
    second_one = state.get_deployment("arrive_instance_two", 1)
    assert first_one and first_two and second_one
    assert first_one["runtime_root"] != first_two["runtime_root"]
    assert first_one["runtime_root"] != second_one["runtime_root"]

    cleanup_generation = _job(
        automation_id="arrive_instance_one",
        job_type=WorkerJobType.CLEANUP,
        generation=1,
        package_sha256=verified.package_sha256,
        plugin_version=manifest.version,
        cleanup_scope="GENERATION",
    )
    # BackgroundService reserves every command first. The cleanup gate must
    # exclude this exact job while still rejecting every other RUNNING job.
    assert state.begin_once(cleanup_generation)
    assert runtime.cleanup_instance(cleanup_generation)["disposed"] is True
    assert state.get_deployment("arrive_instance_one", 1) is None
    assert state.get_deployment("arrive_instance_one", 2) is not None
    package_record = state.get_package(manifest.plugin_id, manifest.version)
    assert package_record is not None and package_record["reference_count"] == 2


def test_unknown_write_blocks_instance_purge_before_files_are_removed(tmp_path: Path) -> None:
    manifest, package, trust = _windows_package()
    verified = verify_signed_plugin_zip(package, verifier=trust)
    state = WindowsWorkerStateStore(tmp_path / "state")
    runtime = WindowsLocalPluginRuntime(
        state=state,
        package_storage=FilesystemPluginStorage(tmp_path / "packages"),
        signature_verifier=trust,
        package_fetcher=_Fetcher(package, plugin_version=manifest.version),
    )
    install = _job(
        automation_id="arrive_instance_one",
        job_type=WorkerJobType.INSTALL,
        generation=1,
        package_sha256=verified.package_sha256,
        plugin_version=manifest.version,
    )
    runtime.install_or_upgrade(install)
    invoke = _job(
        automation_id="arrive_instance_one",
        job_type=WorkerJobType.INVOKE,
        generation=1,
        package_sha256=verified.package_sha256,
        plugin_version=manifest.version,
    )
    assert state.begin_once(invoke)
    assert state.prior_result(invoke.job_id)["status"] == "OUTCOME_UNKNOWN"
    purge = _job(
        automation_id="arrive_instance_one",
        job_type=WorkerJobType.UNINSTALL,
        generation=1,
        package_sha256=verified.package_sha256,
        plugin_version=manifest.version,
        cleanup_scope="INSTANCE",
    )
    instance = state.get_instance("arrive_instance_one")
    assert instance is not None and Path(str(instance["instance_root"])).is_dir()
    with pytest.raises(Exception, match="active job or unknown write"):
        runtime.cleanup_instance(purge)
    assert Path(str(instance["instance_root"])).is_dir()
    assert state.get_instance("arrive_instance_one") is not None
