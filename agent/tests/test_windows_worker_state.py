from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.windows_worker.models import WorkerJob, WorkerJobStatus, WorkerJobType
from agent.windows_worker.state import (
    WindowsWorkerStateStore,
    read_worker_cleanup_safety_snapshot,
)


def _job(
    *,
    job_type: WorkerJobType = WorkerJobType.INVOKE,
    operation_type: str = "external_write",
    automation_id: str = "customer_sync_one",
    plugin_id: str = "customer_sync",
    version: str = "1.0.0",
) -> WorkerJob:
    now = datetime.now(timezone.utc)
    return WorkerJob(
        job_id=str(uuid.uuid4()),
        automation_id=automation_id,
        plugin_id=plugin_id,
        plugin_version=version,
        job_type=job_type,
        status=WorkerJobStatus.CLAIMED,
        payload={},
        target_device_id="office_pc_one",
        available_at=now,
        deadline_at=now + timedelta(minutes=5),
        requires_interactive_session=False,
        operation_type=operation_type,
        max_attempts=1,
    )


def test_replay_and_outbound_state_survive_restart(tmp_path: Path) -> None:
    root = tmp_path / "worker"
    store = WindowsWorkerStateStore(root)
    first_message = str(uuid.uuid4())
    second_message = str(uuid.uuid4())
    assert store.advance_sequence(
        device_id="office_pc_one",
        sequence=7,
        message_id=first_message,
    )
    assert not store.advance_sequence(
        device_id="office_pc_one",
        sequence=7,
        message_id=second_message,
    )
    assert store.next_outbound_sequence("office_pc_one") == 0
    envelope = {
        "schema_version": 1,
        "message_id": second_message,
        "device_id": "office_pc_one",
        "sequence": 0,
        "issued_at": "2026-08-15T00:00:00Z",
        "expires_at": "2026-08-15T00:05:00Z",
        "kind": "HEARTBEAT",
        "body": {},
        "key_id": "device-key",
        "signature": "signature",
    }
    store.queue_outbound(envelope)

    restarted = WindowsWorkerStateStore(root)
    assert not restarted.advance_sequence(
        device_id="office_pc_one",
        sequence=7,
        message_id=str(uuid.uuid4()),
    )
    assert restarted.advance_sequence(
        device_id="office_pc_one",
        sequence=8,
        message_id=str(uuid.uuid4()),
    )
    assert restarted.next_outbound_sequence("office_pc_one") == 1
    assert restarted.next_pending_outbound() == envelope
    restarted.acknowledge_outbound(second_message)
    assert restarted.next_pending_outbound() is None


def test_restart_marks_unfinished_write_unknown_but_read_failed(tmp_path: Path) -> None:
    root = tmp_path / "worker"
    store = WindowsWorkerStateStore(root)
    write_job = _job()
    read_job = _job(operation_type="read")
    assert store.begin_once(write_job)
    assert store.begin_once(read_job)

    restarted = WindowsWorkerStateStore(root)
    write_result = restarted.prior_result(write_job.job_id)
    read_result = restarted.prior_result(read_job.job_id)
    assert write_result == {
        "status": "OUTCOME_UNKNOWN",
        "result": {},
        "error_code": "WORKER_RESTARTED_AFTER_PROTECTED_JOB_STARTED",
    }
    assert read_result == {
        "status": "FAILED",
        "result": {},
        "error_code": "WORKER_RESTARTED_BEFORE_RESULT",
    }
    assert restarted.has_unknown_write(write_job.automation_id)
    assert not restarted.begin_once(write_job)


def test_cleanup_gate_excludes_only_its_own_job_and_host_snapshot_is_non_consuming(
    tmp_path: Path,
) -> None:
    store = WindowsWorkerStateStore(tmp_path / "worker")
    cleanup = _job(job_type=WorkerJobType.UNINSTALL)
    other_read = _job(operation_type="read")
    assert store.begin_once(cleanup)
    assert not store.has_cleanup_blocker(
        cleanup.automation_id,
        excluding_job_id=cleanup.job_id,
    )
    assert store.begin_once(other_read)
    assert store.has_cleanup_blocker(
        cleanup.automation_id,
        excluding_job_id=cleanup.job_id,
    )
    assert store.cleanup_safety_snapshot() == {
        "active_jobs": 2,
        "unknown_writes": 0,
    }
    # Reading the host uninstall gate must not convert RUNNING rows to a
    # terminal result; only recovery of a concrete job may do so.
    assert store.cleanup_safety_snapshot()["active_jobs"] == 2
    database_before = store.database_path.read_bytes()
    assert read_worker_cleanup_safety_snapshot(store.database_path) == {
        "active_jobs": 2,
        "unknown_writes": 0,
    }
    assert store.database_path.read_bytes() == database_before


def test_generation_references_and_purge_are_exact(tmp_path: Path) -> None:
    store = WindowsWorkerStateStore(tmp_path / "worker")
    package_root = tmp_path / "packages" / "customer_sync" / "1.0.0-deadbeef"
    first_instance = tmp_path / "instances" / "customer_sync_one"
    second_instance = tmp_path / "instances" / "customer_sync_two"
    first_runtime = first_instance / "1"
    second_runtime = second_instance / "1"
    for path in (package_root, first_runtime, second_runtime):
        path.mkdir(parents=True)
    common = {
        "generation": 1,
        "plugin_id": "customer_sync",
        "plugin_version": "1.0.0",
        "package_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "install_root": str(package_root.resolve()),
    }
    store.bind_deployment(
        automation_id="customer_sync_one",
        instance_root=str(first_instance.resolve()),
        runtime_root=str(first_runtime.resolve()),
        **common,
    )
    store.bind_deployment(
        automation_id="customer_sync_two",
        instance_root=str(second_instance.resolve()),
        runtime_root=str(second_runtime.resolve()),
        **common,
    )
    package = store.get_package("customer_sync", "1.0.0")
    assert package is not None and package["reference_count"] == 2

    journal = store.prepare_purge(
        purge_id=str(uuid.uuid4()),
        automation_id="customer_sync_one",
    )
    assert journal["package_root"] is None
    store.finalize_purge(purge_id=str(journal["purge_id"]))
    assert store.get_instance("customer_sync_one") is None
    package = store.get_package("customer_sync", "1.0.0")
    assert package is not None and package["reference_count"] == 1
    assert store.get_instance("customer_sync_two") is not None
