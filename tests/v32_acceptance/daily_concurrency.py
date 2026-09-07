"""A02: four real daily packages and an independently failing real package."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import secrets
import threading
import time
from uuid import uuid4

from Crypto.PublicKey import ECC

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.package import Ed25519TrustStore
from agent.orchestration.context_builder import ContextBuilder
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.first_party import build_production_first_party_core_handler_map
from plugin_core_adapters.problem_actions import build_production_problem_handler_map
from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, CHILD_CODE, DailyAccounts
from tests.v32_acceptance.daily_scan import ACTOR, setup_scan, signed_request
from tests.v32_acceptance.daily_stats import setup_stats
from tests.v32_acceptance.daily_stats_protocol import DailyStatsProtocol
from tests.v32_acceptance.daily_problems import setup_split
from tests.v32_acceptance.decision_maintenance import setup_instance
from tests.v32_acceptance.first_party_fixture import bootstrap, isolated_migration_accounts
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.owned_database import connect_owned, prepare_owned
from tests.v32_acceptance.problem_fixture import ACCOUNTS, SPLIT_TARGET, ProblemAccounts, ProblemSupplier
from tests.v32_acceptance.post_success_delivery import exercise_post_success
from tests.v32_acceptance.runner_fixture import RunnerFixture

ROOT = Path(__file__).resolve().parents[2]
DATABASE = 'v32_a02_test'
OUTPUT = ROOT / '.task_tmp/v32/reliability/a02-daily-concurrency.json'


class Accounts:
    def list_accounts(self, **_options):
        return DailyAccounts().list_accounts() + ProblemAccounts().list_accounts()

    def provider(self, identity):
        if identity == ACCOUNT_ID:
            return DailyAccounts()
        if identity in ACCOUNTS.values():
            return ProblemAccounts()
        raise ValueError('Unbound isolated account')

    def require_active_binding_descriptor(self, identity):
        return self.provider(identity).require_active_binding_descriptor(identity)

    def public_credentials(self, identity):
        return self.provider(identity).public_credentials(identity)


class BoundaryGate:
    def __init__(self):
        self.enabled = False
        self.started = threading.Event()
        self.release = threading.Event()

    def wait(self):
        if self.enabled:
            self.started.set()
            if not self.release.wait(25):
                raise TimeoutError('Owned A02 protocol barrier deadline')


class DailyBoundary(DailyStatsProtocol):
    def __init__(self, gate):
        super().__init__()
        self.gate = gate

    def handle(self, path, query, arguments):
        if path == '/dataOperation/saveTables':
            self.gate.wait()
        return super().handle(path, query, arguments)


class ProblemBoundary(ProblemSupplier):
    def __init__(self, runtime, gate):
        super().__init__(runtime)
        self.gate = gate

    def _post(self, path, payload):
        if path == '/problem' and payload['action'] == 'query':
            self.gate.wait()
        return super()._post(path, payload)


def wait_run(management, run_id, *, expected=None, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = management.repository.get_run(run_id)
        if row and row['status'] in {'COMPLETED', 'FAILED_TERMINAL', 'CANCELLED', 'BLOCKED_DATA', 'PARTIAL'}:
            if expected and row['status'] != expected:
                raise AssertionError({key: row[key] for key in ('run_id', 'status', 'error_code', 'error_summary')})
            return row
        time.sleep(.05)
    raise AssertionError('A02 actual Runner deadline: ' + run_id)


def failing_package(management):
    source, archive = management.task_env / 'failure-source', management.task_env / 'failure.zip'
    init_service_v2_source(source, plugin_id='v32_a02_failure', name='A02 isolated failure', version='1.0.0')
    main = source / 'payload/main.py'
    script = main.read_text()
    trigger = '        _read_request()'
    if script.count(trigger) != 1:
        raise AssertionError('Generated failure fixture entry changed')
    main.write_text(script.replace(trigger, trigger + '\n        raise ValueError("isolated intentional fifth-task failure")'))
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    installed = management.management.install_service_v2(package, request_id=str(uuid4()),
        transport_package_sha256=sha256(package).hexdigest(),
        raw_intent=json.dumps({'instance_name': 'A02 isolated failure', 'permissions_confirmed': True}), actor=ACTOR)
    identity = installed['automation_id']
    entry = management.catalog.require(identity)
    management.management.save_plugin_settings(identity, config={}, account_bindings={}, resource_bindings={},
        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    entry = management.catalog.require(identity)
    management.management.set_enabled(identity, enabled=True, request_id=str(uuid4()), expected_record_version=entry.record_version, actor=ACTOR)
    management.targets.reconcile_project(identity)
    return identity


def main():
    prepare_owned(DATABASE)
    runtime = OUTPUT.parent / ('a02-' + uuid4().hex)
    runtime.mkdir(parents=True)
    account_manager = Accounts()
    private_key = ECC.generate(curve='Ed25519')
    trust = Ed25519TrustStore({'v32-a02': private_key.public_key().export_key(format='raw')})
    manifests = resolve_first_party_manifests(ToolRegistry(), _plugin_ids=frozenset({'sync_scan_codes', 'sync_arrival_stats'}))
    keys = {(item['operation'], item['action']) for manifest in manifests.values() for item in manifest.runtime_permissions['broker_operations']}
    migration_accounts = isolated_migration_accounts()
    for identity in ('scan_codes', 'arrival_stats'):
        migration_accounts[identity] = {'account_id': [ACCOUNT_ID]}
    migration_accounts['self_pickup_problem_upload'] = {role: [value] for role, value in ACCOUNTS.items()}
    migration_accounts['split_pending_problem_upload'] = {'account_id': [ACCOUNTS['account_id']]}
    scan_gate, problem_gate = BoundaryGate(), BoundaryGate()
    report = {'status': 'RUNNING'}
    try:
        with DailyBoundary(scan_gate) as daily, daily.authentication_boundaries(), ProblemBoundary(runtime / 'supplier', problem_gate) as problems:
            # Statistics and split really share the same bound destination.
            problems.resources[SPLIT_TARGET] = daily.resources[SPLIT_TARGET]
            resources = dict(daily.resources)
            for key, value in problems.resources.items():
                if key in resources and resources[key] != value:
                    raise AssertionError('Ambiguous owned resource identity')
                resources[key] = value
            def sheet(action, arguments):
                if arguments['spreadsheet_token'] == 'isolated-daily-workbook':
                    return daily.feishu_operation(action, arguments)
                return problems.feishu_operation(action, arguments)
            handlers = build_production_first_party_core_handler_map(cursor_secret=secrets.token_bytes(32),
                account_manager=account_manager, allowed_action_keys=keys, capability_authorizer=daily.authorize)
            problem_handlers = build_production_problem_handler_map(cursor_secret=secrets.token_bytes(32),
                account_manager=account_manager, resource_loader=resources.get, feishu_operation=sheet,
                problem_action=problems.problem_action, capability_authorizer=problems.authorize)
            if set(handlers) & set(problem_handlers):
                raise AssertionError('Ambiguous real broker handler identity')
            handlers.update(problem_handlers)
            with ManagementFixture(connection_factory=lambda: connect_owned(DATABASE), runtime_root=runtime,
                    account_manager=account_manager, broker_handlers=handlers, resource_provider=resources.get,
                    upload_signature_verifier=trust, enable_directory_faults=False,
                    migration_account_bindings=migration_accounts) as management:
                bootstrap(management, private_key=private_key, trust=trust, key_id='v32-a02')
                for identity in ('scan_codes', 'arrival_stats'):
                    management.targets.reconcile_project(identity)
                setup_scan(management)
                setup_stats(management)
                setup_instance(management, None)
                setup_split(management)
                failure_id = failing_package(management)
                with RunnerFixture(management, saved_resource_provider=resources.get,
                        context_builder=ContextBuilder(account_resolver=lambda _command: account_manager.list_accounts())) as runner:
                    previews = {}
                    for identity in ('scan_codes', 'self_pickup_problem_upload', 'split_pending_problem_upload'):
                        path = f'/internal/v1/automation-projects/{identity}/' + ('invoke' if identity == 'scan_codes' else 'selection-previews')
                        preview = signed_request(management, path, payload={'request_id': str(uuid4())})
                        wait_run(management, preview['run_id'], expected='COMPLETED')
                        previews[identity] = preview['run_id']
                    assert daily.ledger == [] and problems.persisted_problems() == []
                    scan_gate.enabled = problem_gate.enabled = True
                    runs = {}
                    scan = signed_request(management, '/internal/v1/automation-projects/scan_codes/invoke',
                        payload={'request_id': str(uuid4()), 'preview_run_id': previews['scan_codes']})
                    runs['scan_codes'] = scan['run_id']
                    assert scan_gate.started.wait(15), 'Actual scan did not reach isolated write boundary'
                    for identity, codes in [('self_pickup_problem_upload', ['R_M03_STANDARD']),
                            ('split_pending_problem_upload', ['SYNTHETIC-SPLIT', 'SYNTHETIC-NOT-ARRIVED'])]:
                        receipt = signed_request(management, f'/internal/v1/automation-projects/{identity}/selection-previews/{previews[identity]}/confirm',
                            payload={'request_id': str(uuid4()), 'selected_bill_codes': codes})
                        runs[identity] = receipt['run_id']
                    assert problem_gate.started.wait(10), 'Actual problem task did not reach external boundary'
                    receipt = signed_request(management, '/internal/v1/automation-projects/arrival_stats/invoke', payload={'request_id': str(uuid4())})
                    runs['arrival_stats'] = receipt['run_id']
                    receipt = signed_request(management, f'/internal/v1/automation-projects/{failure_id}/invoke', payload={'request_id': str(uuid4())})
                    failed = wait_run(management, receipt['run_id'], expected='FAILED_TERMINAL', timeout=8)
                    active_after_failure = {identity: management.repository.get_run(run_id)['status'] for identity, run_id in runs.items()}
                    assert all(status not in {'CANCELLED', 'FAILED_TERMINAL', 'COMPLETED'} for status in active_after_failure.values()), active_after_failure
                    waiting_stats = management.repository.get_run(runs['arrival_stats'])
                    assert waiting_stats['started_at'] is None
                    assert waiting_stats['execution_attempt_count'] == 0
                    assert waiting_stats['steps'] and all(step['status'] == 'PENDING' for step in waiting_stats['steps'])
                    assert not any(request['action'] == 'FIND_DISPATCH_FORECAST_CENTER' for request in daily.requests)
                    report['dependency_wait'] = {'status': 'PASS', 'statistics_started_at': waiting_stats['started_at'],
                        'statistics_attempts': waiting_stats['execution_attempt_count'],
                        'statistics_step_states': [step['status'] for step in waiting_stats['steps']],
                        'source_http_requests': list(daily.requests)}
                    scan_gate.release.set()
                    problem_gate.release.set()
                    results = {}
                    for identity, run_id in runs.items():
                        row = wait_run(management, run_id, expected='COMPLETED')
                        assert all(step['postcondition_status'] == 'VERIFIED' for step in row['steps'])
                        results[identity] = {key: row[key] for key in ('run_id', 'status', 'started_at', 'finished_at', 'execution_attempt_count', 'steps')}
                    assert results['arrival_stats']['started_at'] >= results['scan_codes']['finished_at']
                    assert [row['BILL_CODE'] for row in daily.ledger] == [CHILD_CODE]
                    assert {row['bill_code'] for row in problems.persisted_problems()} == {'R_M03_STANDARD', 'SYNTHETIC-SPLIT', 'SYNTHETIC-NOT-ARRIVED'}
                    report['post_success_delivery'] = exercise_post_success(management=management,
                        connection_factory=lambda: connect_owned(DATABASE), run_ids=runs,
                        external_state=lambda: {'scan_ledger': daily.ledger, 'problem_ledger': problems.persisted_problems(),
                            'sheet_writes': daily.sheet_writes})
                    report.update(status='PASS', results=results, failure={key: failed[key] for key in ('run_id', 'status', 'error_code')},
                        active_after_failure=active_after_failure, runtime=runner.snapshot(), scan_ledger=daily.ledger,
                        problem_ledger=problems.persisted_problems(), sheet_writes=daily.sheet_writes,
                        constraints='Scan/statistics share one account and real scan snapshot; self/split share account; statistics/split share a physical sheet. Existing constraints remain active.')
    except Exception as error:
        report.update(status='FAIL', error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        scan_gate.release.set()
        problem_gate.release.set()
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'output': str(OUTPUT)}))


if __name__ == '__main__':
    main()
