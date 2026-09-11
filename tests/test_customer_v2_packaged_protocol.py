from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.customer_connectors_v2 import build_customer_connectors
from agent.automation_plugins.first_party_handler_common import customer_problem_identity
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from tests.service_v2_production_protocol_support import PackagedConnectorHost


def test_customer_zip_queries_all_bound_accounts_and_rechecks_exact_old_identity(tmp_path):
    calls=[]
    accounts={'isolated-rh':'ronghui','isolated-yd':'yunda'}
    def describe(account):
        assert account in accounts
        return {'account_id':account,'system':accounts[account],'session_profile':'isolated'}
    def platform(arguments):
        calls.append(arguments)
        if arguments['action']=='detail':
            return {'ok':True,'platform':arguments['platform'],'action':'detail','account_id':arguments['account_id'],
                    'details':[{'status':'已关闭'}]}
        external=arguments['platform']+'-'+arguments['direction']
        return {'ok':True,'source_site_code':'isolated-site','rows':[{'platform':arguments['platform'],
            'source_direction':arguments['direction'],'external_id':external,'waybill_no':'R-'+external,'status':'待处理'}],
            'stats':{'returned':1,'total':1,'total_authoritative':True}}
    reviewed=build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(describe_account=describe,customer_action=platform))
    registry=ConnectorRegistry(build_customer_connectors(reviewed))
    context=CoreBrokerInvocationContext(automation_id='isolated-customer',plugin_version='2.0.0',tool_name='sync_customer_service_problems_v2',
        operation='service.invoke',action='run',role='__system__',account_bindings={'customer_service_source':tuple(accounts)})
    host=PackagedConnectorHost(tmp_path,'sync_customer_service_problems_v2',registry,context)
    assert host.contract.account_roles[0]['collection'] is True
    key=customer_problem_identity(account_id='isolated-rh',platform='ronghui',external_id='previous-issue',source_direction='received')
    result=host.execute({'direction':'both','recheck_items':[{'dedupe_key':key,'platform':'ronghui','external_id':'previous-issue','source_direction':'received'}]},operation='run')
    assert result['status']=='SUCCESS',result
    assert {call['account_id'] for call in calls}==set(accounts)
    assert len(result['data']['records'])==4
    assert result['data']['rechecks'][0]['status']=='RESOLVED'
    assert result['data']['rechecks'][0]['dedupe_key']==key
    assert len(calls)==5 and not host.receipts
    assert all(account not in str(result) for account in accounts)
