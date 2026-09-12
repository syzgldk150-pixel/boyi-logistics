"""Disabled migration schedules are validated, never exposed as runnable entries."""
import copy
from types import SimpleNamespace

import pytest

from shared.automation_project_authorization import (
    AutomationProjectContractError, AutomationProjectInstanceDefinition,
    compile_automation_project_contract,
)
from tests.test_automation_project_authorization import _service_v2_fragment


@pytest.fixture
def disabled_schedule():
    definition = AutomationProjectInstanceDefinition(automation_id='migration-target', plugin_id='test_plugin',
        tool_name='service_projection_read', argument_templates={'run_now':{}}, dynamic_argument_resolvers={},
        account_bindings={}, allowed_entrypoints=frozenset({'run_now'}), project_config={}, resource_bindings={})
    fragment = _service_v2_fragment(definition)
    fragment['allowed_entrypoints'].append('daily_run')
    fragment['entrypoint_kinds']['daily_run'] = 'scheduler'
    fragment['invocation_contracts']['daily_run'] = {**copy.deepcopy(fragment['invocation_contracts']['run_now']), 'contribution_kind':'scheduler'}
    row = {'id':'service-v2:saved-disabled', 'automation_id':definition.automation_id,
        'tool_name':fragment['action_id'], 'automation_generation':1, 'tool_params':{},
        'configuration_version':1, 'enabled':False, 'cron_expression':'55 23 * * *'}
    return definition, fragment, row


def compile_saved(values):
    definition, fragment, row = values
    return compile_automation_project_contract(definition, catalog=SimpleNamespace(),
        scheduled_rows=[row], plugin_contract_provider=lambda _:fragment)


def test_disabled_physical_schedule_keeps_manual_call_available(disabled_schedule):
    contract = compile_saved(disabled_schedule)
    assert set(contract.invocation_contracts) == {'run_now'}
    assert contract.allowed_entrypoints == frozenset({'console'})
    assert contract.snapshot['scheduled_configurations'][0]['enabled'] is False


@pytest.mark.parametrize('field,value,code', [
    ('enabled',True,'PROJECT_SCHEDULE_CONTRIBUTION_AMBIGUOUS'),
    ('automation_id','another','PROJECT_SCHEDULE_IDENTITY_MISMATCH'),
    ('automation_generation',2,'PROJECT_SCHEDULE_GENERATION_MISMATCH'),
    ('tool_params',{'invented':True},'PROJECT_SCHEDULE_ARGUMENTS_INVALID'),
])
def test_inactive_schedule_does_not_bypass_existing_checks(disabled_schedule, field, value, code):
    disabled_schedule[2][field] = value
    with pytest.raises(AutomationProjectContractError) as denied:
        compile_saved(disabled_schedule)
    assert denied.value.code == code


@pytest.mark.parametrize("required", [False, True])
def test_disabled_placeholder_does_not_require_executable_arguments(disabled_schedule, required):
    definition, fragment, row = disabled_schedule
    scheduled = fragment['invocation_contracts']['daily_run']
    scheduled['argument_template'] = {'mode': {'source': 'literal', 'value': 'incremental'}}
    scheduled['input_schema'] = {'type':'object', 'additionalProperties':False,
        'properties':{'mode':{'type':'string','enum':['incremental']}},
        'required':['mode'] if required else []}
    contract = compile_saved(disabled_schedule)
    assert set(contract.invocation_contracts) == {'run_now'}
    assert row['tool_params'] == {}
    assert contract.snapshot['scheduled_configurations'][0]['enabled'] is False

    # Enabling the contribution requires compilation of the declared arguments;
    # the persisted placeholder cannot become a runnable scheduler request.
    fragment['enabled_entrypoints'].append('daily_run')
    from dataclasses import replace
    enabled_definition = replace(definition,
        allowed_entrypoints=frozenset({'run_now','daily_run'}),
        argument_templates={'run_now':{},'daily_run':{'mode':'incremental'}})
    with pytest.raises(AutomationProjectContractError) as denied:
        compile_saved((enabled_definition, fragment, row))
    assert denied.value.code in {'PROJECT_SCHEDULE_ARGUMENTS_INVALID','PROJECT_SCHEDULE_ARGUMENTS_STALE'}
    row['tool_params'] = {'mode':'incremental'}
    active = compile_saved((enabled_definition, fragment, row))
    assert 'scheduler:'+row['id'] in active.invocation_contracts
