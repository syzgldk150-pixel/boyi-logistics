"""Single Console-to-Agent HTTP boundary."""

import uuid
from urllib.parse import unquote, urlsplit

from console.app_support import *  # noqa: F403
from shared.service_identity import (
    ConsoleIdentityError,
    build_console_identity_headers,
)

_SESSION_USER_UNSET = object()


def mysql_console_principal(
    user: dict[str, Any] | None | object = _SESSION_USER_UNSET,
) -> dict[str, Any] | None:
    """Build an Agent principal only from a real MySQL administrator session."""

    session_user = current_admin_user() if user is _SESSION_USER_UNSET else user
    if not isinstance(session_user, dict) or bool(
        session_user.get("is_legacy_basic_auth")
    ):
        return None
    try:
        actor_id = int(session_user.get("id") or 0)
    except (TypeError, ValueError):
        actor_id = 0
    username = str(session_user.get("username") or "").strip()
    role = str(session_user.get("control_plane_role") or "admin").strip().lower()
    if actor_id <= 0 or not username or role not in {"admin", "super_admin"}:
        return None
    return {
        "actor_type": "console_admin",
        "actor_id": str(actor_id),
        "roles": [role],
        "display_name": str(
            session_user.get("display_name") or username
        ).strip()[:200],
        "authenticated_by": "mysql_admin_session",
    }


