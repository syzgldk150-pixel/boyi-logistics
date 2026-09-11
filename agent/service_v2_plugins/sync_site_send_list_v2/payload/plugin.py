"""网点出港清单的可独立升级业务包与接口映射。"""
from connector_adapter import invoke_primitive

PLUGIN_ID = 'sync_site_send_list_v2'
PRIMITIVES = {('browser.invoke', 'ronghui.site_send.read_page', 'account_id'): ('connector.boyi.site_send_ronghui@1', 'read_page', 'read'), ('network.request', 'feishu.bitable.replace_snapshot', 'site_send_bitable'): ('connector.boyi.site_send_bitable@1', 'replace', 'external_write'), ('network.request', 'feishu.sheet.replace', 'site_send_sheet'): ('connector.boyi.site_send_sheet@1', 'replace', 'external_write')}

def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)

def prepare_arguments(arguments):
    values = dict(arguments)
    # This daily plugin selects today's business date when no date was configured.
    if 'target_date' not in values:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        values['target_date'] = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    return values
