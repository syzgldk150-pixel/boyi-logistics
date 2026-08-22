"""Server-side exact-device dispatch with release-hold integration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from agent.windows_worker.models import (
    DeviceServiceState,
    InteractiveSessionState,
    WorkerDeviceSnapshot,
    WorkerHealth,
    WorkerJob,
    WorkerJobStatus,
)
from agent.windows_worker.ports import (
    FailClosedReleaseHoldProvider,
    ReleaseHoldProvider,
    WorkerJobRepositoryPort,
)


class WorkerCoordinator:
    """Coordinates jobs while allowing heartbeats during a release hold."""

    def __init__(
        self,
        repository: WorkerJobRepositoryPort,
        *,
        release_hold_provider: ReleaseHoldProvider | None = None,
    ) -> None:
        self._repository = repository
        self._release_hold = release_hold_provider or FailClosedReleaseHoldProvider()

    def heartbeat(self, snapshot: WorkerDeviceSnapshot) -> WorkerHealth:
        self._repository.record_heartbeat(snapshot)
        return WorkerHealth(
            held=self._release_hold.is_held(),
            active_jobs=self._repository.count_active_jobs(),
            service_state=snapshot.service_state,
            session_state=snapshot.session_state,
        )

    def health(self, device_id: str | None = None) -> WorkerHealth:
        device = self._repository.get_device(device_id) if device_id else None
        return WorkerHealth(
            held=self._release_hold.is_held(),
            active_jobs=self._repository.count_active_jobs(),
            service_state=device.service_state if device else None,
            session_state=device.session_state if device else None,
        )

    def claim_next(
        self,
        *,
        device_id: str,
        worker_id: str,
        now: datetime | None = None,
    ) -> WorkerJob | None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self._repository.expire_due_jobs(device_id=device_id, now=current)
        # Heartbeats and expiry remain observable, but install/invoke/cleanup
        # cannot be claimed while scheduler-release.pause is present.
        if self._release_hold.is_held():
            return None
        device = self._repository.get_device(device_id)
        if device is None or device.service_state != DeviceServiceState.ONLINE:
            return None
        interactive = device.session_state == InteractiveSessionState.AVAILABLE
        job = self._repository.claim_for_exact_device(
            device_id=device_id,
            worker_id=worker_id,
            now=current,
            allow_interactive=interactive,
        )
        if job is None:
            return None
        if job.target_device_id != device_id or job.max_attempts != 1:
            raise RuntimeError("Worker repository violated exact-device/max-attempts contract")
        if job.requires_interactive_session and not interactive:
            raise RuntimeError("Worker repository claimed an interactive job without an available session")
        return job

    def mark_running(self, job_id: str, *, worker_id: str) -> WorkerJob:
        return self._repository.mark_running(
            job_id,
            worker_id=worker_id,
            expected_status=WorkerJobStatus.CLAIMED,
        )

    def complete(
        self,
        job: WorkerJob,
        *,
        worker_id: str,
        result: Mapping[str, Any],
        process_confirmed: bool,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> WorkerJob:
        if process_confirmed:
            status = WorkerJobStatus.SUCCEEDED if not error_code else WorkerJobStatus.FAILED
        elif job.is_write:
            status = WorkerJobStatus.OUTCOME_UNKNOWN
            error_code = "WRITE_OUTCOME_UNKNOWN"
            error_summary = "Worker lost a definitive result after a protected write may have started"
        else:
            status = WorkerJobStatus.FAILED
            error_code = error_code or "WORKER_RESULT_UNCONFIRMED"
        return self._repository.complete_job(
            job.job_id,
            worker_id=worker_id,
            status=status,
            result=dict(result),
            error_code=error_code,
            error_summary=error_summary,
        )

    def can_cleanup(self, *, automation_id: str, plugin_id: str, version: str) -> bool:
        if self._release_hold.is_held():
            return False
        return not self._repository.has_unknown_write(
            automation_id=automation_id,
            plugin_id=plugin_id,
            version=version,
        )
