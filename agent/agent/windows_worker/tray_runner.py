"""Interactive-session runner boundary for Office/browser/UI automation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping, Protocol, runtime_checkable

from agent.automation_plugins.errors import WorkerProtocolError
from agent.windows_worker.models import InteractiveSessionState, WorkerJob, WorkerJobType
from agent.windows_worker.windows_session import current_interactive_session_state


@runtime_checkable
class InstanceProcessRunnerPort(Protocol):
    def run(self, job: WorkerJob) -> Mapping[str, Any]: ...

    def cleanup(self, job: WorkerJob) -> Mapping[str, Any]: ...


class FailClosedInstanceProcessRunner:
    """Explicit blocker until a closed browser/Office adapter is injected."""

    @staticmethod
    def _unavailable() -> WorkerProtocolError:
        return WorkerProtocolError(
            "No closed Windows action adapter is installed",
            code="TRAY_ACTION_ADAPTER_UNAVAILABLE",
        )

    def run(self, job: WorkerJob) -> Mapping[str, Any]:
        del job
        raise self._unavailable()

    def cleanup(self, job: WorkerJob) -> Mapping[str, Any]:
        del job
        raise self._unavailable()


class InteractiveTrayRunner:
    """Runs only instance-bound jobs in the logged-in desktop session."""

    def __init__(
        self,
        process_runner: InstanceProcessRunnerPort,
        *,
        session_state_provider: Callable[[], InteractiveSessionState] = (
            current_interactive_session_state
        ),
    ) -> None:
        self._process_runner = process_runner
        self._session_state_provider = session_state_provider

    def _current_session_state(self) -> InteractiveSessionState:
        state = self._session_state_provider()
        if not isinstance(state, InteractiveSessionState):
            raise WorkerProtocolError(
                "Tray Runner session probe returned an invalid state",
                code="INTERACTIVE_SESSION_UNAVAILABLE",
            )
        return state

    def session_state(self) -> str:
        return self._current_session_state().value

    def _require_interactive_session(self, job: WorkerJob) -> None:
        if (
            job.requires_interactive_session
            and self._current_session_state() != InteractiveSessionState.AVAILABLE
        ):
            raise WorkerProtocolError(
                "Interactive Windows session is unavailable",
                code="INTERACTIVE_SESSION_UNAVAILABLE",
            )

    def run_instance_action(self, job: WorkerJob) -> Mapping[str, Any]:
        if job.job_type != WorkerJobType.INVOKE:
            raise WorkerProtocolError("Tray Runner accepts INVOKE jobs only")
        self._require_interactive_session(job)
        return self._process_runner.run(job)

    def cleanup_instance(self, job: WorkerJob) -> Mapping[str, Any]:
        if job.job_type not in {WorkerJobType.UNINSTALL, WorkerJobType.CLEANUP}:
            raise WorkerProtocolError("Tray Runner cleanup requires UNINSTALL or CLEANUP")
        self._require_interactive_session(job)
        return self._process_runner.cleanup(job)

    # NamedPipeTrayServer uses the narrow TrayActionPort names.  Keep these
    # aliases explicit so no dynamic getattr or arbitrary command dispatch is
    # introduced at the Service/Tray boundary.
    def run(self, job: WorkerJob) -> Mapping[str, Any]:
        return self.run_instance_action(job)

    def cleanup(self, job: WorkerJob) -> Mapping[str, Any]:
        return self.cleanup_instance(job)
