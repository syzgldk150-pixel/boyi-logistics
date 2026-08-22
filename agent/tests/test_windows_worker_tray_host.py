from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from agent.automation_plugins.errors import WorkerProtocolError
from agent.windows_worker.models import (
    InteractiveSessionState,
    WorkerJob,
    WorkerJobStatus,
    WorkerJobType,
)
from agent.windows_worker.tray_host import WindowsTrayHost
from agent.windows_worker.tray_runner import (
    FailClosedInstanceProcessRunner,
    InteractiveTrayRunner,
)


def _job(
    *,
    job_type: WorkerJobType = WorkerJobType.INVOKE,
    requires_interactive_session: bool = True,
) -> WorkerJob:
    now = datetime.now(timezone.utc)
    return WorkerJob(
        job_id=str(uuid.uuid4()),
        automation_id="arrive_instance_one",
        plugin_id="sync_arrive_list",
        plugin_version="1.0.0",
        job_type=job_type,
        status=WorkerJobStatus.CLAIMED,
        payload={"generation": 1},
        target_device_id="office_pc_one",
        available_at=now,
        deadline_at=now + timedelta(minutes=5),
        requires_interactive_session=requires_interactive_session,
        operation_type="read",
        cleanup_scope=(
            "INSTANCE"
            if job_type in {WorkerJobType.UNINSTALL, WorkerJobType.CLEANUP}
            else None
        ),
    )


class _ProcessRunner:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str]] = []

    def run(self, job: WorkerJob) -> Mapping[str, Any]:
        self.jobs.append(("run", job.job_id))
        return {"ran": True}

    def cleanup(self, job: WorkerJob) -> Mapping[str, Any]:
        self.jobs.append(("cleanup", job.job_id))
        return {"cleaned": True}


def test_tray_runner_probes_current_session_for_every_interactive_action() -> None:
    states = iter(
        (
            InteractiveSessionState.AVAILABLE,
            InteractiveSessionState.LOCKED,
            InteractiveSessionState.LOGGED_OUT,
        )
    )
    process = _ProcessRunner()
    runner = InteractiveTrayRunner(process, session_state_provider=lambda: next(states))
    invoke = _job()
    assert runner.session_state() == "AVAILABLE"
    with pytest.raises(WorkerProtocolError) as locked:
        runner.run(invoke)
    assert locked.value.code == "INTERACTIVE_SESSION_UNAVAILABLE"
    cleanup = _job(job_type=WorkerJobType.CLEANUP)
    with pytest.raises(WorkerProtocolError) as logged_out:
        runner.cleanup(cleanup)
    assert logged_out.value.code == "INTERACTIVE_SESSION_UNAVAILABLE"
    assert process.jobs == []


def test_source_distribution_tray_adapter_fails_closed() -> None:
    runner = InteractiveTrayRunner(
        FailClosedInstanceProcessRunner(),
        session_state_provider=lambda: InteractiveSessionState.AVAILABLE,
    )
    with pytest.raises(WorkerProtocolError) as unavailable:
        runner.run(_job())
    assert unavailable.value.code == "TRAY_ACTION_ADAPTER_UNAVAILABLE"


class _SingleInstance:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self) -> _SingleInstance:
        self.entered += 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.exited += 1


class _Server:
    def __init__(self) -> None:
        self.calls: list[tuple[threading.Event | None, int | None]] = []

    def serve_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        max_requests: int | None = None,
    ) -> None:
        self.calls.append((stop_event, max_requests))


def test_tray_host_requires_windows_and_one_logged_in_session() -> None:
    server = _Server()
    lock = _SingleInstance()
    host = WindowsTrayHost(
        server=server,
        single_instance=lock,
        session_state_provider=lambda: InteractiveSessionState.AVAILABLE,
        platform_name="posix",
    )
    with pytest.raises(WorkerProtocolError) as wrong_platform:
        host.run_forever()
    assert wrong_platform.value.code == "TRAY_WINDOWS_REQUIRED"

    host = WindowsTrayHost(
        server=server,
        single_instance=lock,
        session_state_provider=lambda: InteractiveSessionState.LOGGED_OUT,
        platform_name="nt",
    )
    with pytest.raises(WorkerProtocolError) as logged_out:
        host.run_forever()
    assert logged_out.value.code == "INTERACTIVE_SESSION_UNAVAILABLE"
    assert lock.entered == 0


def test_tray_host_can_start_locked_but_actions_remain_session_gated() -> None:
    server = _Server()
    lock = _SingleInstance()
    stop_event = threading.Event()
    host = WindowsTrayHost(
        server=server,
        single_instance=lock,
        session_state_provider=lambda: InteractiveSessionState.LOCKED,
        platform_name="nt",
    )
    host.run_forever(stop_event, max_requests=1)
    assert lock.entered == 1
    assert lock.exited == 1
    assert server.calls == [(stop_event, 1)]
