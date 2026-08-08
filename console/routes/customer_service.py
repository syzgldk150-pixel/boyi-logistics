"""Customer-service problem-workspace routes."""

from __future__ import annotations

from typing import Any


_ACTION_ROUTES = {
    "/customer-service/problems/detail": "detail",
    "/customer-service/problems/mark-read": "mark_read",
    "/customer-service/problems/reply": "reply",
    "/customer-service/problems/publish": "publish",
}


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/modules/customer-service":
        app._render_customer_service(handler, query)
        return True
    if path == "/customer-service/problems/attachments/preview":
        app._handle_customer_service_attachment_preview(handler, query)
        return True
    if path == "/customer-service/problem-settings":
        app._handle_customer_service_problem_settings_get(handler)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/customer-service/problem-settings":
        app._handle_customer_service_problem_settings_post(handler)
        return True
    if path == "/customer-service/problems/query":
        app._handle_customer_service_problem_query(handler)
        return True
    action = _ACTION_ROUTES.get(path)
    if action:
        app._handle_customer_service_problem_agent_action(handler, action)
        return True
    if path == "/customer-service/problems/attachments/upload":
        app._handle_customer_service_attachment_upload(handler)
        return True
    return False
