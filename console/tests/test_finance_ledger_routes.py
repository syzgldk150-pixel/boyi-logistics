"""Finance ledger Console boundary: routes, permissions and the write-request gate."""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from console.app import LocalDocFlowApp
from console.routes import finance
from shared.finance import FinanceActor, FinanceError
from shared.identity_permissions import SUPER_ADMIN
from shared.identity_routes import console_permission


class JsonHandler:
    def __init__(self, payload=None, *, user=None, origin="http://console.test", header_id=None, content_type="application/json"):
        payload = {} if payload is None else payload
        body = json.dumps(payload).encode()
        self.current_admin_user = user if user is not None else {"id": 7, "username": "fixture", "display_name": "测试会计"}
        self.headers = {"Host": "console.test", "Origin": origin, "Content-Type": content_type,
                        "Content-Length": str(len(body)),
                        "X-Browser-Request-UUID": payload.get("request_id", "") if header_id is None else header_id}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def result(self):
        return json.loads(self.wfile.getvalue())


@pytest.fixture
def app():
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app.settings = SimpleNamespace(agent_timeout_seconds=2)
    app._finance_ledger_repository = Mock()
    return app


@pytest.mark.parametrize(("method", "path", "permission"), [
    ("GET", "/finance/vouchers", "finance.read"),
    ("GET", "/finance/reports/trial-balance", "finance.read"),
    ("GET", "/finance/settings", "finance.read"),
    ("POST", "/finance/vouchers", "finance.manage"),
    ("POST", "/finance/vouchers/12", "finance.manage"),
    ("POST", "/finance/vouchers/12/submit", "finance.manage"),
    ("POST", "/finance/vouchers/12/delete", "finance.manage"),
    ("POST", "/finance/vouchers/12/cancel", SUPER_ADMIN),
    ("POST", "/finance/settings", SUPER_ADMIN),
    ("POST", "/finance/accounts", SUPER_ADMIN),
    ("POST", "/finance/accounts/100201", SUPER_ADMIN),
    ("POST", "/finance/accounts/100201/delete", SUPER_ADMIN),
    ("GET", "/finance/open-items", "finance.read"),
    ("POST", "/finance/parties", "finance.manage"),
    ("POST", "/finance/allocations", "finance.manage"),
    ("POST", "/finance/allocations/9/reverse", "finance.manage"),
    ("GET", "/finance/waybills", "finance.read"),
    ("POST", "/finance/waybills/15/confirm", "finance.manage"),
    ("POST", "/finance/waybills/confirm-batch", "finance.manage"),
    ("POST", "/finance/doc-rules/prepaid", SUPER_ADMIN),
    ("POST", "/finance/sources", SUPER_ADMIN),
    ("POST", "/finance/sources/4", SUPER_ADMIN),
    ("POST", "/finance/sources/4/files", "finance.manage"),
    ("POST", "/finance/line-rules", "finance.manage"),
    ("POST", "/finance/reviews/6/resolve", "finance.manage"),
    ("GET", "/finance/sources/4/batches", "finance.read"),
    ("GET", "/finance/periods/2026-10", "finance.read"),
    ("POST", "/finance/periods/2026-10/close", SUPER_ADMIN),
    ("POST", "/finance/periods/2026-10/reopen", SUPER_ADMIN),
    ("GET", "/finance/reports/balance-sheet", "finance.read"),
    ("GET", "/finance/exports/trial-balance.xlsx", "finance.read"),
    ("POST", "/finance/reviews/6/suggest", "finance.manage"),
])
def test_route_permissions(method, path, permission):
    assert console_permission(method, path) == permission


