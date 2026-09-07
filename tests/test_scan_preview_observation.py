"""Diagnostic helpers retain validation failures and never publish a PASS."""
from datetime import datetime, timezone
import json

import pytest

from agent.orchestration import scan_preview_binding
from agent.orchestration.models import OrchestrationError
from tests.v32_acceptance import scan_preview_observation as observation


@pytest.mark.parametrize('field', ['result_summary_json', 'result_summary'])
@pytest.mark.parametrize('encoded', [False, True])
def test_timestamp_observer_preserves_exact_validation_exception(monkeypatch, tmp_path, field, encoded):
    error = OrchestrationError('SCAN_PREVIEW_INVALID', 'The scan preview observation time is in the future')
    supplied = {'preview_run_id': 'synthetic-preview', 'now': datetime.now(timezone.utc)}
    calls = []

    def validator(_uow, **arguments):
        calls.append(arguments)
        raise error

    class Steps:
        def list_for_run(self, run_id):
            assert run_id == supplied['preview_run_id']
            result = {'data': {'preview_evidence': {'observed_at': '2026-09-07T18:00:00.123456+08:00'}}}
            return [{field: json.dumps(result) if encoded else result}]

    class Uow:
        steps = Steps()

    monkeypatch.setattr(scan_preview_binding, '_load_persisted_scan_preview', validator)
    output = tmp_path / 'timing.json'
    with observation.observe_preview_timestamps(output), pytest.raises(OrchestrationError) as caught:
        scan_preview_binding._load_persisted_scan_preview(Uow(), **supplied)
    assert caught.value is error and calls == [supplied]
    report = json.loads(output.read_text())
    assert report['status'] == 'FAIL'
    assert report['persisted_times'][0]['observed_at'] == '2026-09-07T18:00:00.123456+08:00'
    assert report['validation_now'] == str(supplied['now'])


def test_partial_checkpoint_cannot_pass_or_hide_failed_case(tmp_path):
    path = tmp_path / 'progress.json'
    completed = []
    observation.checkpoint_case(lambda: {'actual': 'completed'}, completed=completed, path=path)
    assert json.loads(path.read_text())['status'] == 'IN_PROGRESS'
    error = AssertionError('actual next case failed')

    def fail(*, mode):
        assert mode == 'UNKNOWN'
        raise error

    with pytest.raises(AssertionError) as caught:
        observation.checkpoint_case(fail, mode='UNKNOWN', completed=completed, path=path)
    assert caught.value is error
    report = json.loads(path.read_text())
    assert report['status'] == 'FAIL' and len(report['completed_cases']) == 1
    assert report['failed_case'] == {'function': 'fail', 'mode': 'UNKNOWN'}


def test_checkpoint_storage_failure_still_fails(monkeypatch, tmp_path):
    def no_write(*_args):
        raise OSError('isolated checkpoint storage failure')

    monkeypatch.setattr(observation, '_write', no_write)
    with pytest.raises(OSError):
        observation.checkpoint_case(lambda: {}, completed=[], path=tmp_path / 'progress.json')
    error = AssertionError('original failure')

    def fail():
        raise error

    with pytest.raises(AssertionError) as caught:
        observation.checkpoint_case(fail, completed=[], path=tmp_path / 'progress.json')
    assert caught.value is error
