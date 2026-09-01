"""Contract tests for the extension-center projection and routes."""

from __future__ import annotations

import subprocess
import shutil
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

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
        self.assertEqual("extension_actions_east instance", package["display_name"])
        self.assertEqual("使用中", package["display_status"])
        self.assertEqual("已用于 1 个项目", package["instance_count_label"])
        instance = package["instances"][0]
        self.assertEqual("使用中", instance["display_status"])
        self.assertNotIn("config", instance)
        self.assertNotIn("account_bindings", instance)
        self.assertNotIn("secret-like-value", str(package))

    def test_known_business_extensions_use_plain_chinese_names(self):
        self.assertEqual(
            "到货统计",
            ExtensionsServiceMixin._extension_display_name(
                {"plugin_id": "arrival_stats", "name": "arrival_stats"}, []
            ),
        )

    def test_extension_list_keeps_runtime_details_out_of_the_novice_view(self):
        app = _ExtensionApp()
        packages, warning, can_manage = app._extension_catalog(self._handler())
        template_dir = Path(__file__).parents[1] / "templates"
        template = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(("html",)),
        ).get_template("extensions.html")

        html = template.render(
            app_title="Console",
            packages=packages,
            detail=None,
            extension_warning=warning,
            can_manage_extensions=can_manage,
        )

        self.assertIn("我的扩展", html)
        self.assertIn("已用于 1 个项目", html)
        self.assertNotIn("权限 · 账号", html)
        self.assertNotIn("Host API", html)
        self.assertNotIn("ACTION_V1", html)
        self.assertEqual(1, html.count("data-extension-open"))

    def test_extension_view_requires_real_non_legacy_mysql_admin(self):
        app = _ExtensionApp()
        self.assertTrue(app._ensure_extension_view_access(self._handler()))
        self.assertFalse(app._ensure_extension_view_access(self._handler(legacy=True)))
        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent[0])
        self.assertEqual("MYSQL_ADMIN_REQUIRED", app.sent[1]["error_code"])
        self.assertFalse(app._ensure_extension_view_access(self._handler(user_id=0)))

    def test_extension_template_uses_one_current_drag_install_entry(self):
        source = (Path(__file__).parents[1] / "templates" / "extensions.html").read_text(
            encoding="utf-8"
        )
        styles = (Path(__file__).parents[1] / "static" / "style.css").read_text(
            encoding="utf-8"
        )
        script = (Path(__file__).parents[1] / "static" / "extensions.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("data-extension-open", source)
        self.assertIn("data-extension-dialog", source)
        self.assertIn("data-extension-dropzone", source)
        self.assertIn("把扩展 ZIP 拖到这里", source)
        self.assertNotIn("data-extension-legacy-install-form", source)
        self.assertNotIn("安装旧版 Action v1", source)
        self.assertNotIn("安装 Service v2", source)
        self.assertIn('class="extension-install-form"', source)
        self.assertIn('class="ghost-btn extension-upgrade"', source)
        self.assertIn(".extension-upgrade:focus-within", styles)
        self.assertIn(".extension-install-dialog", styles)
        self.assertIn("new DataTransfer()", script)
        self.assertIn("inspectForm.requestSubmit()", script)
        self.assertIn("dialog.showModal()", script)
        self.assertIn('credentials: "same-origin"', script)

    def test_current_extension_dialog_uses_closed_projection_and_safe_two_phase_install(self):
        source = (Path(__file__).parents[1] / "templates" / "extensions.html").read_text(
            encoding="utf-8"
        )
        script = (Path(__file__).parents[1] / "static" / "extensions.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("<dialog", source)
        self.assertIn("安装设置", source)
        self.assertIn("使用权限", source)
        self.assertNotIn("Host API", source)
        self.assertNotIn("运行平台", source)
        self.assertNotIn("扩展 ID", source)
        self.assertIn('action="/extensions/inspect"', source)
        self.assertIn('action="/extensions/install"', source)
        self.assertIn('data-extension-final-form', source)
        self.assertIn('data-extension-inspection-warnings', source)
        self.assertIn('body.set("package", file)', script)
        self.assertIn('body.set("request_id", inspectedRequestId)', script)
        self.assertIn('body.set("package", state.frozen.packageFile)', script)
        self.assertIn('body.set("request_id", state.frozen.requestId)', script)
        self.assertIn('body.set("intent", state.frozen.serializedIntent)', script)
        self.assertIn('state.frozen = Object.freeze({', script)
        self.assertIn('state.finalSent = true;', script)
        self.assertIn('credentials: "same-origin"', script)
        self.assertIn('textContent', script)
        self.assertNotIn('innerHTML', script)

    def test_service_v2_intent_has_exact_keys_and_no_browser_authority_fields(self):
        source = (Path(__file__).parents[1] / "templates" / "extensions.html").read_text(
            encoding="utf-8"
        )
        styles = (Path(__file__).parents[1] / "static" / "style.css").read_text(
            encoding="utf-8"
        )
        script = (Path(__file__).parents[1] / "static" / "extensions.js").read_text(
            encoding="utf-8"
        )
        expected_keys = (
            "instance_name",
            "config",
            "account_bindings",
            "resource_bindings",
            "enabled_entrypoints",
            "schedule",
            "permissions_confirmed",
        )
        for key in expected_keys:
            self.assertIn(f"{key}:", script)
        final_form = source.split('data-extension-final-form', 1)[1].split('</form>', 1)[0]
        for forbidden in (
            "automation_id",
            "package_sha256",
            "manifest",
            "digest",
            "service",
            "operation",
            "credential",
        ):
            self.assertNotIn(f'name="{forbidden}"', final_form)
        self.assertIn("account_options", script)
        self.assertIn("resource_options", script)
        self.assertIn("account_pool_available", script)
        self.assertIn("resource_pool_available", script)
        self.assertIn("warnings", script)
        self.assertIn("operations:", script)
        self.assertIn("SAFE_BINDING_ID", script)
        self.assertIn("select.disabled = !hasUsableOptions", script)
        self.assertIn("if (!configSchema || !scheduling) return null;", script)
        self.assertIn("extensionConfigItemType", script)
        self.assertIn("Number.isInteger", script)
        self.assertIn("Number.isFinite", script)
        self.assertIn("control.checkValidity", script)
        self.assertIn("extensionConfigObjectRequired", script)
        self.assertIn('permissionConfirmation.checked = false', script)
        self.assertIn("JSON.stringify(path)", script)
        self.assertIn("请选择", script)
        self.assertIn("item.status_label", script)
        self.assertNotIn('["startup"', script)
        self.assertNotIn('control.type = "text"', script)
        self.assertIn("MAX_SCHEDULE_TIMES", script)
        self.assertNotIn("border-left: 3px", styles)
        self.assertNotIn("border-left: 2px", styles)

    def test_service_v2_final_retry_keeps_frozen_file_intent_and_request_id(self):
        script = (Path(__file__).parents[1] / "static" / "extensions.js").read_text(
            encoding="utf-8"
        )
        frozen_start = script.index("state.frozen = Object.freeze({")
        frozen_block = script[frozen_start:script.index("state.finalSent = true;", frozen_start)]
        self.assertIn("packageFile: state.packageFile", frozen_block)
        self.assertIn("requestId: state.requestId", frozen_block)
        self.assertIn("serializedIntent", frozen_block)
        retry_start = script.index("const sendFinal = async () =>")
        retry_block = script[retry_start:script.index("inspectForm.addEventListener", retry_start)]
        self.assertIn("state.frozen.packageFile", retry_block)
        self.assertIn("state.frozen.requestId", retry_block)
        self.assertIn("state.frozen.serializedIntent", retry_block)
        self.assertIn('finalButton.textContent = "重试相同安装"', retry_block)

    def test_service_v2_projection_rules_execute_in_node(self):
        script_path = (Path(__file__).parents[1] / "static" / "extensions.js").resolve()
        script_source = script_path.read_text(encoding="utf-8")
        node_executable = shutil.which("node") or shutil.which("node.exe")
        if node_executable is None:
            self.skipTest("Node.js is unavailable")
        node_script = """
const fs = require("fs");
const vm = require("vm");
global.Node = class {{}};
global.HTMLElement = class extends Node {{}};
global.HTMLFormElement = class extends HTMLElement {{}};
global.HTMLButtonElement = class extends HTMLElement {{}};
global.HTMLInputElement = class extends HTMLElement {{}};
global.HTMLSelectElement = class extends HTMLElement {{}};
global.Option = class {{}};
global.File = class {{}};
global.document = {{ querySelector: () => null, querySelectorAll: () => [] }};
global.window = {{
  __EXTENSION_WIZARD_TEST__: true,
  crypto: {{ randomUUID: () => "00000000-0000-4000-8000-000000000001" }},
}};
vm.runInThisContext(fs.readFileSync(0, "utf8"));
const {{safeProjection, setStructuredPath}} = window.__extensionWizardTest;
const base = {{
  plugin_id: "example_service",
  name: "Example service",
  version: "2.0.0",
  host_api: {{minimum: "2.0.0", maximum_exclusive: "3.0.0"}},
  permissions: [], account_roles: [], resource_roles: [],
  config_schema: {{
    type: "object", additionalProperties: false,
    properties: {{
      "runtime.mode": {{type: "string", enum: ["safe"], default: "safe"}},
      nested: {{type: "object", additionalProperties: false,
        properties: {{count: {{type: "integer", enum: [1, 2]}}}}, required: ["count"]}},
    }}, required: ["runtime.mode", "nested"],
  }},
  contributions: [{{
    id: "waybill_check", kind: "module_slots", title: "Waybill check", default_enabled: true,
  }}],
  scheduling: {{supported: false, default_schedule: {{kind: "none", times: [], enabled: false}}}},
  account_options: [], resource_options: [], account_pool_available: true,
  resource_pool_available: true, warnings: [],
}};
const valid = safeProjection(base);
if (!valid || !valid.config_schema.properties["runtime.mode"] || !valid.config_schema.properties.nested) throw new Error("valid projection rejected");
if (valid.contributions.length !== 1 || valid.contributions[0].kind !== "module_slots") throw new Error("module slot contribution rejected");
if (Object.keys(valid.contributions[0]).sort().join(",") !== "default_enabled,id,kind,title") throw new Error("module slot contribution widened");
const structured = {{}};
if (!setStructuredPath(structured, ["runtime.mode"], "safe") || structured["runtime.mode"] !== "safe") throw new Error("dot property path changed shape");
const unknown = JSON.parse(JSON.stringify(base));
unknown.config_schema.properties.bad = {{type: "string", pattern: "^secret$"}};
if (safeProjection(unknown) !== null) throw new Error("unsupported schema keyword accepted");
const mismatched = JSON.parse(JSON.stringify(base));
mismatched.config_schema.properties["runtime.mode"] = {{type: "integer", enum: [1.5]}};
if (safeProjection(mismatched) !== null) throw new Error("mismatched enum accepted");
const nullOnly = JSON.parse(JSON.stringify(base));
nullOnly.config_schema.properties["runtime.mode"] = {{type: "string", enum: [null]}};
if (safeProjection(nullOnly) !== null) throw new Error("null enum accepted");
""".replace("{{", "{").replace("}}", "}")
        result = subprocess.run(
            [node_executable, "--input-type=commonjs", "-e", node_script],
            check=False,
            capture_output=True,
            text=True,
            input=script_source,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)


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
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/inspect", "/extensions/inspect", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/install", "/extensions/install", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/project_east/enable", "/extensions/project_east/enable", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/project_east/upgrade", "/extensions/project_east/upgrade", {}))
        self.assertFalse(extension_routes.handle_post(app, "handler", "/extensions/project_east/nope", "/extensions/project_east/nope", {}))
        self.assertEqual(
            [
                ("upload", {"inspect_only": True}),
                ("upload", {}),
                ("action", "project_east", "enable"),
                ("upload", {"automation_id": "project_east"}),
            ],
            app.calls,
        )

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
