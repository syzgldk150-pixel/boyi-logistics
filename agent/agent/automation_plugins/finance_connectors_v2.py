"""Finance capture, independent verification and ledger primitives for V2."""
import base64
from collections.abc import Mapping

from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation, ConnectorInvocationError
from agent.automation_plugins.connector_schemas import TEXT, DATE, BOOL, COUNT, CURSOR, object_schema as obj, array_schema as arr, source_record_schema
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


ROLES = ("finance_quote_source", "finance_daxiang_s_source", "finance_self_pickup_source")
FINANCE_OPERATIONS = {
    "capture_page": ("browser.invoke", "ronghui.finance.capture_page", "read"),
    "verify_totals": ("browser.invoke", "ronghui.finance.verify_source_totals", "read"),
    "acquire_batch": ("ledger.invoke", "finance.batch.acquire", "internal_write"),
    "write_snapshot": ("ledger.invoke", "finance.source_snapshot.write", "internal_write"),
    "commit_projection": ("ledger.invoke", "finance.projection.commit", "internal_write"),
}
_REFERENCE_FIELDS = {"capture_ref", "source_context_ref", "run_ref"}


def _references(value, *, encode):
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if key in _REFERENCE_FIELDS and isinstance(item,str) and item:
                if encode:
                    if not item.startswith("finance:"):
                        raise ConnectorInvocationError("Finance primitive reference is invalid")
                    item = "financeref_" + base64.urlsafe_b64encode(item.encode()).decode().rstrip("=")
                else:
                    if not item.startswith("financeref_") or len(item)>2048:
                        raise ConnectorInvocationError("Finance reference is invalid")
                    try:
                        payload=item.removeprefix("financeref_")
                        item=base64.b64decode(payload+"="*(-len(payload)%4),altchars=b"-_",validate=True).decode()
                    except (ValueError,UnicodeError) as exc:
                        raise ConnectorInvocationError("Finance reference is invalid") from exc
                    if not item.startswith("finance:"):
                        raise ConnectorInvocationError("Finance reference is invalid")
                result[key]=item
            else:
                result[key]=_references(item,encode=encode)
        return result
    if isinstance(value,list):
        return [_references(item,encode=encode) for item in value]
    return value


def build_finance_connectors(reviewed):
    raw = source_record_schema(depth=3)
    scalar = source_record_schema()
    target = obj({"source_role":TEXT,"target_date":DATE})
    common = {"schema_version":COUNT}
    capture_in = obj({**common,"target_date":DATE,"page_number":COUNT,"page_size":COUNT,"capture_ref":CURSOR})
    capture_out = obj({**common,"capture_ref":TEXT,"source_context_ref":TEXT,"page_number":COUNT,"page_row_count":COUNT,
        "source_total":COUNT,"items":arr(raw),"pagination_complete":BOOL,"next_page_number":{"oneOf":[COUNT,{"type":"null"}]},
        "raw_source":raw},["schema_version","capture_ref","source_context_ref","page_number","page_row_count","source_total","items","pagination_complete","next_page_number"])
    verify_fields = {**common,"target_date":DATE,"capture_ref":TEXT,"source_context_ref":TEXT,"capture_sha256":TEXT,
        "transaction_count":COUNT,"page_row_counts":arr(COUNT),"computed_metrics":scalar}
    verify_in=obj({**verify_fields,"raw_source_sha256":TEXT,"transactions":arr(raw),"summaries":arr(raw)},list(verify_fields))
    verify_out=obj({**common,"verified":BOOL,"capture_ref":TEXT,"source_context_ref":TEXT,"capture_sha256":TEXT,
        "remote_total":COUNT,"summary_semantics":TEXT,"summaries":arr(raw),"observed_metrics":scalar})
    batch_in=obj({**common,"contract":raw,"contract_sha256":TEXT})
    batch_out=obj({**common,"acquired":BOOL,"batch_id":COUNT,"contract_sha256":TEXT,"targets":arr(target),"skipped_disabled_count":COUNT})
    snapshot_fields={**common,"batch_id":COUNT,"contract_sha256":TEXT,"target_date":DATE,"outcome":TEXT}
    snapshot_in=obj({**snapshot_fields,"failure":raw,"capture_ref":TEXT,"source_context_ref":TEXT,"transactions":arr(raw),
        "summaries":arr(raw),"validation":raw,"validation_sha256":TEXT},list(snapshot_fields))
    snapshot_out=obj({**common,"committed":BOOL,"batch_id":COUNT,"outcome":TEXT,"record_count":COUNT,"summary_count":COUNT,
        "written_row_count":COUNT,"run_ref":TEXT,"validation_sha256":{"oneOf":[TEXT,{"type":"null"}]},"new_fee_item_count":COUNT,"historical_revision_count":COUNT})
    commit_in=obj({**common,"batch_id":COUNT,"contract_sha256":TEXT,"outcomes":arr(raw)})
    commit_out=obj({**common,"committed":BOOL,"batch_id":COUNT,"contract_sha256":TEXT,"status":TEXT,
        "successful_runs":COUNT,"no_data_runs":COUNT,"failed_runs":COUNT,"written_record_count":COUNT})
    specs=(
        ("capture_page",capture_in,capture_out),
        ("verify_totals",verify_in,verify_out),
        ("acquire_batch",batch_in,batch_out),
        ("write_snapshot",snapshot_in,snapshot_out),
        ("commit_projection",commit_in,commit_out),
    )
    accounts=tuple(ConnectorRequirementContract(service=f"connector.boyi.{role}@1",account_role=role,
                    allowed_systems=("ronghui",),required=True) for role in ROLES)
    result=[]
    for account in accounts:
        operations=[]
        for name,inputs,outputs in specs:
            operation,action,effect = FINANCE_OPERATIONS[name]
            handler=reviewed_connector_handler(reviewed,tool="sync_finance_bills",operation=operation,action=action,role=account.account_role,
                account=account,additional_accounts=tuple(item for item in accounts if item!=account),
                decode_arguments=lambda value:_references(value,encode=False),encode_result=lambda value:_references(value,encode=True))
            operations.append(ConnectorOperation(name=name,effect=Effect(effect),input_schema=inputs,output_schema=outputs,handler=handler,
                max_input_bytes=64*1024*1024,max_output_bytes=10*1024*1024))
        result.append(ConnectorDescriptor(service=account.service,title=account.account_role,binding_kind=Kind.ACCOUNT,
            account_role=account.account_role,allowed_systems=("ronghui",),operations=tuple(operations)))
    return tuple(result)
