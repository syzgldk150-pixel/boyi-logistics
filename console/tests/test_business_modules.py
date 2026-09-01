from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from console.navigation import CONSOLE_NAVIGATION
from console.routes import business_modules as business_modules_routes
from console.services.business_modules import BusinessModulesServiceMixin
from shared.business_modules import BUSINESS_MODULE_CATALOG


class _Console(BusinessModulesServiceMixin):
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict | None]] = []
        self.sent: list[tuple[int, dict]] = []
        self.redirects: list[str] = []
        self.settings = SimpleNamespace(app_title="test")

    def _agent_request(self, method, endpoint, *, payload=None, **_kwargs):
        self.calls.append((method, endpoint, payload))
        return self.response

    def _mysql_console_principal(self, _user):
        return {"actor_id": "1"}

    def _send_json(self, _handler, status, payload):
        self.sent.append((int(status), payload))

    def _send_html(self, _handler, body):
        self.html = body

    def _redirect(self, _handler, location):
        self.redirects.append(location)


class _Handler:
    def __init__(self, *, role: str = "super_admin", user_id: int = 1, legacy: bool = False) -> None:
        self.current_admin_user = {
            "id": user_id,
            "role": role,
            "username": "owner",
            "is_legacy_basic_auth": legacy,
        }


def _health(*, include_components: bool = True) -> dict:
    components = (
        {
            "mysql": "connected",
            "scheduler": {"state": "running"},
            "automation_plugins": {"ok": True},
        }
        if include_components
        else {}
    )
    return {
        "ok": True,
        "data": {
            "status": "ok",
            "release_sha": "test-sha",
            "instance_id": "agent-1",
            "uptime": "1d 2h 3m",
            "memory_mb": 12.5,
            "components": components,
            "last_tool_run": {"must_not": "appear"},
        },
    }


def test_fixed_navigation_never_queries_legacy_lifecycle_state() -> None:
    app = _Console({"ok": False})

    routes = {item["route"] for item in app._business_module_navigation({})}

    assert len(routes) == len(CONSOLE_NAVIGATION)
    assert "/work-items" not in routes
    assert "/settings/modules" not in routes
    assert app.calls == []


def test_system_status_is_super_admin_only_and_not_a_fixed_module() -> None:
    app = _Console(_health())
    super_admin = {"id": 1, "role": "super_admin", "is_legacy_basic_auth": False}
    ordinary_admin = {"id": 2, "role": "admin", "is_legacy_basic_auth": False}

    assert "/settings/system-status" in {item["route"] for item in app._business_module_navigation(super_admin)}
    assert "/settings/system-status" not in {item["route"] for item in app._business_module_navigation(ordinary_admin)}
    assert "/settings/system-status" not in {item["route"] for item in app._business_module_navigation({})}
    assert len(BUSINESS_MODULE_CATALOG) == 15


def test_mobile_navigation_repairs_retired_module_manager_preference() -> None:
    app = _Console(_health())
    user = {
        "id": 1,
        "role": "super_admin",
        "is_legacy_basic_auth": False,
        "ui_preferences_json": json.dumps({"mobile_bottom_nav": ["/settings/modules", "/tracking", "/automations"]}),
    }

    routes = app._business_module_mobile_navigation_for_user(user)

    assert "/settings/modules" not in routes
    assert "/tracking" in routes and "/automations" in routes


def test_system_status_projects_only_whitelisted_health_fields() -> None:
    app = _Console(_health(include_components=False))

    snapshot = app._system_status_snapshot(_Handler())

    assert app.calls == [("GET", "/internal/v1/health", None)]
    assert snapshot["release_sha"] == "test-sha"
    assert snapshot["memory_mb"] == 12.5
    assert snapshot["components"][0]["value"] == "不可用"
    assert "last_tool_run" not in snapshot


def test_system_status_supports_real_scalar_and_ok_component_shapes() -> None:
    app = _Console(_health())

    snapshot = app._system_status_snapshot(_Handler())
    components = {item["key"]: item["value"] for item in snapshot["components"]}

    assert components["mysql"] == "connected"
    assert components["scheduler"] == "running"
    assert components["automation_plugins"] == "正常"


def test_system_status_marks_agent_failures_and_missing_values_unavailable() -> None:
    app = _Console({"ok": False, "error": {"code": "UNAVAILABLE"}})

    snapshot = app._system_status_snapshot(_Handler())

    assert snapshot["available"] is False
    assert snapshot["status"] == "不可用"
    assert all(item["value"] == "不可用" for item in snapshot["components"])


def test_system_status_requires_super_admin_and_reuses_status_template() -> None:
    root = Path(__file__).resolve().parents[1]
    app = _Console(_health())
    app.template_env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=True)
    app.template_env.globals.update(current_admin_user=lambda: None, navigation_for_user=lambda: (), mobile_navigation_for_user=lambda _user: ())

    app._render_system_status(_Handler(role="admin"), {})
    assert app.sent[-1][0] == 403
    app._render_system_status(_Handler(legacy=True), {})
    assert app.sent[-1][0] == 403

    app._render_system_status(_Handler(), {})
    assert "系统状态 - test" in app.html
    assert "智能服务与平台组件健康概览" in app.html
    assert "test-sha" in app.html


def test_legacy_module_get_routes_stay_read_only_and_page_route_redirects() -> None:
    app = _Console({"ok": True, "data": {"items": []}})
    handler = _Handler()

    assert business_modules_routes.handle_get(app, handler, "/settings/modules", "/settings/modules", {})
    assert app.redirects == ["/settings/system-status"]
    assert business_modules_routes.handle_get(app, handler, "/settings/modules/receipts/audit", "/settings/modules/receipts/audit", {})
    assert app.calls[-1][:2] == ("GET", "/internal/v1/admin/modules/receipts/audit")
    assert business_modules_routes.handle_post(app, handler, "/settings/modules/lifecycle", "/settings/modules/lifecycle", {}) is False


def test_legacy_module_reads_require_real_super_admin() -> None:
    for handler in (_Handler(role="admin"), _Handler(legacy=True), _Handler(user_id=0)):
        app = _Console({"ok": True, "data": {"items": []}})

        assert business_modules_routes.handle_get(
            app,
            handler,
            "/settings/modules/receipts/audit",
            "/settings/modules/receipts/audit",
            {},
        )
        assert app.sent[-1][0] == 403
        assert app.calls == []
