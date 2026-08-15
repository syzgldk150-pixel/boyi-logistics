"""Authenticated local named-pipe boundary between Session 0 and Tray Runner."""

from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from multiprocessing.connection import Client, Listener
from typing import Any, Callable, Mapping, Protocol

from agent.automation_plugins.errors import WorkerProtocolError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.windows_worker.models import WorkerJob, worker_job_from_mapping, worker_job_to_mapping


_PIPE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,80}$")
_IPC_SCHEMA_VERSION = 1
_MAX_IPC_BYTES = 1024 * 1024
_INVALID_REQUEST_ID = "00000000-0000-0000-0000-000000000000"


class PipeConnectionPort(Protocol):
    def send_bytes(self, value: bytes) -> None: ...

    def recv_bytes(self, maxlength: int | None = None) -> bytes: ...

    def close(self) -> None: ...


class PipeListenerPort(Protocol):
    def accept(self) -> PipeConnectionPort: ...

    def close(self) -> None: ...


def worker_pipe_address(device_id: str) -> str:
    if not _PIPE_NAME_RE.fullmatch(device_id):
        raise ValueError("Windows Worker device_id is invalid for a named pipe")
    return rf"\\.\pipe\boyi-automation-{device_id}"


def _auth_key(value: bytes) -> bytes:
    key = bytes(value)
    if len(key) < 32 or len(key) > 128:
        raise ValueError("Tray pipe authentication key must be from 32 to 128 bytes")
    return key


def _canonical_request_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value else None


