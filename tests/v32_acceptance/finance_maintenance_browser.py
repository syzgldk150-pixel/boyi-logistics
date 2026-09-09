"""Actual Console browser interactions used by the isolated finance drill."""
from __future__ import annotations

from decimal import Decimal
import os
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from playwright.sync_api import sync_playwright


class FinanceBrowser:
    def __init__(self, console, *, module="finance"):
        self.console = console
        if module not in {"finance", "customer_service"}:
            raise ValueError("collector browser requires an actual data module")
        self.module = module
        self.sources_path = "/modules/" + module.replace("_", "-") + "/data-sources"
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            executable_path=os.environ["V32_CHROMIUM_EXECUTABLE"], headless=True)
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1080})
        self.context.route("**/*", lambda route: route.continue_()
            if urlparse(route.request.url).hostname == "127.0.0.1" else route.abort())
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        console.login(self.page, next_path=self.sources_path)

    def card(self, automation_id):
        return self.page.locator(f'[data-plugin-instance][data-automation-id="{automation_id}"]')

    def open_sources(self):
        self.page.goto(self.console.url + self.sources_path, wait_until="domcontentloaded")
        self.page.wait_for_function("document.documentElement.dataset.pluginConfigurationDelegated === 'true'")

    def install(self, artifact, *, instance_name="M02 隔离真实财务采集"):
        self.open_sources()
        self.page.locator("[data-extension-open]").click()
        with self.page.expect_response(lambda response: "/automations/extensions/inspect" in response.url) as response:
            self.page.locator('[data-extension-inspect-form] input[type="file"]').set_input_files(
                {"name": "finance-baseline.zip", "mimeType": "application/zip", "buffer": artifact["bytes"]})
        body = response.value.json()
        if response.value.status != 200 or body.get("ok") is not True:
            raise AssertionError(f"actual finance module inspection failed: {body}")
        self.page.locator("[data-extension-instance-name]").fill(instance_name)
        self.page.locator("[data-extension-permissions-confirmed]").check()
        with self.page.expect_response(lambda response: "/automations/extensions/install" in response.url) as response:
            self.page.locator("[data-extension-install-submit]").click()
        if response.value.status != 200:
            raise AssertionError(f"actual finance module installation failed: {response.value.json()}")
        self.page.wait_for_url(self.console.url + self.sources_path)
        card = self.page.locator(f'[data-plugin-instance][aria-label="{instance_name} 插件实例"]')
        card.wait_for(state="visible")
        if card.count() != 1:
            raise AssertionError("installed financial instance must appear exactly once in its module")
        return card.get_attribute("data-automation-id")

    def save_accounts(self, automation_id, accounts, *, config_values=None, invalid_configs=()):
        with self.page.expect_response(lambda response: "/settings/bridge" in response.url
                and response.request.method == "POST"
                and response.request.post_data_json.get("operation") == "context") as context_response:
            self.page.goto(self.console.url + f"/automations/{automation_id}/settings", wait_until="domcontentloaded")
        context_body = context_response.value.json()
        if context_body.get("ok") is not True or context_body.get("data", {}).get("account_catalog_available") is not True:
            raise AssertionError(f"actual settings context failed: {context_body}; JS errors: {self.errors}")
        rejected = []
        settings = context_body["data"]["settings"]
        form = self.page.locator("[data-default-plugin-settings]")
        for config in invalid_configs:
            response = self.context.request.post(self.console.url + form.get_attribute("data-endpoint"),
                data={"bridge_session": form.get_attribute("data-session"), "operation": "save", "payload": {
                    "config": config, "account_bindings": settings["account_bindings"],
                    "resource_bindings": settings["resource_bindings"],
                    "expected_project_configuration_version": settings["project_configuration_version"],
                    "request_id": str(uuid4())}},
                headers={"X-Requested-With": "XMLHttpRequest", "Origin": self.console.url})
            body = response.json()
            if response.status < 400 or body.get("ok") is True:
                raise AssertionError("invalid actual settings were accepted")
            rejected.append({"config": config, "http_status": response.status, "response": body})
        self.page.locator("[data-default-plugin-settings] button[type=submit]").wait_for(state="visible")
        for role, account in accounts.items():
            self.page.locator(f"#plugin-account-{role}").select_option(account)
        for key, value in (config_values or {}).items():
            field = next(field for field in settings["default_config_fields"] if field["path"] == key)
            self.page.locator(f'[data-config-key="{key}"]').select_option(str(field["enum"].index(value)))
        with self.page.expect_response(lambda response: "/settings/bridge" in response.url
                and response.request.post_data_json.get("operation") == "save") as response:
            self.page.locator("[data-default-plugin-settings] button[type=submit]").click()
        body = response.value.json()
        if response.value.status != 200 or body.get("ok") is not True:
            raise AssertionError(f"actual account selection save failed: {body}")
        return {"previous_config": settings["config"], "selected_config": config_values or {}, "rejected": rejected}

    def run(self, automation_id):
        self.open_sources()
        result = self.submit_current_run(automation_id)
        body = result["body"]
        if result["http_status"] != 202 or body.get("ok") is not True:
            raise AssertionError(f"actual module Invocation acceptance failed: {body}")
        if body.get("run_id") or not body.get("invocation_id"):
            raise AssertionError(f"collector returned an old Run instead of its actual Invocation: {body}")
        return body["invocation_id"]

    def submit_current_run(self, automation_id):
        """Click the current DOM without refreshing its potentially stale facts."""
        if self.card(automation_id).count() != 1:
            raise AssertionError("target card missing: " + self.page.locator("main").inner_text())
        form = self.card(automation_id).locator("xpath=ancestor::form")
        if form.locator("[data-run-now]").count() != 1:
            raise AssertionError("target run button missing: " + form.inner_text())
        with self.page.expect_response(lambda response: urlparse(response.url).path == "/automations/tasks/run-now") as response:
            form.locator("[data-run-now]").click()
        return {"http_status": response.value.status, "body": response.value.json()}

    def upgrade(self, automation_id, artifact, *, expect_success=True):
        self.open_sources()
        card = self.card(automation_id)
        card.locator("[data-extension-upgrade]").set_input_files(
            {"name": "finance-update.zip", "mimeType": "application/zip", "buffer": artifact["bytes"]})
        with self.page.expect_response(lambda response: urlparse(response.url).path
                == f"/automations/plugins/{automation_id}/upgrade") as response:
            card.locator('[data-extension-action="upgrade"]').click()
        body = response.value.json()
        success = response.value.status == 200 and body.get("ok") is True
        if success != expect_success:
            raise AssertionError(f"actual module upgrade result differs: {body}")
        return {"http_status": response.value.status, "body": body}

    def uninstall(self, automation_id, *, expect_success=True):
        self.open_sources()
        confirmations = []
        def confirm(dialog):
            confirmations.append(dialog.message)
            dialog.accept()
        self.page.once("dialog", confirm)
        with self.page.expect_response(lambda response: urlparse(response.url).path
                == f"/automations/plugins/{automation_id}/uninstall") as response:
            self.card(automation_id).locator('[data-extension-action="uninstall"]').click()
        body = response.value.json()
        success = response.value.status == 200 and body.get("ok") is True
        if success != expect_success or len(confirmations) != 1:
            raise AssertionError(f"actual confirmed collector uninstall failed: {body}")
        return {"http_status": response.value.status, "body": body, "confirmations": confirmations}

    def set_enabled(self, automation_id, *, enabled):
        action = "enable" if enabled else "disable"
        self.open_sources()
        with self.page.expect_response(lambda response: urlparse(response.url).path
                == f"/automations/plugins/{automation_id}/{action}") as response:
            self.card(automation_id).locator(f'[data-extension-action="{action}"]').click()
        body = response.value.json()
        if response.value.status != 200 or body.get("ok") is not True:
            raise AssertionError(f"actual collector {action} failed: {body}")
        return {"http_status": response.value.status, "body": body}

    def verify_finance(self, source_ids, *, target_date, expected_waybills):
        self.page.goto(self.console.url + "/modules/finance", wait_until="domcontentloaded")
        self.page.wait_for_function("document.querySelector('[data-source-selector]')?.options.length >= 4")
        self.page.locator("[data-finance-start-date]").fill(target_date)
        self.page.locator("[data-finance-end-date]").fill(target_date)
        rows = []
        for source_id in source_ids:
            with self.page.expect_response(lambda response: urlparse(response.url).path == "/finance/summary"
                    and parse_qs(urlparse(response.url).query).get("source_ids") == [source_id]
                    and parse_qs(urlparse(response.url).query).get("start_date") == [target_date]
                    and parse_qs(urlparse(response.url).query).get("end_date") == [target_date]) as response:
                self.page.locator("[data-source-selector]").select_option(source_id)
            body = response.value.json()
            if response.value.status != 200 or body.get("ok") is not True:
                raise AssertionError(f"actual source-filtered finance query failed: {body}")
            self.page.wait_for_function("document.querySelector('[data-finance-status]')?.textContent === '财务总览已更新。' && !document.querySelector('[data-finance-refresh]').disabled")
            expense = self.page.locator('[data-finance-metric="total_expense"] strong').inner_text()
            from decimal import InvalidOperation
            try:
                displayed = Decimal(expense.replace("元", "").replace(",", "").strip())
            except InvalidOperation as error:
                raise AssertionError(f"browser finance amount missing: {expense}; source response={body}") from error
            if displayed != Decimal("1.2500"):
                raise AssertionError(f"browser finance amount differs: {expense}; source response={body}")
            entries = self.context.request.get(self.console.url + "/finance/entries", params={
                "start_date": target_date, "end_date": target_date, "source_ids": source_id}).json()
            serialized = str(entries)
            if expected_waybills[source_id] not in serialized:
                raise AssertionError(f"browser detail query omitted actual parsed waybill: {entries}")
            rows.append({"source_id": source_id, "displayed_expense": expense,
                "summary": body, "entries": entries})
        return rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if _args[0] is not None:
            self.page.screenshot(path=str(self.console.runtime / "failed-browser.png"), full_page=True)
            (self.console.runtime / "failed-browser.html").write_text(self.page.content(), encoding="utf-8")
        self.context.close()
        self.browser.close()
        self.playwright.stop()
