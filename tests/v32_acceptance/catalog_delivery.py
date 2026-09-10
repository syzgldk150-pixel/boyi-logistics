"""A16/A19: actual stale HTTP delivery and inaccessible database boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlparse
from uuid import uuid4

import pymysql

from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.daily_concurrency import wait_invocation
from tests.v32_acceptance.daily_scan import ACTOR, signed_request
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.owned_database import connect_owned, prepare_owned
from tests.v32_acceptance.problem_browser import ProblemBrowser
from tests.direct_invocation_fixture import DirectFixture
from tests.v32_acceptance.unrelated_fixture import install_unrelated

ROOT = Path(__file__).resolve().parents[2]
DATABASE = 'v32_catalog_delivery_test'
OUTPUT = ROOT / '.task_tmp/v32/reliability/catalog-delivery.json'


def stale_delivery(browser, target, independent):
    app = browser.console.app
    original = app._load_automation_plugin_catalog_uncached
    started, release = threading.Event(), threading.Event()
    captured = []

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        if not started.is_set():
            captured.append(result)
            started.set()
            if not release.wait(30):
                raise TimeoutError('A16 owned response barrier expired')
        return result

    browser.open()
    assert browser.card(target).count() == 1
    app._clear_automation_plugin_catalog_cache()
    app._load_automation_plugin_catalog_uncached = delayed
    pending = browser.context.new_page()
    try:
        pending.evaluate('(url) => { location.href = url; }', browser.console.url + '/automations')
        deadline = time.monotonic() + 15
        while not started.is_set() and time.monotonic() < deadline:
            pending.wait_for_timeout(50)  # Pump Playwright's loopback route callbacks.
        assert started.is_set(), 'real signed catalog request did not reach delivery barrier'
        browser.page.once('dialog', lambda dialog: dialog.accept())
        path = f'/automations/plugins/{target}/uninstall'
        with browser.page.expect_response(lambda response: urlparse(response.url).path == path) as response:
            browser.card(target).locator('[data-extension-action="uninstall"]').click()
        body = response.value.json()
        assert response.value.status == 200 and body.get('ok') is True, body
        release.set()
        pending.wait_for_url(browser.console.url + '/automations')
        pending.wait_for_load_state('domcontentloaded')
        assert pending.locator(f'[data-plugin-instance][data-automation-id="{target}"]').count() == 0
        pending.get_by_text('插件目录已变化，请刷新后继续。', exact=False).wait_for()
        pending.reload(wait_until='domcontentloaded')
        assert pending.locator(f'[data-plugin-instance][data-automation-id="{target}"]').count() == 0
        assert pending.locator(f'[data-plugin-instance][data-automation-id="{independent}"]').count() == 1
        assert len(captured) == 1
        return {'status': 'PASS', 'mutation_http_status': response.value.status,
            'delayed_actual_catalog_responses': len(captured), 'old_instance_revived': False,
            'explicit_invalidated_warning': True, 'fresh_independent_card_present': True}
    finally:
        release.set()
        app._load_automation_plugin_catalog_uncached = original
        pending.close()


def main():
    prepare_owned(DATABASE)
    runtime = OUTPUT.parent / ('catalog-delivery-' + uuid4().hex)
    runtime.mkdir(parents=True)
    healthy = threading.Event()
    healthy.set()
    refused = socket.socket()
    refused.bind(('127.0.0.1', 0))
    port = refused.getsockname()[1]  # Bound without listen: actual ECONNREFUSED.
    failures = []

    def connection_factory():
        if healthy.is_set():
            return connect_owned(DATABASE)
        try:
            return pymysql.connect(host='127.0.0.1', port=port, connect_timeout=1)
        except pymysql.err.OperationalError as error:
            failures.append(error.args[0])
            raise

    report = {'status': 'RUNNING'}
    try:
        with ManagementFixture(connection_factory=connection_factory, runtime_root=runtime,
                enable_directory_faults=False) as management:
            target = install_unrelated(management, actor=ACTOR, plugin_id='v32_stale_target', name='A16 retired instance')
            independent = install_unrelated(management, actor=ACTOR, plugin_id='v32_stale_independent', name='A16 independent instance')
            with ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                    signing_secret=management.signing_secret, runtime_root=runtime / 'console') as console:
                with ProblemBrowser(console) as browser:
                    report['A16'] = stale_delivery(browser, target, independent)
                    console.app._clear_automation_plugin_catalog_cache()
                    healthy.clear()
                    try:
                        browser.open()
                        assert browser.page.locator('[data-plugin-instance]').count() == 0
                        browser.page.get_by_text('插件目录暂不可用', exact=True).wait_for()
                        warning = browser.page.locator('.auto-feedback--warning').all_text_contents()
                        assert failures, 'actual database refusal was never reached'
                        assert any(text.strip() for text in warning), 'database unavailability was not shown'
                        report['A19'] = {'status': 'PASS', 'actual_database_error_codes': list(failures),
                            'actionable_instances_during_outage': 0, 'warnings': warning}
                    finally:
                        healthy.set()
                        console.app._clear_automation_plugin_catalog_cache()
                    browser.open()
                    assert browser.card(independent).count() == 1
                    assert not browser.errors, browser.errors
            with DirectFixture(management) as runner:
                submitted = signed_request(management, f'/internal/v1/automation-projects/{independent}/invoke',
                    payload={'request_id': str(uuid4())})
                result = wait_invocation(runner, submitted['invocation_id'], expected='COMPLETED')
                report['recovered_real_invocation'] = {key: result[key] for key in ('invocation_id', 'status')}
                report['runtime'] = runner.snapshot()
            report['status'] = 'PASS'
    except Exception as error:
        report.update(status='FAIL', error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        healthy.set()
        refused.close()
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'output': str(OUTPUT)}))


if __name__ == '__main__':
    main()
