"""Real package algorithms across the Unix broker and production write checks."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4
import secrets

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.delivery_site_handlers import DeliverySiteHandlerPorts, build_delivery_site_handler_map
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from agent.automation_plugins.list_connectors_v2 import build_list_connectors
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_arrival_connectors_v2 import _setup


def _host(tmp_path, plugin, *, published=False, invalid_readback=False):
    writes = []
    _, _, row, _, _ = _setup()
    site = dict(tracking_number="R12345678901", send_site="发货站", package_type="纸箱", destination="目的站", pieces=2, weight=3)
    def page(rows):
        return lambda *_: {"items": deepcopy(rows), "returned": len(rows), "total": len(rows)}
    def project(records, day):
        writes.append(("projection", records, day))
        return {"ok":True,"verified":True,"record_count":len(records)}
    def sheet(resource, rows, day):
        writes.append((resource, rows, day))
        return {"ok":True,"verified":True,"record_count":len(rows)}
    def unexpected(*_):
        raise AssertionError("unrelated operation")
    ports = FirstPartyCoreHandlerPorts(describe_account=lambda account:{"account_id":account,"system":"ronghui","session_profile":"isolated"},
        arrive_list_read_page=page([row]), site_send_read_page=page([site,{**site,"tracking_number":"H_RECEIPT"}, {**site,"tracking_number":"R_EXCLUDED","send_site":"邵阳大祥站"}]),
        replace_waybill_snapshot=project,replace_arrival_forecast_snapshot=project,replace_arrive_sheet_resource=sheet,
        read_arrival_report_publication=lambda account, resource, day: {"target_date":day,"statistics_published":published,"record_count":int(published)})
    reviewed = build_first_party_core_handler_map(ports)
    def fresh_write(resource, rows, day, marker):
        marker()
        writes.append((resource, rows, day))
        return {"ok":True,"verified":not invalid_readback,"record_count":len(rows),"before_sha256":"a"*64,"after_sha256":"b"*64,
                "before_observation_id":str(uuid4()),"after_observation_id":str(uuid4()),"write_response_received":True}
    site_handlers = build_delivery_site_handler_map(DeliverySiteHandlerPorts(describe_account=ports.describe_account,
        site_bitable_replace=fresh_write, site_sheet_replace=fresh_write,
        delivery_bitable_write=unexpected, delivery_projection_update=unexpected),cursor_secret=secrets.token_bytes(32))
    original_sheet = reviewed[("network.request","feishu.sheet.replace")]
    reviewed[("network.request","feishu.sheet.replace")] = lambda context, args: (
        site_handlers[("network.request","feishu.sheet.replace")](context,args)
        if context.tool_name=="sync_site_send_list" else original_sheet(context,args))
    reviewed[("network.request","feishu.bitable.replace_snapshot")] = site_handlers[("network.request","feishu.bitable.replace_snapshot")]
    registry = ConnectorRegistry(build_list_connectors(reviewed))
    prefix = "arrive_list" if plugin=="sync_arrive_list_v2" else "site_send"
    resources = ({"arrive_primary_sheet":"feishu_sheet","arrive_secondary_sheet":"feishu_sheet"} if prefix=="arrive_list"
                 else {"site_send_bitable":"feishu_bitable","site_send_sheet":"feishu_sheet"})
    class Resources:
        def require_active(self, *, resource_id, allowed_kinds):
            role = resource_id.removeprefix("isolated-")
            assert resources[role] in allowed_kinds
            return {"resource_id":resource_id,"kind":resources[role]}
    context = CoreBrokerInvocationContext(automation_id="isolated-list",plugin_version="2.0.0",tool_name=plugin,
        operation="service.invoke",action="run",role="__system__",account_bindings={f"{prefix}_ronghui":("isolated-account",)},
        resource_bindings={role:f"isolated-{role}" for role in resources})
    return PackagedConnectorHost(tmp_path,plugin,registry,context,resource_resolver=Resources()), writes


@pytest.mark.parametrize("published",[False,True])
def test_arrive_list_zip_preserves_statistics_and_refreshes_forecast(tmp_path,published):
    host,writes = _host(tmp_path,"sync_arrive_list_v2",published=published)
    result = host.execute({"target_date":"2026-09-11"},operation="run")
    assert result["status"]=="SUCCESS",result
    sheets = [write for write in writes if write[0]!="projection"]
    assert len(sheets)==(0 if published else 2)
    assert result["data"]["bill_codes"]==1
    assert len([write for write in writes if write[0]=="projection"])==2
    assert all(write[2]=="2026-09-11" for write in writes)


@pytest.mark.parametrize("invalid_readback",[False,True])
def test_site_send_zip_filters_rows_and_requires_fresh_write_proof(tmp_path,invalid_readback):
    host,writes = _host(tmp_path,"sync_site_send_list_v2",invalid_readback=invalid_readback)
    result = host.execute({"target_date":"2026-09-11"},operation="run")
    if invalid_readback:
        assert result["status"]=="FAILED",result
        assert result["meta"]["write_outcome"]=="WRITE_OUTCOME_UNKNOWN"
        assert len(writes)==1
    else:
        assert result["status"]=="SUCCESS",result
        assert result["data"]["fetched"]==3 and result["data"]["normalized"]==1
        assert len(writes)==2 and len(host.receipts)==2
        assert writes[0][1][0]["fields"]["pieces"]==2
        assert writes[1][1][0][3]==2
