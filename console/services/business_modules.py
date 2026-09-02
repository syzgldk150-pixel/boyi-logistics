"""Fixed-module navigation and the super-admin system-status projection.

The legacy business-module lifecycle remains available only through its
read-only Agent compatibility API. It is deliberately not a Console routing,
navigation, or command-admission dependency.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from console.app_support import *  # noqa: F403
from console.navigation import CONSOLE_CONTROL_PLANE_NAVIGATION, CONSOLE_NAVIGATION


_SYSTEM_HEALTH_COMPONENTS = (
    ("mysql", "业务数据库"),
    ("scheduler", "调度器"),
    ("workflow_runner", "工作流执行器"),
    ("automation_plugins", "自动化扩展"),
    ("automation_workers", "自动化工作节点"),
    ("tms_session", "融辉登录会话"),
)
_UNAVAILABLE = "不可用"
_CURRENT_REQUEST_USER = object()


class BusinessModulesServiceMixin:
    """Keep fixed menus independent from legacy lifecycle records."""

    @staticmethod
    def _can_see_system_status_navigation(user: Mapping[str, Any] | None) -> bool:
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
        control_plane = []
        if self._can_see_system_status_navigation(normalized_user):
            control_plane.extend(
                item
                for item in CONSOLE_CONTROL_PLANE_NAVIGATION
                if item["route"] == "/settings/system-status"
            )
        return (*CONSOLE_NAVIGATION, *control_plane)

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
        return BusinessModulesServiceMixin._can_see_system_status_navigation(user)

    @staticmethod
    def _health_field(value: object) -> str | int | float:
        if isinstance(value, bool):
            return "正常" if value else "异常"
        if isinstance(value, (str, int, float)):
            return value
        return _UNAVAILABLE

    @classmethod
    def _health_component_value(cls, value: object) -> str | int | float:
        if isinstance(value, Mapping):
            for key in ("state", "status", "healthy", "ok"):
                if key in value:
                    return cls._health_field(value.get(key))
            return _UNAVAILABLE
        return cls._health_field(value)

    def _system_status_snapshot(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        result = self._agent_request(
            "GET",
            "/internal/v1/health",
            timeout=4,
            console_principal=self._mysql_console_principal(
                getattr(handler, "current_admin_user", None)
            ),
        )
        payload = result.get("data") if isinstance(result.get("data"), Mapping) else {}
        available = bool(result.get("ok")) and bool(payload)
        components = payload.get("components") if isinstance(payload.get("components"), Mapping) else {}
        return {
            "available": available,
            "status": self._health_field(payload.get("status")) if available else _UNAVAILABLE,
            "release_sha": self._health_field(payload.get("release_sha")) if available else _UNAVAILABLE,
            "instance_id": self._health_field(payload.get("instance_id")) if available else _UNAVAILABLE,
            "uptime": self._health_field(payload.get("uptime")) if available else _UNAVAILABLE,
            "memory_mb": self._health_field(payload.get("memory_mb")) if available else _UNAVAILABLE,
            "components": tuple(
                {
                    "key": key,
                    "label": label,
                    "value": self._health_component_value(components.get(key)) if available else _UNAVAILABLE,
                }
                for key, label in _SYSTEM_HEALTH_COMPONENTS
            ),
        }

    def _render_system_status(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict[str, list[str]],
    ) -> None:
        if not self._is_module_super_admin(handler):
            self._send_json(
                handler,
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error_code": "SUPER_ADMIN_REQUIRED", "message": "只有超级管理员可以查看系统状态。"},
            )
            return
        template = self.template_env.get_template("admin_accounts.html")
        body = template.render(
            app_title=self.settings.app_title,
            system_status_only=True,
            system_health=self._system_status_snapshot(handler),
            is_super_admin=True,
            users=(),
            feishu_binding={},
            binding_challenge=None,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

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
