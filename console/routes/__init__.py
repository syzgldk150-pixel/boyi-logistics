"""Route boundary for the Console HTTP server.

The Console deliberately keeps ``ThreadingHTTPServer``.  These routers only
own path dispatch; domain operations remain on the application service until
they can be moved without changing an externally visible HTTP behaviour.
"""

from __future__ import annotations

from typing import Any

from . import assets, auth, automation, customer_service, documents, finance, monitoring, ocr, receipts, waybills


class ConsoleRouteDispatcher:
    """Dispatch authenticated Console routes by bounded business area."""

    _ROUTERS = (
        auth,
        automation,
        monitoring,
        finance,
        customer_service,
        receipts,
        ocr,
        waybills,
        documents,
        assets,
    )

    def handle_public_get(
        self,
        app: Any,
        handler: Any,
        path: str,
        raw_path: str,
        query: dict[str, list[str]],
    ) -> bool:
        """Dispatch routes that intentionally do not require a Console session."""

        for router in (auth, assets):
            route_handler = getattr(router, "handle_public_get", None)
            if route_handler and route_handler(app, handler, path, raw_path, query):
                return True
        return False

    def handle_public_post(
        self,
        app: Any,
        handler: Any,
        path: str,
        raw_path: str,
        query: dict[str, list[str]],
    ) -> bool:
        """Dispatch unauthenticated form actions such as login."""

        route_handler = getattr(auth, "handle_public_post", None)
        return bool(route_handler and route_handler(app, handler, path, raw_path, query))

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

    def handle_write(
        self,
        app: Any,
        handler: Any,
        path: str,
        raw_path: str,
        query: dict[str, list[str]],
        method: str,
    ) -> bool:
        """Dispatch authenticated PUT/PATCH/DELETE proxy requests."""

        for router in self._ROUTERS:
            route_handler = getattr(router, "handle_write", None)
            if route_handler and route_handler(app, handler, path, raw_path, query, method):
                return True
        return False
