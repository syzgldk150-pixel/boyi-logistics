"""Server and Worker-side infrastructure ports."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from agent.windows_worker.models import WorkerDeviceSnapshot, WorkerJob, WorkerJobStatus


@runtime_checkable
class ReleaseHoldProvider(Protocol):
    def is_held(self) -> bool: ...


class FailClosedReleaseHoldProvider:
    """Safe default until main injects the scheduler-release marker source."""

    def is_held(self) -> bool:
        return True


@runtime_checkable
class WorkerJobRepositoryPort(Protocol):
    def record_heartbeat(self, snapshot: WorkerDeviceSnapshot) -> None: ...

    def get_device(self, device_id: str) -> WorkerDeviceSnapshot | None: ...

    def expire_due_jobs(self, *, device_id: str, now: datetime) -> Sequence[str]:
        """Fail expired jobs and create visible work items in one transaction."""

    def claim_for_exact_device(
        self,
        *,
        device_id: str,
        worker_id: str,
        now: datetime,
        allow_interactive: bool,
    ) -> WorkerJob | None:
        """Claim with MySQL 8 SKIP LOCKED; never select another device."""

    def mark_running(self, job_id: str, *, worker_id: str, expected_status: WorkerJobStatus) -> WorkerJob: ...

    def complete_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        status: WorkerJobStatus,
        result: Mapping[str, Any],
        error_code: str | None,
        error_summary: str | None,
    ) -> WorkerJob: ...

    def count_active_jobs(self) -> int: ...

    def has_unknown_write(self, *, automation_id: str, plugin_id: str, version: str) -> bool: ...


@runtime_checkable
class WorkerTransportPort(Protocol):
    def poll(self) -> Mapping[str, Any] | None: ...

    def send(self, envelope: Mapping[str, Any]) -> None: ...


@runtime_checkable
class TrayRunnerPort(Protocol):
    def session_state(self) -> str: ...

    def run_instance_action(self, job: WorkerJob) -> Mapping[str, Any]: ...

    def cleanup_instance(self, job: WorkerJob) -> Mapping[str, Any]: ...


@runtime_checkable
class LocalWorkerRuntimePort(Protocol):
    """Worker-owned package/instance state; no server credential storage."""

    def begin_once(self, job: WorkerJob) -> bool:
        """Reserve a job ID once; false means its persisted result is reused."""

    def prior_result(self, job_id: str) -> Mapping[str, Any] | None: ...

    def install_or_upgrade(self, job: WorkerJob) -> Mapping[str, Any]:
        """Verify package bytes, share immutable version by refcount, isolate instance data."""

    def cleanup_instance(self, job: WorkerJob) -> Mapping[str, Any]:
        """Delete instance data and shared bytes only after the last reference."""

    def has_unknown_write(self, automation_id: str) -> bool: ...

    def has_cleanup_blocker(
        self,
        automation_id: str,
        *,
        excluding_job_id: str,
    ) -> bool:
        """Block deletion for every other active job or unknown write."""

    def save_result(self, job_id: str, result: Mapping[str, Any]) -> None: ...

    def count_active_jobs(self) -> int: ...
