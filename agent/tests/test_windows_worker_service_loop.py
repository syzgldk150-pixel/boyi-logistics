from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
from Crypto.PublicKey import ECC

from agent.automation_plugins.errors import WorkerProtocolError
from agent.automation_plugins.package import (
    Ed25519PackageSigner,
    Ed25519TrustStore,
)
from agent.windows_worker.background_service import WindowsWorkerBackgroundService
from agent.windows_worker.models import (
    InteractiveSessionState,
    WorkerJob,
    WorkerJobStatus,
    worker_job_from_mapping,
)
from agent.windows_worker.protocol import (
    MemoryReplayGuard,
    RepositoryReplayGuard,
    sign_worker_envelope,
    verify_worker_envelope,
)
from agent.windows_worker.service_loop import WindowsWorkerServiceLoop
from agent.windows_worker.state import WindowsWorkerStateStore


class _Runtime:
    def __init__(self) -> None:
        self.results: dict[str, Mapping[str, Any]] = {}
        self.install_jobs: list[WorkerJob] = []

    def begin_once(self, job: WorkerJob) -> bool:
        return job.job_id not in self.results

    def prior_result(self, job_id: str) -> Mapping[str, Any] | None:
        return self.results.get(job_id)

    def install_or_upgrade(self, job: WorkerJob) -> Mapping[str, Any]:
        self.install_jobs.append(job)
        return {"installed": job.automation_id}

    def cleanup_instance(self, job: WorkerJob) -> Mapping[str, Any]:
        return {"cleaned": job.automation_id}

    def has_unknown_write(self, automation_id: str) -> bool:
        return False

    def has_cleanup_blocker(
        self,
        automation_id: str,
        *,
        excluding_job_id: str,
    ) -> bool:
        del automation_id, excluding_job_id
        return False

    def save_result(self, job_id: str, result: Mapping[str, Any]) -> None:
        self.results[job_id] = dict(result)

    def count_active_jobs(self) -> int:
        return 0


class _Tray:
    def session_state(self) -> str:
        return InteractiveSessionState.AVAILABLE.value

    def run_instance_action(self, job: WorkerJob) -> Mapping[str, Any]:
        return {"ok": True, "generation": job.payload["generation"]}

    def cleanup_instance(self, job: WorkerJob) -> Mapping[str, Any]:
        return {"ok": True}


class _LockedTray(_Tray):
    def session_state(self) -> str:
        return InteractiveSessionState.LOCKED.value


class _FailingTray(_Tray):
    def run_instance_action(self, job: WorkerJob) -> Mapping[str, Any]:
        del job
        raise OSError("test-only pipe failure")


class _Transport:
    def __init__(self, incoming: Mapping[str, Any]) -> None:
        self.incoming: list[Mapping[str, Any] | None] = [incoming, None]
        self.sent: list[Mapping[str, Any]] = []
        self.fail_first_send = True

    def poll(self) -> Mapping[str, Any] | None:
        return self.incoming.pop(0) if self.incoming else None

    def send(self, envelope: Mapping[str, Any]) -> None:
        if self.fail_first_send:
            self.fail_first_send = False
            raise RuntimeError("network unavailable")
        self.sent.append(dict(envelope))


def _signer(key_id: str) -> tuple[Ed25519PackageSigner, Ed25519TrustStore]:
    key = ECC.generate(curve="Ed25519")
    signer = Ed25519PackageSigner(key_id=key_id, private_key=key)
    trust = Ed25519TrustStore({key_id: key.public_key().export_key(format="raw")})
    return signer, trust


def _job_mapping(now: datetime) -> dict[str, Any]:
    return {
        "job_id": str(uuid.uuid4()),
        "automation_id": "arrive_instance_one",
        "automation_generation": 2,
        "plugin_id": "sync_arrive_list",
        "plugin_version": "1.0.0",
        "job_type": "INVOKE",
        "status": "CLAIMED",
        "payload": {"generation": 2},
        "target_device_id": "office_pc_one",
        "available_at": now.isoformat(),
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
        "requires_interactive_session": True,
        "operation_type": "read",
        "attempt_count": 0,
        "max_attempts": 1,
        "cleanup_scope": None,
    }


