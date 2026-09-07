"""A22 actual login and existing fixed page navigation, with no business writes."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from shared.contracts import api_success
from tests.v32_acceptance.browser_performance import TASK_ENV
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.management_fixture import ManagementFixture

PATHS = ('/ocr', '/waybills', '/tracking', '/receipts', '/modules/customer-service', '/modules/finance')


def main():
    report = {'status': 'RUNNING', 'pages': [], 'page_errors': [], 'external_requests': [], 'optional_print_bridge': []}
    try:
        with ManagementFixture(enable_directory_faults=False) as management:
            management.app.add_api_route('/internal/v1/admin/accounts',
                lambda: api_success({'accounts': management.account_manager.list_accounts()}), methods=['GET'])
            with ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                    signing_secret=management.signing_secret) as console, sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=os.environ['V32_CHROMIUM_EXECUTABLE'], headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1080})
                def local_only(route):
                    if urlparse(route.request.url).hostname == '127.0.0.1':
                        route.continue_()
                    else:
                        parsed = urlparse(route.request.url)
                        attempt = {'host': parsed.hostname, 'port': parsed.port, 'path': parsed.path}
                        if parsed.hostname == 'localhost' and parsed.port in {8000, 18000} and parsed.path == '/CLodopfuncs.js':
                            # Existing optional native print discovery; this page
                            # smoke does not connect to or exercise a printer.
                            report['optional_print_bridge'].append({**attempt, 'status': 'NOT_AVAILABLE_IN_ISOLATED_BROWSER'})
                        else:
                            report['external_requests'].append(attempt)
                        route.abort()
                context.route('**/*', local_only)
                page = context.new_page()
                page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
                console.login(page, next_path='/automations')
                for path in PATHS:
                    with page.expect_response(lambda response: urlparse(response.url).path == path and response.request.method == 'GET', timeout=30000) as pending:
                        page.locator(f'[data-nav-list] a[href="{path}"]').first.click()
                    response = pending.value
                    if response.status != 200:
                        raise AssertionError(f'fixed page {path} returned HTTP {response.status}')
                    page.wait_for_url(console.url + path)
                    page.wait_for_function("!document.body.classList.contains('content-loading')")
                    current = page.locator('.main-content:not([hidden])')
                    heading = current.locator('h1').first.inner_text().strip()
                    controls = current.locator('input,button,select,textarea').count()
                    if not heading or controls == 0:
                        raise AssertionError('fixed page lost its actual heading or controls: ' + path)
                    screenshot = TASK_ENV / ('legacy-' + path.strip('/').replace('/', '-') + '.png')
                    page.screenshot(path=str(screenshot))
                    report['pages'].append({'path': path, 'http_status': response.status, 'heading': heading,
                        'controls': controls, 'status': 'PASS', 'screenshot': str(screenshot)})
                context.close()
                browser.close()
        if report['page_errors'] or report['external_requests']:
            raise AssertionError('fixed page navigation produced errors or external requests')
        report['status'] = 'PASS'
        return 0
    except Exception as exc:
        report.update(status='FAIL', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        output = TASK_ENV / 'legacy-page-smoke.json'
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'output': str(output)}))


if __name__ == '__main__':
    raise SystemExit(main())
