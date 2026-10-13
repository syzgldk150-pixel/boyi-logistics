"""财务采集、校验和汇总由包内 action 实现。"""
from connector_adapter import invoke_primitive
PLUGIN_ID = 'sync_finance_bills_v2'
PRIMITIVES = {('browser.invoke', 'ronghui.finance.capture_page', 'finance_quote_source'): ('connector.boyi.finance_quote_source@1', 'capture_page', 'read'), ('browser.invoke', 'ronghui.finance.verify_source_totals', 'finance_quote_source'): ('connector.boyi.finance_quote_source@1', 'verify_totals', 'read'), ('ledger.invoke', 'finance.batch.acquire', 'finance_quote_source'): ('connector.boyi.finance_quote_source@1', 'acquire_batch', 'internal_write'), ('ledger.invoke', 'finance.source_snapshot.write', 'finance_quote_source'): ('connector.boyi.finance_quote_source@1', 'write_snapshot', 'internal_write'), ('ledger.invoke', 'finance.projection.commit', 'finance_quote_source'): ('connector.boyi.finance_quote_source@1', 'commit_projection', 'internal_write'), ('browser.invoke', 'ronghui.finance.capture_page', 'finance_daxiang_s_source'): ('connector.boyi.finance_daxiang_s_source@1', 'capture_page', 'read'), ('browser.invoke', 'ronghui.finance.verify_source_totals', 'finance_daxiang_s_source'): ('connector.boyi.finance_daxiang_s_source@1', 'verify_totals', 'read'), ('ledger.invoke', 'finance.batch.acquire', 'finance_daxiang_s_source'): ('connector.boyi.finance_daxiang_s_source@1', 'acquire_batch', 'internal_write'), ('ledger.invoke', 'finance.source_snapshot.write', 'finance_daxiang_s_source'): ('connector.boyi.finance_daxiang_s_source@1', 'write_snapshot', 'internal_write'), ('ledger.invoke', 'finance.projection.commit', 'finance_daxiang_s_source'): ('connector.boyi.finance_daxiang_s_source@1', 'commit_projection', 'internal_write'), ('browser.invoke', 'ronghui.finance.capture_page', 'finance_self_pickup_source'): ('connector.boyi.finance_self_pickup_source@1', 'capture_page', 'read'), ('browser.invoke', 'ronghui.finance.verify_source_totals', 'finance_self_pickup_source'): ('connector.boyi.finance_self_pickup_source@1', 'verify_totals', 'read'), ('ledger.invoke', 'finance.batch.acquire', 'finance_self_pickup_source'): ('connector.boyi.finance_self_pickup_source@1', 'acquire_batch', 'internal_write'), ('ledger.invoke', 'finance.source_snapshot.write', 'finance_self_pickup_source'): ('connector.boyi.finance_self_pickup_source@1', 'write_snapshot', 'internal_write'), ('ledger.invoke', 'finance.projection.commit', 'finance_self_pickup_source'): ('connector.boyi.finance_self_pickup_source@1', 'commit_projection', 'internal_write')}
def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)
def prepare_arguments(arguments):
    values = dict(arguments)
    if "collection_request" not in values:
        return values
    request = values.pop("collection_request")
    if not isinstance(request, dict):
        raise ValueError("finance collection request must be an object")
    allowed = {"mode", "target_date", "start_date", "end_date", "batch_id", "rescan_days"}
    if "mode" not in request or set(request) - allowed:
        raise ValueError("finance collection request contains undeclared fields")
    # A page request is one complete occurrence; saved backfill dates or retry
    # IDs must not leak into this collection or alter the scheduler settings.
    return dict(request)