@pytest.mark.parametrize(("method", "path", "call"), [
    ("GET", "/finance/settings", ("_handle_finance_ledger_get", "settings")),
    ("GET", "/finance/reports/ledger", ("_handle_finance_ledger_get", "ledger")),
    ("GET", "/finance/vouchers/42", ("_handle_finance_ledger_get", "voucher", "42")),
    ("POST", "/finance/vouchers", ("_handle_finance_ledger_post", "voucher_create")),
    ("POST", "/finance/vouchers/42", ("_handle_finance_ledger_post", "voucher_update", "42")),
    ("POST", "/finance/vouchers/42/submit", ("_handle_finance_ledger_post", "voucher_submit", "42")),
    ("POST", "/finance/vouchers/42/cancel", ("_handle_finance_ledger_post", "voucher_cancel", "42")),
    ("POST", "/finance/accounts/1002", ("_handle_finance_ledger_post", "account_update", "1002")),
    ("POST", "/finance/accounts/100201/delete", ("_handle_finance_ledger_post", "account_delete", "100201")),
    ("GET", "/finance/parties", ("_handle_finance_ledger_get", "parties")),
    ("GET", "/finance/parties/sender-candidates", ("_handle_finance_ledger_get", "sender_candidates")),
    ("GET", "/finance/open-items", ("_handle_finance_ledger_get", "open_items")),
    ("GET", "/finance/allocations", ("_handle_finance_ledger_get", "allocations")),
    ("GET", "/finance/reports/aging", ("_handle_finance_ledger_get", "aging")),
    ("GET", "/finance/reports/statement", ("_handle_finance_ledger_get", "statement")),
    ("POST", "/finance/parties", ("_handle_finance_ledger_post", "party_create")),
    ("POST", "/finance/parties/3", ("_handle_finance_ledger_post", "party_update", "3")),
    ("POST", "/finance/allocations", ("_handle_finance_ledger_post", "allocation_create")),
    ("POST", "/finance/allocations/auto-waybill", ("_handle_finance_ledger_post", "allocation_auto")),
    ("POST", "/finance/allocations/9/reverse", ("_handle_finance_ledger_post", "allocation_reverse", "9")),
    ("POST", "/finance/allocations/daily", ("_handle_finance_ledger_post", "allocation_daily")),
    ("GET", "/finance/waybills", ("_handle_finance_ledger_get", "waybill_postings")),
    ("GET", "/finance/doc-rules", ("_handle_finance_ledger_get", "doc_rules")),
    ("POST", "/finance/waybills/15/confirm", ("_handle_finance_ledger_post", "waybill_confirm", "15")),
    ("POST", "/finance/waybills/confirm-batch", ("_handle_finance_ledger_post", "waybill_confirm_batch")),
    ("POST", "/finance/doc-rules/collect", ("_handle_finance_ledger_post", "doc_rule_update", "到付")),
    ("GET", "/finance/sources", ("_handle_finance_ledger_get", "sources")),
    ("GET", "/finance/sources/4/batches", ("_handle_finance_ledger_get", "source_batches", "4")),
    ("GET", "/finance/sources/4/files", ("_handle_finance_ledger_get", "source_files", "4")),
    ("GET", "/finance/source-batches/8/lines", ("_handle_finance_ledger_get", "batch_lines", "8")),
    ("GET", "/finance/line-rules", ("_handle_finance_ledger_get", "line_rules")),
    ("GET", "/finance/reviews", ("_handle_finance_ledger_get", "reviews")),
    ("GET", "/finance/anomalies", ("_handle_finance_ledger_get", "anomalies")),
    ("POST", "/finance/sources", ("_handle_finance_ledger_post", "source_create")),
    ("POST", "/finance/sources/4", ("_handle_finance_ledger_post", "source_update", "4")),
    ("POST", "/finance/source-files/5/discard", ("_handle_finance_ledger_post", "source_file_discard", "5")),
    ("POST", "/finance/line-rules", ("_handle_finance_ledger_post", "line_rule_save")),
    ("POST", "/finance/reviews/6/resolve", ("_handle_finance_ledger_post", "review_resolve", "6")),
    ("POST", "/finance/anomalies/7/acknowledge", ("_handle_finance_ledger_post", "anomaly_acknowledge", "7")),
    ("POST", "/finance/sources/4/files", ("_handle_finance_source_upload", "4")),
    ("GET", "/finance/periods/2026-10", ("_handle_finance_ledger_get", "period_status", "2026-10")),
    ("POST", "/finance/periods/2026-10/close", ("_handle_finance_ledger_post", "period_close", "2026-10")),
    ("POST", "/finance/periods/2026-10/reopen", ("_handle_finance_ledger_post", "period_reopen", "2026-10")),
    ("GET", "/finance/reports/profit-loss", ("_handle_finance_ledger_get", "profit_loss")),
    ("GET", "/finance/reports/balance-sheet", ("_handle_finance_ledger_get", "balance_sheet")),
    ("GET", "/finance/reports/cash-flow", ("_handle_finance_ledger_get", "cash_flow")),
    ("GET", "/finance/reports/waybill-margin", ("_handle_finance_ledger_get", "waybill_margin")),
    ("POST", "/finance/reviews/6/suggest", ("_handle_finance_review_suggest", "6")),
])
def test_routes_dispatch(method, path, call):
    stub = Mock()
    handled = (finance.handle_get if method == "GET" else finance.handle_post)(stub, "handler", path, path, {})
    assert handled
    name, *args = call
    method_mock = getattr(stub, name)
    assert method_mock.call_count == 1
    positional = method_mock.call_args.args
    assert positional[0] == "handler" and positional[1] == args[0]
    if len(args) > 1:
        assert positional[-1] == args[1]


