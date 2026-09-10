from pathlib import Path
import ast

import pytest

from agent.tms_runtime.account_change_guard import compose_account_change_guards


@pytest.mark.parametrize('failure', [None, 'direct', 'durable'])
def test_both_owners_held_through_mutation_and_failed_admission_releases_first(failure):
    events, active = [], set()
    def guard(name):
        def begin(account):
            assert account == 'synthetic-account'
            events.append(('begin', name))
            if name == failure:
                raise RuntimeError(name)
            active.add(name)
            def release():
                events.append(('release', name))
                active.remove(name)
            return release
        return begin
    begin = compose_account_change_guards(guard('direct'), guard('durable'))
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            begin('synthetic-account')
        assert not active
        assert events == ([('begin', 'direct')] if failure == 'direct' else
            [('begin', 'direct'), ('begin', 'durable'), ('release', 'direct')])
    else:
        release = begin('synthetic-account')
        assert active == {'direct', 'durable'}
        release()
        release()
        assert not active
        assert events[-2:] == [('release', 'durable'), ('release', 'direct')]


def test_other_guard_releases_even_when_one_release_raises():
    events = []
    def durable(_account):
        def release():
            events.append('durable')
            raise RuntimeError('durable release failure')
        return release
    release = compose_account_change_guards(lambda _: lambda: events.append('direct'), durable)('synthetic')
    with pytest.raises(RuntimeError, match='durable release failure'):
        release()
    assert events == ['durable', 'direct']


def test_main_sets_the_combined_guard_once():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'agent/main.py').read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'set_credentials_change_guard'
        and not (isinstance(node.args[0], ast.Constant) and node.args[0].value is None)]
    assert len(calls) == 1
    composition = calls[0].args[0]
    assert isinstance(composition, ast.Call) and composition.func.id == 'compose_account_change_guards'
    assert [ast.unparse(arg) for arg in composition.args] == [
        'direct_invocations.begin_credentials_change', 'schedule_policy_service.begin_credentials_change']
