"""Windows automation Worker protocol and state-machine primitives."""

from agent.windows_worker.coordinator import WorkerCoordinator
from agent.windows_worker.background_service import WindowsWorkerBackgroundService
from agent.windows_worker.configuration import WindowsWorkerConfiguration
from agent.windows_worker.models import (
    DeviceServiceState,
    InteractiveSessionState,
    WorkerJob,
    WorkerJobStatus,
    WorkerJobType,
)

__all__ = [
    "DeviceServiceState",
    "InteractiveSessionState",
    "WorkerCoordinator",
    "WindowsWorkerBackgroundService",
    "WindowsWorkerConfiguration",
    "WorkerJob",
    "WorkerJobStatus",
    "WorkerJobType",
]
