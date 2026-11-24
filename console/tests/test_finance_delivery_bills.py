"""财务“派件账单”的 Console 边界：路由、权限、白名单转发与失败原因（虚构数据）。"""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from console.app import LocalDocFlowApp
from console.routes import finance
from shared.identity_permissions import SUPER_ADMIN
from shared.identity_routes import agent_permission, business_permission, console_permission


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
    app._mysql_console_principal = lambda *args: {"actor_id": "7"}
    return app


def test_permissions():
    assert console_permission("GET", "/modules/finance/delivery-bills") == "finance:view"
    assert console_permission("GET", "/finance/delivery-bills") == "finance:view"
    assert console_permission("POST", "/finance/delivery-bills/confirm") == "finance:operate"
    assert console_permission("POST", "/finance/delivery-bills/fees/save") == "finance:operate"
    assert console_permission("POST", "/finance/delivery-bills/rules/trial") == "finance:operate"
    for path in ("/finance/delivery-bills/rules", "/finance/delivery-bills/settings", "/finance/delivery-bills/rules/3/retire"):
        assert console_permission("POST", path) == SUPER_ADMIN
    assert agent_permission("GET", "/internal/v1/admin/finance/delivery-bills") == "finance:view"
    assert agent_permission("POST", "/internal/v1/admin/finance/delivery-bills/confirm") == "finance:operate"
    assert business_permission("delivery-bill-settlements") == "finance:view"


def test_routes_dispatch():
    mock = Mock()
    assert finance.handle_get(mock, object(), "/modules/finance/delivery-bills", "", {})
    mock._render_delivery_bills.assert_called_once()
    assert finance.handle_get(mock, object(), "/finance/delivery-bills", "", {"start": ["2026-11-01"]})
    assert mock._handle_delivery_bills_get.call_args.args[1] == "bill"
    assert finance.handle_get(mock, object(), "/finance/delivery-bills/rules", "", {})
    assert mock._handle_delivery_bills_get.call_args.args[1] == "rules"
    for action in ("confirm", "revoke", "fees/prepare", "fees/compute", "fees/save", "rules", "rules/trial", "settings"):
        assert finance.handle_post(mock, object(), f"/finance/delivery-bills/{action}", "", {})
        assert mock._handle_delivery_bills_post.call_args.args[1] == action
    assert finance.handle_post(mock, object(), "/finance/delivery-bills/rules/12/retire", "", {})
    assert mock._handle_delivery_bills_post.call_args.args[1:] == ("rules/retire", "12")
    assert not finance.handle_post(mock, object(), "/finance/delivery-bills/other", "", {})


def test_query_is_forwarded_and_reason_kept(app):
    calls = []

    def agent(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {"ok": True, "data": {"rows": []}}

    app._agent_request = agent
    handler = Handler()
    app._handle_delivery_bills_get(handler, "bill", {"start": ["2026-11-01"], "end": ["2026-11-24"], "x": ["1"]})
    assert handler.status == 200
    assert calls[0][:2] == ("GET", "/internal/v1/admin/finance/delivery-bills?start=2026-11-01&end=2026-11-24")
    assert calls[0][2]["console_principal"] == {"actor_id": "7"}

    bad = Handler()
    app._handle_delivery_bills_get(bad, "bill", {"start": ["2026/11/01"], "end": ["2026-11-24"]})
    assert bad.status == 400

    app._agent_request = lambda *a, **k: {"ok": False, "status": 502,
                                          "error": {"code": "DELIVERY_SETTLEMENT_UNAVAILABLE", "message": "融辉结算明细读取失败：登录已失效"}}
    failed = Handler()
    app._handle_delivery_bills_get(failed, "bill", {"start": ["2026-11-01"], "end": ["2026-11-24"]})
    assert failed.status == 502 and "登录已失效" in failed.result()["message"]

    app._mysql_console_principal = lambda *args: None
    denied = Handler()
    app._handle_delivery_bills_get(denied, "rules", {})
    assert denied.status == 403


def test_writes_forward_only_allowed_fields_with_the_browser_request_id(app):
    calls = []
    app._agent_request = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs)) or {"ok": True, "data": {}}
    app._finance_write_body = lambda handler: ({"snapshot_id": "s", "items": [{"tracking_number": "R1"}],
                                                "ronghui_amount": "999"}, object(), "11111111-2222-4333-8444-555555555555")
    handler = Handler()
    app._handle_delivery_bills_post(handler, "confirm")
    assert handler.status == 200
    method, endpoint, kwargs = calls[-1]
    assert (method, endpoint) == ("POST", "/internal/v1/admin/finance/delivery-bills/confirm")
    assert kwargs["payload"] == {"snapshot_id": "s", "items": [{"tracking_number": "R1"}],
                                 "request_id": "11111111-2222-4333-8444-555555555555"}

    app._finance_write_body = lambda handler: ({"snapshot_id": "s", "tracking_number": "R1", "request_id": "x"}, object(), "id")
    app._handle_delivery_bills_post(Handler(), "fees/prepare")
    assert calls[-1][2]["payload"] == {"snapshot_id": "s", "tracking_number": "R1"}

    app._finance_write_body = lambda handler: ({"expected_revision": 2}, object(), "rid")
    app._handle_delivery_bills_post(Handler(), "rules/retire", "12")
    assert calls[-1][1] == "/internal/v1/admin/finance/delivery-bills/rules/12/retire"
    assert calls[-1][2]["payload"] == {"expected_revision": 2, "request_id": "rid"}

    app._finance_write_body = lambda handler: None
    before = len(calls)
    app._handle_delivery_bills_post(Handler(), "confirm")
    assert len(calls) == before