def test_unknown_finance_paths_are_not_routed():
    stub = Mock()
    assert not finance.handle_get(stub, "h", "/finance/summary", "", {})
    assert not finance.handle_post(stub, "h", "/finance/vouchers/abc/submit", "", {})
    assert not finance.handle_post(stub, "h", "/finance/doc-rules/cash", "", {})


def test_write_passes_session_actor_and_matching_request_id(app):
    request_id = str(uuid4())
    app._finance_ledger_repository.submit_voucher.return_value = {"id": 5, "status": "submitted"}
    handler = JsonHandler({"request_id": request_id, "expected_revision": 2})
    app._handle_finance_ledger_post(handler, "voucher_submit", "5")
    assert handler.status == 200 and handler.result() == {"ok": True, "data": {"id": 5, "status": "submitted"}}
    args, kwargs = app._finance_ledger_repository.submit_voucher.call_args
    assert args == (5, {"expected_revision": 2})
    assert kwargs == {"request_id": request_id, "actor": FinanceActor("7", "测试会计")}


@pytest.mark.parametrize("handler_kwargs", [
    {"header_id": str(uuid4())},
    {"origin": "http://evil.test"},
    {"user": {"id": 0, "is_legacy_basic_auth": True}},
    {"content_type": "text/plain"},
])
def test_write_gate_refuses_before_ledger(app, handler_kwargs):
    handler = JsonHandler({"request_id": str(uuid4())}, **handler_kwargs)
    app._handle_finance_ledger_post(handler, "voucher_create")
    assert handler.status in {400, 403}
    app._finance_ledger_repository.create_draft.assert_not_called()


def test_ledger_refusal_keeps_code_and_status(app):
    app._finance_ledger_repository.submit_voucher.side_effect = FinanceError("PERIOD_CLOSED", "2026-10 已封账，不能再记账。")
    handler = JsonHandler({"request_id": str(uuid4()), "expected_revision": 1})
    app._handle_finance_ledger_post(handler, "voucher_submit", "5")
    assert handler.status == 409
    assert handler.result() == {"ok": False, "error_code": "PERIOD_CLOSED", "message": "2026-10 已封账，不能再记账。"}


def test_get_reports_pass_query(app):
    app._finance_ledger_repository.trial_balance.return_value = {"rows": []}
    handler = JsonHandler()
    app._handle_finance_ledger_get(handler, "trial_balance", {"month": ["2026-10"], "include_zero": ["1"]})
    app._finance_ledger_repository.trial_balance.assert_called_once_with("2026-10", include_zero=True)
    assert handler.result()["ok"]


def test_party_id_query_is_validated_before_ledger(app):
    handler = JsonHandler()
    app._handle_finance_ledger_get(handler, "open_items", {"party_id": ["abc"]})
    assert handler.status == 400 and handler.result()["error_code"] == "FINANCE_INVALID_REQUEST"
    app._finance_ledger_repository.open_items.assert_not_called()
    handler = JsonHandler()
    app._finance_ledger_repository.open_items.return_value = {"items": []}
    app._handle_finance_ledger_get(handler, "open_items", {"party_id": ["3"], "account": ["112201"]})
    app._finance_ledger_repository.open_items.assert_called_once_with(party_id=3, account_code="112201", as_of=None)


