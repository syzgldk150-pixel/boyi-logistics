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
