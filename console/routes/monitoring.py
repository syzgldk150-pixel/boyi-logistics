"""Console portal and operational-monitoring routes."""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path in {"/", "/portal"}:
        app._render_portal(handler, query)
        return True
    if path == "/monitoring/summary":
        app._handle_monitoring_summary(handler, query)
        return True
    if path == "/monitoring/daily-sign":
        app._handle_monitoring_daily_sign(handler, query)
        return True
    if path == "/monitoring/stream":
        app._handle_monitoring_stream(handler, query)
        return True
    if path == "/monitoring/detail-link":
        app._handle_monitoring_detail_link(handler, query)
        return True
    return False


def handle_post(_app: Any, _handler: Any, _path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    return False
