"""Composition of ordinary Console reads; no scheduler or historical Run."""

from __future__ import annotations

from agent.tms_runtime.direct_business import DirectBusinessService
from agent.tms_runtime.direct_execution import call_blocking
from agent.tms_runtime.receipts_query import query_receipts_async
from agent.send_waybills_business import build_direct_waybill_query_service
from tools.receipt_feishu_detail_query_tool import query_receipt_feishu_detail
from plugin_core_adapters.waybill_query import prepare_waybill_sources, waybill_connection


def build_business_service(*, catalog, invocations):
    service = DirectBusinessService(registry=catalog, invocation_service=invocations)
    service.register_read("receipts-query", lambda params, timeout_sec: query_receipts_async(params, timeout_sec, read_lifecycle=invocations))
    service.register_read("send-waybills-query", build_direct_waybill_query_service(
        connection_factory=waybill_connection, prepare_sources=prepare_waybill_sources, read_lifecycle=invocations))

    async def receipt_detail(params, timeout_sec):
        return await call_blocking(query_receipt_feishu_detail, params, timeout_sec=timeout_sec)

    service.register_read("receipt-feishu-detail", receipt_detail)
    return service
