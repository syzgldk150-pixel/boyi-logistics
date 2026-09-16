"""Semantic checks for artifacts produced by the existing acceptance driver."""
from collections import Counter
from hashlib import sha256
import math
from pathlib import Path
import zipfile


BUSINESS_PROBES = frozenset({'daily_scan_statistics', 'daily_problems', 'daily_concurrency',
    'settings_effective', 'custom_settings', 'catalog_delivery', 'scan_recovery',
    'unknown_resource_scope', 'scan_cancel_recovery', 'customer_collection_flow',
    'finance_source_state', 'field_maintenance', 'decision_maintenance', 'plugin_cli_batch', 'run_acceptance'})


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _distribution(rows, field, limit, minimum, reported):
    _require(isinstance(rows, list) and len(rows) >= minimum, 'insufficient raw performance samples')
    _require(all(row.get('error') is None and type(row.get(field)) in {int, float}
                 and math.isfinite(row[field]) and row[field] >= 0 for row in rows), 'failed or missing measured samples')
    values = sorted(row[field] for row in rows)
    actual = {'samples': len(rows), 'successful_samples': len(rows), 'errors': 0,
        'p50_ms': values[math.ceil(len(values) * .5) - 1],
        'p95_ms': values[math.ceil(len(values) * .95) - 1], 'max_ms': max(values), 'threshold_ms': limit}
    _require(all(reported.get(key) == value for key, value in actual.items()), 'performance summary differs from raw measurements')
    _require(actual['p95_ms'] <= limit and reported.get('status') == 'PASS', 'performance threshold not met')


def _artifact(artifact, root):
    _require(artifact.get('runtime_model') == 'SERVICE_V2', 'maintenance package is not current V2')
    path = Path(artifact['archive']).resolve()
    path.relative_to((root / '.task_tmp').resolve())
    _require(path.is_file() and sha256(path.read_bytes()).hexdigest() == artifact['sha256'], 'maintenance ZIP digest mismatch')
    with zipfile.ZipFile(path) as archive:
        actual = {name: sha256(archive.read(name)).hexdigest() for name in archive.namelist()}
    _require(actual == artifact['files'] and actual['manifest.json'] == artifact['manifest_sha256'], 'maintenance member digest mismatch')


