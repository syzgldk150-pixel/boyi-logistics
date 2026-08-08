"""Provider boundaries used by :class:`SessionBroker`.

The broker remains the compatibility façade for existing callers.  Provider
selection is isolated here so Ronghui and Yunda login flows can evolve without
teaching schedulers or tools about provider-specific private methods.
"""

from __future__ import annotations

from typing import Any, Protocol


class SessionProviderAdapter(Protocol):
    def send_code(self) -> dict[str, Any]: ...

    def submit_code(self, code: str) -> dict[str, Any]: ...


class RonghuiSessionAdapter:
    def __init__(self, broker: Any) -> None:
        self._broker = broker

    def send_code(self) -> dict[str, Any]:
        return self._broker._send_code_ronghui()

    def submit_code(self, code: str) -> dict[str, Any]:
        return self._broker._submit_code_ronghui(code)


class YundaSessionAdapter:
    def __init__(self, broker: Any) -> None:
        self._broker = broker

    def send_code(self) -> dict[str, Any]:
        return self._broker._send_code_yunda()

    def submit_code(self, code: str) -> dict[str, Any]:
        return self._broker._submit_code_yunda(code)
