"""Production arrival-statistics Connectors over the reviewed infrastructure ports.

The package owns the business algorithm. The Host owns exact account/resource
resolution, one primitive at a time, and the fresh-readback checks already used
by the production adapters. No plugin code or whole-script fallback runs here.
"""
from __future__ import annotations


from agent.automation_plugins.connector_registry import (
    ConnectorBindingKind, ConnectorDescriptor, ConnectorOperation,
)
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler
from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.first_party_handler_support import (
    _ARRIVE_FIELDS, _ARRIVAL_STATS_FIELDS, _ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS,
    _ARRIVAL_SNAPSHOT_FIELDS, _PENDING_FIELDS, _SCAN_READ_FIELDS,
    _SCAN_SNAPSHOT_FIELDS, _SCAN_SOURCE_FIELDS,
)
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.connector_schemas import (
    TEXT as _TEXT, DATE as _DATE, BOOL as _BOOL, COUNT as _COUNT, CURSOR as _CURSOR,
    object_schema as _object, array_schema as _array, record_schema as _record,
)


_ACCOUNT_ROLE = "arrival_stats_tms"
_TOOL = "sync_arrival_stats"
def _records_input(fields, *, optional=(), slot=False):
    properties = {"records": _array(_record(fields, optional=optional)), "target_date": _DATE}
    if slot:
        properties["resource_slot"] = {"type": "string", "maxLength": 64}
    return _object(properties)


def _projection_result(**extra):
    return _object({"target_date": _DATE, "record_count": _COUNT, "committed": _BOOL, **extra})


def _handler(reviewed, *, operation, action, role):
    resource = None
    if operation == "network.request":
        resource = ConnectorRequirementContract(service=f"connector.boyi.{role}@1",
            binding_kind=ConnectorBindingKind.RESOURCE, resource_role=role,
            allowed_resource_kinds=("feishu_sheet",), required=True)
    return reviewed_connector_handler(reviewed, tool=_TOOL, operation=operation, action=action, role=role,
        account=ConnectorRequirementContract(service="connector.boyi.arrival_stats_tms@1",
            account_role=_ACCOUNT_ROLE, allowed_systems=("ronghui",), required=True),
        resource=resource, account_alias="account_id")


def build_arrival_connectors(reviewed) -> tuple[ConnectorDescriptor, ...]:
    def op(name, action, effect, input_schema, output_schema, *, operation, role="account_id"):
        return ConnectorOperation(name=name, effect=effect, input_schema=input_schema,
            output_schema=output_schema, handler=_handler(reviewed, operation=operation, action=action, role=role),
            max_input_bytes=64 * 1024 * 1024, max_output_bytes=10 * 1024 * 1024)

    def descriptor(suffix, operations, *, kind, role=None):
        return ConnectorDescriptor(service=f"connector.boyi.{suffix}@1", title=suffix,
            account_role=_ACCOUNT_ROLE if kind is ConnectorBindingKind.ACCOUNT else None,
            allowed_systems=("ronghui",) if kind is ConnectorBindingKind.ACCOUNT else (),
            binding_kind=kind, resource_role=role,
            allowed_resource_kinds=("feishu_sheet",) if kind is ConnectorBindingKind.RESOURCE else (),
            operations=tuple(operations))

    date_input = _object({"target_date": _DATE})
    page_input = _object({"target_date": _DATE, "cursor": _CURSOR,
                          "page_size": {"type": "integer", "minimum": 1, "maximum": 200}},
                         ["target_date", "page_size"])
    def page_output(fields):
        row = _record(fields, optional=tuple(key for key in fields if key != "tracking_number")) if fields == _ARRIVE_FIELDS else _record(fields)
        return _object({"items": _array(row), "next_cursor": _CURSOR, "pagination_complete": _BOOL})
    tms = descriptor("arrival_stats_tms", [
        op("arrive_list_read_page", "ronghui.arrive_list.read_page", Effect.READ, page_input,
           page_output(_ARRIVE_FIELDS), operation="browser.invoke"),
        op("scan_read_page", "ronghui.scan.read_page", Effect.READ, page_input,
           page_output(_SCAN_SOURCE_FIELDS), operation="browser.invoke"),
        op("waybill_detail_read", "ronghui.waybill_detail.read", Effect.READ,
           _object({"tracking_number": _TEXT}),
           _object({"tracking_number": _TEXT, "found": _BOOL, "record": _record(_ARRIVE_FIELDS)},
                   ["tracking_number", "found"]), operation="browser.invoke"),
    ], kind=ConnectorBindingKind.ACCOUNT)

    def projection(name, action, effect, input_schema, output_schema):
        return op(name, action, effect, input_schema, output_schema, operation="projection.invoke")
    projection_ops = [
        projection("completed_before", "arrival.snapshot.completed_before", Effect.READ, date_input,
                   _object({"tracking_numbers": _array(_TEXT), "pagination_complete": _BOOL})),
        projection("scan_read", "scan.snapshot.read", Effect.READ, date_input,
                   _object({"items": _array(_record(_SCAN_READ_FIELDS)), "pagination_complete": _BOOL})),
        projection("pending_read", "waybill.pending.read", Effect.READ, date_input,
                   _object({"items": _array(_record(_PENDING_FIELDS)), "pagination_complete": _BOOL})),
        projection("scan_replace", "scan.snapshot.replace", Effect.INTERNAL_WRITE,
                   _records_input(_SCAN_SNAPSHOT_FIELDS), _projection_result(verified=_BOOL, identities_sha256=_TEXT)),
        projection("scan_cleanup", "scan.snapshot.cleanup", Effect.INTERNAL_WRITE,
                   _object({"retention_days": {"type": "integer", "minimum": 0, "maximum": 3650}}),
                   _object({"retention_days": _COUNT, "deleted": _COUNT, "committed": _BOOL, "skipped": _BOOL})),
        projection("waybill_replace", "waybill.snapshot.replace", Effect.INTERNAL_WRITE,
                   _records_input(_ARRIVE_FIELDS, optional=_ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS), _projection_result()),
        projection("arrival_replace", "arrival.snapshot.replace", Effect.INTERNAL_WRITE,
                   _records_input(_ARRIVAL_SNAPSHOT_FIELDS), _projection_result()),
        projection("split_pending_refresh", "split_pending.snapshot.refresh", Effect.INTERNAL_WRITE,
                   _records_input(_ARRIVAL_STATS_FIELDS, optional=_ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS), _projection_result()),
    ]
    descriptors = [tms, descriptor("arrival_stats_projection", projection_ops, kind=ConnectorBindingKind.HOST_INTERNAL)]
    for suffix in ("primary", "secondary", "pending", "archive", "split_pending"):
        role = f"arrival_stats_{suffix}_sheet"
        archive = suffix == "archive"
        fields = _PENDING_FIELDS if suffix == "pending" else _ARRIVAL_STATS_FIELDS
        result = _projection_result(verified=_BOOL) if archive else _object({
            "resource_slot": _TEXT, "record_count": _COUNT, "committed": _BOOL, "verified": _BOOL})
        descriptors.append(descriptor(role, [op("add" if archive else "replace",
            "feishu.sheet.add" if archive else "feishu.sheet.replace", Effect.EXTERNAL_WRITE,
            _records_input(fields, optional=() if suffix == "pending" else _ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS, slot=not archive),
            result, operation="network.request", role=role)], kind=ConnectorBindingKind.RESOURCE, role=role))
    return tuple(descriptors)

