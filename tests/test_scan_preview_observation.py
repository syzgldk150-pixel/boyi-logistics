"""Diagnostic helpers retain validation failures and never publish a PASS."""
import json

import pytest

from tests.v32_acceptance import scan_preview_observation as observation


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
