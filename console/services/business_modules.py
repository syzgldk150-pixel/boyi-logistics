"""Fixed-module navigation and the legacy module read-only compatibility.

The legacy business-module lifecycle remains available only through its
read-only Agent compatibility API. It is deliberately not a Console routing,
navigation, or command-admission dependency.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from console.app_support import *  # noqa: F403
from console.navigation import CONSOLE_NAVIGATION
from shared.identity_routes import console_permission


_CURRENT_REQUEST_USER = object()


class BusinessModulesServiceMixin:
    """Keep fixed menus independent from legacy lifecycle records."""

    @staticmethod
    def _is_real_super_admin(user: Mapping[str, Any] | None) -> bool:
        if not isinstance(user, Mapping):
            return False
        try:
            user_id = int(user.get("id") or 0)
        except (TypeError, ValueError):
            return False
        return (
            not bool(user.get("is_legacy_basic_auth"))
            and str(user.get("role") or "") == "super_admin"
            and user_id > 0
        )

    def _business_module_navigation(
        self,
        user: Mapping[str, Any] | None | object = _CURRENT_REQUEST_USER,
    ) -> tuple[dict[str, str], ...]:
        if user is _CURRENT_REQUEST_USER:
            user = current_admin_user()
        normalized_user = user if isinstance(user, Mapping) else None
        if normalized_user and not self._is_real_super_admin(normalized_user):
            permissions = normalized_user.get("access_permissions", ())
            return tuple(item for item in CONSOLE_NAVIGATION if console_permission("GET", item["route"]) in permissions)
        return CONSOLE_NAVIGATION

    def _business_module_mobile_nav(
        self,
        user: Mapping[str, Any] | None,
        navigation: list[dict[str, str]],
    ) -> tuple[str, ...]:
        candidates = [item["route"] for item in navigation if item["route"] != "/"]
        if len(candidates) < 3:
            return tuple(candidates)
        try:
            stored = json.loads(str((user or {}).get("ui_preferences_json") or "{}"))
        except (TypeError, ValueError):
            stored = {}
        selected = [route for route in stored.get("mobile_bottom_nav", []) if route in candidates]
        repaired = list(dict.fromkeys(selected))
        for route in candidates:
            if len(repaired) >= 3:
                break
            if route not in repaired:
                repaired.append(route)
        return tuple(repaired[:3])

    def _business_module_mobile_navigation_for_user(
        self,
        user: Mapping[str, Any] | None,
    ) -> tuple[str, ...]:
        return self._business_module_mobile_nav(user, list(self._business_module_navigation(user)))

    @staticmethod
    def _is_module_super_admin(handler: BaseHTTPRequestHandler) -> bool:
        user = getattr(handler, "current_admin_user", None) or current_admin_user() or {}
        return BusinessModulesServiceMixin._is_real_super_admin(user)

    def _handle_legacy_business_modules_data(
        self,
        handler: BaseHTTPRequestHandler,
        module_code: str = "",
        *,
        audit: bool = False,
    ) -> None:
        """Keep the old super-admin read surface without restoring its UI."""

        if not self._is_module_super_admin(handler):
            self._send_json(
                handler,
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error_code": "SUPER_ADMIN_REQUIRED", "message": "只有超级管理员可以读取旧模块审计。"},
            )
            return
        endpoint = "/internal/v1/admin/modules"
        if module_code:
            endpoint += f"/{module_code}"
        if audit:
            endpoint += "/audit"
        result = self._agent_request(
            "GET",
            endpoint,
            timeout=12,
            console_principal=self._mysql_console_principal(
                getattr(handler, "current_admin_user", None)
            ),
        )
        self._send_json(
            handler,
            HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY,
            {"ok": bool(result.get("ok")), "data": result.get("data"), "error": result.get("error")},
        )
