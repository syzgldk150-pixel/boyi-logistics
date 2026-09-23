"""Actual installed V2 collectors retain business history across producer changes."""
from pathlib import Path
import secrets
from uuid import uuid4

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.customer_connectors_v2 import build_customer_connectors
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from shared.customer_service_repository import CustomerServiceRepository
from shared.data_sources import DataSourceError, DataSourceRepository
from tests.direct_invocation_fixture import DirectFixture, direct_repository  # noqa: F401
from tests.v32_acceptance.customer_collection_flow import Accounts, CustomerSource, ACCOUNTS, configured_instance
from tests.v32_acceptance.finance_maintenance import ACTOR
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.service_v2_artifacts import build_artifact, install_artifact


@pytest.fixture(scope='module')
def source_runtime(direct_repository):  # noqa: F811
    root = Path(__file__).resolve().parents[1] / '.task_tmp/phase1/source-switch' / uuid4().hex
    artifact = build_artifact('sync_customer_service_problems_v2', root / 'artifacts')
    accounts = Accounts()
    with CustomerSource() as supplier:
        handlers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
            describe_account=accounts.require_active_binding_descriptor, customer_action=supplier.read),
            cursor_secret=secrets.token_bytes(32))
        with ManagementFixture(connection_factory=direct_repository._connection_factory, runtime_root=root / 'host',
                account_manager=accounts, broker_handlers=handlers, enable_directory_faults=False,
                connector_registry=ConnectorRegistry(build_customer_connectors(handlers))) as host:
            identities = []
            for index in range(2):
                identity = install_artifact(host, artifact, actor=ACTOR, name='隔离来源接续 ' + str(index), module='customer_service')
                configured_instance(host, automation_id=identity, account=ACCOUNTS[0])
                host.targets.reconcile_project(identity)
                identities.append(identity)
            with DirectFixture(host) as runtime:
                receipt = host.policy.invoke_console(identities[0], request_id=str(uuid4()), actor=ACTOR)
                assert runtime.service.wait_sync(receipt['invocation_id'])['status'] == 'COMPLETED'
                with direct_repository._connection_factory() as connection:
                    rows = DataSourceRepository(connection).list_sources('customer_service')
                    assert len(rows) == 1
                    source_id = rows[0]['source_id']
                    with connection.cursor() as cursor:
                        cursor.execute("INSERT INTO customer_problem_manual_fields(source_id,external_id,source_direction,note,revision,updated_at) VALUES(%s,'CS-FLOW-GUID','received','保留人工备注',1,UTC_TIMESTAMP(6))", (source_id,))
                    connection.commit()
                yield host, runtime, supplier, identities, source_id


@pytest.mark.parametrize('round_index', range(20))
def test_actual_v2_producer_switch_retains_identity_and_rejects_late_publication(source_runtime, monkeypatch, round_index, record_property):
    host, runtime, supplier, identities, source_id = source_runtime
    with host.repository._connection_factory() as connection:
        source = DataSourceRepository(connection).get(source_id)
        before = CustomerServiceRepository(connection).query(source_ids=[source_id])
    old = source['producer_instance_id']
    target = next(identity for identity in identities if identity != old)
    captured = []
    publish = runtime.service._publish_result
    def observed(*args):
        captured.append(args)
        return publish(*args)
    monkeypatch.setattr(runtime.service, '_publish_result', observed)
    receipt = host.policy.invoke_console(old, request_id=str(uuid4()), actor=ACTOR)
    assert runtime.service.wait_sync(receipt['invocation_id'])['status'] == 'COMPLETED'
    assert len(captured) == 1
    entry = host.catalog.require(old)
    host.management.set_enabled(old, enabled=False, request_id=str(uuid4()), expected_record_version=entry.record_version, actor=ACTOR)
    entry = host.catalog.require(target)
    if not entry.enabled:
        host.management.set_enabled(target, enabled=True, request_id=str(uuid4()), expected_record_version=entry.record_version, actor=ACTOR)
    host.targets.reconcile_project(target)
    request = str(uuid4())
    switched = host.packages.continue_data_source(source_id, producer_instance_id=target,
        expected_revision=source['revision'], expected_producer_instance_id=old, request_id=request)
    assert switched['source_id'] == source_id and switched['producer_instance_id'] == target
    assert host.packages.continue_data_source(source_id, producer_instance_id=target,
        expected_revision=source['revision'], expected_producer_instance_id=old, request_id=request)['revision'] == switched['revision']
    with pytest.raises(DataSourceError, match='NO_LONGER_ACTIVE'):
        publish(*captured[0])
    with host.repository._connection_factory() as connection:
        with pytest.raises(DataSourceError, match='STALE'):
            DataSourceRepository(connection).assert_producer(source_id, producer_instance_id=old,
                producer_generation=source['producer_generation'], revision=source['revision'])
    fresh = host.policy.invoke_console(target, request_id=str(uuid4()), actor=ACTOR)
    result = runtime.service.wait_sync(fresh['invocation_id'])
    assert result['status'] == 'COMPLETED', result
    with host.repository._connection_factory() as connection:
        after = CustomerServiceRepository(connection).query(source_ids=[source_id])
        current = DataSourceRepository(connection).get(source_id)
    keys = ('source_id', 'external_id', 'source_direction', 'waybill_no', 'note')
    assert [[row[key] for key in keys] for row in before['rows']] == [[row[key] for key in keys] for row in after['rows']]
    assert current['producer_instance_id'] == target and current['source_id'] == source_id
    assert after['stats']['row_count'] == 1 and after['rows'][0]['note'] == '保留人工备注'
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)
    record_property('source_id', source_id)
    record_property('actual_invocation', result['invocation_id'])
