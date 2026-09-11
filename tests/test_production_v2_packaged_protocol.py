from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_arrival_connectors_v2 import _setup
from tests.test_scan_connectors_v2 import _host
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest

from agent.orchestration.direct_invocation_previews import confirm_preview
from agent.automation_plugins.manifest import canonical_json_bytes
from tests.test_problem_connectors_v2 import _host as problem_host
from tests.test_problem_plugin_core_handlers import _problem_result


def _confirm(host, preview, arguments, *, scan):
    invocation_id = str(uuid4())
    contract = SimpleNamespace(automation_generation=1,contract_hash="c"*64,project_configuration_version=1)
    entry = SimpleNamespace(automation_id=host.context.automation_id,plugin_id=host.manifest.plugin_id,runtime_model="SERVICE_V2")
    row = {"status":"COMPLETED","automation_id":entry.automation_id,"generation":1,"actor_id":"isolated-user",
        "invocation_json":{"contract_hash":contract.contract_hash,"project_configuration_version":1},
        "result_json":preview,"arguments_json":{**arguments,"dry_run":True}}
    return confirm_preview(SimpleNamespace(get=lambda identifier: row if identifier==invocation_id else None),invocation_id,
        entry=entry,contract=contract,actor_id="isolated-user",arguments=arguments,
        selected_bill_codes=None if scan else [row["bill_code"] for row in preview["data"]["candidates"]],scan=scan)


def test_arrival_real_zip_process_writes_calculated_counts_through_production_broker(tmp_path):
    registry, context, row, calls, writes = _setup()
    host = PackagedConnectorHost(tmp_path,"sync_arrival_stats_v2",registry,context)
    result = host.execute({"target_date":"2026-09-11","pending_sheet_disabled":False},operation="run")
    assert result["status"] == "SUCCESS", result
    assert host.receipts and writes
    primary = [item for item in writes if item[0]=="isolated-primary-table"]
    assert len(primary) == 1
    assert primary[0][2][0]["quantity"] == 2


@pytest.mark.parametrize("entrypoint", ["console", "webhook"])
def test_scan_real_zip_console_preview_stays_read_only(tmp_path, entrypoint):
    rows = [{"bill_code":"R123456789010001","destination":"A站","scan_type":"到货",
             "scan_time":"2026-09-11 08:00:00","scan_site":"大祥S站"}]
    fixture = _host(scan_read_page=lambda *_:{"items":rows,"returned":1,"total":1,"total_authoritative":True})
    host = PackagedConnectorHost(tmp_path,"sync_scan_codes_v2",fixture.registry,fixture.context)
    result = host.execute({"target_date":"2026-09-11","dry_run":True},operation="preview",entrypoint=entrypoint)
    assert result["status"] == "SUCCESS", result
    assert result["data"]["dry_run"] is True
    assert not host.receipts


@pytest.mark.parametrize("entrypoint", ["console", "webhook"])
def test_scan_zip_formal_uses_host_confirmed_plan_and_fresh_readback(tmp_path, entrypoint):
    row = {"bill_code":"R123456789010001","destination":"A站","scan_type":"到货",
           "scan_time":"2026-09-11 08:00:00","scan_site":"大祥S站"}
    writes = []
    def snapshot(records, day):
        writes.append(("snapshot",records))
        return {"ok":True,"verified":True,"record_count":len(records),"readback_count":len(records),
            "identities_sha256":hashlib.sha256(canonical_json_bytes(sorted(records,key=lambda row:row["raw_code"]))).hexdigest()}
    def submit(_account,items):
        writes.append(("scan",items))
        return {"ok":True,"stage":"done","write_started_at":"2026-09-11T00:00:00+00:00",
            "write_finished_at":"2026-09-11T00:00:05+00:00","detail":{"items":items,
            "stations":[{"station_name":"A站","count":len(items),"bill_codes":[item["bill_code"] for item in items]}],
            "total_scanned":len(items),"skipped_signed_codes":[]}}
    def verify(_account,items,start,end):
        assert items == writes[-1][1]
        writes.append(("readback",items))
        return {"ok":True,"verified":True,"record_count":len(items),
                "identities_sha256":hashlib.sha256(canonical_json_bytes(items)).hexdigest()}
    fixture = _host(scan_read_page=lambda *_:{"items":[row],"returned":1,"total":1,"total_authoritative":True},
        replace_scan_snapshot=snapshot,scan_next_submit=submit,scan_next_verify=verify)
    host = PackagedConnectorHost(tmp_path,"sync_scan_codes_v2",fixture.registry,fixture.context)
    arguments = {"target_date":"2026-09-11"}
    preview = host.execute({**arguments,"dry_run":True},operation="preview",entrypoint=entrypoint)
    assert preview["status"] == "SUCCESS", preview
    formal = _confirm(host,preview,arguments,scan=True)
    result = host.execute(formal,operation="execute",entrypoint=entrypoint)
    assert result["status"] == "SUCCESS", (result, writes)
    assert [item[0] for item in writes] == ["snapshot","scan","readback"]
    assert len(host.receipts) == 2  # Each actual write starts exactly once.


