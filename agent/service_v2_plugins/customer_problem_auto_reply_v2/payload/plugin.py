"""客服自动回复只挑选要尝试的问题件；是否允许回复、回复内容和平台写入全部由宿主核验。"""
from connector_adapter import invoke_primitive
PLUGIN_ID = 'customer_problem_auto_reply_v2'
PRIMITIVES = {
    ('projection.invoke','customer_auto_reply.candidates','customer_service_source'): ('connector.boyi.customer_auto_reply@1','candidates','read'),
    ('projection.invoke','customer_auto_reply.draft','customer_service_source'): ('connector.boyi.customer_auto_reply@1','draft','read'),
    ('browser.invoke','customer_auto_reply.reply','customer_service_source'): ('connector.boyi.customer_auto_reply@1','reply','external_write'),
}
def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)
def prepare_arguments(arguments):
    return dict(arguments)