def test_service_persists_result_before_retrying_exact_signed_message(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    server_signer, server_trust = _signer("server-key")
    device_signer, device_trust = _signer("device-key")
    command = sign_worker_envelope(
        signer=server_signer,
        device_id="office_pc_one",
        sequence=1,
        kind="COMMAND",
        body={
            "job": _job_mapping(now),
            "dispatch": {
                "release_hold": False,
                "authorization_id": str(uuid.uuid4()),
                "release_sha": "abcdef1",
            },
        },
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    transport = _Transport(command)
    state = WindowsWorkerStateStore(tmp_path / "state")
    service = WindowsWorkerBackgroundService(
        device_id="office_pc_one",
        runtime=_Runtime(),
        tray=_Tray(),
    )
    loop = WindowsWorkerServiceLoop(
        device_id="office_pc_one",
        worker_version="1.0.0",
        service=service,
        transport=transport,
        state=state,
        server_verifier=server_trust,
        server_key_id="server-key",
        device_signer=device_signer,
        replay_guard=RepositoryReplayGuard(state),
        heartbeat_snapshot=lambda: {
            "service_state": "ONLINE",
            "session_state": "AVAILABLE",
            "release_hold": False,
            "active_jobs": 0,
        },
    )
    with pytest.raises(RuntimeError, match="network unavailable"):
        loop.run_once(now=now)
    pending = state.next_pending_outbound()
    assert pending is not None and pending["kind"] == "JOB_STATUS"

    assert loop.run_once(now=now + timedelta(seconds=1)) is False
    assert [item["kind"] for item in transport.sent] == ["JOB_STATUS", "HEARTBEAT"]
    replay = MemoryReplayGuard()
    verified = [
        verify_worker_envelope(
            item,
            verifier=device_trust,
            replay_guard=replay,
            expected_device_id="office_pc_one",
            expected_key_id="device-key",
            now=now + timedelta(seconds=1),
        )
        for item in transport.sent
    ]
    assert verified[0]["body"] == {
        "job_id": command["body"]["job"]["job_id"],
        "dispatch_message_id": command["message_id"],
        "dispatch_authorization_id": command["body"]["dispatch"][
            "authorization_id"
        ],
        "status": "SUCCEEDED",
        "process_confirmed": True,
        "result": {"ok": True, "generation": 2},
        "error_code": None,
    }
    assert verified[1]["body"]["worker_version"] == "1.0.0"
    assert state.next_pending_outbound() is None


def test_locked_session_is_blocked_before_write_and_pipe_failure_is_unknown() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    mapping = {
        **_job_mapping(now),
        "operation_type": "external_write",
    }
    locked_runtime = _Runtime()
    locked = WindowsWorkerBackgroundService(
        device_id="office_pc_one",
        runtime=locked_runtime,
        tray=_LockedTray(),
    ).execute(
        worker_job_from_mapping(mapping),
        now=now,
        signed_dispatch_authorized=True,
    )
    assert locked.status == WorkerJobStatus.BLOCKED_DATA
    assert locked.process_confirmed is True
    assert locked.error_code == "INTERACTIVE_SESSION_UNAVAILABLE"
    assert locked_runtime.results[mapping["job_id"]]["status"] == "BLOCKED_DATA"

    failed_mapping = {**mapping, "job_id": str(uuid.uuid4())}
    failed_runtime = _Runtime()
    failed = WindowsWorkerBackgroundService(
        device_id="office_pc_one",
        runtime=failed_runtime,
        tray=_FailingTray(),
    ).execute(
        worker_job_from_mapping(failed_mapping),
        now=now,
        signed_dispatch_authorized=True,
    )
    assert failed.status == WorkerJobStatus.OUTCOME_UNKNOWN
    assert failed.process_confirmed is False
    assert failed.error_code == "WRITE_OUTCOME_UNKNOWN"


def test_signed_install_derives_fixed_relative_package_path_from_dispatch() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    server_signer, server_trust = _signer("server-key")
    authorization_id = "f6d9dc71-b197-4800-bad3-4efe484406df"
    package_sha256 = "a" * 64
    mapping = {
        **_job_mapping(now),
        "job_type": "INSTALL",
        "payload": {
            "package": {
                "plugin_id": "sync_arrive_list",
                "version": "1.0.0",
                "package_sha256": package_sha256,
            }
        },
        "requires_interactive_session": False,
    }
    command = sign_worker_envelope(
        signer=server_signer,
        device_id="office_pc_one",
        sequence=3,
        kind="COMMAND",
        body={
            "job": mapping,
            "dispatch": {
                "release_hold": False,
                "authorization_id": authorization_id,
                "release_sha": "abcdef1",
            },
        },
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    runtime = _Runtime()
    result = WindowsWorkerBackgroundService(
        device_id="office_pc_one",
        runtime=runtime,
        tray=_Tray(),
    ).execute_signed_command(
        command,
        verifier=server_trust,
        replay_guard=MemoryReplayGuard(),
        expected_server_key_id="server-key",
        now=now,
    )
    assert result.status == WorkerJobStatus.SUCCEEDED
    assert len(runtime.install_jobs) == 1
    assert runtime.install_jobs[0].payload == {
        "package_url": (
            "/internal/v1/automation/worker/packages/sync_arrive_list/1.0.0/"
            f"{package_sha256}/{authorization_id}"
        ),
        "package_sha256": package_sha256,
    }

    forged = copy.deepcopy(command)
    forged["body"]["job"]["payload"] = {
        "package_url": "https://attacker.test/plugin.zip",
        "package_sha256": package_sha256,
        "manifest_sha256": "b" * 64,
    }
    forged = sign_worker_envelope(
        signer=server_signer,
        device_id="office_pc_one",
        sequence=4,
        kind="COMMAND",
        body=forged["body"],
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with pytest.raises(WorkerProtocolError, match="package identity is not closed"):
        WindowsWorkerBackgroundService(
            device_id="office_pc_one",
            runtime=_Runtime(),
            tray=_Tray(),
        ).execute_signed_command(
            forged,
            verifier=server_trust,
            replay_guard=MemoryReplayGuard(),
            expected_server_key_id="server-key",
            now=now,
        )