def test_self_pickup_zip_preview_selects_both_accounts_and_confirms_real_operations(tmp_path):
    fixture_path = Path(__file__).parent/"fixtures/service_v2/self_pickup_problem_upload_v2/self_pickup_case.json"
    rows = json.loads(fixture_path.read_text())["rows"]
    operations = []
    def action(account, operation, plan):
        operations.append((account["account_id"],operation,plan["bill_code"]))
        if operation=="query":
            return {"ready":True,"existing":None}
        return {**_problem_result(plan,confirmed=operation=="verify"),"postpone_updated":True}
    fixture = problem_host("self_pickup_problem_upload",sheet_rows_read=lambda *_:{"complete":True,"rows":rows},problem_action=action)
    host = PackagedConnectorHost(tmp_path,"self_pickup_problem_upload_v2",fixture.registry,fixture.context)
    arguments = {"include_daxiang_s_self_pickup":True}
    preview = host.execute({**arguments,"dry_run":True,"selected_bill_codes":[],"preview_fingerprint":""},operation="preview")
    assert preview["status"] == "SUCCESS", preview
    formal = _confirm(host,preview,arguments,scan=False)
    result = host.execute(formal,operation="execute")
    assert result["status"] == "SUCCESS", result
    assert operations[:2] == [("account-primary","query","R_SELF"),("account-daxiang","query","R_DX_PICK")]
    assert operations[2:] == [(account,operation,code) for account,code in
        [("account-primary","R_SELF"),("account-daxiang","R_DX_PICK")] for operation in ["create","verify"]]
    assert len(host.receipts) == 2


def test_split_zip_executes_selected_problem_and_publishes_ledger(tmp_path):
    from tests.test_problem_plugin_production_adapter import _split_header, _split_row
    rows = [_split_header(),_split_row("R12345678901",expected="3",arrived="1")]
    events = []
    def record_event(account,event):
        events.append((account["account_id"],event))
        return {"ok":True,"verified":True}
    fixture = problem_host("split_pending_problem_upload",sheet_rows_read=lambda *_:{"complete":True,"rows":rows},
                           problem_event_upsert=record_event)
    host = PackagedConnectorHost(tmp_path,"split_pending_problem_upload_v2",fixture.registry,fixture.context)
    preview = host.execute({"dry_run":True,"selected_bill_codes":[],"preview_fingerprint":""},operation="preview")
    assert preview["status"] == "SUCCESS", preview
    formal = _confirm(host,preview,{},scan=False)
    result = host.execute(formal,operation="execute")
    assert result["status"] == "SUCCESS", result
    assert len(events) == 1 and events[0][0] == "account-primary"
    assert events[0][1]["bill_code"] == "R12345678901"


@pytest.mark.parametrize("dry_run", [True, False])
def test_arrival_zip_webhook_calculates_without_writing_only_when_requested(tmp_path, dry_run):
    registry, context, row, calls, writes = _setup()
    host = PackagedConnectorHost(tmp_path, "sync_arrival_stats_v2", registry, context)
    result = host.execute({"target_date":"2026-09-11", "pending_sheet_disabled":False, "dry_run":dry_run},
        operation="preview" if dry_run else "run", entrypoint="webhook")
    assert result["status"] == "SUCCESS", result
    assert result["data"]["dry_run"] is dry_run
    assert bool(host.receipts) is not dry_run
    assert bool(writes) is not dry_run
