"""Warehouse monitor routes: page and scan-time lookup (video plays on the warehouse LAN)."""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/warehouse-monitor":
        app._render_warehouse_monitor(handler, query)
        return True
    if path == "/warehouse-monitor/lookup":
        app._handle_warehouse_monitor_lookup(handler, query)
        return True
    return False


def handle_post(_app: Any, _handler: Any, _path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    return False
