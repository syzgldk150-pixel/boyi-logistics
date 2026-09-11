"""获取当日寄件数据独立业务插件。"""
from connector_adapter import invoke_primitive
PLUGIN_ID = 'sync_daily_send_orders_v2'
PRIMITIVES = {('ledger.invoke', 'sync_daily_send_orders.lock.acquire', 'account_id'): ('connector.boyi.daily_send_source@1', 'acquire', 'internal_write'), ('ledger.invoke', 'sync_daily_send_orders.lock.release', 'account_id'): ('connector.boyi.daily_send_source@1', 'release', 'internal_write'), ('browser.invoke', 'ronghui.send_order.read_page', 'account_id'): ('connector.boyi.daily_send_source@1', 'read_page', 'read'), ('projection.invoke', 'waybill.ronghui.replace_date', 'account_id'): ('connector.boyi.daily_send_source@1', 'replace_date', 'internal_write'), ('network.request', 'feishu.bitable.list_records', 'send_order_bitable'): ('connector.boyi.send_order_bitable@1', 'list_records', 'read'), ('network.request', 'feishu.bitable.delete_records', 'send_order_bitable'): ('connector.boyi.send_order_bitable@1', 'delete_records', 'external_write'), ('network.request', 'feishu.bitable.write_records', 'send_order_bitable'): ('connector.boyi.send_order_bitable@1', 'write_records', 'external_write')}
def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)
def prepare_arguments(arguments):
    return dict(arguments)
