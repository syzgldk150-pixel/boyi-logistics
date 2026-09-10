"""Lazy, signed projection of recent real plugin executions."""

from http import HTTPStatus
from urllib.parse import quote

from shared.plugin_invocation_repository import ACTIVE_INVOCATION_STATUSES, TERMINAL_INVOCATION_STATUSES


class AutomationInvocationHistoryMixin:
    def _handle_automation_invocations_get(self, handler, automation_id: str) -> None:
        context = self._control_plane_read_context(handler)
        if context is None:
            return
        identity = self._automation_project_id(automation_id)
        if not identity or identity != automation_id:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "插件标识无效。"})
            return
        response = self._agent_request(
            "GET", f"/internal/v1/automation-projects/{quote(identity, safe='')}/invocations",
            timeout=5, console_principal=context["_console_principal"],
        )
        data = response.get("data")
        rows = data.get("items") if isinstance(data, dict) else None
        items, seen = [], set()
        if response.get("ok") and isinstance(rows, list) and len(rows) <= 100:
            for row in rows:
                invocation_id = row.get("invocation_id") if isinstance(row, dict) else None
                if (not invocation_id or self._normalize_browser_request_uuid(invocation_id) != invocation_id
                        or invocation_id in seen or row.get("automation_id") != identity
                        or row.get("status") not in ACTIVE_INVOCATION_STATUSES | TERMINAL_INVOCATION_STATUSES
                        or row.get("invocation_phase") not in {"preview", "formal", "run"}
                        or row.get("source") not in {"console", "feishu", "scheduler", "harness", "webhook", "events", "module_slots"}
                        or not isinstance(row.get("started_at"), str) or not row["started_at"]):
                    break
                seen.add(invocation_id)
                items.append({key: row.get(key) for key in (
                    "invocation_id", "automation_id", "source", "status", "invocation_phase", "started_at", "finished_at",
                )})
            else:
                self._send_json(handler, HTTPStatus.OK, {"automation_id": identity, "items": items})
                return
        self._send_json(handler, HTTPStatus.BAD_GATEWAY, {
            "error": "执行记录暂时无法读取，请重试。", "error_code": "INVALID_INVOCATION_HISTORY",
        })
