"""Real self-pickup decision package drill in the dedicated M03 database."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import difflib
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from uuid import uuid4

import pymysql
from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import first_party_payload_files, resolve_first_party_manifests
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.package import Ed25519PackageSigner, Ed25519TrustStore, build_signed_plugin_zip, verify_signed_plugin_zip
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.models import Actor, ActorType
from agent.tool_registry import ToolRegistry
from shared.contracts import api_success
from plugin_core_adapters.problem_actions import build_production_problem_handler_map
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.problem_fixture import ACCOUNTS, RESOURCE_ID, ProblemAccounts, ProblemSupplier
from tests.v32_acceptance.runner_fixture import RunnerFixture
from tests.v32_acceptance.first_party_fixture import bootstrap, isolated_migration_accounts
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.problem_browser import ProblemBrowser
from tests.v32_acceptance.host_freeze import process_identity, verify_host
from tests.v32_acceptance.unrelated_fixture import install_unrelated

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".task_tmp" / "v32" / "m03"
DATABASE = "v32_m03_test"
ISOLATED_DATABASES = frozenset({DATABASE, 'v32_a01_problem_test', 'v32_m01_test'})
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "v32-m03-admin", roles=("super_admin",), authenticated_by="mysql_admin_session")


def connect():
    database = os.environ.get('AGENT_DB_NAME')
    if database not in ISOLATED_DATABASES or os.environ.get("AGENT_DB_HOST") != "127.0.0.1" or os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        raise RuntimeError("M03 requires its explicit isolated database")
    return pymysql.connect(host="127.0.0.1", port=int(os.environ["AGENT_DB_PORT"]),
        user=os.environ["AGENT_DB_USER"], password=os.environ["AGENT_DB_PASS"],
        database=database, charset="utf8mb4", autocommit=False, cursorclass=pymysql.cursors.DictCursor)


def prepare_database():
    from tests.test_mysql_orchestration_integration import MySqlOrchestrationIntegrationTests, _load_migration_runner
    database = os.environ.get('AGENT_DB_NAME')
    if database not in ISOLATED_DATABASES or os.environ.get("AGENT_DB_HOST") != "127.0.0.1":
        raise RuntimeError("M03 cannot initialize another database")
    fixture = MySqlOrchestrationIntegrationTests
    fixture.pymysql, fixture.host = pymysql, "127.0.0.1"
    fixture.port, fixture.user = int(os.environ["AGENT_DB_PORT"]), os.environ["AGENT_DB_USER"]
    fixture.password, fixture.runner = os.environ["AGENT_DB_PASS"], _load_migration_runner()
    with pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
            autocommit=True) as connection, connection.cursor() as cursor:
        # This flag rebuilds only this dedicated synthetic drill database.
        cursor.execute("DROP DATABASE IF EXISTS " + database)
        cursor.execute("CREATE DATABASE " + database + " CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    fixture._run_migrations(database)
    fixture._run_migrations(database, check_only=True)
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE scheduled_tasks SET enabled=0")
        connection.commit()


def build_artifacts(private_key, output_root):
    plugin_id = "self_pickup_problem_upload"
    source = resolve_first_party_manifests(ToolRegistry(), _plugin_ids=frozenset({plugin_id}))[plugin_id]
    stamp = int(time.time())
    artifacts = {}
    source_payload = first_party_payload_files(source)["payload/action.py"]
    for index, label in enumerate(("baseline", "candidate")):
        mapping = source.to_signed_mapping()
        mapping["version"] = source.version if label == "baseline" else f"98.3.{stamp + index}"
        manifest = AutomationPluginManifest.from_mapping(mapping)
        files = first_party_payload_files(source)
        if label == "candidate":
            old = b'delivery_method == rule["delivery_method"]'
            new = '(delivery_method == rule["delivery_method"] or (rule["delivery_method"] == "自提" and delivery_method == "预约自提"))'.encode()
            if files["payload/action.py"].count(old) != 1:
                raise AssertionError("M03 decision edit must resolve exactly once")
            files["payload/action.py"] = files["payload/action.py"].replace(old, new)
        package = build_signed_plugin_zip(manifest, files,
            signer=Ed25519PackageSigner(key_id="v32-m03", private_key=private_key))
        verified = verify_signed_plugin_zip(package,
            verifier=Ed25519TrustStore({"v32-m03": private_key.public_key().export_key(format="raw")}))
        source_root = output_root / label
        source_root.mkdir(parents=True)
        for path, contents in files.items():
            target = source_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        (source_root / 'manifest.json').write_text(json.dumps(manifest.to_signed_mapping(), ensure_ascii=False, indent=2)+'\n')
        archive_path = output_root / (plugin_id + '-' + manifest.version + '.zip')
        archive_path.write_bytes(package)
        test_command = [os.sys.executable, str(ROOT / 'tests/v32_acceptance/decision_payload_test.py'), '--expected', str(index + 1)]
        test_env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(source_root), str(source_root/'payload'))), PYTHON_DOTENV_DISABLED='1')
        tested = subprocess.run(test_command, cwd=source_root, env=test_env, check=True,
            capture_output=True, text=True, timeout=30)
        artifacts[label] = {"version": manifest.version, "bytes": package,
            "sha256": sha256(package).hexdigest(),
            "archive": str(archive_path), "source_root": str(source_root),
            "manifest_sha256": verified.manifest_sha256,
            "local_test": json.loads(tested.stdout), "local_test_command": test_command,
            "local_test_environment": {'PYTHONPATH':test_env['PYTHONPATH'], 'PYTHON_DOTENV_DISABLED':'1'},
            "files": {path: sha256(value).hexdigest() for path, value in files.items()}}
        if label == 'candidate':
            (output_root / 'decision.patch').write_text(''.join(difflib.unified_diff(
                source_payload.decode().splitlines(keepends=True), files['payload/action.py'].decode().splitlines(keepends=True),
                fromfile='a/payload/action.py', tofile='b/payload/action.py')), encoding='utf-8')
    changed = [path for path, digest in artifacts["baseline"]["files"].items()
        if artifacts["candidate"]["files"][path] != digest]
    if changed != ["payload/action.py"]:
        raise AssertionError(f"M03 candidate changed another payload: {changed}")
    return artifacts


@contextmanager
def composed():
    account_manager = ProblemAccounts()
    private_key = ECC.generate(curve="Ed25519")
    runtime = RUNTIME / ("run-" + uuid4().hex)
    runtime.mkdir(parents=True)
    artifacts = build_artifacts(private_key, runtime / 'artifacts')
    migration_accounts = isolated_migration_accounts()
    migration_accounts["self_pickup_problem_upload"] = {role: [value] for role, value in ACCOUNTS.items()}
    migration_accounts['split_pending_problem_upload'] = {'account_id':[ACCOUNTS['account_id']]}
    with ProblemSupplier(runtime) as supplier:
        handlers = build_production_problem_handler_map(cursor_secret=secrets.token_bytes(32),
            account_manager=account_manager, resource_loader=supplier.resource_loader,
            feishu_operation=supplier.feishu_operation, problem_action=supplier.problem_action,
            capability_authorizer=supplier.authorize)
        trust = Ed25519TrustStore({"v32-m03": private_key.public_key().export_key(format="raw")})
        with ManagementFixture(connection_factory=connect, runtime_root=runtime,
                account_manager=account_manager, broker_handlers=handlers, resource_provider=supplier.resource_loader,
                upload_signature_verifier=trust, enable_directory_faults=False,
                migration_account_bindings=migration_accounts) as management:
            management.app.add_api_route('/internal/v1/admin/accounts',
                lambda: api_success({'accounts': account_manager.list_accounts()}), methods=['GET'])
            management.bootstrap_result = bootstrap(management, private_key=private_key, trust=trust, key_id="v32-m03")
            context = ContextBuilder(account_resolver=lambda _command: account_manager.list_accounts())
            with RunnerFixture(management, context_builder=context) as runner:
                yield management, runner, supplier, artifacts


def setup_instance(management, artifact):
    automation_id = "self_pickup_problem_upload"
    reconciled = management.targets.reconcile_project(automation_id)
    print("m03_initial_reconcile=" + str(reconciled), flush=True)
    entry = management.catalog.require(automation_id)
    management.management.save_plugin_settings(automation_id, config={"include_daxiang_s_self_pickup": True},
        account_bindings={role: [value] for role, value in ACCOUNTS.items()},
        resource_bindings={"self_pickup_source_sheet": RESOURCE_ID}, request_id=str(uuid4()),
        expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    entry = management.catalog.require(automation_id)
    management.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
        expected_record_version=entry.record_version, actor=ACTOR)
    management.targets.reconcile_project(automation_id)
    projection = management.policy.get_policy_projection(automation_id)
    management.policy.update_policy(automation_id, mode='PROJECT_FULL_AUTO', request_id=str(uuid4()),
        comment='Explicit isolated M03 synthetic write authorization',
        expected_policy_version=projection['policy_version'],
        expected_project_configuration_version=projection['project_configuration_version'], actor=ACTOR)
    return automation_id


def wait_result(run_id):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status,error_code,error_summary FROM agent_runs WHERE run_id=%s", (run_id,))
            run = cursor.fetchone()
            if run and run["status"] in {"COMPLETED", "FAILED_TERMINAL", "CANCELLED", "PARTIAL", "WAITING_APPROVAL"}:
                cursor.execute("SELECT result_summary_json,postcondition_status FROM agent_run_steps WHERE run_id=%s ORDER BY step_order", (run_id,))
                steps = cursor.fetchall()
                cursor.execute('''SELECT lease.generation,lease.outcome,generation.plugin_version,
                    lease.runtime_metadata_sha256,lease.released_at FROM automation_project_generation_leases lease
                    JOIN automation_project_generations generation ON generation.automation_id=lease.automation_id AND generation.generation=lease.generation
                    WHERE lease.orchestration_run_id=%s ORDER BY lease.acquired_at''', (run_id,))
                leases = cursor.fetchall()
                for lease in leases:
                    lease['released_at'] = lease['released_at'].isoformat() if lease['released_at'] else None
                cursor.execute('SELECT action,outcome,COUNT(*) AS count FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s GROUP BY action,outcome ORDER BY action,outcome', (run_id,))
                receipts = cursor.fetchall()
                return {"run_id": run_id, **run, "steps": steps, 'leases':leases, 'write_receipts':receipts}
        time.sleep(0.1)
    raise RuntimeError("real M03 Runner did not reach a terminal result")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="Pre-freeze execution; never declares formal maintenance PASS")
    parser.add_argument("--host-freeze", type=Path)
    options = parser.parse_args()
    if os.environ.get('AGENT_DB_NAME') != DATABASE:
        parser.error('M03 requires its exact v32_m03_test database')
    if not options.smoke and options.host_freeze is None:
        parser.error('Formal M03 requires --host-freeze; use --smoke for preparation')
    if options.prepare:
        prepare_database()
    before_host = verify_host(options.host_freeze) if options.host_freeze else {'status':'NOT_FROZEN_PREPARATION'}
    before_process = process_identity()
    report = {'status':'RUNNING', 'host_before':before_host, 'phases':[], 'unrelated_runs':[]}
    output = RUNTIME / ('preparation.json' if options.smoke else 'maintenance-evidence.json')
    try:
        with composed() as (management, runner, supplier, artifacts):
            instance = setup_instance(management, artifacts['baseline'])
            unrelated_id = install_unrelated(management, actor=ACTOR, plugin_id='v32_m03_unrelated', name='M03 无关计算任务')
            stop = threading.Event()
            def keep_running():
                while not stop.is_set():
                    started = time.monotonic()
                    try:
                        receipt = management.policy.invoke_console(unrelated_id, request_id=str(uuid4()), actor=ACTOR)
                        completed = wait_result(receipt.run_id)
                        report['unrelated_runs'].append({'started':started, 'finished':time.monotonic(), **completed})
                        if completed['status'] != 'COMPLETED':
                            return
                    except Exception as exc:
                        report['unrelated_runs'].append({'status':'FAIL','error':type(exc).__name__+': '+str(exc)})
                        return
                    stop.wait(0.1)
            background = threading.Thread(target=keep_running, daemon=True)
            background.start()
            try:
                with ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                        signing_secret=management.signing_secret, runtime_root=management.task_env/'console') as console:
                    startup = {'process':before_process,'agent_http_startup_id':management.startup_id,'console_http_startup_id':console.startup_id,
                        'topology':'Agent HTTP, Console HTTP and Runner threads in one unchanged Python host process; actual isolated plugin subprocesses'}
                    report['startup_before'] = startup
                    with ProblemBrowser(console) as browser:
                        for label, codes in [('baseline',['R_M03_STANDARD']),('candidate',['R_M03_STANDARD','R_M03_RESERVED']),('rollback',['R_M03_STANDARD'])]:
                            phase = {'label':label,'started':time.monotonic()}
                            if label != 'baseline':
                                phase['upgrade'] = browser.upgrade(instance, artifacts['candidate' if label == 'candidate' else 'baseline'])
                            preview = browser.preview(instance, expected_codes=codes)
                            result = wait_result(browser.confirm(instance))
                            if result['status'] != 'COMPLETED' or any(step['postcondition_status'] != 'VERIFIED' for step in result['steps']):
                                raise AssertionError(f'actual decision execution failed: {result}')
                            actual_codes = [row['bill_code'] for step in result['steps'] for row in json.loads(step['result_summary_json'])['data']['results']]
                            if sorted(actual_codes) != sorted(codes):
                                raise AssertionError('actual verified business results differ from selected candidate DOM')
                            expected_version = artifacts['candidate' if label=='candidate' else 'baseline']['version']
                            if not result['leases'] or any(lease['plugin_version']!=expected_version for lease in result['leases']):
                                raise AssertionError('actual execution lease did not use the expected plugin version')
                            phase.update(preview=preview, result=result, ledger=supplier.persisted_problems(), finished=time.monotonic())
                            report['phases'].append(phase)
                        if browser.errors:
                            raise AssertionError('Actual browser JavaScript errors: '+str(browser.errors))
                        report['startup_after'] = {'process':process_identity(),'agent_http_startup_id':management.startup_id,
                            'console_http_startup_id':console.startup_id,'topology':startup['topology']}
                        assert report['startup_before'] == report['startup_after']
            finally:
                stop.set()
                background.join(timeout=65)
            if background.is_alive() or len(report['unrelated_runs']) < 2 or any(run['status'] != 'COMPLETED' for run in report['unrelated_runs']):
                raise AssertionError('Unrelated actual task did not remain available')
            report.update(runtime=runner.snapshot(), external_requests=supplier.requests,
                artifacts={label:{key:value for key,value in artifact.items() if key!='bytes'} for label,artifact in artifacts.items()},
                bootstrap=management.bootstrap_result)
        report['host_after'] = verify_host(options.host_freeze) if options.host_freeze else {'status':'NOT_FROZEN_PREPARATION'}
        report['status'] = 'NOT_FROZEN_PREPARATION' if options.smoke else 'PASS'
        return 0
    except Exception as exc:
        report.update(status='FAIL', error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'status':report['status'],'phases':len(report['phases']), 'unrelated_runs':len(report['unrelated_runs']), 'output':str(output)}))


if __name__ == "__main__":
    raise SystemExit(main())
