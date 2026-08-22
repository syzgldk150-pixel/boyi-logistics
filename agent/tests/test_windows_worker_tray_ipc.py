from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from agent.automation_plugins.errors import WorkerProtocolError
from agent.windows_worker.models import WorkerJob, WorkerJobStatus, WorkerJobType, worker_job_from_mapping
from agent.windows_worker.tray_ipc import NamedPipeTrayClient, NamedPipeTrayServer, worker_pipe_address
from agent.windows_worker.windows_session import current_interactive_session_state


class _Actions:
    def session_state(self) -> str:
        return "AVAILABLE"

    def run(self, job: WorkerJob) -> Mapping[str, Any]:
        return {"job_id": job.job_id, "ok": True}

    def cleanup(self, job: WorkerJob) -> Mapping[str, Any]:
        return {"automation_id": job.automation_id, "clean": True}


class _Connection:
    def __init__(self, server: NamedPipeTrayServer) -> None:
        self._server = server
        self.request: bytes | None = None

    def send_bytes(self, value: bytes) -> None:
        self.request = bytes(value)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        assert self.request is not None
        request = json.loads(self.request.decode("utf-8"))
        response = self._server._dispatch(request)  # type: ignore[attr-defined]
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        assert maxlength is None or len(encoded) <= maxlength
        return encoded

    def close(self) -> None:
        return None


def _job() -> WorkerJob:
    now = datetime.now(timezone.utc)
    return WorkerJob(
        job_id=str(uuid.uuid4()),
        automation_id="arrive_instance_one",
        plugin_id="sync_arrive_list",
        plugin_version="1.0.0",
        job_type=WorkerJobType.INVOKE,
        status=WorkerJobStatus.CLAIMED,
        payload={"generation": 1, "target_date": "2026-08-15"},
        target_device_id="office_pc_one",
        available_at=now,
        deadline_at=now + timedelta(minutes=5),
        requires_interactive_session=True,
        operation_type="read",
    )


def test_named_pipe_client_server_contract_is_closed_and_authenticated() -> None:
    key = b"k" * 32
    server = NamedPipeTrayServer(
        device_id="office_pc_one",
        auth_key=key,
        actions=_Actions(),
    )

    def client_factory(address: str, *, family: str, authkey: bytes) -> _Connection:
        assert address == r"\\.\pipe\boyi-automation-office_pc_one"
        assert family == "AF_PIPE"
        assert authkey == key
        return _Connection(server)

    client = NamedPipeTrayClient(
        device_id="office_pc_one",
        auth_key=key,
        client_factory=client_factory,
    )
    job = _job()
    assert worker_pipe_address("office_pc_one").endswith("office_pc_one")
    assert client.session_state() == "AVAILABLE"
    assert client.run_instance_action(job) == {"job_id": job.job_id, "ok": True}
    cleanup_job = WorkerJob(
        **{
            **job.__dict__,
            "job_type": WorkerJobType.CLEANUP,
            "cleanup_scope": "INSTANCE",
        }
    )
    assert client.cleanup_instance(cleanup_job) == {
        "automation_id": "arrive_instance_one",
        "clean": True,
    }


def test_named_pipe_rejects_response_replay_and_never_deserializes_pickle() -> None:
    class _MismatchedConnection:
        def send_bytes(self, value: bytes) -> None:
            assert value.startswith(b"{")
            assert b"\x80\x04" not in value

        def recv_bytes(self, maxlength: int | None = None) -> bytes:
            del maxlength
            return json.dumps(
                {
                    "schema_version": 1,
                    "request_id": str(uuid.uuid4()),
                    "ok": True,
                    "result": {"session_state": "AVAILABLE"},
                    "error_code": None,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

        def close(self) -> None:
            return None

    client = NamedPipeTrayClient(
        device_id="office_pc_one",
        auth_key=b"k" * 32,
        client_factory=lambda *_args, **_kwargs: _MismatchedConnection(),
        request_id_factory=lambda: "92f44b33-b465-4ad4-9310-47b61c658658",
    )
    with pytest.raises(WorkerProtocolError, match="response fields"):
        client.session_state()


def test_named_pipe_server_rejects_unversioned_or_noncanonical_request_id() -> None:
    server = NamedPipeTrayServer(
        device_id="office_pc_one",
        auth_key=b"k" * 32,
        actions=_Actions(),
    )
    response = server._dispatch(  # type: ignore[attr-defined]
        {"action": "SESSION_STATE", "request_id": str(uuid.uuid4())}
    )
    assert response["ok"] is False
    assert response["error_code"] == "TRAY_REQUEST_INVALID"
    response = server._dispatch(  # type: ignore[attr-defined]
        {
            "schema_version": 1,
            "request_id": "92F44B33-B465-4AD4-9310-47B61C658658",
            "action": "SESSION_STATE",
        }
    )
    assert response["ok"] is False
    assert response["error_code"] == "TRAY_REQUEST_INVALID"


def test_worker_payload_rejects_accounts_credentials_and_deep_values() -> None:
    job = _job()
    mapping = {
        "job_id": job.job_id,
        "automation_id": job.automation_id,
        "automation_generation": job.automation_generation,
        "plugin_id": job.plugin_id,
        "plugin_version": job.plugin_version,
        "job_type": job.job_type.value,
        "status": job.status.value,
        "payload": {"generation": 1, "account_id": "should-not-cross"},
        "target_device_id": job.target_device_id,
        "available_at": job.available_at.isoformat(),
        "deadline_at": job.deadline_at.isoformat(),
        "requires_interactive_session": True,
        "operation_type": "read",
        "attempt_count": 0,
        "max_attempts": 1,
        "cleanup_scope": None,
    }
    with pytest.raises(WorkerProtocolError, match="accounts or credentials"):
        worker_job_from_mapping(mapping)
    mapping["payload"] = {"generation": 1, "nested": {"session_token": "secret"}}
    with pytest.raises(WorkerProtocolError, match="accounts or credentials"):
        worker_job_from_mapping(mapping)


def test_non_windows_session_probe_fails_closed() -> None:
    state = current_interactive_session_state().value
    if os.name != "nt":
        assert state == "LOGGED_OUT"
    else:
        assert state in {"AVAILABLE", "LOCKED", "LOGGED_OUT"}
