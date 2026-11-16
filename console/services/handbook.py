"""Console side of the 业务手册 module (robot agent layer).

Who may open, edit or approve is decided once in ``shared.identity_routes``
(checked by ``_ensure_authorized`` before this mixin runs, and again by the
Agent): the module grants viewing and editing, while approve, resume and retire
need a super administrator. The Console never decides whether compiled content
is valid or may take effect: the Agent validates every compile, trial and
approval. This page only shows the note, the Chinese restatement, the checks and
the trial, and forwards actions.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import quote

from console.app_support import current_admin_user

_ENTRY_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ACTIONS = frozenset({"save", "compile", "trial", "acknowledge", "approve", "pause", "retire", "resume"})
_TIMEOUTS = {"compile": 150, "trial": 330}
_FEE_ACTIONS = frozenset({"copy", "save", "checks", "trial", "submit", "withdraw", "approve", "retire"})
_FEE_FIELDS = {"save": ("version_label", "effective_from", "content", "checks", "row_version"),
               "checks": ("checks", "row_version"),
               "trial": ("settlement_weight_kg", "elevator", "floor", "goods_type", "piece_counts", "pieces_over_limit",
                         "total_pieces", "boundary"),
               "copy": ()}


class HandbookServiceMixin:
    def _handbook_context(self, handler: BaseHTTPRequestHandler, *, write: bool) -> dict[str, Any] | None:
        return self._control_plane_write_context(handler) if write else self._control_plane_read_context(handler)

    def _handbook_reply(self, handler: BaseHTTPRequestHandler, result: dict[str, Any], fallback: str) -> None:
        if result.get("ok"):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result.get("data")})
            return
        error = result.get("error")
        code = result.get("error_code") or (error.get("code") if isinstance(error, dict) else None) or "HANDBOOK_FAILED"
        message = (error.get("message") if isinstance(error, dict) else error) or fallback
        status = result.get("status") if result.get("status") in (404, 409, 422, 503) else 502
        self._send_json(handler, status, {"ok": False, "error_code": str(code), "message": str(message)})

    def _render_handbook(self, handler: BaseHTTPRequestHandler) -> None:
        if self._handbook_context(handler, write=False) is None:
            return
        user = current_admin_user() or {}
        is_super_admin = self._is_super_admin_user(user)
        can_edit = is_super_admin or "handbook:operate" in (user.get("access_permissions") or ())
        template = self.template_env.get_template("handbook.html")
        self._send_html(handler, template.render(app_title=self.settings.app_title, can_edit=can_edit,
                                                 is_super_admin=is_super_admin))

    def _handle_handbook_get(self, handler: BaseHTTPRequestHandler, entry_id: str | None) -> None:
        context = self._handbook_context(handler, write=False)
        if context is None:
            return
        if entry_id is not None and not _ENTRY_ID.fullmatch(entry_id):
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "没有找到这条业务说明。"})
            return
        endpoint = "/internal/v1/admin/handbook" + (f"/{quote(entry_id, safe='')}" if entry_id else "")
        result = self._agent_request("GET", endpoint, timeout=15, console_principal=context["_console_principal"])
        self._handbook_reply(handler, result, "业务手册暂时无法读取。")

    def _handle_handbook_post(self, handler: BaseHTTPRequestHandler, entry_id: str | None, action: str | None) -> None:
        context = self._handbook_context(handler, write=True)
        if context is None:
            return
        body = self._parse_json_body(handler)
        principal = context["_console_principal"]
        if entry_id is None:
            payload = {"title": body.get("title"), "body": body.get("body")}
            if not all(isinstance(value, str) for value in payload.values()):
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请填写标题和说明。"})
                return
            result = self._agent_request("POST", "/internal/v1/admin/handbook", payload=payload, timeout=15,
                                         console_principal=principal)
            self._handbook_reply(handler, result, "保存失败，请稍后重试。")
            return
        if not _ENTRY_ID.fullmatch(entry_id) or action not in _ACTIONS:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "不支持的操作。"})
            return
        if action == "save":
            payload = {"title": body.get("title"), "body": body.get("body"), "row_version": body.get("row_version")}
        elif action in {"approve", "acknowledge"}:
            payload = {"version": body.get("version")}
            if action == "acknowledge":
                payload["indices"] = body.get("indices")
        elif action in {"pause", "retire", "resume"}:
            payload = {"row_version": body.get("row_version")}
        else:
            payload = {}
        result = self._agent_request("POST", f"/internal/v1/admin/handbook/{quote(entry_id, safe='')}/{action}",
                                     payload=payload, timeout=_TIMEOUTS.get(action, 15), console_principal=principal)
        self._handbook_reply(handler, result, "操作失败，请稍后重试。")

    # -- 计算规则 (fee rules) ------------------------------------------------------------

    def _handle_fee_rules_get(self, handler: BaseHTTPRequestHandler, rule_id: str | None) -> None:
        context = self._handbook_context(handler, write=False)
        if context is None:
            return
        if rule_id is not None and not _ENTRY_ID.fullmatch(rule_id):
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "没有找到这个规则版本。"})
            return
        endpoint = "/internal/v1/admin/fee-rules" + (f"/{quote(rule_id, safe='')}" if rule_id else "")
        result = self._agent_request("GET", endpoint, timeout=15, console_principal=context["_console_principal"])
        self._handbook_reply(handler, result, "计算规则暂时无法读取。")

    def _handle_fee_rules_post(self, handler: BaseHTTPRequestHandler, rule_id: str, action: str) -> None:
        context = self._handbook_context(handler, write=True)
        if context is None:
            return
        if not _ENTRY_ID.fullmatch(rule_id) or action not in _FEE_ACTIONS:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "不支持的操作。"})
            return
        body = self._parse_json_body(handler)
        fields = _FEE_FIELDS.get(action, ("row_version",))
        payload = {key: body[key] for key in fields if key in body}
        result = self._agent_request("POST", f"/internal/v1/admin/fee-rules/{quote(rule_id, safe='')}/{action}",
                                     payload=payload, timeout=15, console_principal=context["_console_principal"])
        self._handbook_reply(handler, result, "操作失败，请稍后重试。")

    # -- 试问 -----------------------------------------------------------------------------

    def _handle_handbook_try(self, handler: BaseHTTPRequestHandler) -> None:
        """Forward one question; the Agent answers it read-only, exactly as the robot would."""
        context = self._handbook_context(handler, write=True)
        if context is None:
            return
        question = self._parse_json_body(handler).get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请输入不超过 500 字的问题。"})
            return
        result = self._agent_request("POST", "/internal/v1/admin/handbook-try", payload={"question": question.strip()},
                                     timeout=75, console_principal=context["_console_principal"])
        self._handbook_reply(handler, result, "试问失败，请稍后重试。")
