"""Host-reserved Feishu commands and existing public Webhook paths, owned by fixed plugins.

The host parses these command texts itself (``direct_tool_router``) so the
scan / problem-upload preview and confirmation flows stay host-owned. Only the
Service V2 plugin named here may declare such a command or route; the call then
goes to the one enabled instance whose active contribution declares it exactly.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Callable, Mapping

from agent.automation_plugins.errors import PluginConflictError
from agent.direct_tool_router import direct_tool_request_from_text


# Fixed Feishu route key -> the only plugin allowed to declare its commands.
FIXED_FEISHU_ROUTE_OWNERS = MappingProxyType({
    "builtin.scan_codes": "sync_scan_codes_v2",
    "builtin.arrive_list": "sync_arrive_list_v2",
    "builtin.send_order": "sync_daily_send_orders_v2",
    "builtin.yunda_send_waybills": "sync_yunda_send_waybills_v2",
    "builtin.yunda_dispatch_forecast": "sync_yunda_dispatch_forecast_v2",
    "builtin.arrival_stats": "sync_arrival_stats_v2",
    "builtin.self_pickup_problem_upload": "self_pickup_problem_upload_v2",
    "builtin.split_pending_problem_upload": "split_pending_problem_upload_v2",
})

# Existing public callback path -> (owning plugin, its declared managed route).
FIXED_WEBHOOK_ROUTES = MappingProxyType({
    "webhook/sign-status": ("sync_delivery_status_v2", "sign-status"),
    "webhook/phase7/scan": ("sync_scan_codes_v2", "phase7-scan"),
    "webhook/phase7/stats": ("sync_arrival_stats_v2", "phase7-stats"),
})


def fixed_feishu_route_key(command: str) -> str:
    """The fixed route a command text belongs to, or ``""``."""

    request = direct_tool_request_from_text(command)
    if not isinstance(request, Mapping) or request.get("mode") not in {"automation_project", "automation_preview"}:
        return ""
    return str(request.get("automation_route_key") or "")


class FixedEntrypointOwnership:
    """Decide fixed-entrypoint ownership from code and the live projection only."""

    def __init__(
        self,
        *,
        project_plugin_id: Callable[[str], str | None],
        resolve_webhook_route: Callable[..., Any] | None = None,
    ) -> None:
        if not callable(project_plugin_id):
            raise TypeError("project_plugin_id must be callable")
        self._project_plugin_id = project_plugin_id
        self._resolve_webhook_route = resolve_webhook_route

    def bind_webhook_resolver(self, resolve_webhook_route: Callable[..., Any]) -> None:
        if not callable(resolve_webhook_route):
            raise TypeError("resolve_webhook_route must be callable")
        self._resolve_webhook_route = resolve_webhook_route

    def allow_reserved_feishu_command(
        self,
        automation_id: str,
        generation: int,
        contribution_id: str,
        command: str,
    ) -> bool:
        """A reserved command text may only be declared by its fixed owner plugin."""

        del generation, contribution_id
        owner = FIXED_FEISHU_ROUTE_OWNERS.get(fixed_feishu_route_key(command))
        return bool(owner and self._project_plugin_id(str(automation_id)) == owner)

    def webhook_target(self, source_path: str) -> Mapping[str, Any] | None:
        """The one enabled target for an existing public path, or ``None`` for other paths."""

        reviewed = FIXED_WEBHOOK_ROUTES.get(str(source_path or "").strip("/"))
        if reviewed is None:
            return None
        plugin_id, route = reviewed
        if self._resolve_webhook_route is None:
            raise PluginConflictError("Webhook route projection is unavailable",
                                      code="PROJECT_RUNTIME_PROJECTION_STALE")
        target = self._resolve_webhook_route(method="POST", route=route)
        if target is None:
            return {"route": route, "automation_id": None}
        automation_id = str(getattr(target, "automation_id", "") or "")
        if self._project_plugin_id(automation_id) != plugin_id:
            raise PluginConflictError("Webhook route is declared by another plugin",
                                      code="CONTRIBUTION_ROUTE_CONFLICT")
        return {
            "automation_id": automation_id,
            "generation": int(getattr(target, "generation")),
            "contribution_id": str(getattr(target, "contribution_id")),
            "route": route,
        }


__all__ = [
    "FIXED_FEISHU_ROUTE_OWNERS",
    "FIXED_WEBHOOK_ROUTES",
    "FixedEntrypointOwnership",
    "fixed_feishu_route_key",
]
