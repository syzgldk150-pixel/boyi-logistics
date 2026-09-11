"""Bound source, synchronization lease and storage ports for daily sending."""
from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import BOOL, COUNT, DATE, TEXT, array_schema as arr, object_schema as obj, source_record_schema
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


def build_daily_send_connectors(reviewed):
    account = ConnectorRequirementContract(service='connector.boyi.daily_send_source@1', account_role='daily_send_source', allowed_systems=('ronghui',), required=True)
    resource = ConnectorRequirementContract(service='connector.boyi.send_order_bitable@1', binding_kind=Kind.RESOURCE, resource_role='send_order_bitable', allowed_resource_kinds=('feishu_bitable',), required=True)
    rows = arr(source_record_schema(depth=2))
    source_specs = (
        ('acquire', 'ledger.invoke', 'sync_daily_send_orders.lock.acquire', Effect.INTERNAL_WRITE, obj({}), obj({'acquired':BOOL, 'lease_ref':TEXT})),
        ('release', 'ledger.invoke', 'sync_daily_send_orders.lock.release', Effect.INTERNAL_WRITE, obj({'lease_ref':TEXT}), obj({'committed':BOOL, 'released':BOOL})),
        ('read_page', 'browser.invoke', 'ronghui.send_order.read_page', Effect.READ, obj({'target_date':DATE, 'page_index':COUNT, 'page_size':COUNT}), obj({'items':rows, 'total':COUNT})),
        ('replace_date', 'projection.invoke', 'waybill.ronghui.replace_date', Effect.INTERNAL_WRITE, obj({'records':rows, 'target_date':DATE}), obj({'committed':BOOL, 'upserted':COUNT, 'updates':COUNT, 'creates':COUNT, 'deleted_stale':COUNT})),
    )
    resource_specs = (
        ('list_records', 'network.request', 'feishu.bitable.list_records', Effect.READ, obj({'offset':COUNT,'page_size':COUNT,'fields':arr(TEXT)}), obj({'items':arr(obj({'record_ref':TEXT,'fields':source_record_schema(depth=2)}))})),
        ('delete_records', 'network.request', 'feishu.bitable.delete_records', Effect.EXTERNAL_WRITE, obj({'record_refs':arr(TEXT)}), obj({'committed':BOOL,'deleted':COUNT})),
        ('write_records', 'network.request', 'feishu.bitable.write_records', Effect.EXTERNAL_WRITE, obj({'records':rows}), obj({'committed':BOOL,'written':COUNT})),
    )
    descriptors = []
    for requirement, specs in ((account, source_specs), (resource, resource_specs)):
        role = 'account_id' if requirement == account else 'send_order_bitable'
        operations = tuple(ConnectorOperation(name=name, effect=effect, input_schema=inputs, output_schema=outputs,
            handler=reviewed_connector_handler(reviewed, tool='sync_daily_send_orders', operation=operation, action=action, role=role,
                account=account if requirement == account else None, account_alias='account_id' if requirement == account else None,
                resource=resource if requirement == resource else None), max_input_bytes=32*1024*1024, max_output_bytes=10*1024*1024)
            for name, operation, action, effect, inputs, outputs in specs)
        descriptors.append(ConnectorDescriptor(service=requirement.service, title='寄件数据接口', binding_kind=requirement.binding_kind,
            account_role=requirement.account_role, allowed_systems=requirement.allowed_systems,
            resource_role=requirement.resource_role, allowed_resource_kinds=requirement.allowed_resource_kinds, operations=operations))
    return tuple(descriptors)
