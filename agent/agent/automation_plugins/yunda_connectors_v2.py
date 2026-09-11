"""Yunda source and verified output primitives for independently shipped plugins."""
from agent.automation_plugins.broker import VERIFIED_WRITE_NOOP_FIELD
from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import TEXT, DATE, BOOL, COUNT, CURSOR, object_schema as obj, array_schema as arr, record_schema as record, source_record_schema
from agent.automation_plugins.first_party_handler_support import _YUNDA_DISPATCH_FIELDS, _YUNDA_SEND_FIELDS
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


def build_yunda_connectors(reviewed):
    result = []
    for tool, prefix, fields, resources in (
        ("sync_yunda_dispatch_forecast", "yunda_dispatch", _YUNDA_DISPATCH_FIELDS,
         {"dispatch_forecast_bitable":("feishu_bitable", "feishu.bitable.append_yunda_dispatch_forecast")}),
        ("sync_yunda_send_waybills", "yunda_send", _YUNDA_SEND_FIELDS,
         {"send_waybills_bitable":("feishu_bitable", "feishu.bitable.replace_yunda_send_waybills_date"),
          "send_waybills_sheet":("feishu_sheet", "feishu.sheet.replace_yunda_send_waybills")}),
    ):
        source_service = f"connector.boyi.{prefix}_source@1"
        role = f"{prefix}_source"
        account = ConnectorRequirementContract(service=source_service, account_role=role, allowed_systems=("yunda",), required=True)
        def op(name, operation, action, effect, inputs, outputs, *, source_role="account_id", resource=None):
            return ConnectorOperation(name=name, effect=effect, input_schema=inputs, output_schema=outputs,
                handler=reviewed_connector_handler(reviewed,tool=tool,operation=operation,action=action,role=source_role,
                    account=account,resource=resource,account_alias="account_id"),
                max_input_bytes=64*1024*1024,max_output_bytes=10*1024*1024)
        page_arguments = {"target_date":DATE,"cursor":CURSOR,"page_size":{"type":"integer","minimum":1,"maximum":200}}
        required = ["target_date","page_size"]
        if prefix=="yunda_dispatch":
            page_arguments["dest_brch"] = TEXT
            required.append("dest_brch")
        page_output = obj({"items":arr(source_record_schema(depth=2)),"next_cursor":CURSOR,"pagination_complete":BOOL})
        actions = (("read_page","yunda.dispatch_forecast.read_page"),) if prefix=="yunda_dispatch" else (
            ("send_page","yunda.send_waybill.list_page"),("special_line_page","yunda.special_line.list_page"))
        source_ops = [op(name,"browser.invoke",action,Effect.READ,obj(page_arguments,required),page_output) for name,action in actions]
        if prefix=="yunda_send":
            for name, action in (("tracking_detail","yunda.waybill.tracking_detail"),("original_data","yunda.waybill.original_data"),
                                 ("renderer_detail","yunda.send_waybill.renderer_detail")):
                inputs = {"bill_code":TEXT}
                if name=="renderer_detail": inputs["created_dot_code"] = TEXT
                source_ops.append(op(name,"browser.invoke",action,Effect.READ,obj(inputs,["bill_code"]),obj({"record":source_record_schema(depth=3)})))
        result.append(ConnectorDescriptor(service=source_service,title=prefix,binding_kind=Kind.ACCOUNT,
            account_role=role,allowed_systems=("yunda",),operations=tuple(source_ops)))
        sink_input = obj({"records":arr(record(fields)),"target_date":DATE,"ensure_fields":BOOL},["records","target_date"])
        common_result = {"target_date":DATE,"record_count":COUNT,"committed":BOOL,"verified":BOOL,"readback_count":COUNT,"readback_sha256":TEXT}
        for resource_role,(kind,action) in resources.items():
            service = f"connector.boyi.{resource_role}@1"
            resource = ConnectorRequirementContract(service=service,binding_kind=Kind.RESOURCE,resource_role=resource_role,
                allowed_resource_kinds=(kind,),required=True)
            optional = {key:COUNT for key in ("written","deleted","created_fields","skipped")}
            optional[VERIFIED_WRITE_NOOP_FIELD] = BOOL
            operation = op("commit","network.request",action,Effect.EXTERNAL_WRITE,sink_input,
                obj({**common_result,**optional},list(common_result)),source_role=resource_role,resource=resource)
            result.append(ConnectorDescriptor(service=service,title=resource_role,binding_kind=Kind.RESOURCE,
                account_role=None,allowed_systems=(),resource_role=resource_role,allowed_resource_kinds=(kind,),operations=(operation,)))
        if prefix=="yunda_send":
            output = obj({**common_result,**{key:COUNT for key in ("upserted","updates","creates","deleted_stale")}},list(common_result))
            operation = op("replace_date","projection.invoke","waybill.yunda.replace_date",Effect.INTERNAL_WRITE,sink_input,output)
            result.append(ConnectorDescriptor(service="connector.boyi.yunda_send_projection@1",title="韵达寄件投影",
                binding_kind=Kind.HOST_INTERNAL,account_role=None,allowed_systems=(),operations=(operation,)))
    return tuple(result)
