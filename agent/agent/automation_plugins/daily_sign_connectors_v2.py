"""Infrastructure contracts for the package-owned daily-sign workflow."""
from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import CELL, TEXT, array_schema as arr, object_schema as obj, source_record_schema
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


DAILY_SIGN_PORTS = {
    "daily_sign_r13": ("account", "r13", {"read_r13":"read", "source_scope":"read", "describe":"read"}),
    "daily_sign_tms": ("account", "ronghui", {"read_problems":"read", "read_signs":"read", "read_tracking":"read", "read_details":"read", "source_scope":"read", "describe":"read"}),
    "daily_sign_store": ("internal", "", {"start_run":"internal_write", "load_state":"read", "earliest_date":"read", "build_marker":"read", "persist_snapshot":"internal_write", "verify_snapshot":"read", "finish_run":"internal_write", "verify_run":"read"}),
    "daily_sign_bitable": ("resource", "feishu_bitable", {"list_fields":"read", "update_field":"external_write", "create_field":"external_write", "list_records":"read", "write_records":"external_write", "delete_records":"external_write"}),
    "daily_sign_sheet": ("resource", "feishu_sheet", {"describe":"read", "read_sheet":"read", "write_sheet":"external_write", "clear_sheet":"external_write"}),
}


def daily_sign_broker_action_keys():
    return frozenset(("daily_sign.port", f"{role}.{name}") for role, (_, _, operations) in DAILY_SIGN_PORTS.items() for name in operations)


def _schemas(role, name):
    raw = source_record_schema(depth=3)
    rows = arr(source_record_schema(depth=3), maximum=100000)
    value = {"oneOf": [CELL, raw, rows]}
    inputs, outputs = raw, value
    if role == "daily_sign_store":
        if name in {"start_run", "load_state", "earliest_date"}:
            inputs = obj({})
        if name == "load_state":
            # Keyed ledger maps travel as rows, avoiding a small object-width
            # limit on the number of waybills. Keys are reconstructed exactly.
            outputs = obj({"ledger":rows, "arrivals":rows, "problems":rows, "signs":rows,
                "sign_verifications":rows, "target_station_codes":arr(TEXT, maximum=100000),
                "source_refs":arr(TEXT, maximum=100000), "arrival_source_proof":raw})
        if name in {"build_marker", "persist_snapshot", "verify_snapshot"}:
            fields = {key:rows for key in ("problem_events", "sign_events", "ledger_rows", "sign_verification_states", "publication_rows")}
            if name != "build_marker":
                fields.update(run_id=TEXT, persistence_marker=raw)
            inputs = obj(fields)
        if name in {"finish_run", "verify_run"}:
            completion = source_record_schema(depth=2)
            diagnostics = source_record_schema(depth=3)
            diagnostics["properties"]["excluded_child_candidate_codes"] = arr(TEXT, maximum=100000)
            completion["properties"]["diagnostics_json"] = diagnostics
            inputs = obj({"run_id":TEXT, "values" if name == "finish_run" else "expected_values":completion})
    elif name.startswith("read_") and role in {"daily_sign_r13", "daily_sign_tms"}:
        # R13 and sign gateways return {data: rows}; keep their full page size.
        gateway = source_record_schema(depth=3)
        gateway["properties"]["data"] = {"oneOf":[rows,raw]}
        outputs = {"oneOf":[rows,gateway]}
    elif role == "daily_sign_bitable":
        inputs["properties"].update(records=rows, record_ids=arr(TEXT, maximum=100000))
        result = source_record_schema(depth=3)
        result["properties"]["items"] = rows
        outputs = result
    elif role == "daily_sign_sheet":
        matrix = arr(arr(CELL, maximum=256), maximum=100000)
        inputs["properties"]["values"] = matrix
        outputs = source_record_schema(depth=3)
        value_range = obj({"values":matrix}, required=[])
        outputs["properties"].update(values=matrix, valueRange=value_range,
            data=obj({"valueRange":value_range, "values":matrix}, required=[]))
    return obj({"values":inputs}), obj({"value":outputs})


def build_daily_sign_connectors(reviewed):
    # The source contracts preserve business fields for package-only adapters.
    # Nested values remain typed/bounded and use the registry's credential checks.
    descriptors = []
    for role, (kind, system, operations) in DAILY_SIGN_PORTS.items():
        service = f"connector.boyi.{role}@1"
        account = ConnectorRequirementContract(service=service, account_role=role, allowed_systems=(system,), required=True) if kind == "account" else None
        resource = ConnectorRequirementContract(service=service, binding_kind=Kind.RESOURCE, resource_role=role, allowed_resource_kinds=(system,), required=True) if kind == "resource" else None
        descriptors.append(ConnectorDescriptor(service=service, title="每日应签数据接口",
            binding_kind=Kind.ACCOUNT if account else Kind.RESOURCE if resource else Kind.HOST_INTERNAL,
            account_role=role if account else None, allowed_systems=(system,) if account else (),
            resource_role=role if resource else None, allowed_resource_kinds=(system,) if resource else (),
            operations=tuple(ConnectorOperation(name=name, effect=Effect(effect),
                input_schema=_schemas(role, name)[0], output_schema=_schemas(role, name)[1],
                handler=reviewed_connector_handler(reviewed, tool="sync_daily_should_sign", operation="daily_sign.port",
                    action=f"{role}.{name}", role=role, account=account, resource=resource),
                max_input_bytes=32*1024*1024, max_output_bytes=10*1024*1024)
                for name, effect in operations.items())))
    return tuple(descriptors)
