"""Authenticated Console proxy for the fixed, read-only Harness surface.

The Console does not host an agent, choose tools, or carry project identity. It
only renders the host-owned page and forwards the two closed Harness requests
to Agent using the existing signed Console-to-Agent adapter. The browser may
send a request UUID, an Agent-issued session UUID, and bounded message text;
all other identity and capability fields remain server-owned.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from console.services.agent_api import mysql_console_principal
from shared.redaction import is_sensitive_key, redact_text


HARNESS_JSON_MAX_BYTES = 32 * 1024
HARNESS_MESSAGE_MAX_CHARS = 4_000
HARNESS_RESPONSE_MAX_DEPTH = 8
HARNESS_RESPONSE_MAX_ITEMS = 128
HARNESS_RESPONSE_MAX_TEXT = 8_192

_HARNESS_SESSION_FIELDS = frozenset({"request_uuid"})
_HARNESS_MESSAGE_FIELDS = frozenset({"request_uuid", "session_id", "message"})
_HARNESS_TOP_LEVEL_RESPONSE_FIELDS = frozenset(
    {
        "session_id",
        "request_uuid",
        "persistence_status",
        "status",
        "availability",
        "process",
        "evidence",
        "result",
        "tool_summaries",
        "tools",
        "assistant_message",
        "message_id",
        "created_at",
        "read_only",
        "tool_calls",
        "blocked_reason",
        "next_poll_after_ms",
    }
)
_HARNESS_FORBIDDEN_RESPONSE_KEYS = frozenset(
    {
        "automation_id",
        "service",
        "operation",
        "account_id",
        "resource_id",
        "provider_id",
        "actor",
        "actor_roles",
        "tool_name",
        "command_type",
        "task_id",
        "plan_hash",
        "contract_hash",
        "contract_id",
        "contribution_id",
        "file",
        "path",
        "file_path",
        "filename",
        "module",
        "package",
        "package_path",
        "plugin_id",
        "source_code",
        "source",
    }
)
_HARNESS_ERROR_STATUS = {
    HTTPStatus.BAD_REQUEST,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
}
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_.-]{0,95}$")


class _DuplicateHarnessKey(ValueError):
    """Raised when a request object repeats a field name."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateHarnessKey(key)
        result[key] = value
    return result


