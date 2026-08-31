"""Closed Connector adapter for the scan-codes Service v2 package.

The embedded v1 action owns pagination, classification, preview revalidation,
snapshot conservation, batching, and per-batch verification.  This module maps
only its exact reviewed primitives to Host-owned Connector services.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


PLUGIN_ID = "sync_scan_codes_v2"
SERVICE_NAME = "plugin.sync_scan_codes_v2.scan_codes@1"
PREVIEW_OPERATION = "preview"
EXECUTE_OPERATION = "execute"
SYSTEM_ROLE = "__system__"
CONTRIBUTION_TARGETS = {
    "console": ("execute_console", "console", EXECUTE_OPERATION),
    "feishu": ("execute_feishu", "feishu", EXECUTE_OPERATION),
}

SCAN_CONNECTOR = "connector.boyi.scan_ronghui@1"
PROJECTION_CONNECTOR = "connector.boyi.scan_projection@1"
ACCOUNT_ROLE = "account_id"

BROWSER_OPERATION = "browser.invoke"
PROJECTION_OPERATION = "projection.invoke"
SERVICE_INVOKE_OPERATION = "service.invoke"

READ_PAGE_ACTION = "ronghui.scan.read_page"
SNAPSHOT_REPLACE_ACTION = "scan.snapshot.replace"
SUBMIT_ACTION = "ronghui.scan_next.submit"
VERIFY_ACTION = "ronghui.scan_next.verify"

READ_PAGE_CONNECTOR_OPERATION = "read_page"
SNAPSHOT_REPLACE_CONNECTOR_OPERATION = "snapshot_replace"
SUBMIT_CONNECTOR_OPERATION = "submit"
VERIFY_CONNECTOR_OPERATION = "verify"

PREVIEW_PREFLIGHT_SERVICES = (SCAN_CONNECTOR,)
EXECUTE_PREFLIGHT_SERVICES = (
    SCAN_CONNECTOR,
    PROJECTION_CONNECTOR,
)
MUTATING_CONNECTOR_OPERATIONS = frozenset(
    {
        SNAPSHOT_REPLACE_CONNECTOR_OPERATION,
        SUBMIT_CONNECTOR_OPERATION,
    }
)

BrokerCall = Callable[..., object]


def _legacy_result(value: object) -> object:
    """Expose only an opaque Host Evidence receipt to the v1 action."""

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
    arguments: Mapping[str, object] | None = None,
) -> tuple[str, str]:
    """Resolve one v1 primitive without forwarding its local role."""

    if role != ACCOUNT_ROLE:
        raise ValueError("scan-codes account role is not declared")

    if operation == BROWSER_OPERATION:
        connector_operation = {
            READ_PAGE_ACTION: READ_PAGE_CONNECTOR_OPERATION,
            SUBMIT_ACTION: SUBMIT_CONNECTOR_OPERATION,
            VERIFY_ACTION: VERIFY_CONNECTOR_OPERATION,
        }.get(action)
        if connector_operation is not None:
            return SCAN_CONNECTOR, connector_operation

    if operation == PROJECTION_OPERATION and action == SNAPSHOT_REPLACE_ACTION:
        return PROJECTION_CONNECTOR, SNAPSHOT_REPLACE_CONNECTOR_OPERATION

    raise ValueError("scan-codes primitive is not declared")


def _validate_preflight(preflight_services: tuple[str, ...]) -> None:
    if not isinstance(preflight_services, tuple) or preflight_services not in {
        PREVIEW_PREFLIGHT_SERVICES,
        EXECUTE_PREFLIGHT_SERVICES,
    }:
        raise ValueError("scan-codes Connector preflight is incomplete or reordered")


def service_invoke_adapter(
    broker: BrokerCall,
    operation: str,
    *,
    action: str,
    role: str,
    arguments: Mapping[str, object],
    preflight_services: tuple[str, ...] = (),
) -> object:
    """Adapt one embedded v1 primitive to one Host ``service.invoke`` call."""

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
        _validate_preflight(preflight_services)
        request_arguments["preflight_services"] = list(preflight_services)
    return _legacy_result(
        broker(
            SERVICE_INVOKE_OPERATION,
            action=connector_operation,
            role=SYSTEM_ROLE,
            arguments=request_arguments,
        )
    )


__all__ = [
    "ACCOUNT_ROLE",
    "BROWSER_OPERATION",
    "CONTRIBUTION_TARGETS",
    "EXECUTE_OPERATION",
    "EXECUTE_PREFLIGHT_SERVICES",
    "MUTATING_CONNECTOR_OPERATIONS",
    "PLUGIN_ID",
    "PREVIEW_OPERATION",
    "PREVIEW_PREFLIGHT_SERVICES",
    "PROJECTION_CONNECTOR",
    "PROJECTION_OPERATION",
    "READ_PAGE_ACTION",
    "READ_PAGE_CONNECTOR_OPERATION",
    "SCAN_CONNECTOR",
    "SERVICE_INVOKE_OPERATION",
    "SERVICE_NAME",
    "SNAPSHOT_REPLACE_ACTION",
    "SNAPSHOT_REPLACE_CONNECTOR_OPERATION",
    "SUBMIT_ACTION",
    "SUBMIT_CONNECTOR_OPERATION",
    "SYSTEM_ROLE",
    "VERIFY_ACTION",
    "VERIFY_CONNECTOR_OPERATION",
    "_connector_target",
    "_legacy_result",
    "service_invoke_adapter",
]
