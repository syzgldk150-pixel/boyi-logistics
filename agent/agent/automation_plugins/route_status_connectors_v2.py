"""Host-internal projection port for refreshing Ronghui in-transit route text."""
from agent.automation_plugins.connector_registry import ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation
from agent.automation_plugins.connector_schemas import BOOL, COUNT, TEXT, array_schema as arr, object_schema as obj
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler
from agent.automation_plugins.route_status_handlers import MAX_CANDIDATES, PENDING_ACTION, ROLE, TOOL, UPDATE_ACTION

SERVICE = "connector.boyi.ronghui_route_projection@1"


def route_status_broker_action_keys():
    return frozenset({PENDING_ACTION, UPDATE_ACTION})


def build_route_status_connectors(reviewed):
    limit = {"type": "integer", "minimum": 1, "maximum": MAX_CANDIDATES}
    route = {"type": "string", "minLength": 1, "maxLength": 128}
    specs = (
        ("list_pending", PENDING_ACTION, Effect.READ, obj({"limit": limit}),
         obj({"items": arr(obj({"waybill_no": TEXT, "scan_status": TEXT}), maximum=MAX_CANDIDATES), "truncated": BOOL})),
        ("update_routes", UPDATE_ACTION, Effect.INTERNAL_WRITE,
         obj({"items": arr(obj({"waybill_no": TEXT, "scan_status": route, "signed": BOOL}), maximum=MAX_CANDIDATES)}),
         obj({"committed": BOOL, "record_count": COUNT, "updated": COUNT})),
    )
    operations = tuple(ConnectorOperation(name=name, effect=effect, input_schema=inputs, output_schema=outputs,
        handler=reviewed_connector_handler(reviewed, tool=TOOL, operation=operation, action=action, role=ROLE),
        max_input_bytes=2 * 1024 * 1024, max_output_bytes=2 * 1024 * 1024)
        for name, (operation, action), effect, inputs, outputs in specs)
    return (ConnectorDescriptor(service=SERVICE, title="融辉在途轨迹投影", binding_kind=Kind.HOST_INTERNAL,
        account_role=None, allowed_systems=(), operations=operations),)
