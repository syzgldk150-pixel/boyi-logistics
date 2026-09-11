import hashlib
import secrets
from dataclasses import replace

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry, ConnectorInvocationError
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.scan_connectors_v2 import build_scan_connectors
from tests.production_connector_support import ConnectorTestHost
from tests.test_scan_codes_core_handlers import _context, _descriptor
from tests.first_party_action_payload_support import load_first_party_action


def _host(**ports):
    def unexpected(*_args):
        pytest.fail("This scenario must not call an unrelated infrastructure port")
    ports = {**dict.fromkeys(("scan_read_page","scan_next_submit","scan_next_verify","replace_scan_snapshot"),unexpected),**ports}
    context = replace(_context("service.invoke"),account_bindings={"scan_ronghui":("scan-account",)})
    handlers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(describe_account=_descriptor,**ports),
                                                  cursor_secret=secrets.token_bytes(32))
    return ConnectorTestHost(ConnectorRegistry(build_scan_connectors(handlers)),context,"sync_scan_codes_v2")


def test_scan_connector_requires_authoritative_source_total():
    host = _host(scan_read_page=lambda *_:{"items":[],"returned":0})
    with pytest.raises(ConnectorInvocationError) as failure:
        host.invoke("scan_ronghui","read_page",{"target_date":"2026-09-11","page_size":200})
    assert failure.value.code == "BROKER_SOURCE_TOTAL_REQUIRED"


def test_real_scan_classification_produces_preview_through_production_connector():
    rows = [{"bill_code":"R123456789010001","destination":"A站","scan_type":"到货",
             "scan_time":"2026-09-11 08:00:00","scan_site":"大祥S站"}]
    host = _host(scan_read_page=lambda *_:{"items":rows,"returned":1,"total":1,"total_authoritative":True})
    result = load_first_party_action("sync_scan_codes").run_action({"target_date":"2026-09-11","dry_run":True},host.broker)
    assert result["status"] == "SUCCESS"
    assert result["data"]["dry_run"] is True
    assert host.calls == [("scan_ronghui","read_page")]


@pytest.mark.parametrize("verified",[True,False])
def test_scan_submit_is_followed_by_actual_fresh_ledger_verification(verified):
    items = [{"bill_code":"R123456789010001","station_name":"A站"}]
    calls = []
    def submit(account, requested):
        assert account["account_id"] == "scan-account" and requested == items
        calls.append("submit")
        return {"ok":True,"stage":"done","write_started_at":"2026-09-11T00:00:00+00:00",
            "write_finished_at":"2026-09-11T00:00:05+00:00","detail":{"items":items,
                "stations":[{"station_name":"A站","count":1,"bill_codes":[items[0]["bill_code"]]}],
                "total_scanned":1,"skipped_signed_codes":[]}}
    def readback(account, requested, start, end):
        assert account["account_id"] == "scan-account" and requested == items
        assert start == "2026-09-11T00:00:00+00:00" and end == "2026-09-11T00:00:05+00:00"
        calls.append("readback")
        return {"ok":verified,"verified":verified,"record_count":1,
                "identities_sha256":hashlib.sha256(canonical_json_bytes(items)).hexdigest()}
    host = _host(scan_next_submit=submit,scan_next_verify=readback)
    submitted = host.invoke("scan_ronghui","submit",{"items":items})
    arguments = {key:submitted[key] for key in ("operation_id","items_sha256","submitted","scanned","skipped_signed_codes")}
    if verified:
        result = host.invoke("scan_ronghui","verify",arguments)
        assert result["verified"] and result["scanned"] == result["readback_count"] == 1
    else:
        with pytest.raises(ConnectorInvocationError) as failure:
            host.invoke("scan_ronghui","verify",arguments)
        assert failure.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert calls == ["submit","readback"]
