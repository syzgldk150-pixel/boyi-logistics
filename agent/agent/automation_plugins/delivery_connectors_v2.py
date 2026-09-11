"""Delivery queries and verified updates; classification stays in the plugin."""
from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import BOOL, COUNT, CURSOR, TEXT, array_schema as arr, object_schema as obj, source_record_schema
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


def build_delivery_connectors(reviewed):
    account = ConnectorRequirementContract(service='connector.boyi.delivery_source@1', account_role='delivery_source', allowed_systems=('ronghui',), required=True)
    resource = ConnectorRequirementContract(service='connector.boyi.delivery_status_bitable@1', binding_kind=Kind.RESOURCE, resource_role='delivery_status_bitable', allowed_resource_kinds=('feishu_bitable',), required=True)
    source_specs = (
        ('read_status', 'browser.invoke', 'ronghui.delivery_status.read', Effect.READ, obj({'bill_codes':arr(TEXT,maximum=200)}), obj({'items':arr(obj({'bill_code':TEXT,'status':TEXT}),maximum=200)})),
        ('update_projection', 'projection.invoke', 'waybill.delivery_status.update', Effect.INTERNAL_WRITE, obj({'bill_codes':arr(TEXT),'status':TEXT}), obj({'committed':BOOL,'record_count':COUNT,'updated':COUNT})),
    )
    resource_specs = (
        ('list_views','network.request','feishu.bitable.list_views',Effect.READ,obj({}),obj({'items':arr(obj({'view_id':TEXT,'view_name':TEXT}))})),
        ('list_records','network.request','feishu.bitable.list_records',Effect.READ,obj({'view_id':TEXT,'cursor':CURSOR,'page_size':COUNT}),obj({'items':arr(source_record_schema(depth=2)),'pagination_complete':BOOL,'next_cursor':CURSOR})),
        ('write_records','network.request','feishu.bitable.write_records',Effect.EXTERNAL_WRITE,obj({'records':arr(obj({'record_id':TEXT,'status':TEXT}))}),obj({'committed':BOOL,'record_count':COUNT,'written':COUNT})),
    )
    descriptors = []
    for requirement, specs in ((account, source_specs), (resource, resource_specs)):
        role = 'account_id' if requirement == account else 'delivery_status_bitable'
        operations = tuple(ConnectorOperation(name=name,effect=effect,input_schema=inputs,output_schema=outputs,
            handler=reviewed_connector_handler(reviewed,tool='sync_delivery_status',operation=operation,action=action,role=role,
                account=account if requirement == account else None,account_alias='account_id' if requirement == account else None,
                resource=resource if requirement == resource else None),max_input_bytes=10*1024*1024,max_output_bytes=10*1024*1024)
            for name,operation,action,effect,inputs,outputs in specs)
        descriptors.append(ConnectorDescriptor(service=requirement.service,title='签收状态接口',binding_kind=requirement.binding_kind,
            account_role=requirement.account_role,allowed_systems=requirement.allowed_systems,
            resource_role=requirement.resource_role,allowed_resource_kinds=requirement.allowed_resource_kinds,operations=operations))
    return tuple(descriptors)
