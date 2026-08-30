"""Contract tests for the extension-center projection and routes."""

from __future__ import annotations

import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from console.routes import extensions as extension_routes
from console.routes import automation as automation_routes
from console.services.business_modules import BusinessModulesServiceMixin
from console.services.extensions import ExtensionsServiceMixin


def _package(plugin_id: str) -> dict:
    return {
        "plugin_id": plugin_id,
        "name": f"{plugin_id} package",
        "version": "1.2.3",
        "execution_platform": "server",
        "runtime_model": "ACTION_V1",
        "account_roles": [],
        "resource_roles": [],
        "entrypoints": ["scheduler"],
        "entrypoint_kinds": {"scheduler": "scheduler"},
        "scheduling": {"supported": True, "allowed_kinds": ["daily_times"], "max_daily_times": 2},
    }


def _instance(plugin_id: str, automation_id: str) -> dict:
    return {
        **_package(plugin_id),
        "automation_id": automation_id,
        "instance_name": f"{automation_id} instance",
        "enabled": True,
        "configured": True,
        "state": "ENABLED",
        "reconcile_state": "STABLE",
        "record_version": 2,
        "project_configuration_version": 3,
        "config": {"must_not_render": "secret-like-value"},
        "account_bindings": {},
        "resource_bindings": {},
        "enabled_entrypoints": ["scheduler"],
        "missing_requirements": [],
    }


class _ExtensionApp(ExtensionsServiceMixin, BusinessModulesServiceMixin):
    def __init__(self) -> None:
        self.sent = None

    def _load_automation_plugin_catalog(self, _handler):
        payload = {
            "plugins": [_package("extension_actions"), _package("automations")],
            "instances": [
                _instance("extension_actions", "extension_actions_east"),
                _instance("automations", "automations_legacy"),
            ],
            "resources": [],
            "resource_pool_available": True,
        }
        from console.services.automation_projects import normalize_automation_plugin_catalog

        packages, instances, unsupported = normalize_automation_plugin_catalog(payload)
        return packages, instances, [], unsupported, frozenset(), "", True

    def _send_json(self, _handler, status, payload):
        self.sent = (status, payload)


class ExtensionProjectionTests(unittest.TestCase):
    def _handler(self, *, role="admin", legacy=False, user_id=7):
        return SimpleNamespace(
            current_admin_user={
                "id": user_id,
                "role": role,
                "control_plane_role": role,
                "is_legacy_basic_auth": legacy,
            }
        )

    def test_catalog_filters_fixed_modules_and_never_projects_sensitive_fields(self):
        app = _ExtensionApp()
        packages, warning, can_manage = app._extension_catalog(self._handler())
        self.assertEqual("", warning)
        self.assertTrue(can_manage)
        self.assertEqual(["extension_actions"], [item["plugin_id"] for item in packages])
        package = packages[0]
        self.assertEqual(["无"], package["permissions"][0]["items"])
        instance = package["instances"][0]
        self.assertNotIn("config", instance)
        self.assertNotIn("account_bindings", instance)
        self.assertNotIn("secret-like-value", str(package))

    def test_extension_view_requires_real_non_legacy_mysql_admin(self):
        app = _ExtensionApp()
        self.assertTrue(app._ensure_extension_view_access(self._handler()))
        self.assertFalse(app._ensure_extension_view_access(self._handler(legacy=True)))
        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent[0])
        self.assertEqual("MYSQL_ADMIN_REQUIRED", app.sent[1]["error_code"])
        self.assertFalse(app._ensure_extension_view_access(self._handler(user_id=0)))

    def test_extension_template_distinguishes_packages_and_keeps_install_name_optional(self):
        source = (Path(__file__).parents[1] / "templates" / "extensions.html").read_text(
            encoding="utf-8"
        )
        styles = (Path(__file__).parents[1] / "static" / "style.css").read_text(
            encoding="utf-8"
        )
        script = (Path(__file__).parents[1] / "static" / "extensions.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("<dt>扩展 ID</dt>", source)
        self.assertIn("项目名称（可选）", source)
        self.assertIn('class="extension-install-form"', source)
        self.assertIn('class="ghost-btn extension-upgrade"', source)
        self.assertIn(".extension-upgrade:focus-within", styles)
        self.assertIn(".extension-install-form input,", styles)
        self.assertIn("submit.dataset.requestId || secureRequestId()", script)
        self.assertIn("button.dataset.requestId || secureRequestId()", script)
        self.assertIn('credentials: "same-origin"', script)


class ExtensionRouteTests(unittest.TestCase):
    def test_get_routes_only_list_and_single_detail(self):
        app = SimpleNamespace(calls=[])
        app._render_extensions = lambda handler, query: app.calls.append(("list", handler, query))
        app._render_extension_detail = lambda handler, plugin_id, query: app.calls.append(("detail", plugin_id, query))
        self.assertTrue(extension_routes.handle_get(app, "handler", "/extensions", "/extensions", {"x": ["1"]}))
        self.assertTrue(extension_routes.handle_get(app, "handler", "/extensions/example", "/extensions/example", {}))
        self.assertFalse(extension_routes.handle_get(app, "handler", "/extensions/example/more", "/extensions/example/more", {}))
        self.assertEqual([("list", "handler", {"x": ["1"]}), ("detail", "example", {})], app.calls)

    def test_post_reuses_existing_lifecycle_handlers(self):
        app = SimpleNamespace(calls=[])
        app._handle_automation_plugin_package_upload = lambda handler, **kwargs: app.calls.append(("upload", kwargs))
        app._handle_automation_plugin_instance_action = lambda handler, automation_id, action: app.calls.append(("action", automation_id, action))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/install", "/extensions/install", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/project_east/enable", "/extensions/project_east/enable", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/project_east/upgrade", "/extensions/project_east/upgrade", {}))
        self.assertFalse(extension_routes.handle_post(app, "handler", "/extensions/project_east/nope", "/extensions/project_east/nope", {}))
        self.assertEqual([("upload", {}), ("action", "project_east", "enable"), ("upload", {"automation_id": "project_east"})], app.calls)

    def test_extension_project_setting_link_uses_strict_automation_deep_link(self):
        app = SimpleNamespace(calls=[])
        app._automation_project_id = lambda value: value if value == "project_east" else ""
        app._render_automations = lambda handler, query, **kwargs: app.calls.append((handler, query, kwargs))
        self.assertTrue(
            automation_routes.handle_get(
                app,
                "handler",
                "/automations",
                "/automations",
                {"open_task": ["project_east"]},
            )
        )
        self.assertEqual("project_east", app.calls[0][2]["open_task_id"])
        self.assertTrue(
            automation_routes.handle_get(
                app,
                "handler",
                "/automations",
                "/automations",
                {"open_task": ["not-a-project"]},
            )
        )
        self.assertIsNone(app.calls[1][2]["open_task_id"])
