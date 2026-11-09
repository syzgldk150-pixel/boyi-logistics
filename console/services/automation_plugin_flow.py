"""Console side of plugin flows: view, “一句话改流程”, dry run and save (AI phase 5).

Super administrators only, like plugin settings. The Console never decides
whether a flow is valid: the Agent validates every proposal, dry run and save
(``shared.plugin_flow``). Saving reuses the plugin-settings path, so the flow
gets the same configuration CAS, version history and restore as any setting.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import quote


class AutomationPluginFlowServiceMixin:
    def _flow_super_admin_context(self, handler: BaseHTTPRequestHandler, *, write: bool) -> dict[str, Any] | None:
        context = self._control_plane_write_context(handler) if write else self._control_plane_read_context(handler)
        if context is None:
            return None
        if "super_admin" not in list(context.get("actor_roles") or []):
            self._control_plane_error(handler, HTTPStatus.FORBIDDEN, "SUPER_ADMIN_REQUIRED", "只有超级管理员可以调整插件流程。")
            return None
        return context

    def _flow_agent_error(self, handler: BaseHTTPRequestHandler, result: dict[str, Any], fallback: str) -> None:
        error = result.get("error")
        code = result.get("error_code") or (error.get("code") if isinstance(error, dict) else None) or "FLOW_FAILED"
        message = (error.get("message") if isinstance(error, dict) else error) or fallback
        status = result.get("status") if result.get("status") in (404, 409, 422, 503) else 502
        self._send_json(handler, status, {"ok": False, "error_code": str(code), "message": str(message)})

    def _render_automation_plugin_flow(self, handler: BaseHTTPRequestHandler, automation_id: str) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id or self._flow_super_admin_context(handler, write=False) is None:
            return
        template = self.template_env.get_template("automation_plugin_flow.html")
        self._send_html(handler, template.render(app_title=self.settings.app_title, automation_id=automation_id))

    def _handle_automation_plugin_flow_data(self, handler: BaseHTTPRequestHandler, automation_id: str) -> None:
        automation_id = self._automation_project_id(automation_id)
        context = self._flow_super_admin_context(handler, write=False) if automation_id else None
        if context is None:
            return
        result = self._agent_request("GET", f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/flow",
                                     timeout=15, console_principal=context["_console_principal"])
        if not result.get("ok"):
            self._flow_agent_error(handler, result, "处理流程暂时无法读取。")
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result.get("data")})

    def _handle_automation_plugin_flow_action(self, handler: BaseHTTPRequestHandler, automation_id: str, action: str) -> None:
        automation_id = self._automation_project_id(automation_id)
        context = self._flow_super_admin_context(handler, write=True) if automation_id else None
        if context is None:
            return
        body = self._parse_json_body(handler)
        request_id = self._normalize_browser_request_uuid(handler.headers.get("X-Browser-Request-UUID"))
        base = f"/internal/v1/automation/instances/{quote(automation_id, safe='')}"
        principal = context["_console_principal"]
        if action == "ai-edit":
            instruction = body.get("instruction")
            if not request_id or not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 300:
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请用 1 到 300 字描述想怎么调整。"})
                return
            result = self._agent_request("POST", base + "/flow/ai-edit", payload={"request_id": request_id,
                                         "instruction": instruction.strip()}, timeout=120, console_principal=principal)
        elif action == "dry-run":
            flow = body.get("flow")
            if not isinstance(flow, dict) or not self._service_v2_json_tree_is_safe(flow):
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "流程内容无效。"})
                return
            result = self._agent_request("POST", base + "/flow/dry-run", payload={"flow": flow}, timeout=30,
                                         console_principal=principal)
        else:
            self._save_automation_plugin_flow(handler, base, body, request_id, principal)
            return
        if not result.get("ok"):
            self._flow_agent_error(handler, result, "操作失败，请稍后重试。")
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result.get("data")})

    def _save_automation_plugin_flow(self, handler: BaseHTTPRequestHandler, base: str, body: dict[str, Any],
                                     request_id: str, principal: dict[str, Any]) -> None:
        flow = body.get("flow")
        expected = body.get("expected_project_configuration_version")
        if (not request_id or not isinstance(flow, dict) or not self._service_v2_json_tree_is_safe(flow)
                or isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "保存请求无效，请刷新后重试。"})
            return
        context = self._agent_request("GET", base + "/settings-context", timeout=15, console_principal=principal)
        data = context.get("data") if isinstance(context.get("data"), dict) else None
        if not context.get("ok") or data is None:
            self._flow_agent_error(handler, context, "插件设置暂时无法读取。")
            return
        if data.get("project_configuration_version") != expected:
            self._send_json(handler, HTTPStatus.CONFLICT, {"ok": False, "error_code": "CONFIG_VERSION_CHANGED",
                                                           "message": "插件设置已被其他人修改，请刷新后重新调整。"})
            return
        config = dict(data.get("config") or {})
        config["flow"] = flow
        result = self._agent_request("PUT", base + "/plugin-settings", payload={
            "config": config, "account_bindings": data.get("account_bindings") or {},
            "resource_bindings": data.get("resource_bindings") or {}, "request_id": request_id,
            "expected_project_configuration_version": expected,
        }, timeout=25, console_principal=principal)
        if not result.get("ok"):
            self._flow_agent_error(handler, result, "流程保存失败。")
            return
        self._clear_automation_plugin_catalog_cache()
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result.get("data")})


__all__ = ["AutomationPluginFlowServiceMixin"]
