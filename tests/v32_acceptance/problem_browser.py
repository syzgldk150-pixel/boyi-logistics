"""Actual authenticated Console controls for isolated self/split problem actions."""
from __future__ import annotations
import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


class ProblemBrowser:
    def __init__(self, console):
        self.console = console
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(executable_path=os.environ['V32_CHROMIUM_EXECUTABLE'], headless=True)
        self.context = self.browser.new_context(viewport={'width':1440,'height':1080})
        self.context.route('**/*', lambda route: route.continue_() if urlparse(route.request.url).hostname == '127.0.0.1' else route.abort())
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error:self.errors.append(str(error)))
        console.login(self.page, next_path='/automations')

    def card(self, automation_id):
        return self.page.locator(f'[data-plugin-instance][data-automation-id="{automation_id}"]')

    def open(self):
        self.page.goto(self.console.url + '/automations', wait_until='domcontentloaded')
        self.page.wait_for_function("document.documentElement.dataset.pluginConfigurationDelegated === 'true'")

    def preview(self, automation_id, *, expected_codes):
        self.open()
        form = self.card(automation_id).locator('xpath=ancestor::form')
        with self.page.expect_response(lambda response:urlparse(response.url).path == '/automations/tasks/selection-preview') as response:
            form.locator('[data-run-now]').click()
        body = response.value.json()
        if response.value.status != 202 or body.get('ok') is not True:
            raise AssertionError(f'actual selection preview rejected: {body}')
        self.page.wait_for_function("([id,count]) => document.querySelector(`[data-plugin-instance][data-automation-id=\"${id}\"]`).closest('form').querySelectorAll('[data-selection-preview-list] input[type=checkbox]').length === count",
            arg=[automation_id, len(expected_codes)], timeout=60000)
        codes = form.locator('[data-selection-preview-list] input[type=checkbox]').evaluate_all('nodes => nodes.map(node => node.value)')
        if sorted(codes) != sorted(expected_codes):
            raise AssertionError(f'actual candidate DOM differs: {codes}')
        return {'run_id':body['run_id'], 'codes':codes}

    def confirm(self, automation_id):
        form = self.card(automation_id).locator('xpath=ancestor::form')
        form.locator('[data-selection-preview-toggle-all]').click()
        with self.page.expect_response(lambda response:urlparse(response.url).path == '/automations/tasks/confirm-selection-preview') as response:
            form.locator('[data-selection-preview-confirm]').click()
        body = response.value.json()
        if response.value.status != 202 or body.get('ok') is not True:
            raise AssertionError(f'actual selection confirmation rejected: {body}')
        return body['run_id']

    def upgrade(self, automation_id, artifact):
        self.open()
        card = self.card(automation_id)
        card.locator('[data-extension-upgrade]').set_input_files({'name':'decision-update.zip','mimeType':'application/zip','buffer':artifact['bytes']})
        with self.page.expect_response(lambda response:urlparse(response.url).path == f'/automations/plugins/{automation_id}/upgrade') as response:
            card.locator('[data-extension-action="upgrade"]').click()
        body = response.value.json()
        if response.value.status != 200 or body.get('ok') is not True:
            raise AssertionError(f'actual plugin upgrade or rollback rejected: {body}')
        return {'http_status':response.value.status, 'body':body}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if _args[0] is not None:
            self.page.screenshot(path=str(self.console.runtime/'problem-failure.png'), full_page=True)
            # No HTML/session/auth material is persisted.
        self.context.close()
        self.browser.close()
        self.playwright.stop()
