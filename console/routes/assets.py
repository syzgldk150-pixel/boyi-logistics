"""Static and generated runtime asset routes."""

from __future__ import annotations

from typing import Any


def handle_public_get(
    app: Any,
    handler: Any,
    path: str,
    _raw_path: str,
    _query: dict[str, list[str]],
) -> bool:
    if path.startswith("/static/"):
        app._serve_static_file(handler, path[len("/static/") :])
        return True
    return False


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path.startswith("/runtime/"):
        app._serve_runtime_file(handler, path[len("/runtime/") :])
        return True
    return False


def handle_post(
    _app: Any,
    _handler: Any,
    _path: str,
    _raw_path: str,
    _query: dict[str, list[str]],
) -> bool:
    return False
