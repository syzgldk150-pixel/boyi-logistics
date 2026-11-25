"""Load real ZIP settings in Chromium; only the host message data is synthetic."""

import json
from pathlib import Path
from zipfile import ZipFile

from playwright.sync_api import sync_playwright

from tests.service_v2_package_fixture import build_production_plugin_fixture


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "agent/service_v2_plugins"


def test_every_production_package_settings_renders_and_preserves_object_array_and_accounts(tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        checked = []
        settings_sources = [source for source in sorted(SOURCES.glob("*/manifest.json"))
                            if json.loads(source.read_text(encoding="utf-8")).get("settings_ui") is not None]
        cases = [(source, False) for source in settings_sources]
        cases.append((SOURCES / "sync_arrival_stats_v2/manifest.json", True))
        for source, round_trip in cases:
            archive = build_production_plugin_fixture(source.parent, tmp_path / f"{source.parent.name}-{round_trip}.zip")
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
            page.get_by_text("已载入当前设置。", exact=True).wait_for(timeout=5000)
            assert page.locator("[data-config-field]").count() == len(settings["config_schema"]["properties"])
            assert page.locator("[data-account-role]").count() == len(settings["account_roles"])
            assert page.locator("[data-resource-role]").count() == len(settings["resource_roles"])
            if round_trip:
                page.get_by_role("button", name="保存插件设置").click()
                page.locator("[data-settings-feedback]").filter(has_text="插件设置已保存").wait_for()
                saved = page.evaluate("window.fixtureSaves[0]")
                assert saved["config"] == settings["config"]
                assert saved["account_bindings"] == settings["account_bindings"]
                assert saved["expected_project_configuration_version"] == 3
            if not round_trip:
                checked.append(manifest["plugin_id"])
            page.close()
        assert len(checked) == len(settings_sources)
        browser.close()


def _open_send_settings(browser, tmp_path, plugin_id, config, replies):
    archive = build_production_plugin_fixture(SOURCES / plugin_id, tmp_path / f"{plugin_id}.zip")
    with ZipFile(archive) as package:
        assets = {name: package.read(name) for name in package.namelist() if name.startswith("settings/")}
        manifest = json.loads(package.read("manifest.json"))
    # Required roles are bound to synthetic refs so only the business fields are under test.
    accounts = [{"account_ref": f"acct-{role['role']}", "name": "隔离账号", "system": role["allowed_systems"][0],
                 "status_label": "已登录", "available": True} for role in manifest["account_roles"]]
    resources = [{"resource_id": f"res-{role['role']}", "display_name": "隔离表格", "kind": role["allowed_kinds"][0],
                  "status": "available"} for role in manifest["resource_roles"]]
    settings = {
        "config_schema": manifest["config_schema"], "config": config,
        "account_roles": manifest["account_roles"], "resource_roles": manifest["resource_roles"],
        "account_bindings": {role["role"]: f"acct-{role['role']}" for role in manifest["account_roles"]},
        "resource_bindings": {role["role"]: f"res-{role['role']}" for role in manifest["resource_roles"]},
        "project_configuration_version": 7,
    }
    context = {"settings": settings, "accounts": accounts, "resources": resources,
               "account_catalog_available": True, "resource_catalog_available": True}
    page = browser.new_page()
    page.add_init_script(
        "window.fixtureContext = " + json.dumps(context) + "; window.fixtureReplies = " + json.dumps(replies) + ";"
        + r"""
        window.fixtureRequests = [];
        window.fixtureHeights = [];
        window.addEventListener('message', event => {
            const request = event.data;
            if (request?.type === 'boyi.settings.resize') { window.fixtureHeights.push(request.height); return; }
            if (request?.type !== 'boyi.settings.request') return;
            window.fixtureRequests.push(request);
            const reply = request.operation === 'context'
                ? {ok: true, data: window.fixtureContext}
                : window.fixtureReplies.shift();
            window.postMessage({type: 'boyi.settings.response', bridge_session: request.bridge_session,
                request_id: request.request_id, ...reply}, '*');
        });
    """)
    page.route("http://localhost:19423/**", lambda route: route.fulfill(
        body=assets["settings/" + route.request.url.split("/")[-1].split("?")[0]],
        content_type="text/html" if ".html" in route.request.url else (
            "application/javascript" if ".js" in route.request.url else "text/css")))
    page.goto("http://localhost:19423/index.html?bridge_session=synthetic-session")
    page.get_by_text("已载入当前设置。", exact=True).wait_for(timeout=5000)
    return page


def test_send_settings_use_readable_fields_date_pickers_and_declared_defaults(tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        # The saved config that broke the Yunda instance: a slash date and no sync_nas key.
        page = _open_send_settings(browser, tmp_path, "sync_yunda_send_waybills_v2",
                                   {"target_date": "2026/10/24", "ensure_fields": False, "sync_sql": True},
                                   [{"ok": True, "data": {"project_configuration_version": 8}}])
        assert page.locator('[data-config-field="target_date"]').get_attribute("type") == "date"
        assert page.locator('[data-config-field="target_date"]').input_value() == ""
        assert page.get_by_text("原来保存的“2026/10/24”不是有效日期，保存时会清空。").is_visible()
        assert page.get_by_text("业务日期", exact=True).is_visible()
        assert page.get_by_text("写入 NAS 寄件表格", exact=True).is_visible()
        assert page.locator(".settings-field__title", has_text="韵达账号").is_visible()
        assert page.locator('[data-config-field="sync_nas"]').is_checked()  # declared default, not stored
        assert not page.locator('[data-config-field="ensure_fields"]').is_checked()  # saved value wins
        advanced = page.locator("details.settings-advanced")
        assert advanced.get_attribute("open") is None
        assert advanced.locator('[data-config-field="page_size"]').count() == 1
        assert not page.locator('[data-config-field="page_size"]').is_visible()
        assert page.locator('[data-config-field="page_size"]').get_attribute("placeholder") == "默认 200"
        assert page.evaluate("window.fixtureHeights.length") > 0
        page.locator('[data-config-field="sync_sheet"]').click()
        page.get_by_text("有未保存的修改。", exact=True).wait_for()
        page.get_by_role("button", name="保存插件设置").click()
        page.locator("[data-settings-feedback]").filter(has_text="插件设置已保存").wait_for()
        save = page.evaluate("window.fixtureRequests.find(item => item.operation === 'save')")
        assert save["bridge_client"] == 2
        assert save["payload"]["config"] == {"ensure_fields": False, "sql_only": False, "sync_nas": True,
                                             "sync_sheet": False, "sync_sql": True}
        assert save["payload"]["expected_project_configuration_version"] == 7
        page.close()
        browser.close()


def test_send_settings_offer_reload_after_a_version_conflict(tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = _open_send_settings(browser, tmp_path, "sync_daily_send_orders_v2", {}, [{
            "ok": False, "code": "PLUGIN_SETTINGS_VERSION_CHANGED",
            "message": "设置已经被更新过（可能是上一次保存其实已成功），请载入最新设置，核对后再保存。",
        }])
        page.locator('[data-config-field="start_date"]').fill("2026-11-20")
        page.get_by_role("button", name="保存插件设置").click()
        page.locator("[data-settings-feedback]").filter(has_text="请载入最新设置").wait_for()
        reload = page.get_by_role("button", name="载入最新设置")
        reload.wait_for()
        reload.click()
        page.get_by_text("已载入当前设置。", exact=True).wait_for()
        assert page.evaluate("window.fixtureRequests.filter(item => item.operation === 'context').length") == 2
        assert page.locator('[data-config-field="start_date"]').input_value() == ""
        assert not reload.is_visible()
        page.close()
        browser.close()
