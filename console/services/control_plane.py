"""Console control-plane pages and Agent API proxy operations."""

from __future__ import annotations

import uuid

from console.app_support import *  # noqa: F403
from console.services.agent_api import mysql_console_principal


CONTROL_PLANE_JSON_MAX_BYTES = 256 * 1024
CONTROL_PLANE_QUERY_FIELDS = {
    "work_items": ("q", "status", "priority", "type", "source", "owner", "sla", "page", "page_size", "sort"),
    "collection": ("cursor", "limit"),
}
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
    def _render_work_items(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict[str, list[str]],
    ) -> None:
        template = self.template_env.get_template("work_items.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=str(query.get("message", [""])[0] or ""),
            message_kind=str(query.get("kind", ["info"])[0] or "info"),
        )
        self._send_html(handler, body)

    def _render_work_item_detail(
        self,
        handler: BaseHTTPRequestHandler,
        work_item_id: str,
    ) -> None:
        template = self.template_env.get_template("work_item_detail.html")
        body = template.render(
            app_title=self.settings.app_title,
            work_item_id=work_item_id,
        )
        self._send_html(handler, body)

    def _handle_control_plane_work_items_get(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict[str, list[str]],
    ) -> None:
        endpoint = self._control_plane_endpoint_with_query(
            "/internal/v1/work-items",
            query,
            CONTROL_PLANE_QUERY_FIELDS["work_items"],
        )
        self._control_plane_proxy_get(handler, endpoint, contract="items")

    def _handle_control_plane_work_item_get(
        self,
        handler: BaseHTTPRequestHandler,
        work_item_id: str,
    ) -> None:
        self._control_plane_proxy_get(
            handler,
            f"/internal/v1/work-items/{quote(work_item_id, safe='')}",
            contract="work_item",
        )

    def _handle_control_plane_timeline_get(
        self,
        handler: BaseHTTPRequestHandler,
        work_item_id: str,
        query: dict[str, list[str]],
    ) -> None:
        endpoint = self._control_plane_endpoint_with_query(
            f"/internal/v1/work-items/{quote(work_item_id, safe='')}/timeline",
            query,
            CONTROL_PLANE_QUERY_FIELDS["collection"],
        )
        self._control_plane_proxy_get(handler, endpoint, contract="items")

    def _handle_control_plane_evidence_get(
        self,
        handler: BaseHTTPRequestHandler,
        work_item_id: str,
        query: dict[str, list[str]],
    ) -> None:
        endpoint = self._control_plane_endpoint_with_query(
            f"/internal/v1/work-items/{quote(work_item_id, safe='')}/evidence",
            query,
            CONTROL_PLANE_QUERY_FIELDS["collection"],
        )
        self._control_plane_proxy_get(handler, endpoint, contract="items")

    def _handle_control_plane_run_get(
        self,
        handler: BaseHTTPRequestHandler,
        run_id: str,
    ) -> None:
        self._control_plane_proxy_get(
            handler,
            f"/internal/v1/runs/{quote(run_id, safe='')}",
            contract="run",
        )

    def _handle_control_plane_command_post(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> None:
        context = self._control_plane_write_context(handler)
        if context is None:
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return

        command_type = self._bounded_text(values.get("command_type"), 128)
        parameters = values.get("parameters", {})
        entity_refs = values.get("entity_refs", [])
        correlation_id = self._bounded_text(values.get("correlation_id"), 128)
        browser_request_uuid = self._bounded_text(
            handler.headers.get("X-Browser-Request-UUID"),
            64,
        )
        try:
            normalized_request_uuid = str(uuid.UUID(browser_request_uuid))
        except (ValueError, AttributeError):
            normalized_request_uuid = ""
        if not command_type or not normalized_request_uuid:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_COMMAND",
                "command_type 和 idempotency_key 不能为空。",
            )
            return
        if not isinstance(parameters, dict) or not isinstance(entity_refs, list) or not all(
            isinstance(item, dict) for item in entity_refs
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_COMMAND",
                "parameters 必须是对象，entity_refs 必须是对象列表。",
            )
            return

        payload: dict[str, Any] = {
            "command_type": command_type,
            "parameters": parameters,
            "entity_refs": entity_refs,
            "idempotency_key": (
                f"console:{context['actor']['actor_id']}:{command_type}:"
                f"{normalized_request_uuid}"
            ),
            **self._control_plane_agent_body_context(context),
        }
        if correlation_id:
            payload["correlation_id"] = correlation_id
        result = self._agent_request(
            "POST",
            "/internal/v1/commands",
            payload=payload,
            timeout=self.settings.agent_timeout_seconds,
            console_principal=context["_console_principal"],
        )
        if not result.get("ok"):
            self._control_plane_agent_error(handler, result)
            return
        data = result.get("data")
        run_id = self._bounded_text(data.get("run_id") if isinstance(data, dict) else None, 128)
        if not isinstance(data, dict) or not run_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_AGENT_RUN_CONTRACT",
                "Agent 未返回可追踪的 run_id。",
            )
            return
        self._control_plane_success(
            handler,
            HTTPStatus.ACCEPTED,
            data,
            extra_headers={"Location": f"/control-plane/runs/{quote(run_id, safe='')}"},
        )

    def _handle_control_plane_run_action_post(
        self,
        handler: BaseHTTPRequestHandler,
        run_id: str,
        action: str,
    ) -> None:
        context = self._control_plane_write_context(handler)
        if context is None:
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return

        payload: dict[str, Any] = self._control_plane_agent_body_context(context)
        if action == "cancel":
            comment = self._bounded_text(values.get("comment"), 1000)
            if comment:
                payload["comment"] = comment
        elif action == "retry":
            reason = self._bounded_text(values.get("reason"), 1000)
            if reason:
                payload["reason"] = reason
        elif action == "clarify":
            clarification = self._control_plane_clarification(values.get("clarification"))
            if clarification is None:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "CLARIFICATION_REQUIRED",
                    "请填写补充说明、账号 ID 或参数更新。",
                )
                return
            payload["clarification"] = clarification
        else:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "UNKNOWN_RUN_ACTION",
                "执行操作不存在。",
            )
            return

        result = self._agent_request(
            "POST",
            f"/internal/v1/runs/{quote(run_id, safe='')}/{action}",
            payload=payload,
            timeout=self.settings.agent_timeout_seconds,
            console_principal=context["_console_principal"],
        )
        self._control_plane_forward_run_result(handler, result)

    def _handle_control_plane_approval_post(
        self,
        handler: BaseHTTPRequestHandler,
        approval_id: str,
        decision: str,
    ) -> None:
        context = self._control_plane_write_context(handler)
        if context is None:
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return

        plan_hash = self._bounded_text(values.get("plan_hash"), 128)
        comment = self._bounded_text(values.get("comment"), 2000)
        if not plan_hash:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLAN_HASH_REQUIRED",
                "审批必须绑定 plan_hash。",
            )
            return
        payload = {
            "approval_id": approval_id,
            "plan_hash": plan_hash,
            "comment": comment,
            **self._control_plane_agent_body_context(context),
        }
        result = self._agent_request(
            "POST",
            f"/internal/v1/approvals/{quote(approval_id, safe='')}/{decision}",
            payload=payload,
            timeout=self.settings.agent_timeout_seconds,
            console_principal=context["_console_principal"],
        )
        self._control_plane_forward_run_result(handler, result)

    def _handle_control_plane_assign_post(
        self,
        handler: BaseHTTPRequestHandler,
        work_item_id: str,
    ) -> None:
        context = self._control_plane_write_context(handler)
        if context is None:
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        owner_id = self._bounded_text(values.get("owner_id"), 128)
        if not owner_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "OWNER_REQUIRED",
                "请选择责任人。",
            )
            return
        payload = {
            "owner_id": owner_id,
            **self._control_plane_agent_body_context(context),
        }
        result = self._agent_request(
            "POST",
            f"/internal/v1/work-items/{quote(work_item_id, safe='')}/assign",
            payload=payload,
            timeout=self.settings.agent_timeout_seconds,
            console_principal=context["_console_principal"],
        )
        if not result.get("ok"):
            self._control_plane_agent_error(handler, result)
            return
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("work_item"), dict):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_AGENT_WORK_ITEM_CONTRACT",
                "Agent 返回了无效的事项数据。",
            )
            return
        self._control_plane_success(handler, HTTPStatus.OK, data)

    def _control_plane_proxy_get(
        self,
        handler: BaseHTTPRequestHandler,
        endpoint: str,
        *,
        contract: str,
    ) -> None:
        context = self._control_plane_read_context(handler)
        if context is None:
            return
        result = self._agent_request(
            "GET",
            endpoint,
            timeout=self.settings.agent_timeout_seconds,
            console_principal=context["_console_principal"],
        )
        if not result.get("ok"):
            self._control_plane_agent_error(handler, result)
            return
        data = result.get("data")
        if not self._control_plane_contract_is_valid(data, contract):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_AGENT_RESPONSE",
                "Agent 返回了无效的控制平面数据。",
            )
            return
        self._control_plane_success(handler, HTTPStatus.OK, data)

    def _control_plane_forward_run_result(
        self,
        handler: BaseHTTPRequestHandler,
        result: dict[str, Any],
    ) -> None:
        if not result.get("ok"):
            self._control_plane_agent_error(handler, result)
            return
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("run"), dict):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_AGENT_RUN_CONTRACT",
                "Agent 返回了无效的执行数据。",
            )
            return
        self._control_plane_success(handler, HTTPStatus.OK, data)

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
                "请求体必须是 JSON 对象。",
            )
            return None
        return values

    def _control_plane_endpoint_with_query(
        self,
        base_endpoint: str,
        query: dict[str, list[str]],
        allowed_fields: tuple[str, ...],
    ) -> str:
        pairs: list[tuple[str, str]] = []
        for field in allowed_fields:
            values = query.get(field, [])
            if not isinstance(values, list):
                continue
            for value in values[:10]:
                text = self._bounded_text(value, 256)
                if text:
                    pairs.append((field, text))
        encoded = urlencode(pairs)
        return f"{base_endpoint}?{encoded}" if encoded else base_endpoint

    def _control_plane_contract_is_valid(self, data: Any, contract: str) -> bool:
        if not isinstance(data, dict):
            return False
        if contract == "items":
            return isinstance(data.get("items"), list)
        if contract == "work_item":
            return isinstance(data.get("work_item"), dict)
        if contract == "run":
            return isinstance(data.get("run"), dict)
        return False

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
            or "Agent 控制平面暂时不可用。",
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

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    def _control_plane_clarification(self, value: Any) -> str | dict[str, Any] | None:
        """Allow only the closed Agent clarification v1 browser contract."""

        if isinstance(value, str):
            return self._bounded_text(value, 4000) or None
        if not isinstance(value, dict):
            return None
        if set(value) - {"note", "account_id", "argument_updates"}:
            return None

        result: dict[str, Any] = {}
        note = value.get("note")
        if note is not None:
            if not isinstance(note, str):
                return None
            bounded_note = note.strip()[:4000]
            if bounded_note:
                result["note"] = bounded_note
        account_id = value.get("account_id")
        if account_id is not None:
            if not isinstance(account_id, str):
                return None
            bounded_account = account_id.strip()[:191]
            if not bounded_account:
                return None
            result["account_id"] = bounded_account
        updates = value.get("argument_updates")
        if updates is not None:
            if not isinstance(updates, dict):
                return None
            result["argument_updates"] = updates
        return result or None
