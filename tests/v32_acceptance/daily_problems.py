"""A01 actual Console self/split entries through signed plugins and MySQL."""
from __future__ import annotations
import json
import os
from pathlib import Path
from uuid import uuid4

from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.decision_maintenance import ACTOR, composed, connect, prepare_database, setup_instance, wait_result
from tests.v32_acceptance.problem_browser import ProblemBrowser
from tests.v32_acceptance.problem_fixture import ACCOUNTS, SPLIT_SOURCE, SPLIT_TARGET

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT/'.task_tmp/v32/environment/daily-problems.json'


def setup_split(management):
    automation_id = 'split_pending_problem_upload'
    result = management.targets.reconcile_project(automation_id)
    if result.committed_generation is None:
        raise AssertionError('real split initial runtime unavailable: '+str(result))
    entry = management.catalog.require(automation_id)
    management.management.save_plugin_settings(automation_id, config={}, account_bindings={'account_id':[ACCOUNTS['account_id']]},
        resource_bindings={'split_pending_source_sheet':SPLIT_SOURCE,'split_pending_target_sheet':SPLIT_TARGET},
        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    management.targets.reconcile_project(automation_id)
    policy = management.policy.get_policy_projection(automation_id)
    management.policy.update_policy(automation_id, mode='PROJECT_FULL_AUTO', request_id=str(uuid4()),
        comment='Explicit isolated daily split synthetic write authorization', expected_policy_version=policy['policy_version'],
        expected_project_configuration_version=policy['project_configuration_version'], actor=ACTOR)
    return automation_id


def main():
    if os.environ.get('AGENT_DB_NAME') != 'v32_a01_problem_test':
        raise RuntimeError('A01 problems requires its dedicated v32_a01_problem_test database')
    prepare_database()
    report = {'status':'RUNNING','cases':{}}
    try:
        with composed() as (management, runner, supplier, artifacts):
            self_id = setup_instance(management, artifacts['baseline'])
            split_id = setup_split(management)
            with ConsoleFixture(agent_base_url=management.url,internal_token=management.internal_token,
                    signing_secret=management.signing_secret,runtime_root=management.task_env/'daily-console') as console:
                with ProblemBrowser(console) as browser:
                    for automation_id, expected in [(self_id,['R_M03_STANDARD']),(split_id,['SYNTHETIC-SPLIT','SYNTHETIC-NOT-ARRIVED'])]:
                        preview = browser.preview(automation_id,expected_codes=expected)
                        result = wait_result(browser.confirm(automation_id))
                        if result['status'] != 'COMPLETED' or any(step['postcondition_status']!='VERIFIED' for step in result['steps']):
                            raise AssertionError('actual daily problem execution failed: '+str(result))
                        report['cases'][automation_id] = {'status':'PASS','preview':preview,'result':result}
                    if browser.errors:
                        raise AssertionError('actual Console script failure: '+str(browser.errors))
            with connect() as connection,connection.cursor() as cursor:
                cursor.execute('SELECT tracking_number,problem_type,expected_quantity,arrived_quantity,pending_quantity,upload_status,complaint_status FROM split_pending_problem_items ORDER BY tracking_number')
                rows = cursor.fetchall()
            if len(rows)!=2 or any(row['upload_status']!='success' for row in rows):
                raise AssertionError('real split business projection is incorrect: '+str(rows))
            report.update(runtime=runner.snapshot(), external_requests=supplier.requests,
                external_problems=supplier.persisted_problems(), split_business_rows=rows,
                bootstrap=management.bootstrap_result)
        report['status']='PASS'
        return 0
    except Exception as exc:
        report.update(status='FAIL',error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'status':report['status'],'cases':list(report['cases']),'output':str(OUTPUT)}))


if __name__=='__main__':
    raise SystemExit(main())
