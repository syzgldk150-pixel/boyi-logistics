"""Crash-resilient poll/result loop for the outbound-only Windows service."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from agent.automation_plugins.package import PackageSignatureVerifier, PackageSigner
from agent.windows_worker.background_service import WindowsWorkerBackgroundService
from agent.windows_worker.protocol import ReplayGuardPort, sign_worker_envelope
from agent.windows_worker.state import WindowsWorkerStateStore
from agent.windows_worker.ports import WorkerTransportPort


class WindowsWorkerServiceLoop:
    """Persist every signed response before attempting its HTTPS delivery."""

    def __init__(
        self,
        *,
        device_id: str,
        worker_version: str,
        service: WindowsWorkerBackgroundService,
        transport: WorkerTransportPort,
        state: WindowsWorkerStateStore,
        server_verifier: PackageSignatureVerifier,
        server_key_id: str,
        device_signer: PackageSigner,
        replay_guard: ReplayGuardPort,
        heartbeat_snapshot: Callable[[], Mapping[str, Any]],
        heartbeat_seconds: int = 30,
    ) -> None:
        self._device_id = str(device_id)
        self._worker_version = str(worker_version)
        self._service = service
        self._transport = transport
        self._state = state
        self._server_verifier = server_verifier
        self._server_key_id = str(server_key_id)
        self._device_signer = device_signer
        self._replay_guard = replay_guard
        self._heartbeat_snapshot = heartbeat_snapshot
        self._heartbeat_seconds = max(10, min(int(heartbeat_seconds), 300))
        self._next_heartbeat_at = datetime.min.replace(tzinfo=timezone.utc)

    def _queue_message(self, *, kind: str, body: Mapping[str, Any], now: datetime) -> None:
        sequence = self._state.next_outbound_sequence(self._device_id)
        envelope = sign_worker_envelope(
            signer=self._device_signer,
            device_id=self._device_id,
            sequence=sequence,
            kind=kind,
            body=body,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        self._state.queue_outbound(envelope)

    def flush_outbound(self) -> int:
        sent = 0
        while True:
            envelope = self._state.next_pending_outbound()
            if envelope is None:
                return sent
            self._transport.send(envelope)
            self._state.acknowledge_outbound(str(envelope["message_id"]))
            sent += 1

    def _heartbeat_body(self) -> dict[str, Any]:
        snapshot = dict(self._heartbeat_snapshot())
        if set(snapshot) != {"service_state", "session_state", "release_hold", "active_jobs"}:
            raise ValueError("Windows Worker heartbeat snapshot is not closed")
        if not isinstance(snapshot["release_hold"], bool):
            raise ValueError("Windows Worker heartbeat release_hold is invalid")
        if (
            isinstance(snapshot["active_jobs"], bool)
            or not isinstance(snapshot["active_jobs"], int)
            or snapshot["active_jobs"] < 0
        ):
            raise ValueError("Windows Worker heartbeat active_jobs is invalid")
        return {
            **snapshot,
            "worker_version": self._worker_version,
        }

    def run_once(self, *, now: datetime | None = None) -> bool:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.flush_outbound()
        envelope = self._transport.poll()
        handled = envelope is not None
        if envelope is not None:
            result = self._service.execute_signed_command(
                envelope,
                verifier=self._server_verifier,
                replay_guard=self._replay_guard,
                expected_server_key_id=self._server_key_id,
                now=current,
            )
            self._queue_message(
                kind="JOB_STATUS",
                body={
                    "job_id": result.job_id,
                    "dispatch_message_id": result.dispatch_message_id,
                    "dispatch_authorization_id": result.dispatch_authorization_id,
                    "status": result.status.value,
                    "process_confirmed": result.process_confirmed,
                    "result": dict(result.result),
                    "error_code": result.error_code,
                },
                now=current,
            )
        if current >= self._next_heartbeat_at:
            self._queue_message(
                kind="HEARTBEAT",
                body=self._heartbeat_body(),
                now=current,
            )
            self._next_heartbeat_at = current + timedelta(seconds=self._heartbeat_seconds)
        self.flush_outbound()
        return handled

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        idle_seconds: float = 2.0,
        max_error_backoff_seconds: float = 30.0,
    ) -> None:
        delay = max(0.1, float(idle_seconds))
        maximum = max(delay, float(max_error_backoff_seconds))
        backoff = delay
        while not stop_event.is_set():
            try:
                handled = self.run_once()
            except Exception:
                # The Windows Event Log adapter records only the stable error
                # code/type.  This core loop deliberately does not print an
                # exception that could contain server-controlled text.
                stop_event.wait(backoff)
                backoff = min(maximum, backoff * 2)
                continue
            backoff = delay
            if not handled:
                stop_event.wait(delay)


def sleep_until_stopped(stop_event: threading.Event, seconds: float) -> None:
    """Small injectable helper used by Windows service wrappers."""

    deadline = time.monotonic() + max(0.0, seconds)
    while not stop_event.is_set() and time.monotonic() < deadline:
        stop_event.wait(min(0.25, max(0.0, deadline - time.monotonic())))
