"""Actual Console run/scan controls and same-session idempotent replay."""
from urllib.parse import urlparse

from tests.v32_acceptance.problem_browser import ProblemBrowser


class DailyBrowser(ProblemBrowser):
    def _click(self, button, path):
        with self.page.expect_response(lambda response: urlparse(response.url).path == path) as response:
            button.click()
        actual = response.value
        body = actual.json()
        if actual.status != 202 or body.get("ok") is not True:
            raise AssertionError(f"actual Console action rejected: {body}")
        request = actual.request
        self.last_request = {"url": request.url, "data": request.post_data, "headers": {
            name: request.headers[name] for name in (
                "x-requested-with", "accept", "content-type", "x-browser-request-uuid")}}
        return body

    def run(self, automation_id):
        self.open()
        form = self.card(automation_id).locator("xpath=ancestor::form")
        if not form.locator("[data-run-now]").count():
            reasons = form.locator("button[disabled]").evaluate_all("nodes => nodes.map(node => ({label:node.textContent,title:node.title}))")
            warnings = form.locator(".automation-plugin-block").all_text_contents()
            raise AssertionError(f"actual Console has no enabled run control for {automation_id}: {reasons}; {warnings}")
        return self._click(form.locator("[data-run-now]"), "/automations/tasks/run-now")

    def scan_projection(self):
        form = self.card("scan_codes").locator("xpath=ancestor::form")
        self.page.wait_for_function("""() => {
            const form = document.querySelector('[data-plugin-instance][data-automation-id="scan_codes"]').closest('form');
            const button = form.querySelector('[data-scan-preview-confirm]');
            return !form.querySelector('[data-scan-preview-panel]').hidden && !button.disabled;
        }""", timeout=60000)
        return {"selection_count": int(form.locator("[data-scan-preview-selection]").inner_text()),
            "can_confirm": form.locator("[data-scan-preview-confirm]").is_enabled()}

    def confirm_scan(self):
        form = self.card("scan_codes").locator("xpath=ancestor::form")
        return self._click(form.locator("[data-scan-preview-confirm]"), "/automations/tasks/confirm-scan-preview")

    def replay(self):
        response = self.page.evaluate("""async request => {
            const response = await fetch(request.url, {method:'POST', headers:request.headers, body:request.data});
            return {status:response.status, body:await response.json()};
        }""", self.last_request)
        body = response["body"]
        if response["status"] != 202 or body.get("ok") is not True:
            raise AssertionError(f"actual same-session replay rejected: {body}")
        return body
