"""Closed broker primitives for refreshing Ronghui in-transit route text.

The plugin owns tracking queries and the displayed wording. The Host only lists
unsigned projection rows and applies verified route updates to them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.connector_namespaces import ROUTE_STATUS_TOOL
from agent.automation_plugins.core_adapter import CoreBrokerHandler, CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes


TOOL = ROUTE_STATUS_TOOL
ROLE = "host_internal"
PENDING_ACTION = ("projection.invoke", "waybill.ronghui.route_status.pending")
UPDATE_ACTION = ("projection.invoke", "waybill.ronghui.route_status.update")
MAX_CANDIDATES = 2_000
MAX_ROUTE_TEXT = 128

PendingPort = Callable[[int], list[Mapping[str, Any]]]
UpdatePort = Callable[[list[dict[str, Any]], Callable[[], None] | None], Mapping[str, Any]]


@dataclass(frozen=True)
class RouteStatusHandlerPorts:
    list_pending: PendingPort
    update_routes: UpdatePort


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _require_context(context: CoreBrokerInvocationContext, action: tuple[str, str]) -> None:
    if (context.tool_name, context.operation, context.action, context.role) != (TOOL, *action, ROLE):
        raise _error("route-status broker context is invalid", "BROKER_CONTEXT_INVALID")


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 64:
        raise _error("route-status waybill identity is invalid", "BROKER_ARGUMENT_INVALID")
    return value


class _RouteStatusHandlers:
    def __init__(self, ports: RouteStatusHandlerPorts, *, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("route-status broker secret must contain at least 32 bytes")
        self._ports = ports
        self._secret = bytes(secret)

    def _evidence(self, context: CoreBrokerInvocationContext, purpose: str, proof: Mapping[str, Any]) -> str:
        body = canonical_json_bytes({
            "context": {"automation_id": context.automation_id, "plugin_version": context.plugin_version,
                        "tool_name": context.tool_name},
            "purpose": purpose, "payload": dict(proof),
        })
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(hashlib.sha256(body).digest() + signature).decode("ascii").rstrip("=")

    def pending(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_context(context, PENDING_ACTION)
        if not isinstance(arguments, Mapping) or set(arguments) != {"limit"}:
            raise _error("route-status pending arguments are invalid", "BROKER_ARGUMENT_INVALID")
        limit = arguments["limit"]
        if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
            raise _error("route-status pending limit is invalid", "BROKER_ARGUMENT_INVALID")
        items = []
        for row in self._ports.list_pending(limit):
            items.append({"waybill_no": _identity(row.get("waybill_no")),
                          "scan_status": str(row.get("scan_status") or "")})
        if len({item["waybill_no"] for item in items}) != len(items):
            raise _error("route-status pending identities are duplicated", "BROKER_SOURCE_INVALID")
        proof = {"limit": limit, "record_count": len(items),
                 "identity_set_sha256": hashlib.sha256(canonical_json_bytes(
                     sorted(item["waybill_no"] for item in items))).hexdigest()}
        return {"items": items, "truncated": len(items) >= limit,
                "evidence_ref": self._evidence(context, "route-status-pending", proof)}

    def update(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_context(context, UPDATE_ACTION)
        if not isinstance(arguments, Mapping) or set(arguments) != {"items"} or not isinstance(arguments["items"], list):
            raise _error("route-status update arguments are invalid", "BROKER_ARGUMENT_INVALID")
        items: list[dict[str, Any]] = []
        for raw in arguments["items"]:
            if not isinstance(raw, Mapping) or set(raw) != {"waybill_no", "scan_status", "signed"}:
                raise _error("route-status update item is invalid", "BROKER_ARGUMENT_INVALID")
            text = raw["scan_status"]
            if (not isinstance(text, str) or not text.strip() or text != text.strip()
                    or len(text) > MAX_ROUTE_TEXT or type(raw["signed"]) is not bool):
                raise _error("route-status update item is invalid", "BROKER_ARGUMENT_INVALID")
            items.append({"waybill_no": _identity(raw["waybill_no"]), "scan_status": text, "signed": raw["signed"]})
        if not items or len(items) > MAX_CANDIDATES or len({item["waybill_no"] for item in items}) != len(items):
            raise _error("route-status update set is invalid", "BROKER_ARGUMENT_INVALID")
        # Connector service.invoke already recorded this write's boundary.
        result = self._ports.update_routes(items, context.mark_write_started)
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise _error("route-status update failed", "WRITE_OUTCOME_UNKNOWN")
        if result.get("verified") is not True:
            raise _error("route-status update was not confirmed by a fresh read", "WRITE_OUTCOME_UNKNOWN")
        updated = result.get("updated")
        if type(updated) is not int or updated < 0:
            raise _error("route-status update count is invalid", "BROKER_PROJECTION_MISMATCH")
        proof = {"record_count": len(items), "updated": updated,
                 "items_sha256": hashlib.sha256(canonical_json_bytes(
                     sorted(items, key=lambda item: item["waybill_no"]))).hexdigest()}
        return {"committed": True, "record_count": len(items), "updated": updated,
                "evidence_ref": self._evidence(context, "route-status-update", proof)}

    def handler_map(self) -> dict[tuple[str, str], CoreBrokerHandler]:
        return {PENDING_ACTION: self.pending, UPDATE_ACTION: self.update}


def build_route_status_handler_map(
    ports: RouteStatusHandlerPorts,
    *,
    cursor_secret: bytes,
) -> dict[tuple[str, str], CoreBrokerHandler]:
    return _RouteStatusHandlers(ports, secret=cursor_secret).handler_map()


__all__ = [
    "PENDING_ACTION",
    "ROLE",
    "RouteStatusHandlerPorts",
    "TOOL",
    "UPDATE_ACTION",
    "build_route_status_handler_map",
]
