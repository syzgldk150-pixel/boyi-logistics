"""Windows interactive-session probe used by the logged-in Tray process."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from agent.windows_worker.models import InteractiveSessionState


_INVALID_SESSION_ID = 0xFFFFFFFF
_GENERIC_READ = 0x80000000


def current_interactive_session_state() -> InteractiveSessionState:
    """Return LOGGED_OUT/LOCKED/AVAILABLE without guessing from process state."""

    if os.name != "nt":
        return InteractiveSessionState.LOGGED_OUT
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    active_session = int(kernel32.WTSGetActiveConsoleSessionId())
    if active_session == _INVALID_SESSION_ID:
        return InteractiveSessionState.LOGGED_OUT
    process_session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(process_session)):
        return InteractiveSessionState.LOGGED_OUT
    if int(process_session.value) != active_session:
        return InteractiveSessionState.LOGGED_OUT
    user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    desktop = user32.OpenInputDesktop(0, False, _GENERIC_READ)
    if not desktop:
        return InteractiveSessionState.LOCKED
    try:
        return InteractiveSessionState.AVAILABLE
    finally:
        user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
        user32.CloseDesktop.restype = wintypes.BOOL
        user32.CloseDesktop(desktop)
