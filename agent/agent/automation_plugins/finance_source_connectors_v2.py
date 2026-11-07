"""Finance source Connector: list sources, read pending bill files, submit contract-v1 batches."""
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import BOOL, COUNT, DATE, TEXT, array_schema as arr, object_schema as obj, source_record_schema
from agent.automation_plugins.finance_source_handlers import (
    LIST_ACTION, MAX_READ_BYTES, READ_FILE_ACTION, ROLE, SUBMIT_ACTION, TOOL, finance_source_broker_action_keys,
)
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler

SERVICE = "connector.boyi.finance_sources@1"
NULLABLE_TEXT = {"oneOf": [TEXT, {"type": "null"}]}
NULLABLE_COUNT = {"oneOf": [COUNT, {"type": "null"}]}


def build_finance_source_connectors(reviewed):
    file_item = obj({"id": COUNT, "file_name": TEXT, "content_type": TEXT, "size_bytes": COUNT, "sha256": TEXT, "uploaded_at": NULLABLE_TEXT})
    source_item = obj({"source_key": TEXT, "display_name": TEXT, "kind": TEXT, "provider": TEXT,
                       "covered_until": {"oneOf": [DATE, {"type": "null"}]}, "pending_files": arr(file_item, maximum=500)})
    read_input = obj({"file_id": {"type": "integer", "minimum": 1}, "offset": COUNT,
                      "length": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BYTES}})
    read_output = obj({"file_id": COUNT, "offset": COUNT, "size_bytes": COUNT, "sha256": TEXT, "data_base64": {"type": "string"},
                       "eof": BOOL})
    # Shape only; amounts, dates, identities and declared checks are validated in shared.finance.intake.contract.
    row = obj({"source_key": TEXT, "business_date": DATE, "posted_at": NULLABLE_TEXT, "amount": TEXT, "item_l1": NULLABLE_TEXT,
               "item_l2": NULLABLE_TEXT, "counterparty": NULLABLE_TEXT, "waybill_no": NULLABLE_TEXT, "reference": NULLABLE_TEXT,
               "balance_before": NULLABLE_TEXT, "balance_after": NULLABLE_TEXT, "raw": source_record_schema(depth=2)},
              ["source_key", "business_date", "amount"])
    summary_item = obj({"item_l1": TEXT, "item_l2": TEXT, "direction": TEXT, "count": COUNT, "amount": TEXT})
    batch = obj({"contract": TEXT, "source_key": TEXT, "period": obj({"start": DATE, "end": DATE}),
                 "declares": obj({"balance_chain": BOOL, "summary": BOOL, "opening_closing": BOOL, "complete_period": BOOL}),
                 "opening_balance": NULLABLE_TEXT, "closing_balance": NULLABLE_TEXT,
                 "summary": {"oneOf": [arr(summary_item, maximum=2000), {"type": "null"}]},
                 "rows": arr(row, maximum=50000),
                 "evidence": obj({"kind": TEXT, "refs": arr({"oneOf": [TEXT, COUNT]}, maximum=2000)})},
                ["contract", "source_key", "period", "declares", "rows", "evidence"])
    submit_input = obj({"batch": batch})
    submit_output = obj({"status": TEXT, "batch_id": NULLABLE_COUNT, "replayed": BOOL, "row_count": COUNT, "error_code": TEXT,
                         "message": TEXT, "superseded_batch_id": NULLABLE_COUNT, "posted_days": arr(DATE, maximum=400),
                         "kept_days": arr(DATE, maximum=400), "closed_period_days": arr(DATE, maximum=400)}, ["status", "batch_id"])
    specs = (
        ("list_sources", LIST_ACTION, Effect.READ, obj({}), obj({"sources": arr(source_item, maximum=200)}), 4 * 1024, 2 * 1024 * 1024),
        ("read_file", READ_FILE_ACTION, Effect.READ, read_input, read_output, 4 * 1024, 6 * 1024 * 1024),
        ("submit_batch", SUBMIT_ACTION, Effect.INTERNAL_WRITE, submit_input, submit_output, 64 * 1024 * 1024, 64 * 1024),
    )
    operations = tuple(ConnectorOperation(name=name, effect=effect, input_schema=inputs, output_schema=outputs,
        handler=reviewed_connector_handler(reviewed, tool=TOOL, operation=operation, action=action, role=ROLE),
        max_input_bytes=max_in, max_output_bytes=max_out)
        for name, (operation, action), effect, inputs, outputs, max_in, max_out in specs)
    return (ConnectorDescriptor(service=SERVICE, title="财务来源连接器", binding_kind=Kind.HOST_INTERNAL,
        account_role=None, allowed_systems=(), operations=operations),)


__all__ = ["SERVICE", "build_finance_source_connectors", "finance_source_broker_action_keys"]
