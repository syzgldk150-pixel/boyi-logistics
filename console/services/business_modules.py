"""Console proxy, navigation projection, and page/API gate for business modules."""

from __future__ import annotations

from http import HTTPStatus
import posixpath
import time
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from console.app_support import *  # noqa: F403
from console.navigation import CONSOLE_NAVIGATION
from shared.business_modules import BUSINESS_MODULE_BY_CODE, CORE_MODULE_CODES


class BusinessModulesServiceMixin:
    def _business_module_rows(self, handler: BaseHTTPRequestHandler | None = None) -> dict[str, dict[str, Any]] | None:
        cached = getattr(self, "_business_module_status_cache", None)
        if isinstance(cached, tuple) and time.monotonic() - cached[0] < 2:
            return dict(cached[1])
        user = getattr(handler, "current_admin_user", None) if handler is not None else current_admin_user()
        result = self._agent_request(
            "GET",
            "/internal/v1/admin/modules",
            timeout=12,
            console_principal=self._mysql_console_principal(user),
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else None
        if not result.get("ok") or items is None:
            return None
        rows = {str(item.get("module_code")): item for item in items if isinstance(item, dict)}
        self._business_module_status_cache = (time.monotonic(), dict(rows), str(data.get("release_sha") or ""))
        return rows

    def _invalidate_business_module_status_cache(self) -> None:
        self._business_module_status_cache = None

    def _business_module_navigation(self) -> tuple[dict[str, str], ...]:
        rows = self._business_module_rows()
        if rows is None:
            return tuple(item for item in CONSOLE_NAVIGATION if item["route"] == "/" or self._module_code_for_route(item["route"]) in CORE_MODULE_CODES)
        return tuple(
            item for item in CONSOLE_NAVIGATION
            if str((rows.get(self._module_code_for_route(item["route"])) or {}).get("lifecycle_state")) == "ENABLED"
        )

    def _business_module_mobile_nav(self, user: Mapping[str, Any] | None, navigation: list[dict[str, str]]) -> tuple[str, ...]:
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
        """Keep the template global's established one-argument contract."""

        return self._business_module_mobile_nav(
            user,
            list(self._business_module_navigation()),
        )

    @staticmethod
    def _module_code_for_route(route: str) -> str:
        for code, item in BUSINESS_MODULE_BY_CODE.items():
            if route in item.page_contributions:
                return code
        return ""

    @staticmethod
    def _normalized_module_request_path(path: str) -> str | None:
        """Match the runtime file handler's single-decode path semantics.

        Runtime files are resolved from ``runtime_dir`` after one ``unquote``.
        Normalize that decoded POSIX-relative path before module-prefix matching so
        a raw prefix cannot govern a file owned by another module.  A path which
        escapes the runtime root is invalid rather than an unowned path.
        """

        normalized = str(path or "/")
        if not normalized.startswith("/runtime/"):
            return normalized
        decoded_relpath = unquote(normalized[len("/runtime/") :])
        if decoded_relpath.startswith("/") or "\x00" in decoded_relpath:
            return None

        parts: list[str] = []
        for part in decoded_relpath.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    return None
                parts.pop()
                continue
            parts.append(part)

        resolved = posixpath.normpath("/runtime/" + "/".join(parts))
        return "/runtime/" if resolved == "/runtime" else resolved

    @staticmethod
    def _module_code_for_normalized_request_path(normalized: str) -> str:
        """Return an owner for a path already normalized for module gating."""

        if normalized == "/":
            return "overview"
        matches: list[tuple[int, str]] = []
        for code, item in BUSINESS_MODULE_BY_CODE.items():
            for prefix in (*item.page_contributions, *item.api_contributions):
                if prefix != "/" and (normalized == prefix or normalized.startswith(prefix + "/")):
                    matches.append((len(prefix), code))
        return max(matches, default=(0, ""))[1]

    @staticmethod
    def _module_code_for_request(path: str) -> str:
        normalized = BusinessModulesServiceMixin._normalized_module_request_path(path)
        if normalized is None:
            return ""
        return BusinessModulesServiceMixin._module_code_for_normalized_request_path(normalized)

    def _reject_unavailable_business_module_request(self, handler: BaseHTTPRequestHandler, path: str) -> bool:
        if path == "/settings/modules" or path.startswith("/settings/modules/"):
            return False
        normalized = self._normalized_module_request_path(path)
        if normalized is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "error_code": "INVALID_MODULE_RUNTIME_PATH", "message": "运行时文件路径无效。"})
            return True
        module_code = self._module_code_for_normalized_request_path(normalized)
        if not module_code:
            return False
        rows = self._business_module_rows(handler)
        if rows is None:
            if module_code in CORE_MODULE_CODES:
                return False
            self._send_json(handler, HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error_code": "MODULE_STATUS_UNAVAILABLE", "message": "模块状态暂不可用，已拒绝访问可管理模块。"})
            return True
        state = str((rows.get(module_code) or {}).get("lifecycle_state") or "BLOCKED")
        if state == "ENABLED":
            return False
        self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "error_code": "MODULE_UNAVAILABLE", "message": "该模块当前未安装、已停用或被阻断。"})
        return True

    @staticmethod
    def _is_module_super_admin(handler: BaseHTTPRequestHandler) -> bool:
        user = getattr(handler, "current_admin_user", None) or current_admin_user() or {}
        return not bool(user.get("is_legacy_basic_auth")) and str(user.get("role") or "") == "super_admin" and int(user.get("id") or 0) > 0

    def _require_module_manager_super_admin(self, handler: BaseHTTPRequestHandler) -> bool:
        if self._is_module_super_admin(handler):
            return True
        self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error_code": "SUPER_ADMIN_REQUIRED", "message": "只有超级管理员可以访问模块管理。"})
        return False

    def _require_module_same_origin_write(self, handler: BaseHTTPRequestHandler) -> bool:
        source = str(handler.headers.get("Origin") or handler.headers.get("Referer") or "")
        parsed = urlparse(source)
        if str(handler.headers.get("Host") or "") and parsed.scheme in {"http", "https"} and parsed.netloc.lower() == str(handler.headers.get("Host")).lower():
            return True
        self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error_code": "CSRF_ORIGIN_REJECTED", "message": "模块变更必须从同源 Console 页面发起。"})
        return False

    def _render_business_modules(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        if not self._require_module_manager_super_admin(handler):
            return
        rows = self._business_module_rows(handler)
        template = self.template_env.get_template("business_modules.html")
        cached = getattr(self, "_business_module_status_cache", None)
        release_sha = cached[2] if isinstance(cached, tuple) and len(cached) > 2 else ""
        self._send_html(handler, template.render(app_title=self.settings.app_title, items=list((rows or {}).values()), release_sha=release_sha, unavailable=rows is None, is_super_admin=self._is_module_super_admin(handler), message=query.get("message", [""])[0], message_kind=query.get("kind", ["info"])[0]))

    def _handle_business_modules_data(self, handler: BaseHTTPRequestHandler, module_code: str = "", *, audit: bool = False) -> None:
        if not self._require_module_manager_super_admin(handler):
            return
        endpoint = "/internal/v1/admin/modules" + (f"/{module_code}" if module_code else "") + ("/audit" if audit else "")
        result = self._agent_request("GET", endpoint, timeout=12, console_principal=self._mysql_console_principal(getattr(handler, "current_admin_user", None)))
        self._send_json(handler, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY, {"ok": bool(result.get("ok")), "data": result.get("data"), "error": result.get("error")})

    def _handle_business_module_lifecycle(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._is_module_super_admin(handler):
            self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error_code": "SUPER_ADMIN_REQUIRED", "message": "只有超级管理员可以变更模块生命周期。"})
            return
        if not self._require_module_same_origin_write(handler):
            return
        body = self._parse_json_body(handler)
        if set(body) - {"module_code", "action", "reason", "request_id", "expected_record_version"}:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error_code": "INVALID_MODULE_LIFECYCLE_REQUEST", "message": "模块生命周期请求包含不允许的字段。"})
            return
        code = str(body.get("module_code") or "").strip()
        action = str(body.get("action") or "").strip()
        reason = str(body.get("reason") or "").strip()
        request_id = self._normalize_browser_request_uuid(body.get("request_id"))
        version = body.get("expected_record_version")
        if code not in BUSINESS_MODULE_BY_CODE or action not in {"install", "enable", "disable", "upgrade", "uninstall"} or not reason or not request_id or type(version) is not int or version < 1:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error_code": "INVALID_MODULE_LIFECYCLE_REQUEST", "message": "模块、动作、理由、请求标识或版本无效。"})
            return
        result = self._agent_request("POST", f"/internal/v1/admin/modules/{code}/lifecycle", payload={"action": action, "reason": reason, "request_id": request_id, "expected_record_version": version}, timeout=20, console_principal=self._mysql_console_principal(getattr(handler, "current_admin_user", None)))
        if result.get("ok"):
            self._invalidate_business_module_status_cache()
        self._send_json(handler, HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT, {"ok": bool(result.get("ok")), "data": result.get("data"), "error": result.get("error")})
