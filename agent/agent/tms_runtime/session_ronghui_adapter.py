"""Ronghui login adapter for the TMS session facade."""

from __future__ import annotations

from typing import Any

from agent.tms_runtime.session_provider_base import ProviderSessionAdapterBase


class RonghuiSessionAdapter(ProviderSessionAdapterBase):
    def send_code(self) -> dict[str, Any]:
        return self.send_ronghui_code()

    def submit_code(self, code: str) -> dict[str, Any]:
        return self.submit_ronghui_code(code)
