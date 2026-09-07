"""Run describe/test/package for all six current first-party plugin scopes."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from tests.v32_acceptance.host_freeze import verify_host

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ('sync_scan_codes', 'sync_arrival_stats', 'self_pickup_problem_upload',
    'split_pending_problem_upload', 'sync_finance_bills', 'sync_customer_service_problems')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-freeze', type=Path)
    parser.add_argument('--report', type=Path, help='additional new path for the complete actual summary')
    args = parser.parse_args()
    if args.report and (args.report.exists() or args.report.is_symlink() or not args.report.parent.is_dir()):
        raise ValueError('additional report must be a new path in an existing directory')
    if os.environ.get('AGENT_DB_NAME') != 'v32_cli_test' or os.environ.get('PYTHON_DOTENV_DISABLED') != '1':
        raise RuntimeError('CLI batch requires v32_cli_test and disabled dotenv')
    output = ROOT / '.task_tmp/v32/plugin-cli' / ('run-' + uuid4().hex)
    output.mkdir(parents=True)
    report = {'status': 'RUNNING', 'plugins': {}, 'output': str(output),
        'host_freeze': verify_host(args.host_freeze) if args.host_freeze else {'status': 'NOT_REQUESTED'}}
    cli = [sys.executable, str(ROOT / 'agent/scripts/plugin_maintenance.py')]
    stamp = int(time.time())
    try:
        for index, plugin_id in enumerate(PLUGINS):
            entry = {'commands': [], 'status': 'RUNNING'}
            report['plugins'][plugin_id] = entry
            for action in ('describe', 'test', 'package'):
                command = [*cli, action, plugin_id]
                if args.host_freeze:
                    command += ['--base-ref', report['host_freeze']['host_base_sha']]
                result_path = output / f'{plugin_id}-{action}.json'
                if action != 'describe':
                    command += ['--report', str(result_path)]
                if action == 'package':
                    command += ['--test-signing', '--version', f'98.6.{stamp + index}',
                        '--output', str(output / (plugin_id + '.zip'))]
                log = output / f'{plugin_id}-{action}.log'
                with log.open('x', encoding='utf-8') as stream:
                    completed = subprocess.run(command, cwd=ROOT, env=dict(os.environ),
                        stdout=stream, stderr=subprocess.STDOUT, timeout=600, check=False)
                entry['commands'].append({'action': action, 'command': command, 'exit_code': completed.returncode, 'log': str(log)})
                if completed.returncode != 0:
                    entry['status'] = 'FAIL'
                    break
                if action == 'describe':
                    entry['scope'] = json.loads(log.read_text(encoding='utf-8'))
                else:
                    result = json.loads(result_path.read_text(encoding='utf-8'))
                    if result['tests']['status'] != 'PASS':
                        raise AssertionError('CLI succeeded without successful actual local tests')
                    entry[action] = result
                    if action == 'package':
                        digest = sha256((output / (plugin_id + '.zip')).read_bytes()).hexdigest()
                        if digest != result['artifact']['package_sha256']:
                            raise AssertionError('actual package bytes disagree with packaging report')
            if entry['status'] == 'RUNNING':
                entry['status'] = 'PASS'
            print(json.dumps({'plugin_id': plugin_id, 'status': entry['status']}), flush=True)
        if args.host_freeze:
            report['host_after'] = verify_host(args.host_freeze)
        report['status'] = 'PASS' if all(entry['status'] == 'PASS' for entry in report['plugins'].values()) else 'FAIL'
        return 0 if report['status'] == 'PASS' else 1
    except Exception as exc:
        report.update(status='FAIL', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        report_path = output / 'summary.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if args.report:
            with args.report.open('x', encoding='utf-8') as stream:
                stream.write(report_path.read_text(encoding='utf-8'))
        (output.parent / 'latest.json').write_text(json.dumps({'report': str(report_path), 'status': report['status']}) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'report': str(report_path)}), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
