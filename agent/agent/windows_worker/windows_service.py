"""Minimal Windows SCM host for the background Worker loop.

No pywin32 dependency is required.  Registration/install remains an explicit
administrator deployment action; importing this module never mutates SCM.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Protocol


_SERVICE_WIN32_OWN_PROCESS = 0x00000010
_SERVICE_STOPPED = 0x00000001
_SERVICE_START_PENDING = 0x00000002
_SERVICE_STOP_PENDING = 0x00000003
_SERVICE_RUNNING = 0x00000004
_SERVICE_ACCEPT_STOP = 0x00000001
_SERVICE_ACCEPT_SHUTDOWN = 0x00000004
_SERVICE_CONTROL_STOP = 0x00000001
_SERVICE_CONTROL_INTERROGATE = 0x00000004
_SERVICE_CONTROL_SHUTDOWN = 0x00000005
_NO_ERROR = 0


class WorkerLoopPort(Protocol):
    def run_forever(self, stop_event: threading.Event, **kwargs: object) -> None: ...


class _ServiceStatus(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
    ]


def run_windows_service(service_name: str, loop: WorkerLoopPort) -> None:
    """Attach the current process to SCM and run until STOP/SHUTDOWN."""

    if os.name != "nt":
        raise RuntimeError("Windows Worker service host requires Windows")
    if not service_name or len(service_name) > 256 or "\x00" in service_name:
        raise ValueError("Windows service name is invalid")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    handler_type = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
    service_main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))

    class _ServiceTableEntry(ctypes.Structure):
        _fields_ = [
            ("lpServiceName", wintypes.LPWSTR),
            ("lpServiceProc", service_main_type),
        ]

    stop_event = threading.Event()
    status_handle = wintypes.HANDLE()
    current_status = _ServiceStatus(
        _SERVICE_WIN32_OWN_PROCESS,
        _SERVICE_START_PENDING,
        0,
        _NO_ERROR,
        0,
        1,
        15000,
    )

    advapi32.SetServiceStatus.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ServiceStatus))
    advapi32.SetServiceStatus.restype = wintypes.BOOL

    def set_status(state: int, *, accepted: int = 0, checkpoint: int = 0, wait_hint: int = 0) -> None:
        current_status.dwCurrentState = state
        current_status.dwControlsAccepted = accepted
        current_status.dwCheckPoint = checkpoint
        current_status.dwWaitHint = wait_hint
        if status_handle and not advapi32.SetServiceStatus(status_handle, ctypes.byref(current_status)):
            raise OSError(ctypes.get_last_error(), "SetServiceStatus failed")

    @handler_type
    def handler(control: int, _event_type: int, _event_data: object, _context: object) -> int:
        if control in {_SERVICE_CONTROL_STOP, _SERVICE_CONTROL_SHUTDOWN}:
            try:
                set_status(_SERVICE_STOP_PENDING, checkpoint=1, wait_hint=15000)
            finally:
                stop_event.set()
            return _NO_ERROR
        if control == _SERVICE_CONTROL_INTERROGATE:
            try:
                set_status(
                    int(current_status.dwCurrentState),
                    accepted=int(current_status.dwControlsAccepted),
                    checkpoint=int(current_status.dwCheckPoint),
                    wait_hint=int(current_status.dwWaitHint),
                )
            except OSError:
                pass
        return _NO_ERROR

    advapi32.RegisterServiceCtrlHandlerExW.argtypes = (
        wintypes.LPCWSTR,
        handler_type,
        wintypes.LPVOID,
    )
    advapi32.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE

    @service_main_type
    def service_main(_argc: int, _argv: object) -> None:
        nonlocal status_handle
        status_handle = advapi32.RegisterServiceCtrlHandlerExW(service_name, handler, None)
        if not status_handle:
            return
        set_status(
            _SERVICE_RUNNING,
            accepted=_SERVICE_ACCEPT_STOP | _SERVICE_ACCEPT_SHUTDOWN,
        )
        exit_code = _NO_ERROR
        try:
            loop.run_forever(stop_event)
        except Exception:
            exit_code = 1
        finally:
            current_status.dwWin32ExitCode = exit_code
            set_status(_SERVICE_STOPPED)

    table = (_ServiceTableEntry * 2)()
    table[0].lpServiceName = service_name
    table[0].lpServiceProc = service_main
    table[1].lpServiceName = None
    table[1].lpServiceProc = service_main_type()
    advapi32.StartServiceCtrlDispatcherW.argtypes = (ctypes.POINTER(_ServiceTableEntry),)
    advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
    if not advapi32.StartServiceCtrlDispatcherW(table):
        raise OSError(ctypes.get_last_error(), "StartServiceCtrlDispatcherW failed")


def run_console_worker(loop: WorkerLoopPort, stop_event: threading.Event | None = None) -> None:
    """Explicit foreground host for development/diagnostics, never a fallback."""

    loop.run_forever(stop_event or threading.Event())
