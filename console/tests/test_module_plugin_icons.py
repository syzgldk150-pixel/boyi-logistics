"""Collector plugins are listed per business module for the header icons."""
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from console.app import LocalDocFlowApp
from console.routes.automation import handle_get


def _service(instances, warning=""):
    service = LocalDocFlowApp.__new__(LocalDocFlowApp)
    calls, sent = [], []
    service._control_plane_read_context = lambda _handler: {"_console_principal": {}}

    def load(_handler, **options):
        calls.append(options)
        return [], instances, [], frozenset(), warning, False

    service._load_automation_plugin_catalog = load
    service._send_json = lambda _handler, status, body: sent.append((status, body))
    service._control_plane_error = lambda _handler, status, code, _message: sent.append((status, {"code": code}))
    return service, calls, sent


def test_module_plugins_use_cached_module_catalog_and_safe_fields():
    instances = [
        {"automation_id": "cs_east", "instance_name": "客服问题件采集", "state": "ENABLED",
         "status_label": "已启用", "enabled": True, "blocked": False, "account_bindings": {"secret": "x"}},
        {"automation_id": "cs_west", "instance_name": "", "state": "DISABLED",
         "status_label": "已停用", "enabled": False, "blocked": False},
        {"automation_id": "cs_login", "instance_name": "韵达采集", "state": "BLOCKED_LOGIN",
         "status_label": "账号未登录", "enabled": True, "blocked": True},
    ]
    service, calls, sent = _service(instances)
    assert handle_get(service, SimpleNamespace(), "/module-data-sources/customer_service/plugins", "", {})
    assert calls == [{"module": "customer_service", "summary": True, "prefer_stale": True}]
    status, body = sent[0]
    assert status == HTTPStatus.OK
    assert body["data"]["plugins"] == [
        {"automation_id": "cs_east", "name": "客服问题件采集", "status_label": "已启用", "tone": "ok"},
        {"automation_id": "cs_west", "name": "cs_west", "status_label": "已停用", "tone": "idle"},
        {"automation_id": "cs_login", "name": "韵达采集", "status_label": "账号未登录", "tone": "warn"},
    ]


def test_module_plugins_report_unavailable_catalog():
    service, _calls, sent = _service([], warning="插件目录当前不可用")
    assert handle_get(service, SimpleNamespace(), "/module-data-sources/finance/plugins", "", {})
    assert sent == [(HTTPStatus.SERVICE_UNAVAILABLE, {"code": "PLUGIN_CATALOG_UNAVAILABLE"})]


def test_automation_page_has_no_module_category_tabs():
    template = (Path(__file__).parents[1] / "templates" / "automation.html").read_text(encoding="utf-8")
    assert "automation-category-tabs" not in template
