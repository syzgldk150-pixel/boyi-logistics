"""Production clock manifests must register read tools without granting writes."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer
from agent.automation_plugins.errors import PluginConflictError, PluginExecutionError
from agent.automation_plugins.harness_permissions import project_read_runtime_permissions
from agent.automation_plugins.host_capability_registry import governance_for_effect
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.production import ProductionRuntimeEffectDriver
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.automation_plugins.service_v2_projection import ManagedContributionRegistry
from agent.harness.catalog import HarnessToolCatalog
from tests.test_service_v2_harness_contribution_contract import _harness_material, _runtime_snapshot, _sha


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=['clockin_daxiang_v2', 'clockin_daxiang_s_v2'])
def clock_contract(request):
    source = ROOT/'agent/service_v2_plugins'/request.param/'manifest.json'
    manifest = AutomationPluginManifestV2.from_mapping(json.loads(source.read_text(encoding='utf-8')))
    return manifest, ServiceV2ProjectContract.from_manifest(manifest)


def test_actual_clock_grants_allow_precheck_and_deny_submit(clock_contract, tmp_path):
    _, contract = clock_contract
    raw = copy.deepcopy(dict(contract.runtime_permissions))
    read = project_read_runtime_permissions(raw)
    assert raw == contract.runtime_permissions
    assert read['browser'] is True
    assert [op['action'] for op in read['broker_operations']] == ['ronghui.clock.precheck', 'ronghui.clock.verify']
    issuer = LocalBrokerCapabilityIssuer(tmp_path/'broker.sock')
    token = issuer.issue(automation_id='isolated-clock', plugin_version='1.2.2', tool_name='clock',
        ttl_seconds=30, runtime_permissions=read, account_roles=contract.account_roles,
        resource_roles=(), account_bindings={'operator': 'isolated-clock-account'}, resource_bindings={})
    _, binding = issuer.consume(token, request_id=str(uuid4()), operation='browser.session',
        action='ronghui.clock.precheck', role='operator', arguments={})
    assert binding == 'isolated-clock-account'
    with pytest.raises(PluginExecutionError) as denied:
        issuer.consume(token, request_id=str(uuid4()), operation='browser.session',
            action='ronghui.clock.submit', role='operator', arguments={})
    assert denied.value.code == 'BROKER_OPERATION_DENIED'


def test_clock_harness_can_register_restore_upgrade_and_retire(clock_contract):
    manifest, contract = clock_contract
    base = _runtime_snapshot()
    metadata = copy.deepcopy(dict(base.execution_metadata))
    metadata['runtime_descriptor']['runtime_permissions'] = dict(contract.runtime_permissions)
    metadata['contributions']['harness'] = manifest.to_mapping()['contributes']['harness']
    metadata['service_contracts']['provides'] = manifest.to_mapping()['provides']
    snapshot = replace(base, plugin_id=manifest.plugin_id, plugin_version=manifest.version,
        enabled_entrypoints=('assistant_preview',), execution_metadata=metadata,
        runtime_descriptor_sha256=_sha(metadata['runtime_descriptor']))
    material = _harness_material(snapshot)
    assert ProductionRuntimeEffectDriver._validated_contribution_payload(material) == material
    registry = ManagedContributionRegistry()
    registry.prepare_generation((material,))
    refresh = lambda: {'initialized':True, 'invalid_tasks':[]}
    registry.apply_generation(snapshot.automation_id, 1, refresh=refresh)
    catalog = HarnessToolCatalog(snapshot_provider=registry, invocation_port=SimpleNamespace(invoke=lambda **_: None))
    assert len(catalog.public_tools()) == 1
    next_material = _harness_material(replace(snapshot, generation=2))
    registry.prepare_generation((next_material,))
    registry.apply_generation(snapshot.automation_id, 2, refresh=refresh)
    assert registry.active_snapshot()[0]['generation'] == 2
    registry.apply_generation(snapshot.automation_id, 3, refresh=refresh, expected_registration_ids=())
    registry.unregister(next_material['registration_id'])
    registry.unregister(material['registration_id'])
    assert registry.active_snapshot() == ()
    assert registry.snapshot() == ()


@pytest.mark.parametrize('change', ['effect', 'unknown_action', 'undeclared_browser', 'extra_flag'])
def test_clock_projection_rejects_tampered_signed_governance(clock_contract, change):
    _, contract = clock_contract
    raw = copy.deepcopy(dict(contract.runtime_permissions))
    if change == 'effect':
        operation = raw['broker_operations'][1]
        operation.update(effect='read', broker_effect='read', governance=governance_for_effect('read').to_mapping())
    elif change == 'unknown_action':
        raw['broker_operations'][0]['action'] = 'ronghui.clock.arbitrary'
    elif change == 'undeclared_browser':
        raw['browser'] = False
    else:
        raw['network'] = True
    with pytest.raises(PluginConflictError) as denied:
        project_read_runtime_permissions(raw)
    assert denied.value.code == 'CAPABILITY_UNAVAILABLE'
