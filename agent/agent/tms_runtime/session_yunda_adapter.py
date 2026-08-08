"""Yunda login adapter for the TMS session facade."""

from __future__ import annotations

from typing import Any

from agent.tms_runtime.session_provider_base import ProviderSessionAdapterBase


class YundaSessionAdapter(ProviderSessionAdapterBase):
    def send_code(self) -> dict[str, Any]:
        return self.send_yunda_code()

    def submit_code(self, code: str) -> dict[str, Any]:
        return self.submit_yunda_code(code)
