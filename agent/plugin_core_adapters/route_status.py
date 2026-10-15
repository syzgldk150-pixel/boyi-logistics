"""Production ports for the Ronghui in-transit route refresher."""

from __future__ import annotations

from agent.automation_plugins.route_status_handlers import (
    RouteStatusHandlerPorts,
    build_route_status_handler_map,
)
from plugin_core_adapters.waybill_query import waybill_connection
from shared.runtime_repositories import WaybillRepository

_SOURCE = "ronghui"


def build_production_route_status_ports(*, connection_factory=None) -> RouteStatusHandlerPorts:
    repository = WaybillRepository(connection_factory or waybill_connection)
    return RouteStatusHandlerPorts(
        list_pending=lambda limit: repository.list_unsigned_route_candidates(source=_SOURCE, limit=limit),
        update_routes=lambda items, mark_write_started: repository.update_route_statuses(
            items, source=_SOURCE, mark_write_started=mark_write_started),
    )


def build_production_route_status_handler_map(*, cursor_secret: bytes):
    return build_route_status_handler_map(build_production_route_status_ports(), cursor_secret=cursor_secret)


__all__ = ["build_production_route_status_handler_map", "build_production_route_status_ports"]
