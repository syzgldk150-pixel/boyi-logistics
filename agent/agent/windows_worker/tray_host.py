"""Logged-in Windows Tray host with one authenticated named-pipe endpoint."""

from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Protocol

from agent.automation_plugins.errors import WorkerProtocolError
from agent.windows_worker.configuration import WindowsWorkerConfiguration
from agent.windows_worker.dpapi import read_protected_secret
from agent.windows_worker.models import InteractiveSessionState
from agent.windows_worker.tray_ipc import NamedPipeTrayServer, worker_pipe_address
from agent.windows_worker.tray_runner import (
    FailClosedInstanceProcessRunner,
    InstanceProcessRunnerPort,
    InteractiveTrayRunner,
)
from agent.windows_worker.windows_session import current_interactive_session_state


_ERROR_ALREADY_EXISTS = 183


class TrayServerPort(Protocol):
    def serve_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        max_requests: int | None = None,
    ) -> None: ...


class SingleInstancePort(Protocol):
    def __enter__(self) -> SingleInstancePort: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class WindowsSingleInstance:
    """Per-login-session mutex preventing duplicate Tray hosts."""

    def __init__(self, *, device_id: str) -> None:
        # Reuse the exact device identifier validation used by the pipe.
        worker_pipe_address(device_id)
        self._name = rf"Local\BoyiAutomationTray-{device_id}"
        self._handle: int | None = None

    def __enter__(self) -> WindowsSingleInstance:
        if os.name != "nt":
            raise WorkerProtocolError(
                "Windows Tray host requires Windows",
                code="TRAY_WINDOWS_REQUIRED",
            )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, True, self._name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise WorkerProtocolError(
                "Windows Tray host is already running in this login session",
                code="TRAY_ALREADY_RUNNING",
            )
        self._handle = int(handle)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = wintypes.HANDLE(self._handle)
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)
        self._handle = None


class WindowsTrayHost:
    """Own one Tray process in the active logged-in Windows session."""

    def __init__(
        self,
        *,
        server: TrayServerPort,
        single_instance: SingleInstancePort,
        session_state_provider: Callable[[], InteractiveSessionState],
        platform_name: str = os.name,
    ) -> None:
        self._server = server
        self._single_instance = single_instance
        self._session_state_provider = session_state_provider
        self._platform_name = platform_name

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        max_requests: int | None = None,
    ) -> None:
        if self._platform_name != "nt":
            raise WorkerProtocolError(
                "Windows Tray host requires Windows",
                code="TRAY_WINDOWS_REQUIRED",
            )
        state = self._session_state_provider()
        if state == InteractiveSessionState.LOGGED_OUT:
            raise WorkerProtocolError(
                "Windows Tray host is not running in the active login session",
                code="INTERACTIVE_SESSION_UNAVAILABLE",
            )
        if state not in {InteractiveSessionState.AVAILABLE, InteractiveSessionState.LOCKED}:
            raise WorkerProtocolError(
                "Windows Tray session probe returned an invalid state",
                code="INTERACTIVE_SESSION_UNAVAILABLE",
            )
        with self._single_instance:
            self._server.serve_forever(stop_event, max_requests=max_requests)


def build_windows_tray_host(
    config: WindowsWorkerConfiguration,
    *,
    process_runner: InstanceProcessRunnerPort | None = None,
    session_state_provider: Callable[[], InteractiveSessionState] = (
        current_interactive_session_state
    ),
) -> WindowsTrayHost:
    """Compose the Tray without loading service TLS or device-signing keys.

    A concrete closed browser/Office adapter must be injected by the Windows
    distribution.  The source distribution intentionally installs an
    explicit fail-closed adapter instead of importing project tools or
    executing arbitrary plugin entrypoints.
    """

    entropy = f"boyi-worker:{config.device_id}".encode("utf-8")
    pipe_key = read_protected_secret(
        config.tray_pipe_key_path,
        entropy=entropy + b":tray-pipe",
    )
    actions = InteractiveTrayRunner(
        process_runner or FailClosedInstanceProcessRunner(),
        session_state_provider=session_state_provider,
    )
    server = NamedPipeTrayServer(
        device_id=config.device_id,
        auth_key=pipe_key,
        actions=actions,
    )
    return WindowsTrayHost(
        server=server,
        single_instance=WindowsSingleInstance(device_id=config.device_id),
        session_state_provider=session_state_provider,
    )