def test_bill_upload_passes_raw_bytes_and_session_actor(app):
    request_id = str(uuid4())
    body = "交易时间,金额\n2026-10-07,9.90\n".encode("utf-8")
    handler = JsonHandler()
    handler.rfile = io.BytesIO(body)
    handler.headers = {"Host": "console.test", "Origin": "http://console.test", "Content-Type": "text/csv",
                       "Content-Length": str(len(body)), "X-Browser-Request-UUID": request_id, "X-File-Name": "%E8%B4%A6%E5%8D%95.csv"}
    app._finance_ledger_repository.upload_source_file.return_value = {"id": 1}
    app._handle_finance_source_upload(handler, "4")
    assert handler.status == 200
    args, kwargs = app._finance_ledger_repository.upload_source_file.call_args
    assert args == (4,) and kwargs["data"] == body and kwargs["file_name"] == "账单.csv"
    assert kwargs["request_id"] == request_id and kwargs["actor"] == FinanceActor("7", "测试会计")
    refused = JsonHandler()
    refused.headers = {**handler.headers, "Origin": "http://evil.test"}
    app._handle_finance_source_upload(refused, "4")
    assert refused.status == 403


def test_export_route_and_download_headers(app):
    stub = Mock()
    assert finance.handle_get(stub, "h", "/finance/exports/profit-loss.xlsx", "", {"month": ["2026-10"]})
    stub._handle_finance_export.assert_called_once_with("h", "profit-loss", {"month": ["2026-10"]})
    assert not finance.handle_get(stub, "h", "/finance/exports/Bad Kind.xlsx", "", {})
    sent = {}
    app._send_bytes = lambda handler, status, payload, content_type, **kwargs: sent.update(status=status, payload=payload, type=content_type, **kwargs)
    app._finance_ledger_repository.trial_balance.return_value = {
        "month": "2026-10", "rows": [], "totals": {k: "0.0000" for k in ("opening_debit", "opening_credit", "period_debit",
                                                                          "period_credit", "closing_debit", "closing_credit")},
        "balanced": True}
    app._handle_finance_export(JsonHandler(), "trial-balance", {"month": ["2026-10"]})
    assert sent["status"] == 200 and sent["payload"][:2] == b"PK"
    assert "spreadsheetml" in sent["type"] and "filename*=UTF-8''" in sent["extra_headers"]["Content-Disposition"]
    del app._send_bytes  # _send_json writes through the real _send_bytes
    handler = JsonHandler()
    app._handle_finance_export(handler, "nonsense", {})
    assert handler.status == 400


def test_review_suggest_is_signed_and_forwarded(app):
    request_id = str(uuid4())
    calls = []
    app._control_plane_write_context = lambda handler: {"_console_principal": {"actor_id": "7"}}
    app._read_control_plane_json = lambda handler: {"request_id": request_id}
    app._agent_request = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs)) or {"ok": True, "data": {"id": 6}}
    handler = JsonHandler({"request_id": request_id})
    app._handle_finance_review_suggest(handler, "6")
    assert handler.status == 200 and handler.result() == {"ok": True, "data": {"id": 6}}
    method, endpoint, kwargs = calls[0]
    assert (method, endpoint) == ("POST", "/internal/v1/admin/finance/reviews/6/suggest")
    assert kwargs["payload"] == {"request_id": request_id} and kwargs["console_principal"] == {"actor_id": "7"}
    app._agent_request = lambda *a, **k: {"ok": False, "status": 503, "error": {"code": "FINANCE_AI_UNAVAILABLE", "message": "尚未启用 AI 模型"}}
    failed = JsonHandler({"request_id": request_id})
    app._handle_finance_review_suggest(failed, "6")
    assert failed.status == 503 and failed.result()["error_code"] == "FINANCE_AI_UNAVAILABLE"
