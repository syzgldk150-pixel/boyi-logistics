from copy import deepcopy
import asyncio
import threading
from uuid import uuid4

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.daily_send_connectors_v2 import build_daily_send_connectors
from agent.automation_plugins.daily_send_handlers import DailySendHandlerPorts, build_daily_send_handler_map
from agent.automation_plugins.delivery_connectors_v2 import build_delivery_connectors
from agent.automation_plugins.delivery_site_handlers import DeliverySiteHandlerPorts, build_delivery_site_handler_map
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from tests.service_v2_production_protocol_support import PackagedConnectorHost


def _host(tmp_path, plugin, registry, prefix, resource):
    class Resources:
        def require_active(self, *, resource_id, allowed_kinds):
            assert resource_id == 'isolated-target' and 'feishu_bitable' in allowed_kinds
            return {'resource_id':resource_id, 'kind':'feishu_bitable'}
    context = CoreBrokerInvocationContext(automation_id='isolated-sync',plugin_version='2.0.0',tool_name=plugin,
        operation='service.invoke',action='run',role='__system__',
        account_bindings={prefix+'_source':('isolated-account',)},resource_bindings={resource:'isolated-target'})
    return PackagedConnectorHost(tmp_path,plugin,registry,context,resource_resolver=Resources())


def test_daily_send_zip_replaces_only_target_date_and_matches_projection(tmp_path):
    records = [{'record_id':'old-target','fields':{'运单编号':'R-OLD','发件日期':1778515200000}},
               {'record_id':'keep-other-day','fields':{'运单编号':'R-KEEP','发件日期':1778601600000}}]
    row = {'BILL_CODE':'R001','REGISTER_DATE':'2026-05-12 08:00:00','PIECE_NUMBER':'2','GUEST_FREIGHT':'12.50',
        'BL_SIGNS_MARKING_TEXT':'已签收','GOODS_NAME':'配件','SCAN_TYPE':'签收扫描'}
    projections=[]
    deleted=[]
    def remove(resource, ids):
        deleted.extend(ids)
        records[:] = [record for record in records if record['record_id'] not in ids]
        return {'ok':True,'verified':True,'deleted':len(ids)}
    def write(resource, incoming):
        records.extend({'record_id':'new-'+str(index),'fields':deepcopy(record['fields'])} for index,record in enumerate(incoming))
        return {'ok':True,'verified':True,'written':len(incoming)}
    def project(incoming, day, descriptor):
        projections.extend(incoming)
        return {'ok':True,'verified':True,'upserted':len(incoming),'updates':0,'creates':len(incoming),'deleted_stale':0}
    ports = DailySendHandlerPorts(describe_account=lambda account:{'account_id':account,'system':'ronghui','session_profile':'isolated'},
        source_page=lambda *_:{'items':[row,{'BILL_CODE':'H001','REGISTER_DATE':'2026-05-12 09:00:00'}],'total':2},
        bitable_list=lambda resource,offset,size,fields:[{'record_id':item['record_id'],'fields':{key:item['fields'].get(key) for key in fields}} for item in records[offset:offset+size]],
        bitable_delete=remove,bitable_write=write,projection_replace=project)
    registry=ConnectorRegistry(build_daily_send_connectors(build_daily_send_handler_map(ports,cursor_secret=b'd'*32)))
    host=_host(tmp_path,'sync_daily_send_orders_v2',registry,'daily_send','send_order_bitable')
    result=host.execute({'target_date':'2026-05-12'},operation='run')
    assert result['status']=='SUCCESS',result
    assert deleted==['old-target']
    assert {item['fields']['运单编号'] for item in records}=={'R001','R-KEEP'}
    assert len(projections)==1 and projections[0]['quantity_lines']=='2'
    assert projections[0]['freight_fee']=='12.50'
    # A completed invocation releases its own synchronization scope.
    again=host.execute({'target_date':'2026-05-12'},operation='run')
    assert again['status']=='SUCCESS',again


