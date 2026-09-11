"""查询并更新签收状态独立业务插件。"""
from connector_adapter import invoke_primitive
PLUGIN_ID = 'sync_delivery_status_v2'
PRIMITIVES = {('browser.invoke', 'ronghui.delivery_status.read', 'account_id'): ('connector.boyi.delivery_source@1', 'read_status', 'read'), ('projection.invoke', 'waybill.delivery_status.update', 'account_id'): ('connector.boyi.delivery_source@1', 'update_projection', 'internal_write'), ('network.request', 'feishu.bitable.list_views', 'delivery_status_bitable'): ('connector.boyi.delivery_status_bitable@1', 'list_views', 'read'), ('network.request', 'feishu.bitable.list_records', 'delivery_status_bitable'): ('connector.boyi.delivery_status_bitable@1', 'list_records', 'read'), ('network.request', 'feishu.bitable.write_records', 'delivery_status_bitable'): ('connector.boyi.delivery_status_bitable@1', 'write_records', 'external_write')}
def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)
def prepare_arguments(arguments):
    return dict(arguments)
