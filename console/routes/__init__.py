"""Route boundary for the Console HTTP server.

The Console deliberately keeps ``ThreadingHTTPServer``.  These routers only
own path dispatch; domain operations remain on the application service until
they can be moved without changing an externally visible HTTP behaviour.
"""

from __future__ import annotations

from typing import Any

from . import (
    auth,
    automation,
    control_plane,
    customer_service,
    finance,
    llm_settings,
    monitoring,
    ocr,
    receipts,
    waybills,
)


class ConsoleRouteDispatcher:
    """Dispatch authenticated Console routes by bounded business area."""

    _ROUTERS = (
        auth,
        control_plane,
        llm_settings,
        automation,
        monitoring,
        finance,
        customer_service,
        receipts,
        ocr,
        waybills,
    )

    def handle_get(
        self,
        app: Any,
        handler: Any,
        path: str,
        raw_path: str,
        query: dict[str, list[str]],
    ) -> bool:
        for router in self._ROUTERS:
            if router.handle_get(app, handler, path, raw_path, query):
                return True
        return False

    def handle_post(
        self,
        app: Any,
        handler: Any,
        path: str,
        raw_path: str,
        query: dict[str, list[str]],
    ) -> bool:
        for router in self._ROUTERS:
            if router.handle_post(app, handler, path, raw_path, query):
                return True
        return False
