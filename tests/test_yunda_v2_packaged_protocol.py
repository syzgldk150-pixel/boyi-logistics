import hashlib
from copy import deepcopy

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.yunda_connectors_v2 import build_yunda_connectors
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_automation_plugin_connector_runtime_v2 import _AccountResolver


def _host(tmp_path,plugin,*,empty=False):
    writes=[]
    normal = {"Logistics_Id":"YD-1","Buyer_Destination_Dot_Name":"目的网点","Buyer_Area_Name":"区县",
        "Shipping_Methods":"不上楼","Item_Total_Number":2,"Gross_Weight":"12.5","Settlement_Total_Number":"13",
        "Volume":"0.125","Special_Freight":"115.00","Payment_Type":"到付","Created_Dot_Code":"ISOLATED"}
    dispatch = {"ship_id":"YD-1","unit_cnt":"2","scan_cnt":1,"frgt_wgt":"12.50","frgt_vol":"0.125",
        "pkg_lod_typ":"纸箱","fld_tm":"2026-09-11 01:00:00","plan_tlns":"24","rcv_cust_addr":"测试地址",
        "est_arv_tm":"2026-09-11 08:00:00","due_delv_dt":"2026-09-11 12:00:00","新增字段":"可由插件更新读取"}
    def page(rows):
        return lambda *_:{"items":deepcopy(rows),"returned":len(rows),"total":len(rows)}
    def write(resource,records,day,ensure):
        writes.append((resource,deepcopy(records),day))
        return {"ok":True,"verified":True,"record_count":len(records),"readback_count":len(records),
            "readback_sha256":hashlib.sha256(canonical_json_bytes(records)).hexdigest(),"written":len(records),"deleted":0,
            "no_op":not records}
    def projection(records,day,account):
        result=write("projection",records,day,False)
        return {**result,"upserted":len(records),"deleted_stale":0}
    ports = FirstPartyCoreHandlerPorts(describe_account=lambda account:{"account_id":account,"system":"yunda","session_profile":"isolated"},
        yunda_dispatch_read_page=page([] if empty else [dispatch]),yunda_send_read_page=page([normal]),yunda_special_line_read_page=page([]),
        yunda_tracking_detail_read=lambda _,bill:{"Logistics_Id":bill,"Item_Name":"配件","Packing_Type":"纸箱","Extend_Field1":"200","COD":"115.00"},
        yunda_original_data_read=lambda _,bill:{"Sender_Name":"寄件人","Sender_Phone":"07310000000","Buyer_Name":"收货人","Buyer_Mobile":"13800000000","Buyer_Address":"测试地址"},
        yunda_renderer_detail_read=lambda *_:{"price":{"Total":"81.85"}},append_yunda_dispatch_bitable=write,
        replace_yunda_send_bitable=write,replace_yunda_send_sheet=write,replace_yunda_waybill_projection=projection)
    registry=ConnectorRegistry(build_yunda_connectors(build_first_party_core_handler_map(ports)))
    dispatch_plugin=plugin=="sync_yunda_dispatch_forecast_v2"
    prefix="yunda_dispatch" if dispatch_plugin else "yunda_send"
    resources={"dispatch_forecast_bitable":"feishu_bitable"} if dispatch_plugin else {"send_waybills_bitable":"feishu_bitable","send_waybills_sheet":"feishu_sheet"}
    class Resources:
        def require_active(self,*,resource_id,allowed_kinds):
            kind=resources[resource_id.removeprefix("isolated-")]
            assert kind in allowed_kinds
            return {"resource_id":resource_id,"kind":kind}
    context=CoreBrokerInvocationContext(automation_id="isolated-yunda",plugin_version="2.0.0",tool_name=plugin,
        operation="service.invoke",action="run",role="__system__",account_bindings={prefix+"_source":("isolated-yunda-account",)},
        resource_bindings={role:"isolated-"+role for role in resources})
    return PackagedConnectorHost(tmp_path,plugin,registry,context,resource_resolver=Resources(),account_resolver=_AccountResolver(system="yunda")),writes


@pytest.mark.parametrize("empty",[False,True])
def test_yunda_dispatch_zip_uses_raw_business_fields_and_verified_append(tmp_path,empty):
    host,writes=_host(tmp_path,"sync_yunda_dispatch_forecast_v2",empty=empty)
    result=host.execute({"target_date":"2026-09-11","dest_brch":"ISOLATED"},operation="run")
    assert result["status"]=="SUCCESS",result
    assert len(writes)==1
    if not empty:
        assert writes[0][1][0]["开单件数"]==2
        assert writes[0][1][0]["重量/kg"]=="12.5"


def test_yunda_send_zip_reads_two_sources_and_preserves_amounts_in_three_outputs(tmp_path):
    host,writes=_host(tmp_path,"sync_yunda_send_waybills_v2")
    result=host.execute({"target_date":"2026-09-11","sync_sql":True,"sync_sheet":True},operation="run")
    assert result["status"]=="SUCCESS",result
    assert len(writes)==3 and len(host.receipts)==3
    assert all(item[1][0]["提付"]=="115.00" and item[1][0]["中转运费"]=="81.85" for item in writes)
