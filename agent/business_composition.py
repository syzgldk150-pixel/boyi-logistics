"""Composition of ordinary Console reads; no scheduler or historical Run."""

from __future__ import annotations

from agent.tms_runtime.direct_business import DirectBusinessService
from agent.tms_runtime.direct_execution import call_blocking
from agent.tms_runtime.receipts_query import fetch_receipt_image_async, query_receipts_async
from agent.send_waybills_business import build_direct_waybill_query_service
from tools.receipt_feishu_detail_query_tool import query_receipt_feishu_detail
from tools.phase7_mysql_store import list_scan_times_for_tracking
from plugin_core_adapters.waybill_query import prepare_waybill_sources, waybill_connection


def build_business_service(*, catalog, plugin_catalog, invocations):
    service = DirectBusinessService(registry=catalog, invocation_service=invocations)
    service.register_read("receipts-query", lambda params, timeout_sec: query_receipts_async(params, timeout_sec, read_lifecycle=invocations))
    service.register_read("receipt-attachment-image", lambda params, timeout_sec: fetch_receipt_image_async(
        params, timeout_sec, read_lifecycle=invocations))
    service.register_read("send-waybills-query", build_direct_waybill_query_service(
        connection_factory=waybill_connection,
        # Ronghui accounts come from plugin instances, not the tool registry.
        prepare_sources=lambda providers: prepare_waybill_sources(providers, catalog=plugin_catalog),
        read_lifecycle=invocations))

    async def receipt_detail(params, timeout_sec):
        return await call_blocking(query_receipt_feishu_detail, params, timeout_sec=timeout_sec)

    service.register_read("receipt-feishu-detail", receipt_detail)

    async def warehouse_scan_times(params, timeout_sec):
        if set(params) != {"code"} or not isinstance(params["code"], str):
            return {"ok": False, "error_code": "INVALID_BUSINESS_INPUT", "error": "只接受单号参数。"}
        try:
            return await call_blocking(list_scan_times_for_tracking, params["code"], timeout_sec=timeout_sec)
        except ValueError as exc:
            return {"ok": False, "error_code": "INVALID_BUSINESS_INPUT", "error": str(exc)}

    service.register_read("warehouse-scan-times", warehouse_scan_times)
    return service
