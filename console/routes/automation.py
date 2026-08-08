"""Automation configuration, account and TMS session routes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path in {"/automations", "/workspaces/automations"}:
        app._render_automations(handler, query)
        return True
    if path.startswith("/automation-accounts/") and path.endswith("/status"):
        app._handle_automation_account_status_get(handler, path, query)
        return True
    if path == "/automation-accounts/statuses":
        app._handle_automation_accounts_statuses_get(handler, query)
        return True
    if path == "/automation-accounts":
        app._render_automation_accounts(handler, query)
        return True
    if path == "/automations/session-context":
        app._handle_automation_session_context(handler, query)
        return True
    session_route = app._automation_session_route(path)
    if session_route and session_route[1] == "/status":
        app._handle_tms_session_status(handler, profile=session_route[0], query=query)
        return True
    if path == "/automations/tms-session/status":
        app._handle_tms_session_status(handler, query=query)
        return True
    if path == "/automations/tasks/output":
        app._handle_automation_task_output(handler, query)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if app._handle_automation_account_post(handler, path):
        return True
    if path == "/automations/resources/save":
        app._handle_automation_resource_save(handler)
        return True
    if path == "/automations/tasks/save":
        app._handle_automation_task_save(handler)
        return True
    if path == "/automations/tasks/run-now":
        app._handle_automation_task_run_now(handler)
        return True
    if path == "/automations/tasks/cancel":
        app._handle_automation_task_cancel(handler)
        return True
    session_route = app._automation_session_route(path)
    if session_route and app._handle_automation_session_post(handler, session_route[0], session_route[1]):
        return True
    if path == "/automations/tms-session/send-code":
        app._handle_tms_session_action(
            handler,
            endpoint="/internal/v1/admin/tms/session/send-code",
            payload={},
            success_message="TMS融辉登录已提交；如出现图片验证码，请按图输入后提交。",
            timeout=90,
        )
        return True
    if path == "/automations/tms-session/save-credentials":
        values = app._parse_urlencoded_form(handler)
        app._handle_tms_session_action(
            handler,
            endpoint="/internal/v1/admin/tms/session/credentials",
            payload={
                "username": str(values.get("username", "") or "").strip(),
                "password": str(values.get("password", "") or ""),
                "phone": str(values.get("phone", "") or "").strip(),
            },
            success_message="TMS 默认登录配置已保存。",
            timeout=20,
        )
        return True
    if path == "/automations/tms-session/clear-credentials":
        app._handle_tms_session_action(
            handler,
            endpoint="/internal/v1/admin/tms/session/credentials/clear",
            payload={},
            success_message="TMS 默认登录配置已清空。",
            timeout=20,
        )
        return True
    if path == "/automations/tms-session/submit-code":
        values = app._parse_urlencoded_form(handler)
        sms_code = str(values.get("code", "") or "").strip()
        if not sms_code:
            app._respond_tms_action(
                handler,
                ok=False,
                message="验证码不能为空。",
                kind="warning",
                http_status=HTTPStatus.BAD_REQUEST,
            )
            return True
        app._handle_tms_session_action(
            handler,
            endpoint="/internal/v1/admin/tms/session/submit-code",
            payload={"code": sms_code},
            success_message="TMS 登录成功，共享登录态已更新。",
            timeout=45,
        )
        return True
    if path == "/automations/tms-session/clear":
        app._handle_tms_session_action(
            handler,
            endpoint="/internal/v1/admin/tms/session/clear",
            payload={},
            success_message="TMS 登录态已清除。",
            timeout=20,
        )
        return True
    if path == "/automations/admin/import-phase7-resources":
        app._handle_automation_admin_action(
            handler,
            endpoint="/internal/v1/admin/import-phase7-resources",
            success_message="Phase 7 资源已重新导入。",
        )
        return True
    if path == "/automations/admin/seed-phase7-tasks":
        app._handle_automation_admin_action(
            handler,
            endpoint="/internal/v1/admin/seed-phase7-tasks",
            success_message="Phase 7 默认任务模板已写入并重载调度。",
        )
        return True
    if path == "/automations/admin/reload":
        app._handle_automation_admin_action(
            handler,
            endpoint="/internal/v1/admin/reload",
            success_message="Agent 运行时配置已重载。",
        )
        return True
    return False
