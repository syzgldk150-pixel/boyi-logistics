"""Production scan primitives with bound accounts and server readback."""
from agent.automation_plugins.connector_compatibility import ConnectorRequirementContract
from agent.automation_plugins.connector_registry import (
    ConnectorBindingKind as Kind, ConnectorDescriptor, ConnectorOperation,
)
from agent.automation_plugins.connector_schemas import (
    BOOL, COUNT, CURSOR, DATE, TEXT, array_schema as array, object_schema as obj,
)
from agent.automation_plugins.first_party_handler_support import _SCAN_SOURCE_FIELDS, _SCAN_SNAPSHOT_FIELDS
from agent.automation_plugins.host_capability_registry import CapabilityEffect as Effect
from agent.automation_plugins.reviewed_connectors import reviewed_connector_handler


def build_scan_connectors(reviewed):
    account = ConnectorRequirementContract(service="connector.boyi.scan_ronghui@1",
        binding_kind=Kind.ACCOUNT,account_role="scan_ronghui",allowed_systems=("ronghui",))
    code = {"type":"string","minLength":1,"maxLength":128}
    digest = {"type":"string","pattern":r"^[0-9a-f]{64}$"}
    receipt = {"operation_id":{"type":"string","minLength":1,"maxLength":2048},
        "items_sha256":digest,"submitted":COUNT,"scanned":COUNT,"skipped_signed_codes":array(code,maximum=200)}

    def operation(name, action, effect, input_schema, output_schema, *, projection=False):
        return ConnectorOperation(name=name,effect=effect,input_schema=input_schema,output_schema=output_schema,
            handler=reviewed_connector_handler(reviewed,tool="sync_scan_codes",
                operation="projection.invoke" if projection else "browser.invoke",action=action,role="account_id",
                account=account,account_alias="account_id"),max_input_bytes=64*1024*1024,max_output_bytes=10*1024*1024)

    return (
        ConnectorDescriptor(service=account.service,title="融辉扫描",binding_kind=Kind.ACCOUNT,
            account_role=account.account_role,allowed_systems=account.allowed_systems,operations=(
                operation("read_page","ronghui.scan.read_page",Effect.READ,
                    obj({"target_date":DATE,"page_size":{"type":"integer","minimum":1,"maximum":200},"cursor":CURSOR},["target_date","page_size"]),
                    obj({"items":array(obj({field:TEXT for field in _SCAN_SOURCE_FIELDS}),maximum=200),"next_cursor":CURSOR,"pagination_complete":BOOL})),
                operation("submit","ronghui.scan_next.submit",Effect.EXTERNAL_WRITE,
                    obj({"items":{**array(obj({"bill_code":code,"station_name":TEXT}),maximum=200),"minItems":1}}),
                    obj({**receipt,"postcondition":{"type":"string","pattern":"^uploaded_and_table_cleared$"}})),
                operation("verify","ronghui.scan_next.verify",Effect.READ,obj(receipt),
                    obj({**{key:value for key,value in receipt.items() if key!="operation_id"},"verified":BOOL,
                        "postcondition":{"type":"string","pattern":"^server_ledger_verified$"},"readback_count":COUNT})),
            )),
        ConnectorDescriptor(service="connector.boyi.scan_projection@1",title="扫描快照",binding_kind=Kind.HOST_INTERNAL,account_role=None,allowed_systems=(),
            operations=(operation("snapshot_replace","scan.snapshot.replace",Effect.INTERNAL_WRITE,
                obj({"target_date":DATE,"records":array(obj({field:TEXT for field in _SCAN_SNAPSHOT_FIELDS}))}),
                obj({"target_date":DATE,"record_count":COUNT,"committed":BOOL,"verified":BOOL,"identities_sha256":digest}),projection=True),)),
    )
