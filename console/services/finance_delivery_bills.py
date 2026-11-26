"""财务“派件账单”（邵阳大祥S站）：页面与转发。

账单由 Agent 组成：每日到货表里大祥S站的单 ∩ 融辉结算明细（大祥S站账号实时读取）里已有
“付派送费”的单；派送费按公斤段规则计算（配送方式为自提等的单不算），增值费与其他收入取融辉
结算明细（2026-11-26 起，不再人工选）。Console 不计算、不写库，只转发白名单字段：查询经签名
``GET /internal/v1/admin/finance/delivery-bills``，写操作先按财务写入要求核对真实后台账号、
同源请求与 ``X-Browser-Request-UUID``，再带同一请求标识转发。权限由 ``shared.identity_routes``
决定（财务可见才能查看，可操作才能确认，派送费规则与设置只允许超级管理员）。
"""

from __future__ import annotations

import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import quote

from console.app_support import current_admin_user
from shared.finance.delivery_bills import MAX_SPAN_DAYS, ROUNDINGS, STATION_NAME

_BASE = "/internal/v1/admin/finance/delivery-bills"
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Console path suffix -> (Agent path suffix, allowed body fields, timeout seconds, carries a request id)
_POSTS = {
    "confirm": ("/confirm", ("snapshot_id", "items"), 60, True),
    "revoke": ("/revoke", ("snapshot_id", "tracking_number", "expected_revision"), 30, True),
    "rules": ("/rules", ("effective_from", "segments", "rounding", "note"), 20, True),
    "rules/trial": ("/rules/trial", ("rule", "settlement_weight"), 20, False),
    "settings": ("/settings", ("lookahead_days", "include_types", "exclude_types", "value_added_types",
                               "no_fee_methods", "expected_revision"), 20, True),
}
_STATUSES = (400, 403, 404, 409, 422, 502, 503)


class FinanceDeliveryBillsServiceMixin:
    def _render_delivery_bills(self, handler: BaseHTTPRequestHandler) -> None:
        user = getattr(handler, "current_admin_user", None) or current_admin_user() or {}
        is_super_admin = self._is_super_admin_user(user)
        can_manage = is_super_admin or "finance:operate" in (user.get("access_permissions") or ())
        template = self.template_env.get_template("finance_delivery_bills.html")
        self._send_html(handler, template.render(
            app_title=self.settings.app_title, can_manage=can_manage, is_super_admin=is_super_admin,
            station=STATION_NAME, max_span_days=MAX_SPAN_DAYS, roundings=ROUNDINGS,
        ))

    def _delivery_reply(self, handler: BaseHTTPRequestHandler, result: dict[str, Any], fallback: str) -> None:
        if result.get("ok"):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result.get("data")})
            return
        error = result.get("error")
        code = result.get("error_code") or (error.get("code") if isinstance(error, dict) else None) or "DELIVERY_BILL_FAILED"
        message = (error.get("message") if isinstance(error, dict) else error) or fallback
        status = result.get("status") if result.get("status") in _STATUSES else 502
        self._send_json(handler, status, {"ok": False, "error_code": str(code), "message": str(message)})

    def _delivery_principal(self, handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
        principal = self._mysql_console_principal()
        if principal is None:
            self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error_code": "MYSQL_ADMIN_SESSION_REQUIRED",
                                                            "message": "派件账单需要已登录的后台账号。"})
        return principal

    def _handle_delivery_bills_get(self, handler: BaseHTTPRequestHandler, action: str,
                                   query: dict[str, list[str]] | None = None) -> None:
        principal = self._delivery_principal(handler)
        if principal is None:
            return
        if action == "bill":
            start = str(((query or {}).get("start") or [""])[0]).strip()
            end = str(((query or {}).get("end") or [""])[0]).strip()
            if not _ISO_DATE.fullmatch(start) or not _ISO_DATE.fullmatch(end):
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error_code": "INVALID_DATE",
                                                                  "message": "请选择到货表日期范围。"})
                return
            result = self._agent_request("GET", f"{_BASE}?start={start}&end={end}", timeout=100,
                                         console_principal=principal)
            self._delivery_reply(handler, result, "派件账单读取失败，请稍后重试。")
            return
        endpoint = {"rules": "/rules", "settings": "/settings"}[action]
        result = self._agent_request("GET", _BASE + endpoint, timeout=15, console_principal=principal)
        self._delivery_reply(handler, result, "派件账单设置读取失败，请稍后重试。")

    def _handle_delivery_bills_post(self, handler: BaseHTTPRequestHandler, action: str, rule_id: str | None = None) -> None:
        if action == "rules/retire":
            agent_path, fields, timeout, with_request = f"/rules/{quote(str(rule_id), safe='')}/retire", ("expected_revision",), 20, True
        else:
            agent_path, fields, timeout, with_request = _POSTS[action]
        written = self._finance_write_body(handler)
        if written is None:
            return
        body, _actor, request_id = written
        principal = self._delivery_principal(handler)
        if principal is None:
            return
        payload = {key: body[key] for key in fields if key in body}
        if with_request:
            payload["request_id"] = request_id
        result = self._agent_request("POST", _BASE + agent_path, payload=payload, timeout=timeout,
                                     console_principal=principal)
        self._delivery_reply(handler, result, "派件账单操作未完成，请刷新后核对结果。")
