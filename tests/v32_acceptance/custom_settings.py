"""C04: existing packaged custom iframe, shared accounts, and actual config CAS."""
from __future__ import annotations

from hashlib import sha256
import difflib
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from zipfile import ZipFile

from playwright.sync_api import sync_playwright

from agent.orchestration.models import Actor, ActorType
from agent.automation_plugins.capability_proxy_v2 import build_service_v2_capability_handler_map, UNAVAILABLE_SERVICE_V2_HANDLER_KEYS
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from service_v2_plugins._shared.build_zip import build_plugin_zip
from shared.contracts import api_success
from shared.orchestration_repository import OrchestrationRepository
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.owned_database import connect_owned, prepare_owned
from tests.v32_acceptance.problem_fixture import ACCOUNTS, ProblemAccounts

ROOT = Path(__file__).resolve().parents[2]
DATABASE = 'v32_c04_test'
OUTPUT = ROOT / '.task_tmp/v32/environment/custom-settings.json'
ACTOR = Actor(ActorType.CONSOLE_ADMIN, 'v32-custom-settings', roles=('super_admin',), authenticated_by='mysql_admin_session')
CONFIG = {'sitecode': 'synthetic-site', 'sitefbcode': 'synthetic-hub', 'sitename': '隔离合成站点',
    'sitefbname': '隔离合成分拨', 'first_type': '合成第一次', 'second_type': '合成第二次', 'delay_seconds': 0}


def schedule_rows():
    with connect_owned(DATABASE) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT * FROM scheduled_tasks ORDER BY id')
        return cursor.fetchall()


def ready(page, console, path):
    page.goto(console.url + path, wait_until='domcontentloaded')
    frame = page.frame_locator('[data-plugin-settings-frame]')
    wait_custom_ready(page, frame, console.runtime)
    return frame


def wait_custom_ready(page, frame, runtime):
    try:
        frame.locator('[data-save-settings]:enabled').wait_for(timeout=30000)
    except Exception:
        page.screenshot(path=str(runtime / 'custom-settings-failure.png'), full_page=True)
        raise


def fill(frame, account, site_name):
    for name, value in {**CONFIG, 'sitename': site_name}.items():
        frame.locator(f'[data-config-field="{name}"]').fill(str(value))
    frame.locator('[data-account-role="operator"]').select_option(account)


def save(page, frame, path):
    with page.expect_response(lambda response: urlparse(response.url).path == path + '/bridge', timeout=30000) as pending:
        frame.locator('[data-save-settings]').click()
    return pending.value.status, pending.value.json()