def _canonical_uuid(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical UUID")
    return value


def _bounded_error_code(value: object) -> str:
    candidate = str(value or "HARNESS_UPSTREAM_ERROR").strip().upper()
    return candidate if _SAFE_ERROR_CODE.fullmatch(candidate) else "HARNESS_UPSTREAM_ERROR"


def _bounded_error_message(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("message")
    message = redact_text(value if isinstance(value, str) else "Harness 服务暂时不可用。").strip()
    return message[:500] or "Harness 服务暂时不可用。"


def _safe_response_value(value: object, *, depth: int = 0) -> Any:
    """Copy only finite JSON and reject forbidden fields before browser output."""

    if depth > HARNESS_RESPONSE_MAX_DEPTH:
        raise ValueError("Harness response is too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Harness response contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > HARNESS_RESPONSE_MAX_TEXT:
            raise ValueError("Harness response text is too long")
        return redact_text(value)
    if isinstance(value, Mapping):
        if len(value) > HARNESS_RESPONSE_MAX_ITEMS:
            raise ValueError("Harness response object is too large")
        projected: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized_key = key.strip().lower()
            if (
                normalized_key in _HARNESS_FORBIDDEN_RESPONSE_KEYS
                or (
                    is_sensitive_key(key)
                    and normalized_key
                    not in {"persistence_status", "session_id", "request_uuid", "message_id"}
                )
            ):
                raise ValueError("Harness response contains a forbidden field")
            projected[key] = _safe_response_value(raw_value, depth=depth + 1)
        return projected
    if isinstance(value, (list, tuple)):
        if len(value) > HARNESS_RESPONSE_MAX_ITEMS:
            raise ValueError("Harness response list is too large")
        return [_safe_response_value(item, depth=depth + 1) for item in value]
    raise ValueError("Harness response contains a non-JSON value")


def _project_harness_response(data: object) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("Harness response data must be an object")
    unknown = set(data) - _HARNESS_TOP_LEVEL_RESPONSE_FIELDS
    if unknown:
        raise ValueError("Harness response contains an unsupported top-level field")
    projected = _safe_response_value(data)
    if not isinstance(projected, dict):  # pragma: no cover - guarded above
        raise ValueError("Harness response data must be an object")
    for field_name in ("session_id", "request_uuid", "message_id"):
        if field_name in projected:
            _canonical_uuid(projected[field_name], field_name=field_name)
    return projected


class HarnessServiceMixin:
    """Render and proxy the fixed authenticated Harness workspace."""

    def _render_harness(
        self,
        handler: Any,
        query: dict[str, list[str]],
    ) -> None:
        template = self.template_env.get_template("harness.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=str(query.get("message", [""])[0] or ""),
            message_kind=str(query.get("kind", ["info"])[0] or "info"),
        )
        self._send_html(handler, body)

    def _harness_write_context(self, handler: Any) -> dict[str, Any] | None:
        user = getattr(handler, "current_admin_user", None)
        principal_builder = getattr(self, "_mysql_console_principal", mysql_console_principal)
        try:
            principal = principal_builder(user) if callable(principal_builder) else None
        except Exception:
            principal = None
        if principal is None:
            self._harness_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "MYSQL_ADMIN_SESSION_REQUIRED",
                "Harness 写操作仅允许已登录的管理员会话。",
            )
            return None
        if not self._require_same_origin_write(handler):
            return None
        return {"_console_principal": principal}

    def _read_harness_json(
        self,
        handler: Any,
        *,
        allowed_fields: frozenset[str],
    ) -> dict[str, Any] | None:
        content_type = str(handler.headers.get("Content-Type") or "").lower()
        if content_type.partition(";")[0].strip() != "application/json":
            self._harness_error(
                handler,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "JSON_REQUIRED",
                "Harness 请求只接受 application/json。",
            )
            return None
        try:
            content_length = int(handler.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            content_length = -1
        if content_length < 0 or content_length > HARNESS_JSON_MAX_BYTES:
            self._harness_error(
                handler,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "REQUEST_TOO_LARGE",
                "Harness 请求体超过允许大小。",
            )
            return None
        raw = handler.rfile.read(content_length) if content_length else b""
        if len(raw) != content_length:
            self._harness_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_JSON",
                "Harness 请求体长度与声明不一致。",
            )
            return None
        try:
            values = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateHarnessKey):
            self._harness_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_JSON",
                "Harness 请求体必须是无重复字段的 JSON 对象。",
            )
            return None
        if not isinstance(values, dict) or set(values) != set(allowed_fields):
            self._harness_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_HARNESS_REQUEST",
                "Harness 请求字段不符合闭合合同。",
            )
            return None
        return values

    def _handle_harness_session_create(self, handler: Any) -> None:
        context = self._harness_write_context(handler)
        if context is None:
            return
        values = self._read_harness_json(handler, allowed_fields=_HARNESS_SESSION_FIELDS)
        if values is None:
            return
        try:
            request_uuid = _canonical_uuid(values.get("request_uuid"), field_name="request_uuid")
        except ValueError as exc:
            self._harness_error(handler, HTTPStatus.BAD_REQUEST, "REQUEST_UUID_INVALID", str(exc))
            return
        result = self._agent_request(
            "POST",
            "/internal/v1/harness/sessions",
            payload={"request_uuid": request_uuid},
            timeout=getattr(self.settings, "agent_timeout_seconds", 30),
            console_principal=context["_console_principal"],
        )
        self._forward_harness_result(handler, result, required_fields=("session_id",))

    def _handle_harness_message_post(self, handler: Any) -> None:
        context = self._harness_write_context(handler)
        if context is None:
            return
        values = self._read_harness_json(handler, allowed_fields=_HARNESS_MESSAGE_FIELDS)
        if values is None:
            return
        try:
            request_uuid = _canonical_uuid(values.get("request_uuid"), field_name="request_uuid")
            session_id = _canonical_uuid(values.get("session_id"), field_name="session_id")
        except ValueError as exc:
            self._harness_error(handler, HTTPStatus.BAD_REQUEST, "UUID_INVALID", str(exc))
            return
        message = values.get("message")
        if (
            not isinstance(message, str)
            or len(message) > HARNESS_MESSAGE_MAX_CHARS
            or not message.strip()
        ):
            self._harness_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "MESSAGE_INVALID",
                f"消息不能为空且不得超过 {HARNESS_MESSAGE_MAX_CHARS} 个字符。",
            )
            return
        result = self._agent_request(
            "POST",
            "/internal/v1/harness/messages",
            payload={
                "request_uuid": request_uuid,
                "session_id": session_id,
                "message": message,
            },
            timeout=getattr(self.settings, "agent_timeout_seconds", 30),
            console_principal=context["_console_principal"],
        )
        self._forward_harness_result(handler, result, required_fields=("session_id",))

    # Small aliases keep the route boundary descriptive while preserving a
    # convenient service method for focused callers and tests.
    def _handle_harness_session(self, handler: Any) -> None:
        self._handle_harness_session_create(handler)

    def _handle_harness_message(self, handler: Any) -> None:
        self._handle_harness_message_post(handler)

    def _forward_harness_result(
        self,
        handler: Any,
        result: Mapping[str, Any],
        *,
        required_fields: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(result, Mapping):
            self._harness_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_HARNESS_RESPONSE",
                "Agent 返回了无效的 Harness 响应。",
            )
            return
        if result.get("ok") is not True:
            try:
                upstream_status = HTTPStatus(int(result.get("status")))
            except (TypeError, ValueError):
                upstream_status = HTTPStatus.BAD_GATEWAY
            status = upstream_status if upstream_status in _HARNESS_ERROR_STATUS else HTTPStatus.BAD_GATEWAY
            code = _bounded_error_code(result.get("error_code"))
            message = _bounded_error_message(result.get("error"))
            self._harness_error(handler, status, code, message)
            return
        try:
            data = _project_harness_response(result.get("data"))
        except (TypeError, ValueError):
            self._harness_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_HARNESS_RESPONSE",
                "Agent 返回了无效的 Harness 数据。",
            )
            return
        if any(field_name not in data for field_name in required_fields):
            self._harness_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_HARNESS_RESPONSE",
                "Agent 返回的 Harness 数据缺少必要字段。",
            )
            return
        try:
            upstream_status = HTTPStatus(int(result.get("status")))
        except (TypeError, ValueError):
            upstream_status = HTTPStatus.OK
        if upstream_status not in {HTTPStatus.OK, HTTPStatus.ACCEPTED}:
            self._harness_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_HARNESS_RESPONSE",
                "Agent 返回了不一致的 Harness 状态。",
            )
            return
        status = upstream_status
        self._send_json(handler, status, {"ok": True, "data": data, "error": None})

    def _harness_error(
        self,
        handler: Any,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> None:
        safe_code = _bounded_error_code(code)
        safe_message = _bounded_error_message(message)
        self._send_json(
            handler,
            status,
            {
                "ok": False,
                "data": None,
                "error": {"code": safe_code, "message": safe_message},
            },
        )


__all__ = [
    "HARNESS_JSON_MAX_BYTES",
    "HARNESS_MESSAGE_MAX_CHARS",
    "HarnessServiceMixin",
]
