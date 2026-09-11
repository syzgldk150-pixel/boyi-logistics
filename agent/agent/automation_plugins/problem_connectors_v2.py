"""Production self-pickup and split-pending primitives for Service v2."""
from __future__ import annotations

import base64
import binascii

from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import (
    ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorInvocationError, ConnectorOperation,
)
from agent.automation_plugins.connector_schemas import (
    BOOL, COUNT, TEXT, CELL, array_schema as array, object_schema as obj, record_schema as record,
)
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.problem_handlers import _SNAPSHOT_FIELDS, _SNAPSHOT_PUBLIC_FIELDS
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


_CODE = {"type":"string","minLength":1,"maxLength":128}
_HASH = {"type":"string","pattern":r"^[0-9a-f]{64}$"}
_NAME = {"type":"string","minLength":1,"maxLength":64}
_REF = {"type":"string","pattern":r"^problemref_[A-Za-z0-9_-]+$","maxLength":2048}


def _encode_precondition(data):
    result = dict(data)
    reference = result.get("precondition_ref")
    if not isinstance(reference,str) or not reference.startswith("problem:v1:"):
        raise ConnectorInvocationError("Problem precondition is invalid")
    result["precondition_ref"] = "problemref_" + base64.urlsafe_b64encode(reference.encode("ascii")).decode("ascii").rstrip("=")
    return result


def _decode_precondition(arguments):
    result = dict(arguments)
    reference = result["precondition_ref"]
    try:
        encoded = reference.removeprefix("problemref_")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode("ascii")
        if not raw.startswith("problem:v1:"):
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ConnectorInvocationError("Problem precondition is invalid") from exc
    result["precondition_ref"] = raw
    return result


