"""Customer sources selected exclusively from the Host's bound account pool."""
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import BOOL, COUNT, CURSOR, TEXT, array_schema as arr, object_schema as obj, source_record_schema
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


def build_customer_connectors(reviewed):
    raw = source_record_schema(depth=3)
    list_input = obj({'direction':TEXT,'cursor':CURSOR,'page_size':COUNT})
    list_output = obj({'items':arr(raw),'source_context':raw,'next_cursor':CURSOR,'pagination_complete':BOOL})
    detail_input = obj({'dedupe_key':TEXT,'platform':TEXT,'source_direction':TEXT,'external_id':TEXT,'waybill_no':TEXT},
                       ['dedupe_key','platform','source_direction','external_id'])
    detail_output = obj({'ok':BOOL,'details':{'oneOf':[arr(raw),raw]},'dedupe_key':TEXT,'platform':TEXT,'source_direction':TEXT,'external_id':TEXT,
                         'stats':raw,'item':raw,'rows':arr(raw),'source_site_code':TEXT,'action':TEXT},
                        ['ok','dedupe_key','platform','source_direction','external_id'])
    specs = (('list_page','customer_problem.list_page',list_input,list_output),
             ('detail','customer_problem.detail',detail_input,detail_output))
    operations = tuple(ConnectorOperation(name=name,effect=Effect.READ,input_schema=inputs,output_schema=outputs,
        handler=reviewed_connector_handler(reviewed,tool='sync_customer_service_problems',operation='browser.invoke',
            action=action,role='customer_service_source',account_collection_role='customer_service_source'),
        max_input_bytes=1024*1024,max_output_bytes=10*1024*1024)
        for name,action,inputs,outputs in specs)
    return (ConnectorDescriptor(service='connector.boyi.customer_sources@1',title='客服平台来源',binding_kind=Kind.HOST_INTERNAL,
        account_role=None,allowed_systems=(),operations=operations),)