def _encode_message(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = canonical_json_bytes(dict(value))
    except (TypeError, ValueError) as exc:
        raise WorkerProtocolError("Tray Runner IPC message is not canonical JSON") from exc
    if len(encoded) > _MAX_IPC_BYTES:
        raise WorkerProtocolError("Tray Runner IPC message exceeds the one MiB limit")
    return encoded


def _decode_message(value: bytes) -> Mapping[str, Any]:
    if not isinstance(value, bytes) or not value or len(value) > _MAX_IPC_BYTES:
        raise WorkerProtocolError("Tray Runner IPC message size is invalid")
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("Tray Runner IPC message is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise WorkerProtocolError("Tray Runner IPC message must be an object")
    return decoded


class NamedPipeTrayClient:
    """Session-0 client implementing the TrayRunnerPort contract."""

    def __init__(
        self,
        *,
        device_id: str,
        auth_key: bytes,
        client_factory: Callable[..., PipeConnectionPort] = Client,
        request_id_factory: Callable[[], Any] = uuid.uuid4,
    ) -> None:
        self._address = worker_pipe_address(device_id)
        self._auth_key = _auth_key(auth_key)
        self._client_factory = client_factory
        self._request_id_factory = request_id_factory

    def _call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = str(self._request_id_factory())
        if _canonical_request_id(request_id) is None:
            raise WorkerProtocolError("Tray Runner request ID factory returned an invalid UUID")
        wire_request = {
            "schema_version": _IPC_SCHEMA_VERSION,
            "request_id": request_id,
            **copy.deepcopy(dict(request)),
        }
        connection = self._client_factory(
            self._address,
            family="AF_PIPE",
            authkey=self._auth_key,
        )
        try:
            connection.send_bytes(_encode_message(wire_request))
            response = _decode_message(connection.recv_bytes(_MAX_IPC_BYTES + 1))
        except (EOFError, OSError, WorkerProtocolError) as exc:
            raise WorkerProtocolError("Tray Runner pipe request failed") from exc
        finally:
            connection.close()
        if set(response) != {
            "schema_version",
            "request_id",
            "ok",
            "result",
            "error_code",
        }:
            raise WorkerProtocolError("Tray Runner pipe response schema is invalid")
        if (
            response["schema_version"] != _IPC_SCHEMA_VERSION
            or response["request_id"] != request_id
            or not isinstance(response["ok"], bool)
            or not isinstance(response["result"], dict)
        ):
            raise WorkerProtocolError("Tray Runner pipe response fields are invalid")
        if response["error_code"] is not None and not isinstance(response["error_code"], str):
            raise WorkerProtocolError("Tray Runner pipe error_code is invalid")
        if not response["ok"]:
            error_code = str(response["error_code"] or "TRAY_ACTION_FAILED")
            raise WorkerProtocolError("Tray Runner action failed", code=error_code)
        return copy.deepcopy(dict(response["result"]))

    def session_state(self) -> str:
        response = self._call({"action": "SESSION_STATE"})
        if set(response) != {"session_state"} or response["session_state"] not in {
            "AVAILABLE",
            "LOCKED",
            "LOGGED_OUT",
        }:
            raise WorkerProtocolError("Tray Runner session state is invalid")
        return str(response["session_state"])

    def run_instance_action(self, job: WorkerJob) -> Mapping[str, Any]:
        return self._call({"action": "RUN", "job": worker_job_to_mapping(job)})

    def cleanup_instance(self, job: WorkerJob) -> Mapping[str, Any]:
        return self._call({"action": "CLEANUP", "job": worker_job_to_mapping(job)})


class TrayActionPort(Protocol):
    def session_state(self) -> str: ...

    def run(self, job: WorkerJob) -> Mapping[str, Any]: ...

    def cleanup(self, job: WorkerJob) -> Mapping[str, Any]: ...


class NamedPipeTrayServer:
    """One-request server intended to run inside the logged-in user session."""

    def __init__(
        self,
        *,
        device_id: str,
        auth_key: bytes,
        actions: TrayActionPort,
        listener_factory: Callable[..., PipeListenerPort] = Listener,
    ) -> None:
        self._address = worker_pipe_address(device_id)
        self._auth_key = _auth_key(auth_key)
        self._actions = actions
        self._listener_factory = listener_factory

    def _dispatch(self, request: Any) -> dict[str, Any]:
        request_id = (
            _canonical_request_id(request.get("request_id"))
            if isinstance(request, dict)
            else None
        )
        response_id = request_id or _INVALID_REQUEST_ID
        if (
            not isinstance(request, dict)
            or request.get("schema_version") != _IPC_SCHEMA_VERSION
            or request_id is None
            or request.get("action") not in {
            "SESSION_STATE",
            "RUN",
            "CLEANUP",
            }
        ):
            return {
                "schema_version": _IPC_SCHEMA_VERSION,
                "request_id": response_id,
                "ok": False,
                "result": {},
                "error_code": "TRAY_REQUEST_INVALID",
            }
        action = str(request["action"])
        try:
            if action == "SESSION_STATE":
                if set(request) != {"schema_version", "request_id", "action"}:
                    raise WorkerProtocolError("Tray session request has extra fields")
                result: Mapping[str, Any] = {"session_state": self._actions.session_state()}
            else:
                if set(request) != {
                    "schema_version",
                    "request_id",
                    "action",
                    "job",
                } or not isinstance(request["job"], Mapping):
                    raise WorkerProtocolError("Tray job request schema is invalid")
                job = worker_job_from_mapping(request["job"])
                result = self._actions.run(job) if action == "RUN" else self._actions.cleanup(job)
                if not isinstance(result, Mapping):
                    raise WorkerProtocolError("Tray action result must be an object")
            return {
                "schema_version": _IPC_SCHEMA_VERSION,
                "request_id": response_id,
                "ok": True,
                "result": copy.deepcopy(dict(result)),
                "error_code": None,
            }
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str) or not code:
                code = type(exc).__name__.upper()
            return {
                "schema_version": _IPC_SCHEMA_VERSION,
                "request_id": response_id,
                "ok": False,
                "result": {},
                "error_code": code[:128],
            }

    def serve_once(self) -> None:
        listener = self._listener_factory(
            self._address,
            family="AF_PIPE",
            authkey=self._auth_key,
        )
        try:
            connection = listener.accept()
            try:
                try:
                    request = _decode_message(connection.recv_bytes(_MAX_IPC_BYTES + 1))
                except (EOFError, OSError, WorkerProtocolError):
                    request = None
                connection.send_bytes(_encode_message(self._dispatch(request)))
            finally:
                connection.close()
        finally:
            listener.close()

    def serve_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        max_requests: int | None = None,
    ) -> None:
        """Serve authenticated requests until logout/task shutdown.

        ``Listener.accept`` is intentionally allowed to block.  Task Scheduler
        owns process lifetime at logoff; ``stop_event`` is observed between
        requests and ``max_requests`` exists only for bounded hosts/tests.
        """

        if max_requests is not None and (
            isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests <= 0
        ):
            raise ValueError("Tray Runner max_requests must be a positive integer")
        stopped = stop_event or threading.Event()
        served = 0
        while not stopped.is_set():
            self.serve_once()
            served += 1
            if max_requests is not None and served >= max_requests:
                return
