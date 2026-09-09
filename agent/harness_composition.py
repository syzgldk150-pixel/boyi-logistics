"""Application-root wiring for the AI assistant's trusted readers."""

from __future__ import annotations

import asyncio

from agent.execution_boundary import execution_capability_scope
from agent.harness_read_gateways import ReadOnlyHarnessGateway
from agent.tms_runtime.direct_execution import call_blocking
from shared.runtime_repositories import WaybillRepository
from tools.track_waybill_tool import run_track_waybill


def build_read_only_harness_gateway(runtime: object, repository: object, *, finance_summary=None, invocations=None) -> ReadOnlyHarnessGateway:
    memory = getattr(runtime, "memory")
    loop = asyncio.get_running_loop() if invocations is not None else None

    def tracking(number):
        with execution_capability_scope("track_waybill", ttl_seconds=60):
            return run_track_waybill({"tracking_number": number, "timeout_sec": 20, "client_timeout_sec": 22})

    def read_boundary(name, handler, arguments):
        async def read():
            return await invocations.call_read(operation=name, handler=lambda: call_blocking(handler, arguments, timeout_sec=300))
        return asyncio.run_coroutine_threadsafe(read(), loop).result()

    return ReadOnlyHarnessGateway(
        knowledge_search=memory.search_knowledge,
        waybill_lookup=WaybillRepository(memory.connection_factory).get_by_number,
        tracking_lookup=tracking,
        list_work_items=lambda limit: repository.list_work_items(limit=limit, offset=0),
        get_run=repository.get_run,
        get_evidence=repository.get_evidence,
        finance_summary=finance_summary,
        read_boundary=read_boundary if invocations is not None else None,
    )


__all__ = ["build_read_only_harness_gateway"]
