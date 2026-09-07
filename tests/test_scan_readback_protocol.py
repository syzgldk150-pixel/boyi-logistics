"""Twenty real HTTP readback rounds per outcome; not automatic Run recovery."""
from datetime import datetime, timedelta
import socket
from zoneinfo import ZoneInfo

import pytest
import requests

from plugin_core_adapters.first_party import _scan_next_readback_state
from tests.v32_acceptance.daily_protocol import CHILD_CODE, LOGIN, STATION, DailyProtocol


class LostReplyProtocol(DailyProtocol):
    """The external server commits, then closes the socket before a response."""
    def handle(self, path, query, arguments):
        result = super().handle(path, query, arguments)
        if path == "/dataOperation/saveTables":
            self.write_committed = True
        return result


@pytest.mark.parametrize("round_number", range(20))
@pytest.mark.parametrize("outcome", ("APPLIED", "NOT_APPLIED", "UNKNOWN"))
def test_production_scan_readback_classifies_actual_http_ledger_without_guessing(round_number, outcome, record_property):
    with LostReplyProtocol() as boundary, boundary.authentication_boundaries():
        row = {"BILL_CODE":CHILD_CODE, "PRE_OR_NEXT_STATION":STATION,
            "SCAN_SITE_CODE":LOGIN["loginSiteCode"], "SCAN_TYPE":"发件", "DATA_FROM":"K13"}
        started = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(seconds=1)
        if outcome != "NOT_APPLIED":
            body = requests.Request("POST", boundary.url + "/dataOperation/saveTables",
                json=[{"name":"TAB_SCAN_SEND_ADD", "rows":[row]}]).prepare().body
            assert isinstance(body, bytes)
            # Intentionally abandon the client response after sending the full
            # valid write. The production readback, not that response, decides.
            host, port = boundary.server.server_address
            with socket.create_connection((host, port), timeout=3) as client:
                client.sendall((f"POST /dataOperation/saveTables HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
                client.shutdown(socket.SHUT_WR)
                # Reading one byte proves server processing has started; the
                # business reply is deliberately discarded and never parsed.
                assert client.recv(1)
            if outcome == "UNKNOWN":
                # A second committed server row makes target identity ambiguous.
                with boundary.session() as session:
                    response = session.post(boundary.url + "/dataOperation/saveTables",
                        json=[{"name":"TAB_SCAN_SEND_ADD", "rows":[row]}], timeout=3)
                    response.raise_for_status()
        finished = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=1)
        before = len(boundary.ledger)
        result = _scan_next_readback_state({"session_profile":"v32-daily-profile"},
            [{"bill_code":CHILD_CODE,"station_name":STATION}], ((started, finished),))
        assert result["state"] == outcome
        assert result["record_count"] == before
        assert len(boundary.ledger) == before
        assert any(request["action"] == "FIND_SEND_SCAN_RECORD" for request in boundary.requests)
        record_property("round", round_number)
        record_property("readback", dict(result))
        record_property("actual_side_effect_rows", before)
        record_property("business_response_consumed", False)
