"""Console UI and authorization boundary for global LLM settings."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import urlparse

from console.app_support import *  # noqa: F403


class LLMSettingsServiceMixin:
    def _is_super_admin(self, handler: BaseHTTPRequestHandler) -> bool:
        user = getattr(handler, "current_admin_user", None) or current_admin_user() or {}
        return (
            not bool(user.get("is_legacy_basic_auth"))
            and str(user.get("role") or "") == "super_admin"
            and int(user.get("id") or 0) > 0
        )

    def _require_llm_super_admin(self, handler: BaseHTTPRequestHandler) -> bool:
        if self._is_super_admin(handler):
            return True
        self._send_json(
            handler,
            HTTPStatus.FORBIDDEN,
            {
                "ok": False,
                "error_code": "SUPER_ADMIN_REQUIRED",
                "message": "Only a super administrator can change global model settings.",
            },
        )
        return False

    def _require_same_origin_write(self, handler: BaseHTTPRequestHandler) -> bool:
        host = str(handler.headers.get("Host") or "").strip().lower()
        source = str(handler.headers.get("Origin") or handler.headers.get("Referer") or "").strip()
        parsed = urlparse(source)
        if host and parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host:
            return True
        self._send_json(
            handler,
            HTTPStatus.FORBIDDEN,
            {
                "ok": False,
                "error_code": "CSRF_ORIGIN_REJECTED",
                "message": "The model settings write request did not pass same-origin validation.",
            },
        )
        return False

    @staticmethod
    def _llm_actor(handler: BaseHTTPRequestHandler) -> str:
        user = getattr(handler, "current_admin_user", None) or current_admin_user() or {}
        return str(user.get("username") or user.get("display_name") or "").strip()

    def _render_llm_settings(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        template = self.template_env.get_template("llm_settings.html")
        body = template.render(
            app_title=self.settings.app_title,
            is_super_admin=self._is_super_admin(handler),
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _handle_llm_settings_get(self, handler: BaseHTTPRequestHandler, action: str) -> None:
        if action == "audit" and not self._require_llm_super_admin(handler):
            return
        endpoint = "/internal/v1/admin/llm/audit" if action == "audit" else "/internal/v1/admin/llm/config"
        result = self._agent_request("GET", endpoint, timeout=12)
        if not result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error_code": str(result.get("error_code") or "LLM_SETTINGS_UNAVAILABLE"),
                    "message": str(result.get("error") or "Model settings are temporarily unavailable."),
                },
            )
            return
        payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        if action == "status" and not self._is_super_admin(handler):
            runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
            active = payload.get("active") if isinstance(payload.get("active"), dict) else None
            safe_active = None if active is None else {
                "provider": str(active.get("provider") or ""),
                "model_id": str(active.get("model_id") or ""),
                "status": str(active.get("status") or ""),
                "tested_at": str(active.get("tested_at") or ""),
                "test_passed": bool((active.get("test_result") or {}).get("passed"))
                if isinstance(active.get("test_result"), dict)
                else False,
            }
            payload = {
                "active": safe_active,
                "runtime": {
                    "configured": bool(runtime.get("configured")),
                    "provider": runtime.get("provider"),
                    "model": runtime.get("model"),
                    "source": runtime.get("source"),
                    "health": runtime.get("health"),
                },
                "providers": [
                    {"provider": str(item.get("provider") or ""), "configured": bool(item.get("configured"))}
                    for item in payload.get("providers", []) if isinstance(item, dict)
                ],
                "environment_managed": bool(payload.get("environment_managed")),
                "read_only": True,
            }
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": payload})

    def _handle_llm_settings_post(self, handler: BaseHTTPRequestHandler, action: str) -> None:
        if not self._require_llm_super_admin(handler) or not self._require_same_origin_write(handler):
            return
        body = self._parse_json_body(handler)
        actor = self._llm_actor(handler)
        if not actor:
            self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "message": "Administrator identity is unavailable."})
            return
        endpoint = {
            "save": "/internal/v1/admin/llm/candidates",
            "refresh": "/internal/v1/admin/llm/models/refresh",
            "test": "/internal/v1/admin/llm/test",
            "activate": "/internal/v1/admin/llm/activate",
            "rollback": "/internal/v1/admin/llm/rollback",
            "clear": "/internal/v1/admin/llm/credentials/clear",
        }.get(action)
        if endpoint is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "Unknown model settings action."})
            return
        payload = dict(body)
        payload["actor"] = actor
        result = self._agent_request("POST", endpoint, payload=payload, timeout=100 if action == "test" else 40)
        if not result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "ok": False,
                    "error_code": str(result.get("error_code") or "LLM_SETTINGS_WRITE_FAILED"),
                    "message": str(result.get("error") or "The model settings change was not applied."),
                },
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result.get("data")})
