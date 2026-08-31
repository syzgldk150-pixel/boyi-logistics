"""Route boundary for the fixed authenticated Harness workspace."""

from __future__ import annotations

from typing import Any


def handle_get(
    app: Any,
    handler: Any,
    path: str,
    _raw_path: str,
    query: dict[str, list[str]],
) -> bool:
    """Render the host-owned Harness page; no plugin route is accepted."""

    if path == "/harness":
        app._render_harness(handler, query)
        return True
    return False


def handle_post(
    app: Any,
    handler: Any,
    path: str,
    _raw_path: str,
    _query: dict[str, list[str]],
) -> bool:
    """Proxy the two closed Harness writes to Agent."""

    if path == "/harness/sessions":
        app._handle_harness_session_create(handler)
        return True
    if path == "/harness/messages":
        app._handle_harness_message_post(handler)
        return True
    return False


__all__ = ["handle_get", "handle_post"]
