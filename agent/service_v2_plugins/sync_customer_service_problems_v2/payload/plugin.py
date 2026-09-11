"""客服问题件的字段转换、分页和复核在插件内完成。"""
from connector_adapter import invoke_primitive
PLUGIN_ID = 'sync_customer_service_problems_v2'
PRIMITIVES = {
    ('browser.invoke','customer_problem.list_page','customer_service_source'): ('connector.boyi.customer_sources@1','list_page','read'),
    ('browser.invoke','customer_problem.detail','customer_service_source'): ('connector.boyi.customer_sources@1','detail','read'),
}
def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)
def prepare_arguments(arguments):
    return dict(arguments)
