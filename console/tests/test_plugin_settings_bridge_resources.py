"""The settings bridge reads only the resource pool, never the whole plugin catalog."""

from http import HTTPStatus
from types import SimpleNamespace

from app import LocalDocFlowApp


def test_context_reads_settings_resources_instead_of_the_full_catalog():
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app._session_secret = "fixture-secret"
    principal = {"actor_id": "1", "roles": ["super_admin"]}
    app._control_plane_write_context = lambda _handler: {
        "actor_roles": ["super_admin"], "actor": {"actor_id": "1"}, "_console_principal": principal,
    }
    session = app._automation_plugin_settings_bridge_token("automation.demo", "1")
    app._read_control_plane_json = lambda _handler: {"bridge_session": session, "operation": "context", "payload": {}}
    calls = []

    def agent_request(method, path, **_kwargs):
        calls.append((method, path))
        if path.endswith("/settings-context"):
            return {"ok": True, "data": {"settings_mode": "custom", "resource_roles": [{"role": "input"}],
                                         "account_roles": [], "config_schema": {}, "config": {}}}
        if path.endswith("/settings-resources"):
            return {"ok": True, "data": {"resource_pool_available": True, "resource_pool_problem": "", "resources": [
                {"resource_id": "phase7.input_sheet", "name": "输入表格", "kind": "feishu_sheet",
                 "status": "available", "purpose": "输入表格", "problem_code": ""}]}}
        raise AssertionError(f"unexpected Agent call {path}")

    app._agent_request = agent_request
    sent = {}
    app._send_json = lambda _handler, status, payload: sent.update(status=status, payload=payload)

    app._handle_automation_plugin_settings_bridge(SimpleNamespace(), "automation.demo")

    assert [path for _method, path in calls] == [
        "/internal/v1/automation/instances/automation.demo/settings-context",
        "/internal/v1/automation/instances/automation.demo/settings-resources",
    ]
    assert sent["status"] == HTTPStatus.OK
    data = sent["payload"]["data"]
    assert data["resource_catalog_available"] is True
    assert [item["resource_id"] for item in data["resources"]] == ["phase7.input_sheet"]


def _asset_app(roles):
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app._control_plane_read_context = lambda _handler: {
        "actor_roles": roles, "actor": {"actor_id": "1"}, "_console_principal": {"roles": roles},
    }
    calls = []

    def binary(path, **_kwargs):
        calls.append(path)
        if path.endswith("/settings.css"):
            return {"ok": True, "data": b"body{color:red}", "content_type": "text/css"}
        return {"ok": True, "data": b"<html><head><link rel=\"stylesheet\" href=\"settings.css\"></head><body></body></html>",
                "content_type": "text/html"}

    app._agent_binary_request = binary
    sent = []
    app._send_bytes = lambda *args, **kwargs: sent.append((args, kwargs))
    app._control_plane_error = lambda _handler, status, code, _message: sent.append(("error", status, code))
    return app, calls, sent


def test_settings_files_are_read_from_the_agent_once_until_plugins_change():
    app, calls, sent = _asset_app(["super_admin"])
    handler = SimpleNamespace(send_response=lambda *_a: None, send_header=lambda *_a: None,
                              end_headers=lambda: None, wfile=SimpleNamespace(write=lambda _b: None))
    for _ in range(2):
        app._handle_automation_plugin_settings_asset(handler, "automation.demo", "index.html")
    assert [path.rsplit("/", 1)[-1] for path in calls] == ["index.html", "settings.css"]
    assert not [item for item in sent if item and item[0] == "error"]

    app._clear_automation_plugin_catalog_cache()  # install / upgrade / uninstall
    app._handle_automation_plugin_settings_asset(handler, "automation.demo", "index.html")
    assert len(calls) == 4


def test_cached_settings_files_still_require_super_admin():
    app, calls, sent = _asset_app(["admin"])
    app._handle_automation_plugin_settings_asset(SimpleNamespace(), "automation.demo", "index.html")
    assert calls == []
    assert sent == [("error", HTTPStatus.FORBIDDEN, "SUPER_ADMIN_REQUIRED")]


def test_bridge_reuses_the_page_settings_context_once():
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app._session_secret = "fixture-secret"
    app._control_plane_write_context = lambda _handler: {
        "actor_roles": ["super_admin"], "actor": {"actor_id": "1"}, "_console_principal": {},
    }
    session = app._automation_plugin_settings_bridge_token("automation.demo", "1")
    page_context = {"ok": True, "data": {"settings_mode": "custom", "resource_roles": [], "account_roles": [],
                                         "config_schema": {}, "config": {}}}
    app._remember_settings_context(session, "automation.demo", page_context)
    app._read_control_plane_json = lambda _handler: {"bridge_session": session, "operation": "context", "payload": {}}
    calls = []
    app._agent_request = lambda method, path, **_kwargs: calls.append(path) or page_context
    app._send_json = lambda *_args: None

    app._handle_automation_plugin_settings_bridge(SimpleNamespace(), "automation.demo")
    assert calls == []
    app._handle_automation_plugin_settings_bridge(SimpleNamespace(), "automation.demo")  # a reload reads again
    assert calls == ["/internal/v1/automation/instances/automation.demo/settings-context"]
