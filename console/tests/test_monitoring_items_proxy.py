"""Console message reads keep account selection and link targets server-owned."""

from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

import pytest

from console.routes.monitoring import handle_get
from console.services.monitoring_finance import MonitoringFinanceServiceMixin
from shared.identity_routes import console_permission


class MonitoringApp(MonitoringFinanceServiceMixin):
    def __init__(self, result=None):
        self.result = result if result is not None else {
            "ok": True, "data": {"ok": True, "items": [], "total": 0, "page": 1, "page_size": 20},
        }
        self.calls = []
        self.sent_status = None
        self.sent_payload = None

    def _agent_request(self, method, endpoint, *, payload=None, timeout=None):
        self.calls.append((method, endpoint, payload, timeout))
        return self.result

    def _send_json(self, _handler, status, payload):
        self.sent_status = status
        self.sent_payload = payload


def test_items_route_forwards_only_source_category_page_and_force():
    app = MonitoringApp()
    assert handle_get(app, object(), "/monitoring/items", "/monitoring/items", {
        "system": ["yunda"], "category_id": ["yunda:work_orders"], "page": ["2"], "force": ["yes"],
        "account_id": ["other"], "session_profile": ["other"], "url": ["https://invalid.test/"],
        "page_size": ["1000"],
    })
    method, endpoint, payload, timeout = app.calls[0]
    assert method == "GET"
    assert urlparse(endpoint).path == "/internal/v1/admin/monitoring/items"
    assert parse_qs(urlparse(endpoint).query) == {
        "system": ["yunda"], "category_id": ["yunda:work_orders"], "page": ["2"], "force": ["1"],
    }
    assert payload is None
    assert timeout == 75
    assert app.sent_status == HTTPStatus.OK
    assert app.sent_payload == app.result["data"]


@pytest.mark.parametrize("query", [
    {"system": ["other"]}, {"system": ["yunda"], "page": ["0"]},
    {"system": ["yunda"], "page": ["10001"]}, {"system": ["yunda"], "page": ["1.5"]},
    {"system": ["yunda"], "page": ["invalid"]}, {},
])
def test_invalid_items_query_does_not_call_agent(query):
    app = MonitoringApp()
    app._handle_monitoring_items(object(), query)
    assert app.sent_status == HTTPStatus.BAD_REQUEST
    assert app.sent_payload["ok"] is False
    assert app.sent_payload["total"] is None
    assert app.calls == []


def test_items_defaults_to_first_page_and_fixed_page_size():
    app = MonitoringApp()
    app._handle_monitoring_items(object(), {"system": ["ronghui"]})
    assert parse_qs(urlparse(app.calls[0][1]).query)["page"] == ["1"]
    assert "page_size" not in parse_qs(urlparse(app.calls[0][1]).query)
    assert app.sent_payload["page_size"] == 20


def test_items_proxy_preserves_authorization_failure_without_claiming_no_messages():
    app = MonitoringApp({"ok": False, "status": 403,
                         "error_code": "IDENTITY_PERMISSION_DENIED", "error": "当前身份没有此功能权限。"})
    app._handle_monitoring_items(object(), {"system": ["yunda"]})
    assert app.sent_status == HTTPStatus.FORBIDDEN
    assert app.sent_payload["error_code"] == "IDENTITY_PERMISSION_DENIED"
    assert app.sent_payload["total"] is None


def test_summary_proxy_failure_uses_unknown_counts():
    app = MonitoringApp({"ok": False, "error": "查询失败"})
    result = app._monitoring_snapshot_from_agent()
    assert result["ok"] is False
    assert all(value is None for value in result["totals"].values())


def test_detail_link_does_not_forward_browser_target_or_account_metadata():
    app = MonitoringApp()
    app._handle_monitoring_detail_link(object(), {
        "system": ["yunda"], "category_id": ["yunda:001"], "resource_id": ["attacker"],
        "title": ["Other"], "type_code": ["999"], "target_title": ["Other"],
        "account_id": ["other"], "session_profile": ["other"], "url": ["https://invalid.test/"],
    })
    assert app.calls[0][2] == {"system": "yunda", "category_id": "yunda:001"}


@pytest.mark.parametrize("path", ["/monitoring/items", "/monitoring/detail-link"])
def test_message_reads_keep_super_admin_permission(path):
    assert console_permission("GET", path) == "super_admin"
