from decimal import Decimal
from copy import deepcopy

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.finance_connectors_v2 import build_finance_connectors
from plugin_core_adapters.finance import build_production_finance_handler_map
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_finance_core_adapter import ACCOUNTS, ROLES, _AccountManager, _Repository, _capture


def _host(tmp_path, *, drift=False):
    repository = _Repository()
    captures = []

    def capture(descriptor, target_date):
        captures.append((descriptor['account_id'], target_date))
        result = _capture(descriptor, target_date)
        if drift and len(captures) == 2:
            result.transactions[0]['new_amount'] = '79.7500'
        return result

    reviewed = build_production_finance_handler_map(
        cursor_secret=b'f' * 32, account_manager=_AccountManager(),
        repository_factory=lambda: repository, capture_port=capture,
        capability_authorizer=lambda *_: None,
    )
    registry = ConnectorRegistry(build_finance_connectors(reviewed))
    context = CoreBrokerInvocationContext(
        automation_id='isolated-finance', plugin_version='2.0.0', tool_name='sync_finance_bills_v2',
        operation='service.invoke', action='run', role='__system__',
        account_bindings={role: (account,) for role, account in ACCOUNTS.items()},
    )
    return PackagedConnectorHost(tmp_path, 'sync_finance_bills_v2', registry, context), repository, captures


@pytest.mark.parametrize('drift', [False, True])
def test_finance_zip_captures_three_bound_accounts_and_independently_verifies_ledger(tmp_path, drift):
    host, repository, captures = _host(tmp_path, drift=drift)
    result = host.execute({'mode': 'sync', 'target_date': '2026-07-11', 'rescan_days': 1}, operation='run')
    assert result['status'] == ('FAILED' if drift else 'SUCCESS'), result
    assert len(captures) == 6, result
    assert {account for account, _ in captures} == set(ACCOUNTS.values())
    assert len(repository.runs) == 3, result
    statuses = {run['account_id']: run['status'] for run in repository.runs.values()}
    assert statuses[ACCOUNTS[ROLES[0]]] == ('failed' if drift else 'success')
    assert statuses[ACCOUNTS[ROLES[1]]] == statuses[ACCOUNTS[ROLES[2]]] == 'success'
    transactions = [item for run in repository.runs.values() for item in run.get('transactions', ())]
    assert sum((row.expense for row in transactions), Decimal('0')) == (Decimal('2.50') if drift else Decimal('3.75'))
    assert result['data']['written_transactions'] == len(transactions)
    if drift:
        assert result['error']['code'] == 'FINANCE_SYNC_PARTIAL_FAILED'
        assert result['data']['runs'][0]['failure_code']
        assert host.verified_outcome.accepted is False
        assert host.verification_settlements[-1]['outcome'].value == 'WRITE_VERIFIED'
        from agent.automation_plugins.finance_failure_proof import is_verified_finance_failure
        proof = dict(plugin_id=host.manifest.plugin_id, result=result,
            started_mutating_call_count=len(host.receipts), host_call_observations=host.observations)
        assert is_verified_finance_failure(**proof)
        for field, value in [('service', 'connector.other@1'), ('effect', 'read')]:
            corrupted = deepcopy(host.observations)
            corrupted[-1]['service_target'][field] = value
            assert not is_verified_finance_failure(**{**proof, 'host_call_observations': corrupted})
    else:
        assert result['meta']['write_outcome'] == 'WRITE_VERIFIED'
    assert host.receipts
