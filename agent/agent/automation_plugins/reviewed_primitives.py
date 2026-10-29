"""The Host-reviewed primitives Service V2 Connectors and capabilities may reach.

Production merges every reviewed handler map, then keeps only these keys.
A Connector whose primitive is missing here fails at startup, so the set is
checked against what the production Connector registry actually binds
(``tests/test_reviewed_primitives.py``).
"""
from __future__ import annotations

from agent.automation_plugins.daily_sign_connectors_v2 import daily_sign_broker_action_keys
from agent.automation_plugins.finance_connectors_v2 import finance_platform_capture_broker_action_keys
from agent.automation_plugins.route_status_connectors_v2 import route_status_broker_action_keys


_DIRECT_PRIMITIVES = frozenset({
    ("browser.invoke", "customer_problem.detail"),
    ("browser.invoke", "customer_problem.list_page"),
    ("browser.invoke", "ronghui.arrive_list.read_page"),
    # Service V2 clock-in reaches these through capability_proxy_v2.
    ("browser.invoke", "ronghui.clock.precheck"),
    ("browser.invoke", "ronghui.clock.submit"),
    ("browser.invoke", "ronghui.clock.verify"),
    ("browser.invoke", "ronghui.delivery_status.read"),
    ("browser.invoke", "ronghui.problem.create"),
    ("browser.invoke", "ronghui.problem.query"),
    ("browser.invoke", "ronghui.problem.verify"),
    ("browser.invoke", "ronghui.scan.read_page"),
    ("browser.invoke", "ronghui.scan_next.submit"),
    ("browser.invoke", "ronghui.scan_next.verify"),
    ("browser.invoke", "ronghui.send_order.read_page"),
    ("browser.invoke", "ronghui.site_send.read_page"),
    ("browser.invoke", "ronghui.waybill_detail.read"),
    ("browser.invoke", "yunda.dispatch_forecast.read_page"),
    ("browser.invoke", "yunda.send_waybill.list_page"),
    ("browser.invoke", "yunda.send_waybill.renderer_detail"),
    ("browser.invoke", "yunda.special_line.list_page"),
    ("browser.invoke", "yunda.waybill.original_data"),
    ("browser.invoke", "yunda.waybill.tracking_detail"),
    ("ledger.invoke", "daily_sign.problem_event.upsert"),
    ("ledger.invoke", "sync_daily_send_orders.lock.acquire"),
    ("ledger.invoke", "sync_daily_send_orders.lock.release"),
    ("network.request", "feishu.bitable.append_yunda_dispatch_forecast"),
    ("network.request", "feishu.bitable.delete_records"),
    ("network.request", "feishu.bitable.list_records"),
    ("network.request", "feishu.bitable.list_views"),
    ("network.request", "feishu.bitable.replace_snapshot"),
    ("network.request", "feishu.bitable.replace_yunda_send_waybills_date"),
    ("network.request", "feishu.bitable.write_records"),
    ("network.request", "feishu.sheet.add"),
    ("network.request", "feishu.sheet.read_rows"),
    ("network.request", "feishu.sheet.replace"),
    ("network.request", "feishu.sheet.replace_rows"),
    ("network.request", "feishu.sheet.replace_yunda_send_waybills"),
    ("projection.invoke", "arrival.forecast_snapshot.replace"),
    ("projection.invoke", "arrival.report.publication.read"),
    ("projection.invoke", "arrival.snapshot.completed_before"),
    ("projection.invoke", "arrival.snapshot.replace"),
    ("projection.invoke", "scan.snapshot.cleanup"),
    ("projection.invoke", "scan.snapshot.read"),
    ("projection.invoke", "scan.snapshot.replace"),
    ("projection.invoke", "split_pending.result.upsert"),
    ("projection.invoke", "split_pending.snapshot.read"),
    ("projection.invoke", "split_pending.snapshot.refresh"),
    ("projection.invoke", "split_pending.snapshot.replace"),
    ("projection.invoke", "waybill.delivery_status.lookup"),
    ("projection.invoke", "waybill.delivery_status.update"),
    ("projection.invoke", "waybill.pending.read"),
    ("projection.invoke", "waybill.ronghui.replace_date"),
    ("projection.invoke", "waybill.snapshot.replace"),
    ("projection.invoke", "waybill.yunda.replace_date"),
})

REVIEWED_HOST_PRIMITIVES: frozenset[tuple[str, str]] = (
    _DIRECT_PRIMITIVES
    | daily_sign_broker_action_keys()
    | route_status_broker_action_keys()
    | finance_platform_capture_broker_action_keys()
)

__all__ = ["REVIEWED_HOST_PRIMITIVES"]
