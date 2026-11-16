"""Console side of “需他人复核” review requests (robot agent layer P4); super administrators only.

The Console only lists requests and forwards approve/reject; the Agent re-checks
the requester and runs the stored plan exactly once.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import quote

_REQUEST_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ReviewsServiceMixin:
    def _reviews_context(self, handler: BaseHTTPRequestHandler, *, write: bool) -> dict[str, Any] | None:
        context = self._control_plane_write_context(handler) if write else self._control_plane_read_context(handler)
        if context is None:
            return None
        if "super_admin" not in list(context.get("actor_roles") or []):
            self._control_plane_error(handler, HTTPStatus.FORBIDDEN, "SUPER_ADMIN_REQUIRED", "只有超级管理员可以处理复核请求。")
            return None
        return context

    def _render_reviews(self, handler: BaseHTTPRequestHandler) -> None:
        if self._reviews_context(handler, write=False) is None:
            return
        template = self.template_env.get_template("reviews.html")
        self._send_html(handler, template.render(app_title=self.settings.app_title))

    def _handle_reviews_list(self, handler: BaseHTTPRequestHandler) -> None:
        context = self._reviews_context(handler, write=False)
        if context is None:
            return
        result = self._agent_request("GET", "/internal/v1/admin/reviews", timeout=15,
                                     console_principal=context["_console_principal"])
        self._handbook_reply(handler, result, "复核请求暂时无法读取。")

    def _handle_review_action(self, handler: BaseHTTPRequestHandler, request_id: str, action: str) -> None:
        context = self._reviews_context(handler, write=True)
        if context is None:
            return
        if not _REQUEST_ID.fullmatch(request_id) or action not in {"approve", "reject"}:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "不支持的操作。"})
            return
        body = self._parse_json_body(handler)
        payload: dict[str, Any] = {}
        if action == "reject":
            note = body.get("note")
            payload["note"] = note[:200] if isinstance(note, str) else ""
        result = self._agent_request("POST", f"/internal/v1/admin/reviews/{quote(request_id, safe='')}/{action}",
                                     payload=payload, timeout=200, console_principal=context["_console_principal"])
        self._handbook_reply(handler, result, "操作失败，请稍后重试。")
