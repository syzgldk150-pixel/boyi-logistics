"""Shared deterministic read shortcuts, before either channel calls the model."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from agent.direct_tool_router import (
    BUSINESS_FINANCE_WRITE_RE, business_finance_request_from_text,
    business_operations_request_from_text, direct_tool_request_from_text,
    format_business_operations_summary_reply, format_tool_reply, parse_login_send_code_session,
)
from agent.harness.sidecar import SidecarResult


class ChatTextQueries:
    def __init__(self, reader, *, today=None):
        self.reader = reader
        self.today = today or (lambda: datetime.now(timezone(timedelta(hours=8))).date())

    def reply(self, actor, message, request_id):
        direct = direct_tool_request_from_text(message)
        if direct and not direct.get("automation_route_key"):
            name, params = direct["tool_name"], dict(direct["params"])
            result = direct.get("local_result")
            if result is None:
                result = self.reader(actor, name, params, request_id)
            return SidecarResult(format_tool_reply(name, result), 1)
        if parse_login_send_code_session(message):
            return SidecarResult("请在业务账号页面选择要登录的账号。飞书中也可以发送“登录”选择账号。", 0)
        # Writes remain installed-plugin selection; never reinterpret them as reads.
        if BUSINESS_FINANCE_WRITE_RE.search(message):
            return None
        operations = business_operations_request_from_text(message, today=self.today())
        finance = operations or business_finance_request_from_text(message, today=self.today())
        if finance is None:
            return None
        if finance.get("reply"):
            return SidecarResult(str(finance["reply"]), 0)
        params = dict(finance["params"])
        result = self.reader(actor, "query_business_finance", params, request_id)
        if operations is None or not result.get("success"):
            return SidecarResult(format_tool_reply("query_business_finance", result), 1)
        operation_result = self.reader(actor, "query_automation_operations",
            {name: params[name] for name in ("start_date", "end_date")}, request_id)
        return SidecarResult(format_business_operations_summary_reply(result, operation_result), 2)
