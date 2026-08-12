"""Global LLM settings routes."""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/settings/llm":
        app._render_llm_settings(handler, query)
        return True
    if path == "/settings/llm/status":
        app._handle_llm_settings_get(handler, "status")
        return True
    if path == "/settings/llm/audit":
        app._handle_llm_settings_get(handler, "audit")
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    action = {
        "/settings/llm/candidates": "save",
        "/settings/llm/models/refresh": "refresh",
        "/settings/llm/test": "test",
        "/settings/llm/activate": "activate",
        "/settings/llm/rollback": "rollback",
        "/settings/llm/credentials/clear": "clear",
    }.get(path)
    if action:
        app._handle_llm_settings_post(handler, action)
        return True
    return False
