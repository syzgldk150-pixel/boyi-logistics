"""Contract tests for the retired extension-center routes and the automation install dialog."""

from __future__ import annotations

import subprocess
import shutil
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace


from console.routes import extensions as extension_routes
from console.routes import automation as automation_routes


class ExtensionProjectionTests(unittest.TestCase):
    def test_standalone_extension_center_is_absent_from_navigation(self):
        from console.navigation import CONSOLE_NAVIGATION

        self.assertNotIn("/extensions", {item["route"] for item in CONSOLE_NAVIGATION})
        self.assertIn("/automations", {item["route"] for item in CONSOLE_NAVIGATION})

    def test_service_v2_intent_has_exact_keys_and_no_browser_authority_fields(self):
        source = (Path(__file__).parents[1] / "templates" / "_automation_extension_install_dialog.html").read_text(
            encoding="utf-8"
        )
        styles = (Path(__file__).parents[1] / "static" / "style.css").read_text(
            encoding="utf-8"
        )
        script = (Path(__file__).parents[1] / "static" / "extensions.js").read_text(
            encoding="utf-8"
        )
        expected_keys = ("instance_name", "permissions_confirmed")
        intent_start = script.index("const buildIntent = () => ({")
        intent_block = script[intent_start:script.index("const validateIntent", intent_start)]
        for key in expected_keys:
            self.assertIn(f"{key}:", intent_block)
        for removed in (
            "config:",
            "account_bindings:",
            "resource_bindings:",
            "enabled_entrypoints:",
            "schedule:",
        ):
            self.assertNotIn(removed, intent_block)
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
        self.assertIn("warnings", script)
        self.assertIn("operations:", script)
        self.assertIn('permissionConfirmation.checked = false', script)
        self.assertNotIn("buildConfigInput", script)
        self.assertNotIn("buildBindings", script)
        self.assertNotIn("buildSchedule", script)
        self.assertNotIn("业务账号", source)
        self.assertNotIn("飞书表格", source)
        self.assertNotIn("定时", source)
        self.assertNotIn('["startup"', script)
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
const {{safeProjection}} = window.__extensionWizardTest;
const base = {{
  plugin_id: "example_service",
  name: "Example service",
  version: "2.0.0",
  host_api: {{minimum: "2.0.0", maximum_exclusive: "3.0.0"}},
  permissions: [], account_roles: [], resource_roles: [],
  contributions: [{{
    id: "waybill_check", kind: "module_slots", title: "Waybill check", default_enabled: true,
  }}],
  warnings: [],
}};
const valid = safeProjection(base);
if (!valid) throw new Error("valid projection rejected");
if (valid.contributions.length !== 1 || valid.contributions[0].kind !== "module_slots") throw new Error("module slot contribution rejected");
if (Object.keys(valid.contributions[0]).sort().join(",") !== "effect,id,kind,title") throw new Error("module slot contribution widened");
const malformed = JSON.parse(JSON.stringify(base));
malformed.contributions[0].id = "not safe";
if (safeProjection(malformed)?.contributions.length !== 0) throw new Error("invalid contribution accepted");
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
    def test_get_routes_redirect_to_unified_automation_page(self):
        app = SimpleNamespace(calls=[])
        app._redirect = lambda handler, location: app.calls.append((handler, location))
        self.assertTrue(extension_routes.handle_get(app, "handler", "/extensions", "/extensions", {"x": ["1"]}))
        self.assertTrue(extension_routes.handle_get(app, "handler", "/extensions/example", "/extensions/example", {}))
        self.assertFalse(extension_routes.handle_get(app, "handler", "/extensions/example/more", "/extensions/example/more", {}))
        self.assertEqual(
            [("handler", "/automations"), ("handler", "/automations?plugin=example")],
            app.calls,
        )

    def test_legacy_post_routes_return_gone(self):
        app = SimpleNamespace(calls=[])
        app._control_plane_error = lambda handler, status, code, message: app.calls.append(
            (handler, status, code, message)
        )
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/inspect", "/extensions/inspect", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/install", "/extensions/install", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/project_east/enable", "/extensions/project_east/enable", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/project_east/uninstall", "/extensions/project_east/uninstall", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/project_east/upgrade", "/extensions/project_east/upgrade", {}))
        self.assertTrue(extension_routes.handle_post(app, "handler", "/extensions/project_east/nope", "/extensions/project_east/nope", {}))
        self.assertFalse(extension_routes.handle_post(app, "handler", "/extensions/project_east/nope/more", "/extensions/project_east/nope/more", {}))
        self.assertEqual(6, len(app.calls))
        self.assertTrue(all(call[1] == HTTPStatus.GONE for call in app.calls))
        self.assertTrue(all(call[2] == "EXTENSION_CENTER_RETIRED" for call in app.calls))

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
