"""Automation projects, plugin lifecycle and business-account routes."""

from __future__ import annotations

from typing import Any


def _automation_project_route(path: str) -> tuple[str, str] | None:
    prefix = "/automations/projects/"
    if not path.startswith(prefix):
        return None
    automation_id, separator, suffix = path[len(prefix) :].partition("/")
    if not separator or not automation_id or not suffix:
        return None
    return automation_id, f"/{suffix}"


def _automation_plugin_route(path: str) -> tuple[str, str] | None:
    prefix = "/automations/plugins/"
    if not path.startswith(prefix):
        return None
    remainder = path[len(prefix) :]
    if remainder == "install":
        return "", "install"
    automation_id, separator, action = remainder.partition("/")
    if not separator or not automation_id or not action or "/" in action:
        return None
    return automation_id, action


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
    if path == "/automations/tasks/output":
        app._handle_automation_task_output(handler, query)
        return True
    project_route = _automation_project_route(path)
    if project_route and project_route[1] == "/pending-approvals":
        app._handle_automation_project_pending_approvals_get(
            handler,
            project_route[0],
        )
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
    if path == "/automations/tasks/confirm-scan-preview":
        app._handle_scan_preview_confirmation(handler)
        return True
    if path == "/automations/tasks/cancel":
        app._handle_automation_task_cancel(handler)
        return True
    plugin_route = _automation_plugin_route(path)
    if plugin_route and plugin_route[1] == "install":
        app._handle_automation_plugin_package_upload(handler)
        return True
    if plugin_route and plugin_route[1] == "upgrade":
        app._handle_automation_plugin_package_upload(
            handler,
            automation_id=plugin_route[0],
        )
        return True
    if plugin_route and plugin_route[1] in {"enable", "disable", "uninstall"}:
        app._handle_automation_plugin_instance_action(
            handler,
            plugin_route[0],
            plugin_route[1],
        )
        return True
    if plugin_route and plugin_route[1] == "configuration":
        app._handle_automation_plugin_configuration_save(handler, plugin_route[0])
        return True
    project_route = _automation_project_route(path)
    if project_route and project_route[1] == "/approval-policy":
        app._handle_automation_project_approval_policy(handler, project_route[0])
        return True
    if project_route and project_route[1] in {
        "/pending-approvals/approve",
        "/pending-approvals/reject",
    }:
        app._handle_automation_project_pending_approvals_action(
            handler,
            project_route[0],
            project_route[1].rsplit("/", 1)[-1],
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