class AgentApiServiceMixin:
    @staticmethod
    def _agent_admin_endpoint_requires_principal(endpoint: str) -> bool:
        path = str(urlparse(str(endpoint or "")).path or "/")
        return path in {"/admin", "/internal/v1/admin"} or path.startswith(
            ("/admin/", "/internal/v1/admin/")
        )

    @staticmethod
    def _mysql_console_principal(
        user: dict[str, Any] | None | object = _SESSION_USER_UNSET,
    ) -> dict[str, Any] | None:
        return mysql_console_principal(user)

    @staticmethod
    def _validate_internal_agent_endpoint(endpoint: Any) -> str:
        value = str(endpoint or "")
        parsed = urlsplit(value)
        decoded_path = unquote(parsed.path)
        invalid_segment = any(segment in {".", ".."} for segment in decoded_path.split("/"))
        if (
            not value.startswith("/internal/v1/")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or parsed.path.startswith("//")
            or parsed.path != decoded_path
            or "\\" in decoded_path
            or invalid_segment
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("智能服务接口路径不符合要求")
        return value

    @staticmethod
    def _normalize_browser_request_uuid(value: Any) -> str:
        try:
            return str(uuid.UUID(str(value or "").strip()))
        except (ValueError, AttributeError):
            return ""

    def _send_console_command_receipt(
        self,
        handler: BaseHTTPRequestHandler,
        result: dict[str, Any],
        *,
        message: str,
    ) -> None:
        if not result.get("ok"):
            try:
                upstream_status = HTTPStatus(int(result.get("status")))
            except (TypeError, ValueError):
                upstream_status = HTTPStatus.BAD_GATEWAY
            if upstream_status not in {
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            }:
                upstream_status = HTTPStatus.BAD_GATEWAY
            self._send_json(
                handler,
                upstream_status,
                {
                    "ok": False,
                    "pending": False,
                    "error_code": str(
                        result.get("error_code") or "COMMAND_SUBMIT_FAILED"
                    ),
                    "message": str(result.get("error") or "智能服务任务提交失败。"),
                },
            )
            return

        receipt = result.get("data") if isinstance(result.get("data"), dict) else {}
        run_id = str(receipt.get("run_id") or "").strip()
        payload = {
            "ok": True,
            "pending": True,
            "message": message,
            **receipt,
            "data": receipt,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            handler,
            HTTPStatus.ACCEPTED,
            body,
            "application/json; charset=utf-8",
            cache_control="no-store",
            extra_headers={
                "Location": f"/control-plane/runs/{quote(run_id, safe='')}"
            },
        )

    def _submit_console_tool_command(
        self,
        *,
        trusted_context: dict[str, Any],
        browser_request_uuid: str,
        tool_name: str,
        arguments: dict[str, Any],
        entity_refs: list[dict[str, Any]] | None = None,
        console_entry: str,
    ) -> dict[str, Any]:
        """Submit one trusted Console action as a durable tool command."""

        normalized_request_uuid = self._normalize_browser_request_uuid(
            browser_request_uuid
        )
        if not normalized_request_uuid:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "BROWSER_REQUEST_UUID_REQUIRED",
                "error": "缺少有效且稳定的浏览器请求标识，命令未提交。",
            }

        actor = trusted_context.get("actor") if isinstance(trusted_context, dict) else None
        actor_id = str(actor.get("actor_id") or "").strip() if isinstance(actor, dict) else ""
        safe_tool_name = str(tool_name or "").strip()
        if not actor_id or not safe_tool_name or not isinstance(arguments, dict):
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "INVALID_COMMAND",
                "error": "控制平面命令缺少可信管理员、工具或参数。",
            }

        payload = {
            "command_type": "tool.execute",
            "parameters": {
                "tool_name": safe_tool_name,
                "arguments": dict(arguments),
                "account_id": arguments.get("account_id"),
                "execution_context": {
                    "console_entry": str(console_entry or "")[:200],
                },
                "llm_selected": False,
            },
            "entity_refs": list(entity_refs or []),
            "idempotency_key": (
                f"console:{actor_id}:tool.execute:{normalized_request_uuid}"
            ),
            "actor": actor,
            "actor_roles": list(trusted_context.get("actor_roles") or []),
            "source": "console",
        }
        result = self._agent_request(
            "POST",
            "/internal/v1/commands",
            payload=payload,
            timeout=self.settings.agent_timeout_seconds,
            console_principal=trusted_context.get("_console_principal"),
        )
        if not result.get("ok"):
            return result
        receipt = result.get("data")
        run_id = str(receipt.get("run_id") or "").strip() if isinstance(receipt, dict) else ""
        if not run_id:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_GATEWAY,
                "error_code": "INVALID_AGENT_RUN_CONTRACT",
                "error": "智能服务未返回可追踪的执行编号。",
            }
        return {
            "ok": True,
            "status": HTTPStatus.ACCEPTED,
            "data": receipt,
        }

    def _agent_request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
        console_principal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            endpoint = self._validate_internal_agent_endpoint(endpoint)
        except ValueError as exc:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "INVALID_AGENT_ENDPOINT",
                "error": str(exc),
            }
        agent_internal_api_token = str(
            getattr(self.settings, "agent_internal_api_token", "") or ""
        ).strip()
        if not agent_internal_api_token:
            return {
                "ok": False,
                "status": None,
                "error": "AGENT_INTERNAL_API_TOKEN is not configured",
            }
        url = f"{self.settings.agent_base_url.rstrip('/')}{endpoint}"
        body: bytes | None = None
        headers: dict[str, str] = {
            "X-Agent-Internal-Token": agent_internal_api_token,
        }
        request_payload = dict(payload) if isinstance(payload, dict) else payload
        if isinstance(request_payload, dict) and "_console_principal" in request_payload:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "INVALID_CALLER_CONTEXT",
                "error": "请求内容不得自行指定控制台身份",
            }
        signed_principal = console_principal
        if (
            signed_principal is None
            and self._agent_admin_endpoint_requires_principal(endpoint)
        ):
            signed_principal = self._mysql_console_principal()
            if signed_principal is None:
                return {
                    "ok": False,
                    "status": HTTPStatus.FORBIDDEN,
                    "error_code": "MYSQL_ADMIN_SESSION_REQUIRED",
                    "error": (
                        "管理请求需要真实的数据库管理员会话"
                    ),
                }
        if request_payload is not None:
            body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if signed_principal is not None:
            signing_secret = str(os.getenv("CONSOLE_AGENT_SIGNING_SECRET", "") or "").strip()
            if not signing_secret:
                return {
                    "ok": False,
                    "status": HTTPStatus.SERVICE_UNAVAILABLE,
                    "error_code": "CONSOLE_SIGNING_SECRET_NOT_CONFIGURED",
                    "error": "控制台与智能服务之间的签名配置缺失",
                }
            parsed_url = urlparse(url)
            request_target = parsed_url.path or "/"
            if parsed_url.query:
                request_target += f"?{parsed_url.query}"
            try:
                headers.update(
                    build_console_identity_headers(
                        secret=signing_secret,
                        method=method,
                        request_target=request_target,
                        body=body or b"",
                        principal=signed_principal,
                        nonce=secrets.token_urlsafe(24),
                    )
                )
            except ConsoleIdentityError as exc:
                return {
                    "ok": False,
                    "status": HTTPStatus.SERVICE_UNAVAILABLE,
                    "error_code": exc.code,
                    "error": str(exc),
                }

        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            if timeout is None:
                response_handle = urlopen(request)
            else:
                response_handle = urlopen(
                    request,
                    timeout=timeout or self.settings.agent_timeout_seconds,
                )
            with response_handle as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw) if raw else {}
                if endpoint.startswith("/internal/v1/"):
                    if not isinstance(data, dict) or not {"ok", "data", "error"}.issubset(data):
                        return {
                            "ok": False,
                            "status": response.status,
                            "error": "智能服务返回了无法识别的数据",
                            "error_code": "invalid_internal_contract",
                        }
                    if data.get("ok") is not True:
                        error = data.get("error") if isinstance(data.get("error"), dict) else {}
                        return {
                            "ok": False,
                            "status": response.status,
                            "error": redact_text(
                                error.get("message") or "Internal API request failed"
                            ),
                            "error_code": str(error.get("code") or "internal_api_failed"),
                            "data": data.get("data"),
                        }
                    data = data.get("data")
                return {
                    "ok": True,
                    "status": response.status,
                    "data": data,
                }
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = raw
            error_code = "agent_request_failed"
            if endpoint.startswith("/internal/v1/") and isinstance(data, dict):
                error = data.get("error") if isinstance(data.get("error"), dict) else {}
                error_payload = error.get("message") or data
                error_code = str(error.get("code") or error_code)
                response_data = data.get("data")
            else:
                error_payload = data
                response_data = None
            return {
                "ok": False,
                "status": exc.code,
                "error": redact_sensitive(error_payload or str(exc)),
                "error_code": error_code,
                "data": response_data,
            }
        except URLError as exc:
            return {
                "ok": False,
                "status": None,
                "error": redact_text(exc.reason),
                "error_code": "agent_unreachable",
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": None,
                "error": redact_text(exc),
                "error_code": "agent_request_failed",
            }

    def _agent_binary_request(
        self,
        endpoint: str,
        *,
        timeout: int = 12,
        console_principal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read one bounded non-JSON internal response with the normal identity proof."""

        try:
            endpoint = self._validate_internal_agent_endpoint(endpoint)
        except ValueError as exc:
            return {"ok": False, "status": HTTPStatus.BAD_REQUEST, "error": str(exc)}
        token = str(getattr(self.settings, "agent_internal_api_token", "") or "").strip()
        if not token:
            return {"ok": False, "status": None, "error": "智能服务连接配置缺失"}
        url = f"{self.settings.agent_base_url.rstrip('/')}{endpoint}"
        headers = {"X-Agent-Internal-Token": token}
        principal = console_principal
        if principal is None and self._agent_admin_endpoint_requires_principal(endpoint):
            principal = self._mysql_console_principal()
        if principal is None:
            return {"ok": False, "status": HTTPStatus.FORBIDDEN, "error": "需要管理员登录"}
        signing_secret = str(os.getenv("CONSOLE_AGENT_SIGNING_SECRET", "") or "").strip()
        if not signing_secret:
            return {"ok": False, "status": HTTPStatus.SERVICE_UNAVAILABLE, "error": "服务签名配置缺失"}
        parsed_url = urlparse(url)
        target = parsed_url.path or "/"
        if parsed_url.query:
            target += f"?{parsed_url.query}"
        try:
            headers.update(
                build_console_identity_headers(
                    secret=signing_secret,
                    method="GET",
                    request_target=target,
                    body=b"",
                    principal=principal,
                    nonce=secrets.token_urlsafe(24),
                )
            )
            with urlopen(Request(url, headers=headers, method="GET"), timeout=timeout) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if not data or len(data) > 2 * 1024 * 1024:
                    return {"ok": False, "status": HTTPStatus.BAD_GATEWAY, "error": "设置资源大小无效"}
                return {
                    "ok": True,
                    "status": response.status,
                    "data": data,
                    "content_type": str(response.headers.get_content_type() or "application/octet-stream"),
                }
        except (ConsoleIdentityError, HTTPError, URLError, OSError) as exc:
            return {"ok": False, "status": getattr(exc, "code", None), "error": redact_text(exc)}
