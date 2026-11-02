"""Signed Console request context shared by pages that call the Agent."""

from __future__ import annotations

from console.app_support import *  # noqa: F403
from console.services.agent_api import mysql_console_principal


CONTROL_PLANE_JSON_MAX_BYTES = 256 * 1024
CONTROL_PLANE_FORWARDABLE_STATUSES = {
    HTTPStatus.BAD_REQUEST,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.SERVICE_UNAVAILABLE,
}


class ControlPlaneServiceMixin:
    def _control_plane_write_context(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> dict[str, Any] | None:
        return self._control_plane_session_context(handler, require_same_origin=True)

    @staticmethod
    def _control_plane_agent_body_context(context: dict[str, Any]) -> dict[str, Any]:
        """Copy only audited actor fields; never serialize the signing marker."""

        return {
            "actor": context["actor"],
            "actor_roles": list(context.get("actor_roles") or []),
            "source": "console",
        }

    def _control_plane_read_context(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> dict[str, Any] | None:
        return self._control_plane_session_context(handler, require_same_origin=False)

    def _control_plane_session_context(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        require_same_origin: bool,
    ) -> dict[str, Any] | None:
        user = getattr(handler, "current_admin_user", None)
        actor = mysql_console_principal(user)
        if actor is None:
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "MYSQL_ADMIN_SESSION_REQUIRED",
                "控制平面写操作仅允许已登录的管理员会话。",
            )
            return None
        if require_same_origin and not self._require_same_origin_write(handler):
            return None

        role = str(user.get("control_plane_role") or "admin").strip().lower()
        if role not in {"admin", "super_admin"}:
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "INVALID_CONTROL_PLANE_ROLE",
                "当前管理员没有有效的控制平面角色。",
            )
            return None
        roles = list(actor["roles"])
        return {
            "actor": actor,
            "actor_roles": roles,
            "source": "console",
            "_console_principal": actor,
        }

    def _read_control_plane_json(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> dict[str, Any] | None:
        content_type = str(handler.headers.get("Content-Type") or "").lower()
        media_type = content_type.partition(";")[0].strip()
        if media_type != "application/json":
            self._control_plane_error(
                handler,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "JSON_REQUIRED",
                "控制平面写操作仅接受 application/json。",
            )
            return None
        try:
            content_length = int(handler.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            content_length = -1
        if content_length < 0 or content_length > CONTROL_PLANE_JSON_MAX_BYTES:
            self._control_plane_error(
                handler,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "REQUEST_TOO_LARGE",
                "请求体超过允许大小。",
            )
            return None
        raw = handler.rfile.read(content_length) if content_length else b"{}"
        try:
            values = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            values = None
        if not isinstance(values, dict):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_JSON",
                "请求内容必须是有效的结构化数据。",
            )
            return None
        return values

    def _control_plane_agent_error(
        self,
        handler: BaseHTTPRequestHandler,
        result: dict[str, Any],
    ) -> None:
        try:
            upstream_status = HTTPStatus(int(result.get("status")))
        except (TypeError, ValueError):
            upstream_status = HTTPStatus.BAD_GATEWAY
        status = (
            upstream_status
            if upstream_status in CONTROL_PLANE_FORWARDABLE_STATUSES
            else HTTPStatus.BAD_GATEWAY
        )
        code = self._bounded_text(result.get("error_code"), 128).upper()
        self._control_plane_error(
            handler,
            status,
            code or "AGENT_UPSTREAM_ERROR",
            self._bounded_text(result.get("error"), 1000)
            or "智能服务控制平面暂时不可用。",
            data=result.get("data"),
        )

    def _control_plane_success(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        data: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        payload = {"ok": True, "data": data, "error": None}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            handler,
            status,
            body,
            "application/json; charset=utf-8",
            cache_control="no-store",
            extra_headers=extra_headers,
        )

    def _control_plane_error(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        data: Any = None,
    ) -> None:
        self._send_json(
            handler,
            status,
            {
                "ok": False,
                "data": data,
                "error": {"code": code, "message": message},
            },
        )
