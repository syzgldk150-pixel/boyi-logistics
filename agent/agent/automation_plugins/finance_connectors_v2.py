"""Statement capture Connectors: the three finance roles write, publish and finalize."""

from agent.connector_namespaces import FINANCE_STATEMENT_TOOL
from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import TEXT, DATE, BOOL, COUNT, object_schema as obj, array_schema as arr, source_record_schema
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


ROLES = ("finance_quote_source", "finance_daxiang_s_source", "finance_self_pickup_source")
FINANCE_OPERATIONS = {
    "write_platform_capture": ("ledger.invoke", "finance.platform_capture.write", "internal_write"),
    "publish_capture_day": ("ledger.invoke", "finance.platform_capture.publish_day", "internal_write"),
    "finalize_capture": ("ledger.invoke", "finance.platform_capture.finalize", "internal_write"),
}


def finance_platform_capture_broker_action_keys():
    """Host primitives of the statement capture."""
    return frozenset(operation[:2] for operation in FINANCE_OPERATIONS.values())


def _json_value(depth):
    """Any JSON up to ``depth`` levels; platform pages are kept exactly as returned."""
    scalar={"oneOf":[TEXT,{"type":"number"},{"type":"null"},BOOL]}
    if depth<=0:
        return scalar
    inner=_json_value(depth-1)
    return {"oneOf":[scalar,{"type":"object","properties":{},"required":[],"additionalProperties":inner,
                             "maxProperties":512,"propertyNames":{"type":"string","minLength":1,"maxLength":128}},
                     {"type":"array","items":inner,"maxItems":5000}]}


def build_finance_connectors(reviewed):
    raw = source_record_schema(depth=3)
    scalar = source_record_schema()
    common = {"schema_version":COUNT}
    page=obj({"request":TEXT,"body":scalar,"evidence_ref":TEXT,"response":_json_value(6)})
    platform_in=obj({**common,"period_start":DATE,"period_end":DATE,"field_mapping":raw,"request_profile":raw,
        "statement_pages":arr(page),"billing_statement_pages":arr(page),"detail_pages":arr(page)})
    platform_out=obj({**common,"capture_key":TEXT,"batch_id":COUNT,"site_code":TEXT,"site_name":TEXT,
        "days":arr(DATE),"content_sha256":TEXT,"metrics":scalar})
    day_in=obj({**common,"capture_key":TEXT,"business_date":DATE})
    day_out=obj({**common,"capture_key":TEXT,"business_date":DATE,"outcome":TEXT,"record_count":COUNT,
        "new_fee_item_count":COUNT})
    finalize_in=obj({**common,"capture_key":TEXT})
    finalize_out=obj({**common,"capture_key":TEXT,"batch_id":COUNT,"status":TEXT,"published_days":COUNT,
        "record_count":COUNT})
    specs=(
        ("write_platform_capture",platform_in,platform_out),
        ("publish_capture_day",day_in,day_out),
        ("finalize_capture",finalize_in,finalize_out),
    )
    accounts=tuple(ConnectorRequirementContract(service=f"connector.boyi.{role}@1",account_role=role,
                    allowed_systems=("ronghui",),required=True) for role in ROLES)
    result=[]
    for account in accounts:
        operations=[]
        for name,inputs,outputs in specs:
            operation,action,effect = FINANCE_OPERATIONS[name]
            handler=reviewed_connector_handler(reviewed,tool=FINANCE_STATEMENT_TOOL,operation=operation,action=action,role=account.account_role,
                account=account,additional_accounts=tuple(item for item in accounts if item!=account))
            operations.append(ConnectorOperation(name=name,effect=Effect(effect),input_schema=inputs,output_schema=outputs,handler=handler,
                max_input_bytes=64*1024*1024,max_output_bytes=10*1024*1024))
        result.append(ConnectorDescriptor(service=account.service,title=account.account_role,binding_kind=Kind.ACCOUNT,
            account_role=account.account_role,allowed_systems=("ronghui",),operations=tuple(operations)))
    return tuple(result)