def test_cancelled_daily_send_zip_releases_scope_after_its_host_call_finishes(tmp_path):
    started, finish = threading.Event(), threading.Event()
    source_calls = []
    records = []
    def source(*_):
        source_calls.append(True)
        started.set()
        assert finish.wait(5), "isolated source was not released"
        return {"items": [{"BILL_CODE": "R001", "REGISTER_DATE": "2026-05-12 08:00:00", "PIECE_NUMBER": "2"}], "total": 1}
    def write(_resource, rows):
        records.extend({"record_id": "new-" + str(index), "fields": deepcopy(row["fields"])} for index, row in enumerate(rows))
        return {"ok": True, "verified": True, "written": len(rows)}
    ports = DailySendHandlerPorts(
        describe_account=lambda account: {"account_id": account, "system": "ronghui", "session_profile": "isolated"},
        source_page=source, bitable_list=lambda resource, offset, size, fields: [
            {"record_id": item["record_id"], "fields": {key: item["fields"].get(key) for key in fields}} for item in records[offset:offset+size]],
        bitable_delete=lambda *_: pytest.fail("cancelled call must not write records"),
        bitable_write=write,
        projection_replace=lambda rows, *_: {"ok": True, "verified": True, "upserted": len(rows), "updates": 0, "creates": len(rows), "deleted_stale": 0},
    )
    registry = ConnectorRegistry(build_daily_send_connectors(build_daily_send_handler_map(ports, cursor_secret=b"c" * 32)))
    host = _host(tmp_path, "sync_daily_send_orders_v2", registry, "daily_send", "send_order_bitable")
    async def cancel():
        task = asyncio.create_task(host._execute({"target_date": "2026-05-12"}, operation="run", entrypoint="console"))
        try:
            assert await asyncio.to_thread(started.wait, 5), "package never reached its actual source connector"
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done(), "cancellation released the scope while the Host call still ran"
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    result = host.execute({"target_date": "2026-05-12"}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert len(source_calls) == 2


@pytest.mark.parametrize('preview',[False,True])
def test_delivery_zip_classifies_signed_rows_and_verifies_both_outputs(tmp_path,preview):
    writes=[]
    def unexpected(*_):
        raise AssertionError('unrelated site operation')
    def fresh(rows, marker):
        marker()
        writes.append(deepcopy(rows))
        return {'ok':True,'verified':True,'record_count':len(rows),'before_sha256':'a'*64,'after_sha256':'b'*64,
                'before_observation_id':str(uuid4()),'after_observation_id':str(uuid4()),'write_response_received':True}
    describe=lambda account:{'account_id':account,'system':'ronghui','session_profile':'isolated'}
    readers=build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(describe_account=describe,
        delivery_list_views=lambda *_:[{'view_id':'pending-view','view_name':'未签收明细'}],
        delivery_list_records=lambda *_:{'items':[{'record_id':'rec1','waybill_no':'R001','status':'未签收'},
                                                {'record_id':'rec2','waybill_no':'R002','status':'未签收'}],'returned':2,'total':2},
        delivery_status_read=lambda *_:[{'bill_code':'R001','status':'签收'},{'bill_code':'R002','status':'运输中'}]))
    readers.update(build_delivery_site_handler_map(DeliverySiteHandlerPorts(describe_account=describe,
        site_bitable_replace=unexpected,site_sheet_replace=unexpected,
        delivery_bitable_write=lambda resource,rows,marker:fresh(rows,marker),
        delivery_projection_update=lambda codes,status,marker:fresh(list(codes),marker)),cursor_secret=b's'*32))
    registry=ConnectorRegistry(build_delivery_connectors(readers))
    host=_host(tmp_path,'sync_delivery_status_v2',registry,'delivery','delivery_status_bitable')
    result=host.execute({},operation='preview' if preview else 'run',entrypoint='harness' if preview else 'console')
    assert result['status']=='SUCCESS',result
    assert result['data']['queried']==2 and result['data']['updated']==1 and result['data']['unchanged']==1
    if preview:
        assert not writes and not host.receipts
    else:
        assert writes==[[{'record_id':'rec1','status':'已签收'}],['R001']]
        assert len(host.receipts)==2


@pytest.mark.parametrize("has_pending", [False, True])
def test_delivery_zip_no_change_completes_with_read_evidence_and_no_write(tmp_path, has_pending):
    rows = [{"record_id":"rec1", "waybill_no":"R001", "status":"未签收"}] if has_pending else []
    def unexpected(*_):
        pytest.fail("No-change delivery run must not invent a write")
    describe = lambda account: {"account_id":account, "system":"ronghui", "session_profile":"isolated"}
    readers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
        describe_account=describe,
        delivery_list_views=lambda *_: [{"view_id":"pending-view", "view_name":"未签收明细"}],
        delivery_list_records=lambda *_: {"items":rows, "returned":len(rows), "total":len(rows)},
        delivery_status_read=lambda *_: [{"bill_code":"R001", "status":"运输中"}],
    ))
    readers.update(build_delivery_site_handler_map(DeliverySiteHandlerPorts(
        describe_account=describe, site_bitable_replace=unexpected, site_sheet_replace=unexpected,
        delivery_bitable_write=unexpected, delivery_projection_update=unexpected), cursor_secret=b"s"*32))
    host = _host(tmp_path, "sync_delivery_status_v2", ConnectorRegistry(build_delivery_connectors(readers)),
                 "delivery", "delivery_status_bitable")
    result = host.execute({}, operation="run")
    assert result["status"] == "SUCCESS" and result["data"]["updated"] == 0
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"
    assert host.observations and not host.receipts
    verified = host.verify(result, {}, None, "external_write")
    assert verified.accepted and verified.run_status.value == "COMPLETED"
    assert not host.verification_settlements
    # A fabricated write claim and missing source observations remain failures.
    forged = deepcopy(result)
    forged["meta"]["write_outcome"] = "WRITE_VERIFIED"
    assert not host.verify(forged, {}, None, "external_write", expect_success=False).accepted
    host.observations = ()
    assert not host.verify(result, {}, None, "external_write", expect_success=False).accepted
