"""Account-bound R7 primitives; candidate selection lives in the plugin."""

from agent.automation_plugins.connector_registry import (
    ConnectorBindingKind,
    ConnectorBindingRef,
    ConnectorDescriptor,
    ConnectorInvocationError,
    ConnectorOperation,
)
from agent.automation_plugins.connector_schemas import BOOL, COUNT, TEXT, array_schema, object_schema
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from agent.tms_runtime.scripts.r7_vehicle_tasks import R7TaskError, R7VehicleClient

SERVICE = "connector.boyi.r7_vehicle_tasks@1"
ROLE = "r7_operator"


def build_r7_vehicle_connectors(client_factory=R7VehicleClient.from_account):
    def handler(operation):
        def invoke(binding, arguments):
            if not isinstance(binding, ConnectorBindingRef) or binding.system != "r7":
                raise ConnectorInvocationError("R7 account binding is invalid", code="BROKER_BINDING_INVALID")
            client = None
            try:
                client = client_factory(binding.account_id)
                return getattr(client, operation)(**arguments)
            except R7TaskError as exc:
                raise ConnectorInvocationError("R7 vehicle task operation failed", code=exc.code) from exc
            finally:
                if client is not None:
                    client.close()

        return invoke

    interval = {"start_time": TEXT, "end_time": TEXT}
    row = object_schema({"task_id": TEXT, "task_number": TEXT, "status": COUNT, "planned_departure": TEXT})
    operations = (
        ConnectorOperation(
            name="read_page",
            effect=CapabilityEffect.READ,
            input_schema=object_schema({**interval, "page": {"type": "integer", "minimum": 1, "maximum": 100}}),
            output_schema=object_schema(
                {"items": array_schema(row, maximum=200), "total": COUNT, "page": COUNT, "complete": BOOL}
            ),
            handler=handler("read_page"),
        ),
        ConnectorOperation(
            name="arrive",
            effect=CapabilityEffect.EXTERNAL_WRITE,
            input_schema=object_schema({**interval, "task_id": TEXT, "task_number": TEXT}),
            output_schema=object_schema(
                {"task_id": TEXT, "task_number": TEXT, "confirmed": BOOL, "arrival_time": TEXT, "status": COUNT}
            ),
            handler=handler("arrive"),
        ),
    )
    return (
        ConnectorDescriptor(
            service=SERVICE,
            title="R7运输任务查询及到达待卸",
            binding_kind=ConnectorBindingKind.ACCOUNT,
            account_role=ROLE,
            allowed_systems=("r7",),
            operations=operations,
        ),
    )
