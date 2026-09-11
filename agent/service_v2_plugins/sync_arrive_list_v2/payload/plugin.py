"""到货清单的可独立升级业务包与接口映射。"""
from connector_adapter import invoke_primitive

PLUGIN_ID = 'sync_arrive_list_v2'
PRIMITIVES = {('browser.invoke', 'ronghui.arrive_list.read_page', 'account_id'): ('connector.boyi.arrive_list_ronghui@1', 'read_page', 'read'), ('projection.invoke', 'waybill.snapshot.replace', 'account_id'): ('connector.boyi.arrive_list_projection@1', 'waybill_replace', 'internal_write'), ('projection.invoke', 'arrival.forecast_snapshot.replace', 'account_id'): ('connector.boyi.arrive_list_projection@1', 'forecast_replace', 'internal_write'), ('projection.invoke', 'arrival.report.publication.read', 'arrive_primary_sheet'): ('connector.boyi.arrive_primary_sheet@1', 'publication_read', 'read'), ('network.request', 'feishu.sheet.replace', 'arrive_primary_sheet'): ('connector.boyi.arrive_primary_sheet@1', 'replace', 'external_write'), ('projection.invoke', 'arrival.report.publication.read', 'arrive_secondary_sheet'): ('connector.boyi.arrive_secondary_sheet@1', 'publication_read', 'read'), ('network.request', 'feishu.sheet.replace', 'arrive_secondary_sheet'): ('connector.boyi.arrive_secondary_sheet@1', 'replace', 'external_write')}

def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)

def prepare_arguments(arguments):
    values = dict(arguments)
    return values
