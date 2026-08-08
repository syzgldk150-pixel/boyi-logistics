"""Ronghui TMS runtime hosted inside the published agent service."""

from __future__ import annotations

from typing import Any


__all__ = ["router", "get_session_broker"]


def __getattr__(name: str) -> Any:
    if name == "router":
        from agent.tms_runtime.routes import router

        return router
    if name == "get_session_broker":
        from agent.tms_runtime.session_broker import get_session_broker

        return get_session_broker
    raise AttributeError(name)
