"""韵达寄件运单同步的独立插件接口映射；字段与流程由包内 action 维护。"""
from connector_adapter import invoke_primitive
PLUGIN_ID = 'sync_yunda_send_waybills_v2'
PRIMITIVES = {('browser.invoke', 'yunda.send_waybill.list_page', 'account_id'): ('connector.boyi.yunda_send_source@1', 'send_page', 'read'), ('browser.invoke', 'yunda.special_line.list_page', 'account_id'): ('connector.boyi.yunda_send_source@1', 'special_line_page', 'read'), ('browser.invoke', 'yunda.waybill.tracking_detail', 'account_id'): ('connector.boyi.yunda_send_source@1', 'tracking_detail', 'read'), ('browser.invoke', 'yunda.waybill.original_data', 'account_id'): ('connector.boyi.yunda_send_source@1', 'original_data', 'read'), ('browser.invoke', 'yunda.send_waybill.renderer_detail', 'account_id'): ('connector.boyi.yunda_send_source@1', 'renderer_detail', 'read'), ('network.request', 'feishu.bitable.replace_yunda_send_waybills_date', 'send_waybills_bitable'): ('connector.boyi.send_waybills_bitable@1', 'commit', 'external_write'), ('network.request', 'feishu.sheet.replace_yunda_send_waybills', 'send_waybills_sheet'): ('connector.boyi.send_waybills_sheet@1', 'commit', 'external_write'), ('projection.invoke', 'waybill.yunda.replace_date', 'account_id'): ('connector.boyi.yunda_send_projection@1', 'replace_date', 'internal_write')}
def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)
def prepare_arguments(arguments):
    return dict(arguments)
