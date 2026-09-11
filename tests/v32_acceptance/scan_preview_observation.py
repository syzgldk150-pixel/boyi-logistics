"""Transparent timing evidence; validation and its failures remain unchanged."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import traceback
from unittest.mock import patch

from shared.orchestration_repository_support import _decode_row


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


@contextmanager
def observe_preview_timestamps(path: Path):
    from agent.orchestration import direct_invocation_previews, scan_preview_binding

    original = scan_preview_binding._load_persisted_scan_preview
    direct_original = direct_invocation_previews._load

    def direct_observed(repository, invocation_id, **arguments):
        before = datetime.now(timezone.utc)
        try:
            return direct_original(repository, invocation_id, **arguments)
        except BaseException as error:
            after = datetime.now(timezone.utc)
            if getattr(error, 'code', None) == 'PREVIEW_INVALID':
                evidence = {'status': 'FAIL', 'error_code': error.code,
                    'preview_invocation_id': invocation_id, 'host_before': before, 'host_after': after}
                try:
                    row = repository.get(invocation_id) or {}
                    result = row.get('result_json') or {}
                    evidence['persisted_times'] = {'started_at': row.get('started_at'),
                        'finished_at': row.get('finished_at'), 'observed_at': (result.get('meta') or {}).get('observed_at')}
                    _write(path, evidence)
                except Exception as diagnostic_error:
                    evidence['diagnostic_error'] = type(diagnostic_error).__name__
                print(json.dumps({'scan_preview_timing': evidence}, ensure_ascii=False, default=str), flush=True)
            raise

    def observed(uow, **arguments):
        before = datetime.now(timezone.utc)
        try:
            return original(uow, **arguments)
        except BaseException as error:
            if getattr(error, 'code', None) == 'SCAN_PREVIEW_INVALID' and 'observation time is in the future' in str(error):
                evidence = {'status': 'FAIL', 'error_code': error.code,
                    'preview_run_id': arguments['preview_run_id'], 'validation_now': arguments['now'],
                    'host_before': before, 'host_after': datetime.now(timezone.utc),
                    'callers': [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                        for frame in traceback.extract_stack(limit=8)[:-1]]}
                try:
                    steps = uow.steps.list_for_run(arguments['preview_run_id'])
                    evidence['persisted_times'] = [{'step_finished_at': step.get('finished_at'),
                        'observed_at': scan_preview_binding._row_mapping(
                            _decode_row(dict(step), ('result_summary_json', 'result_summary')),
                            'result_summary_json', 'result_summary')['data']['preview_evidence']['observed_at']}
                        for step in steps]
                    _write(path, evidence)
                except Exception as diagnostic_error:
                    evidence['diagnostic_error'] = type(diagnostic_error).__name__
                print(json.dumps({'scan_preview_timing': evidence}, ensure_ascii=False, default=str), flush=True)
            raise

    with patch.object(scan_preview_binding, '_load_persisted_scan_preview', observed), patch.object(direct_invocation_previews, '_load', direct_observed):
        yield


def checkpoint_case(function, *arguments, completed, path: Path, **options):
    """Persist completed slices on failure; a checkpoint can never be PASS."""
    try:
        result = function(*arguments, **options)
    except BaseException as error:
        evidence = {'status': 'FAIL', 'completed_cases': completed,
            'failed_case': {'function': function.__name__, **options},
            'error_type': type(error).__name__, 'observed_at': datetime.now(timezone.utc)}
        try:
            _write(path, evidence)
        except Exception as diagnostic_error:
            evidence['checkpoint_error'] = type(diagnostic_error).__name__
        print(json.dumps({'scan_recovery_checkpoint': str(path), 'status': 'FAIL',
            'completed_case_count': len(completed), 'failed_case': evidence['failed_case'],
            'checkpoint_error': evidence.get('checkpoint_error')}, ensure_ascii=False), flush=True)
        raise
    completed.append(result)
    _write(path, {'status': 'IN_PROGRESS', 'completed_cases': completed,
        'observed_at': datetime.now(timezone.utc)})
