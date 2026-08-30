"""Compatibility routes for the retired fixed-module lifecycle Console UI."""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/settings/modules":
        app._redirect(handler, "/settings/system-status")
        return True
    if path == "/settings/modules/data":
        app._handle_legacy_business_modules_data(handler)
        return True
    if path.startswith("/settings/modules/") and path.endswith("/data"):
        app._handle_legacy_business_modules_data(handler, path.split("/")[3])
        return True
    if path.startswith("/settings/modules/") and path.endswith("/audit"):
        app._handle_legacy_business_modules_data(handler, path.split("/")[3], audit=True)
        return True
    return False


def handle_post(_app: Any, _handler: Any, _path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    return False
