"""Closed Connector adapter for the self-pickup Service v2 package.

The embedded v1 action owns filtering, selection, and the business write contract.
This module only maps its reviewed primitives to Host-owned Connector services.
No account or resource identity is ever passed into the package payload.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


PLUGIN_ID = "self_pickup_problem_upload_v2"
SERVICE_NAME = "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1"
PREVIEW_OPERATION = "preview"
EXECUTE_OPERATION = "execute"
SYSTEM_ROLE = "__system__"
CONTRIBUTION_TARGETS = {
    "console": ("execute_console", "console"),
    "feishu": ("execute_feishu", "feishu"),
    "service": ("host.service.invoke", "service"),
}

SOURCE_CONNECTOR = "connector.boyi.self_pickup_source_sheet@1"
PRIMARY_CONNECTOR = "connector.boyi.self_pickup_primary_ronghui@1"
DAXIANG_CONNECTOR = "connector.boyi.self_pickup_daxiang_s_ronghui@1"
SOURCE_RESOURCE_ROLE = "self_pickup_source_sheet"
PRIMARY_ACCOUNT_ROLE = "account_id"
DAXIANG_ACCOUNT_ROLE = "daxiang_s_account_id"

SOURCE_READ_ACTION = "feishu.sheet.read_rows"
QUERY_ACTION = "ronghui.problem.query"
CREATE_ACTION = "ronghui.problem.create"
VERIFY_ACTION = "ronghui.problem.verify"
NETWORK_OPERATION = "network.request"
BROWSER_OPERATION = "browser.invoke"
SERVICE_INVOKE_OPERATION = "service.invoke"
READ_ROWS_CONNECTOR_OPERATION = "read_rows"
QUERY_CONNECTOR_OPERATION = "query"
CREATE_CONNECTOR_OPERATION = "create"
VERIFY_CONNECTOR_OPERATION = "verify"

EXECUTE_PREFLIGHT_SERVICES = (
    SOURCE_CONNECTOR,
    PRIMARY_CONNECTOR,
    DAXIANG_CONNECTOR,
)
PREVIEW_PREFLIGHT_SERVICES = (SOURCE_CONNECTOR,)
MUTATING_CONNECTOR_OPERATIONS = frozenset({CREATE_CONNECTOR_OPERATION})

BrokerCall = Callable[..., object]


def _legacy_result(value: object) -> object:
    """Expose only the opaque Host evidence receipt to the v1 action."""

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
            raise ValueError("self-pickup source primitive is not declared")
        return SOURCE_CONNECTOR, READ_ROWS_CONNECTOR_OPERATION

    if operation == BROWSER_OPERATION and action in {
        QUERY_ACTION,
        CREATE_ACTION,
        VERIFY_ACTION,
    }:
        connector = {
            PRIMARY_ACCOUNT_ROLE: PRIMARY_CONNECTOR,
            DAXIANG_ACCOUNT_ROLE: DAXIANG_CONNECTOR,
        }.get(role)
        if connector is None:
            raise ValueError("self-pickup Ronghui role is not declared")
        connector_operation = {
            QUERY_ACTION: QUERY_CONNECTOR_OPERATION,
            CREATE_ACTION: CREATE_CONNECTOR_OPERATION,
            VERIFY_ACTION: VERIFY_CONNECTOR_OPERATION,
        }[action]
        return connector, connector_operation

    raise ValueError("self-pickup primitive is not declared")


def _validate_preflight(preflight_services: tuple[str, ...]) -> None:
    if not isinstance(preflight_services, tuple) or preflight_services not in {
        PREVIEW_PREFLIGHT_SERVICES,
        EXECUTE_PREFLIGHT_SERVICES,
    }:
        raise ValueError("self-pickup Connector preflight is incomplete or reordered")


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

    connector, connector_operation = _connector_target(operation, action, role)
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
    "BROWSER_OPERATION",
    "CONTRIBUTION_TARGETS",
    "CREATE_ACTION",
    "DAXIANG_ACCOUNT_ROLE",
    "DAXIANG_CONNECTOR",
    "EXECUTE_OPERATION",
    "EXECUTE_PREFLIGHT_SERVICES",
    "MUTATING_CONNECTOR_OPERATIONS",
    "NETWORK_OPERATION",
    "PLUGIN_ID",
    "PREVIEW_OPERATION",
    "PREVIEW_PREFLIGHT_SERVICES",
    "PRIMARY_ACCOUNT_ROLE",
    "PRIMARY_CONNECTOR",
    "CREATE_CONNECTOR_OPERATION",
    "QUERY_ACTION",
    "QUERY_CONNECTOR_OPERATION",
    "READ_ROWS_CONNECTOR_OPERATION",
    "SERVICE_NAME",
    "SERVICE_INVOKE_OPERATION",
    "SOURCE_CONNECTOR",
    "SOURCE_READ_ACTION",
    "SOURCE_RESOURCE_ROLE",
    "SYSTEM_ROLE",
    "VERIFY_CONNECTOR_OPERATION",
    "VERIFY_ACTION",
    "_connector_target",
    "_legacy_result",
    "service_invoke_adapter",
]
