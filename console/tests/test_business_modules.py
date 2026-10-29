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


def test_fixed_navigation_never_queries_legacy_lifecycle_state() -> None:
    app = _Console({"ok": False})

    routes = {item["route"] for item in app._business_module_navigation({})}

    assert len(routes) == len(CONSOLE_NAVIGATION)
    assert "/work-items" not in routes
    assert "/settings/modules" not in routes
    assert app.calls == []


def test_system_status_page_is_retired_from_navigation() -> None:
    app = _Console({"ok": False})
    super_admin = {"id": 1, "role": "super_admin", "is_legacy_basic_auth": False}

    assert app._business_module_navigation(super_admin) == CONSOLE_NAVIGATION
    assert "/settings/system-status" not in {item["route"] for item in CONSOLE_NAVIGATION}
    assert not hasattr(app, "_render_system_status")
    assert len(BUSINESS_MODULE_CATALOG) == 16
    assert app.calls == []


def test_mobile_navigation_repairs_retired_module_manager_preference() -> None:
    app = _Console({"ok": False})
    user = {
        "id": 1,
        "role": "super_admin",
        "is_legacy_basic_auth": False,
        "ui_preferences_json": json.dumps({"mobile_bottom_nav": ["/settings/modules", "/tracking", "/automations"]}),
    }

    routes = app._business_module_mobile_navigation_for_user(user)

    assert "/settings/modules" not in routes
    assert "/tracking" in routes and "/automations" in routes


def test_legacy_module_get_routes_stay_read_only_and_page_route_redirects() -> None:
    app = _Console({"ok": True, "data": {"items": []}})
    handler = _Handler()

    assert business_modules_routes.handle_get(app, handler, "/settings/modules", "/settings/modules", {})
    assert app.redirects == ["/settings/accounts"]
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