def validate_probe(name, data, *, root):
    """Return only after this named probe proves its declared current contract."""
    _require(isinstance(data, dict) and data.get('status') == 'PASS', 'probe artifact does not declare completed PASS')
    if name in BUSINESS_PROBES:
        _require(data.get('runtime_model') == 'SERVICE_V2', 'probe did not execute the current SERVICE_V2 model')
    for field in ('page_errors', 'browser_errors', 'external_requests'):
        if field != 'external_requests' or name == 'legacy_page_smoke':
            _require(not data.get(field), 'browser or external boundary failure')
    if name in BUSINESS_PROBES - {'custom_settings', 'catalog_delivery', 'plugin_cli_batch'}:
        invocations = [row for row in _objects(data) if row.get('invocation_id') and row.get('status') == 'COMPLETED'
                       and isinstance(row.get('result'), dict) and row['result'].get('status') == 'SUCCESS']
        _require(invocations, 'no completed real Invocation result in business evidence')
    if name == 'scan_recovery':
        cases = data['cases']
        for mode in ('APPLIED', 'NOT_APPLIED', 'UNKNOWN'):
            selected = [row for row in cases if row.get('mode') == mode]
            _require(len({row['round'] for row in selected}) >= 20, 'scan unknown scenario lacks twenty distinct rounds: ' + mode)
            _require(all(row['result']['status'] == 'WRITE_OUTCOME_UNKNOWN' and row['write_receipts']
                         and row['automatic_recovery_or_replay'] is False for row in selected), 'unknown facts or write receipts missing')
    elif name == 'scan_cancel_recovery':
        _require(len({row['round'] for row in data['cases']}) >= 20, 'cancel drill lacks twenty rounds')
        _require(all(row['busy_call_ended']['error_code'] == 'EXECUTION_RESOURCE_BUSY'
                     and row['cancelled']['status'] == 'WRITE_OUTCOME_UNKNOWN' and row['write_receipts']
                     for row in data['cases']), 'cancellation drain proof missing')
    elif name == 'unknown_resource_scope':
        case = data['cases'][0]
        _require(len(case['same_physical_target_checks']) >= 20 and case['historical_receipts_unchanged'] is True,
                 'same-target explicit calls or immutable history proof missing')
    elif name == 'settings_effective':
        _require(set(data['phases']) == {'baseline', 'changed', 'restored'} and data['host_before'] == data['host_after'],
                 'settings change, restoration or unchanged process proof missing')
    elif name == 'custom_settings':
        _require(data['iframe_isolation']['parentBlocked'] is True and data['stale_page']['http_status'] == 409
                 and data['history_restore']['status'] == 'PASS', 'custom settings isolation/CAS/restore proof missing')
    elif name == 'daily_concurrency':
        _require(set(data['results']) == {'scan_codes', 'arrival_stats', 'self_pickup_problem_upload', 'split_pending_problem_upload'}
                 and data['failure']['status'] == 'FAILED' and data['legacy_counts_before'] == data['legacy_counts_after'],
                 'four actual independent V2 chains or failure isolation missing')
    elif name in {'field_maintenance', 'decision_maintenance'}:
        _require(data['host_before'].get('status') == 'PASS' and data['host_before'] == data['host_after']
                 and data['startup_before'] == data['startup_after'], 'Host freeze or process continuity failed')
        for artifact in data['artifacts'].values():
            _artifact(artifact, root)
        _require(set(data['artifacts']) == {'baseline', 'candidate'}, 'maintenance package pair missing')
        baseline, candidate = (data['artifacts'][key] for key in ('baseline', 'candidate'))
        _require(baseline['sha256'] != candidate['sha256'], 'maintenance candidate did not change')
        if name == 'field_maintenance':
            steps = {row['phase']: row for row in data['steps']}
            _require(set(steps) == {'baseline', 'baseline_source_changed', 'candidate', 'rollback'}, 'field maintenance phase missing')
            _require(steps['baseline_source_changed']['run']['status'] == 'FAILED'
                     and steps['rollback']['changed_source_failure']['status'] == 'FAILED', 'old field package failure was not demonstrated')
            _require(steps['candidate']['unrelated_while_held']['status'] == 'COMPLETED', 'unrelated work did not finish during maintenance')
        else:
            _require(len(data['phases']) == 3 and len(data['unrelated_runs']) >= 2
                     and all(row['status'] == 'COMPLETED' for row in data['unrelated_runs']), 'decision upgrade/rollback or independent work missing')
    elif name == 'plugin_cli_batch':
        expected = {'sync_scan_codes_v2', 'sync_arrival_stats_v2', 'self_pickup_problem_upload_v2',
                    'split_pending_problem_upload_v2', 'sync_finance_bills_v2', 'sync_customer_service_problems_v2'}
        _require(set(data['plugins']) == expected and all(row['status'] == 'PASS' for row in data['plugins'].values()), 'current CLI package coverage incomplete')
        _require(data['host_freeze'].get('status') == 'PASS' and data['host_freeze'] == data['host_after'], 'CLI Host freeze missing')
        for entry in data['plugins'].values():
            _require([row['action'] for row in entry['commands']] == ['describe', 'test', 'package']
                     and all(row['exit_code'] == 0 for row in entry['commands']), 'actual CLI command sequence incomplete')
            _require(entry['test']['tests']['status'] == entry['package']['tests']['status'] == 'PASS', 'CLI local tests incomplete')
            artifact = entry['package']['artifact']
            tested = entry['package']['tests']['tested_material']
            _require(artifact['runtime_model'] == tested['runtime_model'] == 'SERVICE_V2'
                     and artifact['package_sha256'] == tested['package_sha256']
                     and tested['test_files'] and tested['files'], 'CLI artifact is not bound to tested material')
            archive = Path(artifact['path']).resolve()
            archive.relative_to((root / '.task_tmp').resolve())
            _require(sha256(archive.read_bytes()).hexdigest() == artifact['package_sha256'], 'CLI package bytes changed')
            with zipfile.ZipFile(archive) as package:
                _require({name: sha256(package.read(name)).hexdigest() for name in package.namelist()} == tested['files'], 'CLI tested member digest mismatch')
    elif name == 'browser_performance':
        _require(data['concurrent_clients'] >= 4 and data['distinct_authenticated_administrators'] >= 4, 'performance client coverage incomplete')
        for module in ('automation', 'finance', 'customer_service'):
            rows = [row for row in data['raw_samples'] if row['module'] == module]
            _require(all(row['instance_count'] >= 50 for row in rows), 'insufficient actual installed instances')
            _distribution(rows, 'shell_ms', 1000, 30, data['results'][module]['shell'])
            _distribution(rows, 'list_ms', 1500, 30, data['results'][module]['list_and_controls'])
        _require(not [row for row in data['fault_boundary_calls'] if row['phase'] == 'measurement'], 'cold navigation waited for faulted remote dependencies')
    elif name == 'detail_browser':
        _require(data['concurrent_clients'] >= 4 and data['distinct_authenticated_administrators'] >= 4, 'detail clients missing')
        _require(data['data']['customer_service']['records'] >= 10000
                 and len(data['data']['customer_service']['sources']) >= 50
                 and data['data']['finance']['source_count'] >= 50
                 and data['data']['finance']['proof']['row_count'] >= 10000, 'published detail data volume incomplete')
        for module in ('finance', 'customer_service'):
            rows = [row for row in data['raw_samples'] if row['module'] == module]
            _distribution(rows, 'query_dom_ms', 1500, 100, data['results'][module])
    elif name == 'run_acceptance':
        _require(data['concurrent_clients'] >= 4 and data['distinct_authenticated_administrators'] >= 4, 'admission clients missing')
        _distribution(data['raw_samples'], 'acceptance_ms', 500, 100, data['result'])
        _require(len({row['invocation_id'] for row in data['persisted_invocations']}) >= 100
                 and all(row['status'] == 'COMPLETED' and row['result']['status'] == 'SUCCESS'
                         for row in data['persisted_invocations']), 'admitted calls lack durable completed results')
    elif name == 'navigation_probe':
        _distribution(data['raw_samples'], 'tab_ms', 200, 20, data['tab_switch'])
        _require(data['settings']['status'] == 'PASS' and data['module_navigation']['status'] == 'PASS', 'navigation/settings proof missing')
    elif name == 'module_scope':
        counts = Counter(row['module'] for row in data['raw_samples'])
        _require(set(counts) == {'automation', 'finance', 'customer_service'} and all(row['instance_count'] == 50 for row in data['raw_samples']),
                 'module ownership coverage missing')
