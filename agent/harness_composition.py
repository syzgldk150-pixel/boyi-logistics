"""Application-root wiring for the AI assistant's trusted readers."""

from __future__ import annotations

from agent.harness_read_gateways import ReadOnlyHarnessGateway
from shared.runtime_repositories import WaybillRepository
from tools.track_waybill_tool import run_track_waybill


def build_read_only_harness_gateway(runtime: object, repository: object) -> ReadOnlyHarnessGateway:
    memory = getattr(runtime, "memory")
    return ReadOnlyHarnessGateway(
        knowledge_search=memory.search_knowledge,
        waybill_lookup=WaybillRepository(memory.connection_factory).get_by_number,
        tracking_lookup=lambda number: run_track_waybill(
            {"tracking_number": number, "timeout_sec": 20, "client_timeout_sec": 22}
        ),
        list_work_items=lambda limit: repository.list_work_items(limit=limit, offset=0),
        get_run=repository.get_run,
        get_evidence=repository.get_evidence,
    )


__all__ = ["build_read_only_harness_gateway"]
