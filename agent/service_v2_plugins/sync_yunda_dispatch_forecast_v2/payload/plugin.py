"""韵达应派预报的独立插件接口映射；字段与流程由包内 action 维护。"""
from connector_adapter import invoke_primitive
PLUGIN_ID = 'sync_yunda_dispatch_forecast_v2'
PRIMITIVES = {('browser.invoke', 'yunda.dispatch_forecast.read_page', 'account_id'): ('connector.boyi.yunda_dispatch_source@1', 'read_page', 'read'), ('network.request', 'feishu.bitable.append_yunda_dispatch_forecast', 'dispatch_forecast_bitable'): ('connector.boyi.dispatch_forecast_bitable@1', 'commit', 'external_write')}
def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)
def prepare_arguments(arguments):
    return dict(arguments)
