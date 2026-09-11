"""Arrival-list and site-send infrastructure operations; filtering stays in ZIPs."""
from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import TEXT, DATE, BOOL, COUNT, CELL, CURSOR, object_schema as obj, array_schema as arr, record_schema as record
from agent.automation_plugins.first_party_handler_support import _ARRIVE_FIELDS, _SITE_FIELDS
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


def build_list_connectors(reviewed):
    descriptors = []
    for tool, prefix, fields, resources in (
        ("sync_arrive_list", "arrive_list", _ARRIVE_FIELDS,
         {"arrive_primary_sheet": "feishu_sheet", "arrive_secondary_sheet": "feishu_sheet"}),
        ("sync_site_send_list", "site_send", _SITE_FIELDS,
         {"site_send_bitable": "feishu_bitable", "site_send_sheet": "feishu_sheet"}),
    ):
        account_role = f"{prefix}_ronghui"
        account_service = f"connector.boyi.{account_role}@1"
        account = ConnectorRequirementContract(service=account_service, account_role=account_role,
                                                allowed_systems=("ronghui",), required=True)

        def operation(name, primitive, action, effect, inputs, outputs, role="account_id", resource=None):
            return ConnectorOperation(name=name, effect=effect, input_schema=inputs, output_schema=outputs,
                handler=reviewed_connector_handler(reviewed, tool=tool, operation=primitive, action=action,
                    role=role, account=account, resource=resource, account_alias="account_id"),
                max_input_bytes=64*1024*1024, max_output_bytes=10*1024*1024)

        page_result = {"items": arr(record(fields)), "next_cursor": CURSOR, "pagination_complete": BOOL}
        if prefix == "site_send":
            page_result["target_date"] = DATE
        page = operation("read_page", "browser.invoke", f"ronghui.{prefix}.read_page", Effect.READ,
            obj({"target_date": DATE, "cursor": CURSOR, "page_size": {"type": "integer", "minimum": 1, "maximum": 200}},
                ["target_date", "page_size"]), obj(page_result))
        descriptors.append(ConnectorDescriptor(service=account_service, title=prefix,
            binding_kind=Kind.ACCOUNT, account_role=account_role, allowed_systems=("ronghui",), operations=(page,)))
        if prefix == "arrive_list":
            inputs = obj({"records": arr(record(fields)), "target_date": DATE})
            outputs = obj({"target_date": DATE, "record_count": COUNT, "committed": BOOL})
            projections = tuple(operation(name, "projection.invoke", action, Effect.INTERNAL_WRITE, inputs, outputs)
                for name, action in (("waybill_replace", "waybill.snapshot.replace"),
                                     ("forecast_replace", "arrival.forecast_snapshot.replace")))
            descriptors.append(ConnectorDescriptor(service="connector.boyi.arrive_list_projection@1", title="到货投影",
                binding_kind=Kind.HOST_INTERNAL, account_role=None, allowed_systems=(), operations=projections))
        for role, kind in resources.items():
            service = f"connector.boyi.{role}@1"
            resource = ConnectorRequirementContract(service=service, binding_kind=Kind.RESOURCE,
                resource_role=role, allowed_resource_kinds=(kind,), required=True)
            operations = []
            result = {"record_count": COUNT, "committed": BOOL}
            if prefix == "arrive_list":
                operations.append(operation("publication_read", "projection.invoke", "arrival.report.publication.read", Effect.READ,
                    obj({"target_date": DATE}), obj({"target_date": DATE, "statistics_published": BOOL, "record_count": COUNT}), role, resource))
                result["resource_slot"] = TEXT
            if kind == "feishu_bitable":
                values = {"records": arr(obj({"fields": record(fields)})), "target_date": DATE}
                action = "feishu.bitable.replace_snapshot"
            else:
                values = {"values": arr(arr(CELL, maximum=len(fields))), "target_date": DATE}
                action = "feishu.sheet.replace"
                if prefix == "arrive_list":
                    values["resource_slot"] = TEXT
            operations.append(operation("replace", "network.request", action, Effect.EXTERNAL_WRITE,
                                        obj(values), obj(result), role, resource))
            descriptors.append(ConnectorDescriptor(service=service, title=role, binding_kind=Kind.RESOURCE,
                account_role=None, allowed_systems=(), resource_role=role, allowed_resource_kinds=(kind,),
                operations=tuple(operations)))
    return tuple(descriptors)
