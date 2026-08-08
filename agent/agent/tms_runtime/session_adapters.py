"""Provider adapter contract and compatibility exports."""

from __future__ import annotations

from typing import Any, Protocol

class SessionProviderAdapter(Protocol):
    def send_code(self) -> dict[str, Any]: ...

    def submit_code(self, code: str) -> dict[str, Any]: ...
from agent.tms_runtime.session_ronghui_adapter import RonghuiSessionAdapter
from agent.tms_runtime.session_yunda_adapter import YundaSessionAdapter


__all__ = ["RonghuiSessionAdapter", "SessionProviderAdapter", "YundaSessionAdapter"]
