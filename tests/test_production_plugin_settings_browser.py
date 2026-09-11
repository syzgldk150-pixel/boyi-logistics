"""Load real ZIP settings in Chromium; only the host message data is synthetic."""

import json
from pathlib import Path
from zipfile import ZipFile

from playwright.sync_api import sync_playwright

from service_v2_plugins._shared.build_zip import build_plugin_zip


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "agent/service_v2_plugins"


def test_every_production_package_settings_renders_and_preserves_object_array_and_accounts(tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        checked = []
        cases = [(source, False) for source in sorted(SOURCES.glob("*/manifest.json"))]
        cases.append((SOURCES / "sync_arrival_stats_v2/manifest.json", True))
        for source, round_trip in cases:
            archive = build_plugin_zip(source.parent, tmp_path / f"{source.parent.name}-{round_trip}.zip")
            with ZipFile(archive) as package:
                assets = {name: package.read(name) for name in package.namelist() if name.startswith("settings/")}
                manifest = json.loads(package.read("manifest.json"))
            # This exercises each released settings asset with its real schema,
            # plus a shared-control fixture for round-trip fidelity.
            settings = {
                "config_schema": manifest["config_schema"], "config": {},
                "account_roles": manifest["account_roles"], "resource_roles": manifest["resource_roles"],
                "account_bindings": {}, "resource_bindings": {}, "project_configuration_version": 3,
            }
            if round_trip:
                settings.update(config_schema={"properties": {
                    "arrive_list_request_body": {"type": "object"},
                    "skip_bill_codes": {"type": "array"},
                }}, config={"arrive_list_request_body": {}, "skip_bill_codes": ["SYNTHETIC-A", "SYNTHETIC-B"]},
                    account_roles=[{"role": "sources", "collection": True, "required": True,
                                    "allowed_systems": ["ronghui"]}], resource_roles=[],
                    account_bindings={"sources": ["ref-a", "ref-b"]})
            context = {"settings": settings, "accounts": [
                {"account_ref": ref, "name": ref, "system": "ronghui", "status_label": "隔离账号", "available": True}
                for ref in ("ref-a", "ref-b")
            ], "resources": [], "account_catalog_available": True, "resource_catalog_available": True}
            page = browser.new_page()
            page.add_init_script("window.fixtureContext = " + json.dumps(context) + ";" + r"""
                window.fixtureSaves = [];
                window.addEventListener('message', event => {
                    if (event.data?.type !== 'boyi.settings.request') return;
                    const request = event.data;
                    if (request.operation === 'save') window.fixtureSaves.push(request.payload);
                    window.postMessage({type: 'boyi.settings.response', bridge_session: request.bridge_session,
                        request_id: request.request_id, ok: true,
                        data: request.operation === 'context' ? window.fixtureContext : {project_configuration_version: 4}}, '*');
                });
            """)
            page.route("http://localhost:19422/**", lambda route: route.fulfill(
                body=assets["settings/" + route.request.url.split("/")[-1].split("?")[0]],
                content_type="text/html" if ".html" in route.request.url else (
                    "application/javascript" if ".js" in route.request.url else "text/css")))
            page.goto("http://localhost:19422/index.html?bridge_session=synthetic-session")
            page.get_by_text("设置已连接。", exact=True).wait_for(timeout=5000)
            assert page.locator("[data-config-field]").count() == len(settings["config_schema"]["properties"])
            assert page.locator("[data-account-role]").count() == len(settings["account_roles"])
            assert page.locator("[data-resource-role]").count() == len(settings["resource_roles"])
            if round_trip:
                page.get_by_role("button", name="保存插件设置").click()
                page.get_by_text("插件设置已保存。", exact=True).wait_for()
                saved = page.evaluate("window.fixtureSaves[0]")
                assert saved["config"] == settings["config"]
                assert saved["account_bindings"] == settings["account_bindings"]
                assert saved["expected_project_configuration_version"] == 3
            if not round_trip:
                checked.append(manifest["plugin_id"])
            page.close()
        assert len(checked) == len(list(SOURCES.glob("*/manifest.json")))
        browser.close()
