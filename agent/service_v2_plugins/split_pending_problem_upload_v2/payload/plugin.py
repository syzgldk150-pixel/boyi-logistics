"""Closed Connector adapter for the split-pending Service v2 package.

The embedded v1 action owns the 19-column classification, quantity
reconciliation, selection, projection order, and per-waybill write contract.
This module maps only its reviewed primitives to Host-owned Connectors.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


PLUGIN_ID = "split_pending_problem_upload_v2"
SERVICE_NAME = (
    "plugin.split_pending_problem_upload_v2.split_pending_problem_upload@1"
)
PREVIEW_OPERATION = "preview"
EXECUTE_OPERATION = "execute"
SYSTEM_ROLE = "__system__"
CONTRIBUTION_TARGETS = {
    "console": ("execute_console", "console"),
    "feishu": ("execute_feishu", "feishu"),
    "service": ("host.service.invoke", "service"),
}

SOURCE_CONNECTOR = "connector.boyi.split_pending_source_sheet@1"
TARGET_CONNECTOR = "connector.boyi.split_pending_target_sheet@1"
PROJECTION_CONNECTOR = "connector.boyi.split_pending_projection@1"
RONGHUI_CONNECTOR = "connector.boyi.split_pending_ronghui@1"
LEDGER_CONNECTOR = "connector.boyi.split_pending_problem_ledger@1"

SOURCE_RESOURCE_ROLE = "split_pending_source_sheet"
TARGET_RESOURCE_ROLE = "split_pending_target_sheet"
ACCOUNT_ROLE = "account_id"

NETWORK_OPERATION = "network.request"
PROJECTION_OPERATION = "projection.invoke"
BROWSER_OPERATION = "browser.invoke"
LEDGER_OPERATION = "ledger.invoke"
SERVICE_INVOKE_OPERATION = "service.invoke"

SOURCE_READ_ACTION = "feishu.sheet.read_rows"
SNAPSHOT_READ_ACTION = "split_pending.snapshot.read"
SNAPSHOT_REPLACE_ACTION = "split_pending.snapshot.replace"
SHEET_REPLACE_ACTION = "feishu.sheet.replace_rows"
PROBLEM_QUERY_ACTION = "ronghui.problem.query"
PROBLEM_CREATE_ACTION = "ronghui.problem.create"
PROBLEM_VERIFY_ACTION = "ronghui.problem.verify"
EVENT_UPSERT_ACTION = "daily_sign.problem_event.upsert"
RESULT_UPSERT_ACTION = "split_pending.result.upsert"

READ_ROWS_CONNECTOR_OPERATION = "read_rows"
SNAPSHOT_READ_CONNECTOR_OPERATION = "snapshot_read"
SNAPSHOT_REPLACE_CONNECTOR_OPERATION = "snapshot_replace"
REPLACE_ROWS_CONNECTOR_OPERATION = "replace_rows"
PROBLEM_QUERY_CONNECTOR_OPERATION = "problem_query"
PROBLEM_CREATE_CONNECTOR_OPERATION = "problem_create"
PROBLEM_VERIFY_CONNECTOR_OPERATION = "problem_verify"
EVENT_UPSERT_CONNECTOR_OPERATION = "event_upsert"
RESULT_UPSERT_CONNECTOR_OPERATION = "result_upsert"

PREVIEW_PREFLIGHT_SERVICES = (
    SOURCE_CONNECTOR,
    PROJECTION_CONNECTOR,
)
EXECUTE_PREFLIGHT_SERVICES = (
    SOURCE_CONNECTOR,
    TARGET_CONNECTOR,
    PROJECTION_CONNECTOR,
    RONGHUI_CONNECTOR,
    LEDGER_CONNECTOR,
)
MUTATING_CONNECTOR_OPERATIONS = frozenset(
    {
        SNAPSHOT_REPLACE_CONNECTOR_OPERATION,
        REPLACE_ROWS_CONNECTOR_OPERATION,
        PROBLEM_CREATE_CONNECTOR_OPERATION,
        EVENT_UPSERT_CONNECTOR_OPERATION,
        RESULT_UPSERT_CONNECTOR_OPERATION,
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

    if operation == NETWORK_OPERATION and action == SOURCE_READ_ACTION:
        if role != SOURCE_RESOURCE_ROLE:
            raise ValueError("split-pending source primitive is not declared")
        return SOURCE_CONNECTOR, READ_ROWS_CONNECTOR_OPERATION

    if operation == NETWORK_OPERATION and action == SHEET_REPLACE_ACTION:
        if role != TARGET_RESOURCE_ROLE:
            raise ValueError("split-pending target Sheet primitive is not declared")
        return TARGET_CONNECTOR, REPLACE_ROWS_CONNECTOR_OPERATION

    if operation == PROJECTION_OPERATION and role == TARGET_RESOURCE_ROLE:
        connector_operation = {
            SNAPSHOT_READ_ACTION: SNAPSHOT_READ_CONNECTOR_OPERATION,
            SNAPSHOT_REPLACE_ACTION: SNAPSHOT_REPLACE_CONNECTOR_OPERATION,
            RESULT_UPSERT_ACTION: RESULT_UPSERT_CONNECTOR_OPERATION,
        }.get(action)
        if connector_operation is not None:
            return PROJECTION_CONNECTOR, connector_operation

    if operation == BROWSER_OPERATION and role == ACCOUNT_ROLE:
        connector_operation = {
            PROBLEM_QUERY_ACTION: PROBLEM_QUERY_CONNECTOR_OPERATION,
            PROBLEM_CREATE_ACTION: PROBLEM_CREATE_CONNECTOR_OPERATION,
            PROBLEM_VERIFY_ACTION: PROBLEM_VERIFY_CONNECTOR_OPERATION,
        }.get(action)
        if connector_operation is not None:
            return RONGHUI_CONNECTOR, connector_operation

    if (
        operation == LEDGER_OPERATION
        and action == EVENT_UPSERT_ACTION
        and role == ACCOUNT_ROLE
    ):
        return LEDGER_CONNECTOR, EVENT_UPSERT_CONNECTOR_OPERATION

    raise ValueError("split-pending primitive is not declared")


def _validate_preflight(preflight_services: tuple[str, ...]) -> None:
    if not isinstance(preflight_services, tuple) or preflight_services not in {
        PREVIEW_PREFLIGHT_SERVICES,
        EXECUTE_PREFLIGHT_SERVICES,
    }:
        raise ValueError("split-pending Connector preflight is incomplete or reordered")


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
    "EVENT_UPSERT_CONNECTOR_OPERATION",
    "EXECUTE_OPERATION",
    "EXECUTE_PREFLIGHT_SERVICES",
    "LEDGER_CONNECTOR",
    "LEDGER_OPERATION",
    "MUTATING_CONNECTOR_OPERATIONS",
    "NETWORK_OPERATION",
    "PLUGIN_ID",
    "PREVIEW_OPERATION",
    "PREVIEW_PREFLIGHT_SERVICES",
    "PROBLEM_CREATE_CONNECTOR_OPERATION",
    "PROBLEM_QUERY_CONNECTOR_OPERATION",
    "PROBLEM_VERIFY_CONNECTOR_OPERATION",
    "PROJECTION_CONNECTOR",
    "PROJECTION_OPERATION",
    "READ_ROWS_CONNECTOR_OPERATION",
    "REPLACE_ROWS_CONNECTOR_OPERATION",
    "RESULT_UPSERT_CONNECTOR_OPERATION",
    "RONGHUI_CONNECTOR",
    "SERVICE_INVOKE_OPERATION",
    "SERVICE_NAME",
    "SNAPSHOT_READ_CONNECTOR_OPERATION",
    "SNAPSHOT_REPLACE_CONNECTOR_OPERATION",
    "SOURCE_CONNECTOR",
    "SOURCE_RESOURCE_ROLE",
    "SYSTEM_ROLE",
    "TARGET_CONNECTOR",
    "TARGET_RESOURCE_ROLE",
    "_connector_target",
    "_legacy_result",
    "service_invoke_adapter",
]