def main():
    prepare_owned(DATABASE)
    runtime = ROOT / '.task_tmp/v32/custom-settings' / uuid4().hex
    runtime.mkdir(parents=True)
    source = ROOT / 'agent/service_v2_plugins/clockin_daxiang_v2'
    baseline = runtime / 'clockin_daxiang_v2-original.zip'
    build_plugin_zip(source, baseline)
    candidate_source = runtime / 'candidate'
    for name in ('manifest.json', 'payload/plugin.py', 'settings/index.html'):
        target = candidate_source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes())
    manifest_path = candidate_source / 'manifest.json'
    original_manifest = manifest_path.read_text(encoding='utf-8')
    manifest = json.loads(original_manifest)
    manifest['version'] = '98.4.0'
    manifest['contributes']['harness'] = []
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (runtime / 'candidate-manifest.patch').write_text(''.join(difflib.unified_diff(
        original_manifest.splitlines(True), manifest_path.read_text(encoding='utf-8').splitlines(True),
        fromfile='original/manifest.json', tofile='candidate/manifest.json')), encoding='utf-8')
    artifact = runtime / 'clockin_daxiang_v2-settings-test-only.zip'
    build_plugin_zip(candidate_source, artifact)
    with ZipFile(baseline) as original_zip, ZipFile(artifact) as candidate_zip:
        original_contents = {name: original_zip.read(name) for name in original_zip.namelist() if name != 'manifest.json'}
        candidate_contents = {name: candidate_zip.read(name) for name in candidate_zip.namelist() if name != 'manifest.json'}
    if original_contents != candidate_contents:
        raise AssertionError('C04 candidate changed existing settings or business payload')
    package = artifact.read_bytes()
    report = {'status': 'RUNNING', 'artifact': {'path': str(artifact), 'sha256': sha256(package).hexdigest()}, 'page_errors': [], 'http_responses': []}
    report['candidate_scope'] = {'label': 'TEST_ONLY', 'version': manifest['version'],
        'original_version': json.loads(original_manifest)['version'], 'original_sha256': sha256(baseline.read_bytes()).hexdigest(),
        'changed_fields': ['version', 'contributes.harness'], 'unchanged_files': sorted(original_contents),
        'limitation': 'The original clock package assistant contribution fails the existing unsafe-capability guard, even when unselected. This candidate removes only the optional declaration; C04 does not cover AI.',
        'patch': str(runtime / 'candidate-manifest.patch')}
    accounts = ProblemAccounts()
    clock_calls = []

    def forbidden_clock_operation(*_args, **_kwargs):
        clock_calls.append('unexpected clock operation')
        raise AssertionError('C04 settings must never invoke clock business operations')

    reviewed = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
        describe_account=accounts.require_active_binding_descriptor, clock_action=forbidden_clock_operation))
    handlers = build_service_v2_capability_handler_map(
        OrchestrationRepository(lambda: connect_owned(DATABASE)), reviewed_handlers=reviewed)
    handlers = {key: value for key, value in handlers.items() if key not in UNAVAILABLE_SERVICE_V2_HANDLER_KEYS}
    try:
        with ManagementFixture(connection_factory=lambda: connect_owned(DATABASE), runtime_root=runtime,
                account_manager=accounts, broker_handlers=handlers, enable_directory_faults=False) as management:
            management.app.add_api_route('/internal/v1/admin/accounts',
                lambda: api_success({'accounts': accounts.list_accounts()}), methods=['GET'])
            inspection = management.management.inspect_service_v2_upload(package,
                request_id=str(uuid4()), transport_package_sha256=sha256(package).hexdigest(), actor=ACTOR)
            report['inspection'] = {'plugin_id': inspection.get('plugin_id'), 'version': inspection.get('version'),
                'runtime_model': inspection.get('runtime_model')}
            if inspection.get('plugin_id') != manifest['plugin_id'] or inspection.get('version') != manifest['version']:
                raise AssertionError('normal package inspection did not match the candidate identity')
            installed = management.management.install_service_v2(package, request_id=str(uuid4()),
                transport_package_sha256=sha256(package).hexdigest(),
                raw_intent=json.dumps({'instance_name': '已有专属设置页面隔离验收', 'permissions_confirmed': True}), actor=ACTOR)
            automation_id = installed['automation_id']
            initial = management.catalog.require(automation_id)
            if initial.configured or initial.enabled:
                raise AssertionError('missing required configuration was falsely ready')
            asset_bytes, asset_type = management.management.settings_asset(automation_id, 'index.html', actor=ACTOR)
            report['installed_asset'] = {'sha256': sha256(asset_bytes).hexdigest(), 'content_type': asset_type}
            schedules_before = schedule_rows()
            path = f'/automations/{automation_id}/settings'
            with ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                    signing_secret=management.signing_secret, runtime_root=runtime / 'console') as console:
                original_binary_read = console.app._agent_binary_request
                report['binary_reads'] = []
                def observed_binary_read(endpoint, **kwargs):
                    result = original_binary_read(endpoint, **kwargs)
                    report['binary_reads'].append({'path': endpoint, 'ok': result.get('ok'),
                        'status': result.get('status'), 'error': result.get('error')})
                    return result
                console.app._agent_binary_request = observed_binary_read
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=os.environ['V32_CHROMIUM_EXECUTABLE'], headless=True)
                    context = browser.new_context(viewport={'width': 1440, 'height': 1080})
                    context.route('**/*', lambda route: route.continue_() if urlparse(route.request.url).hostname == '127.0.0.1' else route.abort())
                    page = context.new_page()
                    page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
                    page.on('response', lambda response: report['http_responses'].append({'path': urlparse(response.url).path, 'status': response.status}))
                    console.login(page, next_path=path)
                    first = page.frame_locator('[data-plugin-settings-frame]')
                    wait_custom_ready(page, first, console.runtime)
                    child_frames = [frame for frame in page.frames if urlparse(frame.url).path == path + '/assets/index.html']
                    if len(child_frames) != 1:
                        raise AssertionError('expected one actual package settings frame')
                    isolated = child_frames[0].evaluate('''() => {
                        let parentBlocked = false;
                        try { void parent.document.body; } catch (error) { parentBlocked = error.name === "SecurityError"; }
                        return {origin: window.origin, parentBlocked,
                            externalScriptCount: document.querySelectorAll("script[src]").length,
                            externalStylesheetCount: document.querySelectorAll("link[rel=stylesheet]").length};
                    }''')
                    report['iframe_isolation'] = isolated
                    if isolated != {'origin': 'null', 'parentBlocked': True, 'externalScriptCount': 0, 'externalStylesheetCount': 0}:
                        raise AssertionError('custom settings frame lost opaque origin or requires cookie-bearing subresources')
                    popup_url = console.url + path + '/assets/index.html?v32_top_level=1'
                    with context.expect_event('response', predicate=lambda response: response.url == popup_url) as response_event:
                        with page.expect_popup() as popup_event:
                            page.evaluate('(url) => {window.open(url, "_blank");}', popup_url)
                    popup = popup_event.value
                    popup.wait_for_load_state('domcontentloaded')
                    popup_response = response_event.value
                    sandbox_directives = [value.strip() for value in popup_response.headers.get('content-security-policy', '').split(';')
                        if value.strip().startswith('sandbox')]
                    standalone = popup.evaluate('''() => {
                        let openerError = null, cookieError = null;
                        try { void opener.document.body; } catch (error) { openerError = error.name; }
                        try { void document.cookie; } catch (error) { cookieError = error.name; }
                        return {origin: window.origin, openerPresent: opener !== null, openerError, cookieError};
                    }''')
                    report['top_level_isolation'] = {'http_status': popup_response.status,
                        'sandbox_directives': sandbox_directives, **standalone}
                    if (popup_response.status != 200 or sandbox_directives != ['sandbox allow-scripts']
                            or standalone != {'origin': 'null', 'openerPresent': True,
                                'openerError': 'SecurityError', 'cookieError': 'SecurityError'}):
                        raise AssertionError('standalone settings asset can access Console origin, opener or cookies')
                    popup.close()
                    first.locator('[data-config-field="sitename"]').fill('')
                    first.locator('[data-save-settings]').click()
                    first.locator('[data-settings-feedback]').filter(has_text='请先完成必填设置').wait_for()
                    after_invalid = management.catalog.require(automation_id)
                    if after_invalid.project_config_version != initial.project_config_version or after_invalid.configured:
                        raise AssertionError('incomplete form changed durable configuration')
                    other = context.new_page()
                    other.on('pageerror', lambda error: report['page_errors'].append(str(error)))
                    stale = ready(other, console, path)
                    choices = first.locator('[data-account-role="operator"] option').evaluate_all('nodes => nodes.map(node => node.value).filter(Boolean)')
                    if set(choices) != set(ACCOUNTS.values()):
                        raise AssertionError('custom page did not use the unified account directory')
                    fill(first, ACCOUNTS['account_id'], CONFIG['sitename'])
                    status, body = save(page, first, path)
                    report['first_save_response'] = {'status': status, 'body': body}
                    if status != 200 or body.get('ok') is not True:
                        raise AssertionError('custom settings save rejected: ' + str(body))
                    first.locator('[data-settings-feedback]').filter(has_text='插件设置已保存').wait_for()
                    saved = management.catalog.require(automation_id)
                    report['first_saved_configuration'] = dict(saved.project_config)
                    report['first_saved_accounts'] = dict(saved.account_bindings)
                    report['first_saved_runtime'] = {'target_generation': saved.target_generation,
                        'committed_generation': saved.committed_generation, 'reconcile_state': saved.reconcile_state.value}
                    if saved.committed_generation is None or saved.reconcile_state.value != 'STABLE':
                        raise AssertionError('custom settings generation did not complete real reconciliation')
                    if dict(saved.project_config) != CONFIG or saved.account_bindings['operator'] != ACCOUNTS['account_id']:
                        raise AssertionError('custom save differs from actual shared configuration')
                    fill(stale, ACCOUNTS['daxiang_s_account_id'], '迟到页面不应覆盖')
                    conflict_status, conflict_body = save(other, stale, path)
                    if conflict_status != 409 or conflict_body.get('ok') is not False:
                        raise AssertionError('stale custom page was not rejected by real CAS: ' + str(conflict_body))
                    after_conflict = management.catalog.require(automation_id)
                    if after_conflict.project_config_version != saved.project_config_version or dict(after_conflict.project_config) != CONFIG:
                        raise AssertionError('stale page changed shared configuration')
                    fresh = ready(other, console, path)
                    if fresh.locator('[data-account-role="operator"]').input_value() != ACCOUNTS['account_id']:
                        raise AssertionError('reopened custom page lost persisted account reference')
                    fill(fresh, ACCOUNTS['daxiang_s_account_id'], '重新载入后的合成站点')
                    status, body = save(other, fresh, path)
                    if status != 200 or body.get('ok') is not True:
                        report['fresh_save_response'] = {'status': status, 'body': body}
                        raise AssertionError('fresh custom page update rejected: ' + str(body))
                    fresh.locator('[data-settings-feedback]').filter(has_text='插件设置已保存').wait_for()
                    final = management.catalog.require(automation_id)
                    if final.account_bindings['operator'] != ACCOUNTS['daxiang_s_account_id'] or final.project_config['sitename'] != '重新载入后的合成站点':
                        raise AssertionError('reloaded custom page did not save the selected account')
                    if tuple(final.current_enabled_entrypoints) != ('manual_run',) or final.enabled:
                        raise AssertionError('custom settings changed the explicitly closed AI or execution entrypoint state')
                    report['second_saved_configuration'] = {'config': dict(final.project_config),
                        'accounts': dict(final.account_bindings), 'version': final.project_config_version,
                        'generation': final.committed_generation}
                    other.locator('[data-plugin-settings-history] summary').click()
                    other.wait_for_function("document.querySelector('[data-plugin-settings-history] select').options.length > 1", timeout=10000)
                    other.locator('[data-plugin-settings-history] select').select_option(str(saved.committed_generation))
                    other.locator('[data-plugin-settings-history] button').click()
                    other.locator('[data-plugin-settings-history] [role=status]').filter(has_text='设置已恢复').wait_for(timeout=30000)
                    restored = management.catalog.require(automation_id)
                    if (dict(restored.project_config) != CONFIG or restored.account_bindings['operator'] != ACCOUNTS['account_id']
                            or restored.project_config_version <= final.project_config_version
                            or tuple(restored.current_enabled_entrypoints) != ('manual_run',)):
                        raise AssertionError('custom settings history did not restore exact prior configuration and account')
                    reopened = ready(other, console, path)
                    if (reopened.locator('[data-account-role="operator"]').input_value() != ACCOUNTS['account_id']
                            or reopened.locator('[data-config-field="sitename"]').input_value() != CONFIG['sitename']):
                        raise AssertionError('custom page reload did not show restored configuration')
                    report['history_restore'] = {'status': 'PASS', 'source_generation': saved.committed_generation,
                        'committed_generation': restored.committed_generation,
                        'configuration_version': restored.project_config_version}
                    final = restored
                    other.screenshot(path=str(runtime / 'custom-settings.png'), full_page=True)
                    context.close()
                    browser.close()
            if schedule_rows() != schedules_before:
                raise AssertionError('custom settings modified a schedule')
            if report['page_errors']:
                raise AssertionError('custom settings page script errors')
            if clock_calls:
                raise AssertionError('settings exercised a clock business operation')
            report.update(status='PASS', automation_id=automation_id, plugin_version=final.installed_version,
                configured_before=initial.configured, configured_after=final.configured,
                account_directory=choices, initial_version=initial.project_config_version,
                saved_version=saved.project_config_version, final_version=final.project_config_version,
                persisted_config=dict(final.project_config), persisted_accounts=dict(final.account_bindings),
                stale_page={'http_status': conflict_status, 'body': conflict_body},
                schedule_rows_preserved=len(schedules_before), clock_business_calls=len(clock_calls),
                screenshot=str(runtime / 'custom-settings.png'))
        return 0
    except Exception as exc:
        report.update(status='FAIL', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'output': str(OUTPUT)}))


if __name__ == '__main__':
    raise SystemExit(main())
