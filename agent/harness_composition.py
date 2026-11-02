"""Application-root wiring for the AI assistant's trusted readers."""

from __future__ import annotations

import asyncio

from agent.execution_boundary import execution_capability_scope
from agent.harness_read_gateways import ReadOnlyHarnessGateway
from agent.tms_runtime.direct_execution import call_blocking
from shared.runtime_repositories import WaybillRepository
from tools.track_waybill_tool import run_track_waybill
from tools.feishu_knowledge import FeishuKnowledgeSource
from agent.chat_text_queries import ChatTextQueries
from agent.shipment_queries import ShipmentQueryService, unavailable_shipment_source
from plugin_core_adapters.shipment_source import prepare_shipment_query
from shared.shipment_metrics import ShipmentQueryError
from agent.orchestration.models import ActorType, OrchestrationError


def build_chat_text_queries(runtime):
    loop = asyncio.get_running_loop()
    def reader(actor, name, params, request_id):
        source = "feishu" if actor.actor_type is ActorType.FEISHU_USER else "console"
        return asyncio.run_coroutine_threadsafe(runtime.execute_tool(
            name, params, actor=actor, source=source, idempotency_key=f"chat:{request_id}:{name}"), loop).result()
    return ChatTextQueries(reader)


def build_read_only_harness_gateway(runtime: object, *, finance_summary=None, invocations=None) -> ReadOnlyHarnessGateway:
    memory = getattr(runtime, "memory")
    knowledge = FeishuKnowledgeSource()
    loop = asyncio.get_running_loop() if invocations is not None else None

    def tracking(number):
        with execution_capability_scope("track_waybill", ttl_seconds=60):
            return run_track_waybill({"tracking_number": number, "timeout_sec": 20, "client_timeout_sec": 22})

    def read_boundary(name, handler, arguments):
        accounts = ()
        if name == "shipment.query":
            try:
                accounts, handler = prepare_shipment_query(arguments.get("station"))
            except ShipmentQueryError as exc:
                return {"ok": False, "status": "unavailable", "code": exc.code, "message": str(exc)}
        async def read():
            return await invocations.call_read(operation=name, account_ids=accounts,
                handler=lambda: call_blocking(handler, arguments, timeout_sec=300))
        return asyncio.run_coroutine_threadsafe(read(), loop).result()

    def get_run(invocation_id):
        if invocations is None:
            return None
        try:
            row = invocations.get(invocation_id)
        except OrchestrationError as exc:
            if exc.code != "INVOCATION_NOT_FOUND":
                raise
            return None
        if row.get("invocation_id") != invocation_id:
            raise ValueError("Invocation identity mismatch")
        return row

    return ReadOnlyHarnessGateway(
        knowledge_search=knowledge.search,
        waybill_lookup=WaybillRepository(memory.connection_factory).get_by_number,
        tracking_lookup=tracking,
        get_run=get_run,
        finance_summary=finance_summary,
        shipment_query=ShipmentQueryService(read_day=unavailable_shipment_source),
        read_boundary=read_boundary if invocations is not None else None,
    )


__all__ = ["build_read_only_harness_gateway"]
