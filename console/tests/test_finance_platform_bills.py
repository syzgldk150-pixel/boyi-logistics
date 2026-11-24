"""财务“融辉对账单/韵达对账单”的 Console 边界：路由、权限、参数转发与失败原因（虚构数据）。"""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from console.app import LocalDocFlowApp
from console.navigation import CONSOLE_NAVIGATION
from console.routes import finance
from shared.identity_routes import business_permission, console_permission


class Handler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.current_admin_user = {"id": 7, "username": "fixture"}

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def result(self):
        return json.loads(self.wfile.getvalue())


@pytest.fixture
def app():
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app.settings = SimpleNamespace(agent_timeout_seconds=2, app_title="测试")
    app._receipt_attachment_context = lambda: {"_console_principal": {"actor_id": "7"}}
    app._normalize_browser_request_uuid = lambda value: ""
    return app


def test_pages_and_queries_need_finance_view():
    for path in ("/modules/finance/ronghui-bills", "/modules/finance/yunda-bills", "/finance/platform-bills/ronghui"):
        assert console_permission("GET", path) == "finance:view"
    assert business_permission("platform-prepay-bills") == "finance:view"
    group = [item["route"] for item in CONSOLE_NAVIGATION if item["group"] == "finance"]
    assert group == ["/modules/finance", "/modules/finance/ronghui-bills", "/modules/finance/yunda-bills",
                     "/modules/finance/delivery-bills"]


def test_routes_dispatch():
    mock = Mock()
    assert finance.handle_get(mock, object(), "/modules/finance/yunda-bills", "", {})
    mock._render_platform_bills.assert_called_once()
    assert mock._render_platform_bills.call_args.args[1] == "yunda"
    assert finance.handle_get(mock, object(), "/finance/platform-bills/ronghui", "", {"action": ["summary"]})
    assert mock._handle_platform_bills.call_args.args[1:] == ("ronghui", {"action": ["summary"]})
    assert not finance.handle_get(mock, object(), "/finance/platform-bills/best", "", {})


def test_query_is_forwarded_and_platform_reason_is_kept(app):
    calls = []

    def agent(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {"ok": True, "data": {"platform": "yunda", "rows": [], "columns": []}}

    app._agent_request = agent
    handler = Handler()
    app._handle_platform_bills(handler, "yunda", {
        "account_id": ["yunda_default"], "action": ["detail"], "start": ["2026-01-01"], "end": ["2026-01-02"],
        "filters": ['{"account": "77"}'], "ignored": ["x"]})
    assert handler.status == 200 and handler.result()["data"]["platform"] == "yunda"
    method, endpoint, kwargs = calls[0]
    assert (method, endpoint) == ("POST", "/internal/v1/business/platform-prepay-bills")
    assert kwargs["payload"]["params"] == {"platform": "yunda", "account_id": "yunda_default", "action": "detail",
                                           "start": "2026-01-01", "end": "2026-01-02", "filters": {"account": "77"}}
    assert kwargs["console_principal"] == {"actor_id": "7"}

    app._agent_request = lambda *a, **k: {"ok": False, "error_code": "PLATFORM_REJECTED", "error": "韵达拒绝了查询：只可查询最长31天数据"}
    failed = Handler()
    app._handle_platform_bills(failed, "yunda", {"action": ["summary"]})
    assert failed.status == 502 and "31天" in failed.result()["message"]

    bad = Handler()
    app._handle_platform_bills(bad, "ronghui", {"filters": ['{"account": 1}']})
    assert bad.status == 400

    app._receipt_attachment_context = lambda: None
    denied = Handler()
    app._handle_platform_bills(denied, "ronghui", {"action": ["summary"]})
    assert denied.status == 403