def build_problem_connectors(reviewed):
    descriptors = []
    for self_pickup in (True, False):
        prefix = "self_pickup" if self_pickup else "split_pending"
        tool = "self_pickup_problem_upload" if self_pickup else "split_pending_problem_upload"
        maximum = 2000 if self_pickup else 5000
        source_role = f"{prefix}_source_sheet"
        target_role = "split_pending_target_sheet"

        def requirement(suffix, *, kind, role):
            return ConnectorRequirementContract(service=f"connector.boyi.{suffix}@1", binding_kind=kind,
                account_role=role if kind is Kind.ACCOUNT else None,
                allowed_systems=("ronghui",) if kind is Kind.ACCOUNT else (),
                resource_role=role if kind is Kind.RESOURCE else None,
                allowed_resource_kinds=("feishu_sheet",) if kind is Kind.RESOURCE else ())

        def op(name, operation, action, role, effect, input_schema, output_schema, *, account=None, resource=None, **transforms):
            return ConnectorOperation(name=name, effect=effect, input_schema=input_schema, output_schema=output_schema,
                handler=reviewed_connector_handler(reviewed,tool=tool,operation=operation,action=action,role=role,
                    account=account,resource=resource,account_alias=role if account is not None else None,**transforms),
                max_input_bytes=64*1024*1024,max_output_bytes=10*1024*1024)

        def descriptor(suffix, operations, *, kind, role=None):
            return ConnectorDescriptor(service=f"connector.boyi.{suffix}@1", title=suffix, binding_kind=kind,
                account_role=role if kind is Kind.ACCOUNT else None, allowed_systems=("ronghui",) if kind is Kind.ACCOUNT else (),
                resource_role=role if kind is Kind.RESOURCE else None, allowed_resource_kinds=("feishu_sheet",) if kind is Kind.RESOURCE else (),
                operations=tuple(operations))

        source = requirement(source_role,kind=Kind.RESOURCE,role=source_role)
        source_read = op("read_rows","network.request","feishu.sheet.read_rows",source_role,Effect.READ,
            obj({"end_column":{"type":"string","pattern":"^S$"},"max_rows":{"type":"integer","minimum":maximum,"maximum":maximum}}),
            obj({"complete":BOOL,"rows":array(array(CELL,maximum=19),maximum=maximum)}),resource=source)
        descriptors.append(descriptor(source_role,[source_read],kind=Kind.RESOURCE,role=source_role))

        account_services = [("self_pickup_primary_ronghui","account_id","self_pickup_primary"),("self_pickup_daxiang_s_ronghui","daxiang_s_account_id","self_pickup_daxiang_s")] if self_pickup else [("split_pending_ronghui","account_id","split_pending_ronghui")]
        for suffix, role, binding_role in account_services:
            account = requirement(suffix,kind=Kind.ACCOUNT,role=binding_role)
            query_fields = {"bill_code":_CODE}
            if not self_pickup:
                query_fields.update(problem_cause_sha256=_HASH,problem_owner_type=_NAME,problem_type=_NAME)
            query_output = obj({"bill_code":_CODE,"existing":BOOL,"precondition_ref":_REF,"ready":BOOL,
                "external_id":TEXT,"registered_at":TEXT},["bill_code","existing","precondition_ref","ready"])
            create_fields = {"bill_code":_CODE,"precondition_ref":_REF,"problem_cause":{"type":"string","minLength":1,"maxLength":2000},
                "problem_owner_type":_NAME,"problem_type":_NAME,"update_postpone_days":BOOL}
            verify_fields = {"bill_code":_CODE,"external_id":TEXT,"problem_cause_sha256":_HASH,"problem_owner_type":_NAME,"problem_type":_NAME}
            operations = [
                op("query" if self_pickup else "problem_query","browser.invoke","ronghui.problem.query",role,Effect.READ,
                    obj(query_fields),query_output,account=account,encode_result=_encode_precondition),
                op("create" if self_pickup else "problem_create","browser.invoke","ronghui.problem.create",role,Effect.EXTERNAL_WRITE,
                    obj(create_fields),obj({"bill_code":_CODE,"committed":BOOL,"external_id":TEXT,"postpone_updated":BOOL}),
                    account=account,decode_arguments=_decode_precondition),
                op("verify" if self_pickup else "problem_verify","browser.invoke","ronghui.problem.verify",role,Effect.READ,
                    obj(verify_fields),obj({**verify_fields,"confirmed":BOOL,"registered_at":TEXT,"registered_site":TEXT}),account=account),
            ]
            descriptors.append(descriptor(suffix,operations,kind=Kind.ACCOUNT,role=binding_role))

        if self_pickup:
            continue
        target = requirement(target_role,kind=Kind.RESOURCE,role=target_role)
        rows_schema = array(array(CELL,maximum=19),maximum=5001)
        descriptors.append(descriptor(target_role,[op("replace_rows","network.request","feishu.sheet.replace_rows",target_role,
            Effect.EXTERNAL_WRITE,obj({"rows":rows_schema}),obj({"committed":BOOL,"written":COUNT}),resource=target)],kind=Kind.RESOURCE,role=target_role))
        descriptors.append(descriptor("split_pending_projection",[
            op("snapshot_read","projection.invoke","split_pending.snapshot.read",target_role,Effect.READ,
                obj({"max_records":{"type":"integer","minimum":5000,"maximum":5000}}),
                obj({"complete":BOOL,"records":array(obj({key:TEXT for key in _SNAPSHOT_PUBLIC_FIELDS}),maximum=5000)}),resource=target),
            op("snapshot_replace","projection.invoke","split_pending.snapshot.replace",target_role,Effect.INTERNAL_WRITE,
                obj({"records":array(record(sorted(_SNAPSHOT_FIELDS)),maximum=5000)}),obj({"committed":BOOL,"record_count":COUNT}),resource=target),
            op("result_upsert","projection.invoke","split_pending.result.upsert",target_role,Effect.INTERNAL_WRITE,
                obj({"bill_code":_CODE,"complaint_status":_NAME,"problem_item_status":_NAME,"problem_type":_NAME}),
                obj({"committed":BOOL}),resource=target),
        ],kind=Kind.HOST_INTERNAL))
        descriptors.append(descriptor("split_pending_problem_ledger",[
            op("event_upsert","ledger.invoke","daily_sign.problem_event.upsert","account_id",Effect.INTERNAL_WRITE,
                obj({"bill_code":_CODE,"external_id":TEXT,"problem_type":_NAME,"registered_at":TEXT,"registered_site":TEXT}),
                obj({"committed":BOOL}),account=requirement("split_pending_problem_ledger",kind=Kind.ACCOUNT,role="split_pending_ronghui")),
        ],kind=Kind.ACCOUNT,role="split_pending_ronghui"))
    return tuple(descriptors)
