"""Private declarations shared by the first-party broker handlers.

The handler facade remains the stable import surface.  These declarations are
pure operation contracts and argument-shape validation; they do not access
tools, external services, or runtime state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent.automation_plugins.first_party_handler_common import _MAX_RECORDS, _error


_CUSTOMER_TOOL = "sync_customer_service_problems"
_CLOCK_TOOL = "clock_in_dual"
_DAILY_SIGN_TOOL = "sync_daily_should_sign"
_ARRIVE_TOOL = "sync_arrive_list"
_ARRIVAL_STATS_TOOL = "sync_arrival_stats"
_SCAN_TOOL = "sync_scan_codes"
_SITE_SEND_TOOL = "sync_site_send_list"
_SITE_SEND_BITABLE_ROLE = "site_send_bitable"
_SITE_SEND_SHEET_ROLE = "site_send_sheet"
_YUNDA_DISPATCH_TOOL = "sync_yunda_dispatch_forecast"
_YUNDA_SEND_TOOL = "sync_yunda_send_waybills"
_DELIVERY_TOOL = "sync_delivery_status"
_DELIVERY_RESOURCE_ROLE = "delivery_status_bitable"
_CUSTOMER_DIRECTIONS = frozenset({"received", "published", "both"})
_DAILY_SIGN_ARGUMENT_FIELDS = frozenset(
    {
        "enrich_addresses",
        "days",
        "source_start",
        "source_end",
        "problem_page_size",
        "problem_max_pages",
        "problem_retry_attempts",
        "problem_timeout_sec",
        "sign_page_size",
        "sign_max_pages",
        "sign_chunk_days",
        "sign_retry_attempts",
        "sign_timeout_sec",
        "waybill_timeout_sec",
        "browser_batch_size",
        "sign_site_code",
        "exact_sign_conflict_limit",
        "exact_historical_sign_limit",
    }
)
_ARRIVE_FIELDS = (
    "tracking_number",
    "goods_name",
    "package_type",
    "delivery_method",
    "quantity",
    "receipt_number",
    "actual_weight",
    "volume",
    "remarks",
    "destination_station",
    "recipient_name",
    "recipient_phone",
    "recipient_address",
    "settlement_weight",
    "volumetric_weight",
    "shipping_fee",
    "payment_type",
    "pay_on_arrival",
)
_SITE_FIELDS = (
    "tracking_number",
    "send_site",
    "package_type",
    "destination",
    "pieces",
    "weight",
)
_SCAN_SOURCE_FIELDS = (
    "bill_code",
    "destination",
    "scan_type",
    "scan_time",
    "scan_site",
)
_SCAN_SNAPSHOT_FIELDS = (
    "raw_code",
    "destination",
    "code_type",
    "main_tracking",
)
_SCAN_NEXT_FIELDS = ("bill_code", "station_name")
_SCAN_READ_FIELDS = (
    "raw_code",
    "destination",
    "code_type",
)
_ARRIVAL_STATS_FIELDS = (*_ARRIVE_FIELDS, "arrived_quantity")
_ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS = frozenset({"receipt_number", "remarks"})
_ARRIVAL_SNAPSHOT_FIELDS = (
    "tracking_number",
    "destination_station",
    "expected_quantity",
    "arrived_quantity",
    "goods_name",
    "package_type",
    "delivery_method",
    "recipient_address",
)
_PENDING_FIELDS = (
    "tracking_number",
    "destination_station",
    "expected_quantity",
    "arrived_quantity",
    "pending_quantity",
    "first_arrival_at",
    "last_arrival_at",
    "arrival_status",
)


def _arrival_stats_v1_records(
    value: object,
    *,
    fields: Sequence[str],
    label: str,
) -> list[dict[str, Any]]:
    """Validate v1 arrival rows and fill only its documented empty fields."""

    if not isinstance(value, list) or len(value) > _MAX_RECORDS:
        raise _error(f"{label} records are invalid", "BROKER_ARGUMENT_INVALID")
    required = set(fields)
    output: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise _error(f"{label} record schema is invalid", "BROKER_ARGUMENT_INVALID")
        keys = set(raw)
        missing = required - keys
        if keys - required or not missing.issubset(_ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS):
            raise _error(f"{label} record schema is invalid", "BROKER_ARGUMENT_INVALID")
        output.append({**dict(raw), **{field: "" for field in missing}})
    return output


_YUNDA_DISPATCH_FIELDS = (
    "主单号",
    "开单件数",
    "扫描件数",
    "重量/kg",
    "体积/m3",
    "包装类型",
    "清场时间",
    "规划时效",
    "开单目的地址",
    "预计到达时间",
    "应派时间",
)
_YUNDA_SEND_FIELDS = (
    "5.14编号",
    "目的网点",
    "收件区/县",
    "收件地址",
    "寄件人",
    "寄件手机",
    "收货人",
    "收货电话",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "实际重量",
    "现付",
    "月结",
    "提付",
    "中转运费",
    "回单号",
    "备注",
    "结算重量",
    "体积",
    "支付类型",
    "体积重",
    "到付款",
    "日期",
)
_YUNDA_DISPATCH_RESOURCE_ROLE = "dispatch_forecast_bitable"
_YUNDA_SEND_BITABLE_ROLE = "send_waybills_bitable"
_YUNDA_SEND_SHEET_ROLE = "send_waybills_sheet"

# This closed declaration is checked against the signed release contract. A
# newly registered write must be named here and call ``_mark_write_started``
# at the actual mutating port boundary.
MARKED_WRITE_ACTION_KEYS = frozenset(
    {
        ("browser.invoke", "ronghui.clock.submit"),
        ("ledger.invoke", "daily_sign.authoritative_sync"),
        ("projection.invoke", "scan.snapshot.replace"),
        ("projection.invoke", "scan.snapshot.cleanup"),
        ("projection.invoke", "waybill.snapshot.replace"),
        ("projection.invoke", "arrival.forecast_snapshot.replace"),
        ("projection.invoke", "arrival.snapshot.replace"),
        ("projection.invoke", "split_pending.snapshot.refresh"),
        ("network.request", "feishu.sheet.replace"),
        ("network.request", "feishu.sheet.add"),
        ("browser.invoke", "ronghui.scan_next.submit"),
        ("network.request", "feishu.bitable.write_records"),
        ("network.request", "feishu.bitable.replace_snapshot"),
        ("network.request", "feishu.bitable.append_yunda_dispatch_forecast"),
        ("network.request", "feishu.bitable.replace_yunda_send_waybills_date"),
        ("network.request", "feishu.sheet.replace_yunda_send_waybills"),
        ("projection.invoke", "waybill.yunda.replace_date"),
        ("projection.invoke", "waybill.delivery_status.update"),
    }
)


__all__ = [
    "_CUSTOMER_TOOL",
    "_CLOCK_TOOL",
    "_DAILY_SIGN_TOOL",
    "_ARRIVE_TOOL",
    "_ARRIVAL_STATS_TOOL",
    "_SCAN_TOOL",
    "_SITE_SEND_TOOL",
    "_SITE_SEND_BITABLE_ROLE",
    "_SITE_SEND_SHEET_ROLE",
    "_YUNDA_DISPATCH_TOOL",
    "_YUNDA_SEND_TOOL",
    "_DELIVERY_TOOL",
    "_DELIVERY_RESOURCE_ROLE",
    "_CUSTOMER_DIRECTIONS",
    "_DAILY_SIGN_ARGUMENT_FIELDS",
    "_ARRIVE_FIELDS",
    "_SITE_FIELDS",
    "_SCAN_SOURCE_FIELDS",
    "_SCAN_SNAPSHOT_FIELDS",
    "_SCAN_NEXT_FIELDS",
    "_SCAN_READ_FIELDS",
    "_ARRIVAL_STATS_FIELDS",
    "_ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS",
    "_ARRIVAL_SNAPSHOT_FIELDS",
    "_PENDING_FIELDS",
    "_arrival_stats_v1_records",
    "_YUNDA_DISPATCH_FIELDS",
    "_YUNDA_SEND_FIELDS",
    "_YUNDA_DISPATCH_RESOURCE_ROLE",
    "_YUNDA_SEND_BITABLE_ROLE",
    "_YUNDA_SEND_SHEET_ROLE",
    "MARKED_WRITE_ACTION_KEYS",
]
