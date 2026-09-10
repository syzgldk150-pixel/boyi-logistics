"""Actual authenticated Console controls for isolated self/split problem actions."""
from __future__ import annotations
import json
import os
from urllib.parse import parse_qs, urlparse

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
        self.preview_errors = {}
        self.page.on('pageerror', lambda error:self.errors.append(str(error)))
        self.page.on('response', self._record_preview_error)
        console.login(self.page, next_path='/automations')

    def _record_preview_error(self, response):
        parsed = urlparse(response.url)
        if parsed.path != '/automations/tasks/output':
            return
        query = parse_qs(parsed.query)
        if query.get('selection_phase') != ['preview'] or len(query.get('invocation_id', [])) != 1:
            return
        try:
            error = response.json().get('selection_preview_error')
        except Exception as exc:
            self.preview_errors[query['invocation_id'][0]] = {'capture_error':type(exc).__name__}
            return
        if isinstance(error, dict):
            self.preview_errors[query['invocation_id'][0]] = {
                'http_status':response.status,
                'error_code':str(error.get('error_code') or ''),
                'message':str(error.get('message') or ''),
            }

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
        self._wait_preview_candidates(automation_id, body['invocation_id'], expected_count=len(expected_codes))
        codes = form.locator('[data-selection-preview-list] input[type=checkbox]').evaluate_all('nodes => nodes.map(node => node.value)')
        if sorted(codes) != sorted(expected_codes):
            raise AssertionError(f'actual candidate DOM differs: {codes}')
        return {'invocation_id':body['invocation_id'], 'codes':codes}

    def _wait_preview_candidates(self, automation_id, invocation_id, *, expected_count):
        state = self.page.wait_for_function("""([id,count]) => {
            const form = document.querySelector(`[data-plugin-instance][data-automation-id="${id}"]`).closest('form');
            const panel = form.querySelector('[data-selection-preview-panel]');
            if (panel && !panel.hidden && panel.classList.contains('is-error')) {
                return {error:panel.querySelector('[data-selection-preview-message]')?.textContent || ''};
            }
            return form.querySelectorAll('[data-selection-preview-list] input[type=checkbox]').length === count ? {ready:true} : false;
        }""", arg=[automation_id, expected_count], timeout=60000).json_value()
        if 'error' in state:
            evidence = {'automation_id':automation_id, 'invocation_id':invocation_id,
                'page_message':state['error'], 'response':self.preview_errors.get(invocation_id),
                'javascript_errors':self.errors}
            (self.console.runtime/'problem-preview-failure.json').write_text(
                json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            raise AssertionError('actual selection preview reported a terminal error: '+json.dumps(evidence,ensure_ascii=False))

    def confirm(self, automation_id):
        form = self.card(automation_id).locator('xpath=ancestor::form')
        form.locator('[data-selection-preview-toggle-all]').click()
        with self.page.expect_response(lambda response:urlparse(response.url).path == '/automations/tasks/confirm-selection-preview') as response:
            form.locator('[data-selection-preview-confirm]').click()
        body = response.value.json()
        if response.value.status != 202 or body.get('ok') is not True:
            raise AssertionError(f'actual selection confirmation rejected: {body}')
        return body['invocation_id']

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
