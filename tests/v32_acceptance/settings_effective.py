"""M01: real default settings, next signed action, and historical restoration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.decision_maintenance import composed, prepare_database, setup_instance, wait_result
from tests.v32_acceptance.host_freeze import process_identity
from tests.v32_acceptance.problem_browser import ProblemBrowser
from tests.v32_acceptance.problem_fixture import ACCOUNTS

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / '.task_tmp/v32/environment/settings-effective.json'
PRIMARY_CODE = 'R_M01_PRIMARY'
STANDARD_CODE = 'R_M03_STANDARD'


def open_settings(browser, automation_id):
    path = f'/automations/{automation_id}/settings'
    browser.page.goto(browser.console.url + path, wait_until='domcontentloaded')
    browser.page.wait_for_selector('[data-default-plugin-settings] button[type=submit]:enabled')
    return path


def actual_run(browser, supplier, automation_id, expected_codes, expected_accounts):
    started = len(supplier.requests)
    preview = browser.preview(automation_id, expected_codes=expected_codes)
    result = wait_result(browser.confirm(automation_id))
    if result['status'] != 'COMPLETED' or any(step['postcondition_status'] != 'VERIFIED' for step in result['steps']):
        raise AssertionError('actual configured action failed: ' + str(result))
    calls = [item for item in supplier.requests[started:] if item['path'] == '/problem']
    observed = {}
    for item in calls:
        observed.setdefault(item['synthetic_bill_code'], set()).add(item['synthetic_account_id'])
    if observed != {code: {account} for code, account in expected_accounts.items()}:
        raise AssertionError('actual raw external account routing differs: ' + str(observed))
    return {'preview': preview, 'result': result, 'raw_problem_calls': calls,
        'actual_account_by_bill': {code: sorted(values) for code, values in observed.items()}}


def main():
    if os.environ.get('AGENT_DB_NAME') != 'v32_m01_test':
        raise RuntimeError('M01 requires its explicit dedicated test database')
    prepare_database()
    report = {'status': 'RUNNING', 'phases': {}}
    try:
        with composed() as (management, runner, supplier, artifacts):
            automation_id = setup_instance(management, artifacts['baseline'])
            supplier.rows.append([PRIMARY_CODE, '合成配件', '自提', '1', '邵阳自提部', '1'])
            baseline = management.catalog.require(automation_id)
            baseline_generation = baseline.committed_generation
            if baseline_generation is None:
                raise AssertionError('real baseline has no committed generation')
            report['baseline_generation'] = baseline_generation
            report['plugin_version'] = baseline.installed_version
            report['host_before'] = process_identity()
            with ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                    signing_secret=management.signing_secret, runtime_root=management.task_env / 'settings-console') as console:
                report['startup_ids'] = {'agent': management.startup_id, 'console': console.startup_id}
                with ProblemBrowser(console) as browser:
                    original_accounts = {PRIMARY_CODE: ACCOUNTS['account_id'], STANDARD_CODE: ACCOUNTS['daxiang_s_account_id']}
                    report['phases']['baseline'] = actual_run(browser, supplier, automation_id,
                        [PRIMARY_CODE, STANDARD_CODE], original_accounts)
                    settings_path = open_settings(browser, automation_id)
                    form = browser.page.locator('[data-default-plugin-settings]')
                    # The existing boolean control exposes explicit true/false options.
                    form.locator('[data-config-key="include_daxiang_s_self_pickup"]').select_option(label='关闭')
                    form.locator('select[name="account_id"]').select_option(ACCOUNTS['daxiang_s_account_id'])
                    with browser.page.expect_response(lambda response: urlparse(response.url).path == settings_path + '/bridge') as response:
                        form.locator('button[type=submit]').click()
                    if response.value.status != 200 or response.value.json().get('ok') is not True:
                        raise AssertionError('actual default setting save rejected')
                    browser.page.wait_for_function("document.querySelector('[data-settings-feedback]')?.textContent.includes('设置已保存')", timeout=60000)
                    report['phases']['changed'] = actual_run(browser, supplier, automation_id,
                        [PRIMARY_CODE], {PRIMARY_CODE: ACCOUNTS['daxiang_s_account_id']})
                    open_settings(browser, automation_id)
                    history = browser.page.locator('[data-plugin-settings-history]')
                    history.locator('summary').click()
                    history.locator(f'select option[value="{baseline_generation}"]').wait_for(state='attached')
                    history.locator('select').select_option(str(baseline_generation))
                    history.locator('button').click()
                    browser.page.wait_for_function("document.querySelector('[data-plugin-settings-history] [role=status]')?.textContent.includes('设置已恢复')", timeout=60000)
                    report['phases']['restored'] = actual_run(browser, supplier, automation_id,
                        [PRIMARY_CODE, STANDARD_CODE], original_accounts)
                    if browser.errors:
                        raise AssertionError('actual settings browser errors: ' + str(browser.errors))
                report['host_after'] = process_identity()
                if report['host_before'] != report['host_after']:
                    raise AssertionError('settings changes restarted the host')
            for phase in report['phases'].values():
                if not phase['result']['leases'] or any(lease['plugin_version'] != baseline.installed_version for lease in phase['result']['leases']):
                    raise AssertionError('simple settings unexpectedly changed the plugin version')
            report['runtime'] = runner.snapshot()
            report['external_ledger'] = supplier.persisted_problems()
        report['status'] = 'PASS'
        return 0
    except Exception as exc:
        report.update(status='FAIL', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'output': str(OUTPUT)}))


if __name__ == '__main__':
    raise SystemExit(main())
