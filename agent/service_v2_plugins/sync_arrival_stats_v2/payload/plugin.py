"""Declarative identity and the closed ``service.invoke`` adapter.

The arrival-statistics algorithm deliberately lives in the extracted first-party
payload.  This module only translates its reviewed primitive calls to exact
Host Connector identities; it never resolves an account, resource, credential,
or external endpoint.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


PLUGIN_ID = "sync_arrival_stats_v2"
SERVICE_NAME = "plugin.sync_arrival_stats_v2.arrival_stats@1"
SERVICE_OPERATION = "run"
SYSTEM_ROLE = "__system__"
CONTRIBUTION_TARGETS = {
    "console": ("manual_run", "console"),
    "scheduler": ("daily_arrival_stats", "scheduler"),
    "feishu": ("arrival_stats_command", "feishu"),
    "service": ("host.service.invoke", "service"),
}

TMS_CONNECTOR = "connector.boyi.arrival_stats_tms@1"
PROJECTION_CONNECTOR = "connector.boyi.arrival_stats_projection@1"
SHEET_CONNECTORS = {
    "arrival_stats_primary_sheet": "connector.boyi.arrival_stats_primary_sheet@1",
    "arrival_stats_secondary_sheet": "connector.boyi.arrival_stats_secondary_sheet@1",
    "arrival_stats_pending_sheet": "connector.boyi.arrival_stats_pending_sheet@1",
    "arrival_stats_archive_sheet": "connector.boyi.arrival_stats_archive_sheet@1",
    "arrival_stats_split_pending_sheet": "connector.boyi.arrival_stats_split_pending_sheet@1",
}

_TMS_ACTIONS = {
    "ronghui.arrive_list.read_page": "arrive_list_read_page",
    "ronghui.scan.read_page": "scan_read_page",
    "ronghui.waybill_detail.read": "waybill_detail_read",
}
_PROJECTION_ACTIONS = {
    "arrival.snapshot.completed_before": "completed_before",
    "scan.snapshot.read": "scan_read",
    "scan.snapshot.replace": "scan_replace",
    "scan.snapshot.cleanup": "scan_cleanup",
    "waybill.snapshot.replace": "waybill_replace",
    "waybill.pending.read": "pending_read",
    "arrival.snapshot.replace": "arrival_replace",
    "split_pending.snapshot.refresh": "split_pending_refresh",
}
_SHEET_REPLACE_ACTION = "feishu.sheet.replace"
_SHEET_ADD_ACTION = "feishu.sheet.add"
_TMS_OPERATION = "browser.invoke"
_PROJECTION_OPERATION = "projection.invoke"
_NETWORK_OPERATION = "network.request"
_SHEET_SLOTS = {
    "arrival_stats_primary_sheet": "arrival_stats_primary",
    "arrival_stats_secondary_sheet": "arrival_stats_secondary",
    "arrival_stats_pending_sheet": "arrival_stats_pending",
    "arrival_stats_split_pending_sheet": "arrival_stats_split_pending",
}
MUTATING_CONNECTOR_OPERATIONS = frozenset(
    {
        "scan_replace",
        "scan_cleanup",
        "waybill_replace",
        "arrival_replace",
        "split_pending_refresh",
        "replace",
        "add",
    }
)

BrokerCall = Callable[..., object]


def _legacy_result(value: object) -> object:
    """Expose only the Host receipt needed by the embedded v1 result helper."""

    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    host_reference = getattr(value, "host_evidence_ref", None)
    if host_reference is None:
        return result
    if (
        not isinstance(host_reference, str)
        or not host_reference.strip()
        or len(host_reference) > 512
    ):
        raise ValueError("Host evidence reference is invalid")
    existing = result.get("evidence_ref")
    if existing not in (None, "", host_reference):
        raise ValueError("Host evidence references are inconsistent")
    result["evidence_ref"] = host_reference
    return result


def _connector_target(
    operation: str,
    action: str,
    role: str,
    arguments: Mapping[str, object],
) -> tuple[str, str]:
    """Return the exact Connector and operation for one reviewed primitive."""

    if operation == _TMS_OPERATION:
        connector_operation = _TMS_ACTIONS.get(action)
        if connector_operation is None or role != "account_id":
            raise ValueError("arrival statistics TMS primitive is not declared")
        return TMS_CONNECTOR, connector_operation

    if operation == _PROJECTION_OPERATION:
        connector_operation = _PROJECTION_ACTIONS.get(action)
        if connector_operation is None or role != "account_id":
            raise ValueError("arrival statistics projection primitive is not declared")
        return PROJECTION_CONNECTOR, connector_operation

    if operation == _NETWORK_OPERATION and action in {_SHEET_REPLACE_ACTION, _SHEET_ADD_ACTION}:
        connector = SHEET_CONNECTORS.get(role)
        if connector is None:
            raise ValueError("arrival statistics Sheet resource primitive is not declared")
        slot = arguments.get("resource_slot")
        if action == _SHEET_ADD_ACTION:
            if role != "arrival_stats_archive_sheet" or slot not in (None, ""):
                raise ValueError("arrival statistics archive primitive is not declared")
            return connector, "add"
        expected_slot = _SHEET_SLOTS.get(role)
        if expected_slot is None or not isinstance(slot, str) or slot.strip() != expected_slot:
            raise ValueError("arrival statistics Sheet resource slot is inconsistent")
        return connector, "replace"

    raise ValueError("arrival statistics primitive is not declared")


def service_invoke_adapter(
    broker: BrokerCall,
    operation: str,
    *,
    action: str,
    role: str,
    arguments: Mapping[str, object],
    preflight_services: tuple[str, ...] = (),
) -> object:
    """Adapt a v1 primitive callback to one Host ``service.invoke`` call.

    The Host derives the private binding from the signed manifest requirement.
    The v1 ``role`` is used only as a local consistency check and is never
    forwarded as an account/resource identity.
    """

    connector, connector_operation = _connector_target(
        operation,
        action,
        role,
        arguments,
    )
    request_arguments: dict[str, object] = {
        "service": connector,
        "operation": connector_operation,
        "arguments": dict(arguments),
    }
    if preflight_services:
        declared = {TMS_CONNECTOR, PROJECTION_CONNECTOR, *SHEET_CONNECTORS.values()}
        if (
            len(preflight_services) > 8
            or len(preflight_services) != len(set(preflight_services))
            or any(service not in declared for service in preflight_services)
        ):
            raise ValueError("arrival statistics Connector preflight is invalid")
        request_arguments["preflight_services"] = list(preflight_services)
    return _legacy_result(broker(
        "service.invoke",
        action=connector_operation,
        role=SYSTEM_ROLE,
        arguments=request_arguments,
    ))


__all__ = [
    "PLUGIN_ID",
    "CONTRIBUTION_TARGETS",
    "MUTATING_CONNECTOR_OPERATIONS",
    "PROJECTION_CONNECTOR",
    "SERVICE_NAME",
    "SERVICE_OPERATION",
    "SHEET_CONNECTORS",
    "SYSTEM_ROLE",
    "TMS_CONNECTOR",
    "_legacy_result",
    "service_invoke_adapter",
]
