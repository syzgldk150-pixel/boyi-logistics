"""Print JSON contract tests with source-bound synthetic repository data."""

import json
from datetime import date
from decimal import Decimal
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from console.app import LocalDocFlowApp
from console.services.waybill_printing import build_waybill_print_data
from console.services.waybills_receipts import WaybillsReceiptsServiceMixin


@pytest.fixture
def app():
    instance = WaybillsReceiptsServiceMixin()
    instance.repository = Mock()
    instance.repository.get_waybill.return_value = {
        "id": 41,
        "source": "manual",
        "waybill_no": "BY00041",
        "receiver_name": "合成收件人",
        "receipt_required": 1,
        "internal_marker": "not-part-of-label",
    }
    instance._send_json = Mock()
    return instance


@pytest.mark.parametrize("source", ("manual", "ocr", "yunda", "ronghui"))
def test_print_data_reads_exact_source_and_returns_only_label_fields(app, source):
    handler = object()
    app._handle_waybill_print_data(handler, 41, {"source": [source]})
    app.repository.get_waybill.assert_called_once_with(41, source=source)
    args = app._send_json.call_args.args
    assert args[:2] == (handler, HTTPStatus.OK)
    assert args[2]["ok"] is True
    assert args[2]["waybill"]["waybill_no"] == "BY00041"
    assert args[2]["waybill"]["receipt_required"] is True
    assert "internal_marker" not in args[2]["waybill"]
    assert "id" not in args[2]["waybill"]


@pytest.mark.parametrize("query, message", (
    ({}, "请指定运单来源。"),
    ({"source": [""]}, "请指定运单来源。"),
    ({"source": ["unknown"]}, "运单来源无效。"),
))
def test_missing_or_unknown_source_stops_before_repository(app, query, message):
    app._handle_waybill_print_data(object(), 41, query)
    app.repository.get_waybill.assert_not_called()
    assert app._send_json.call_args.args[1:] == (
        HTTPStatus.BAD_REQUEST, {"ok": False, "message": message},
    )


def test_missing_waybill_is_explicitly_not_found(app):
    app.repository.get_waybill.return_value = None
    app._handle_waybill_print_data(object(), 41, {"source": ["manual"]})
    assert app._send_json.call_args.args[1:] == (
        HTTPStatus.NOT_FOUND, {"ok": False, "message": "运单不存在。"},
    )


def test_repository_failure_does_not_expose_internal_error(app):
    app.repository.get_waybill.side_effect = RuntimeError("synthetic private database diagnostic")
    app._handle_waybill_print_data(object(), 41, {"source": ["manual"]})
    assert app._send_json.call_args.args[1:] == (
        HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "message": "运单读取失败，请稍后重试。"},
    )


def test_missing_waybill_number_cannot_be_printed(app):
    app.repository.get_waybill.return_value = {"id": 41, "source": "manual"}
    app._handle_waybill_print_data(object(), 41, {"source": ["manual"]})
    assert app._send_json.call_args.args[1:] == (
        HTTPStatus.CONFLICT, {"ok": False, "message": "运单号缺失，无法打开打印预览。"},
    )


def test_shared_print_data_preserves_zeroes_and_is_json_serializable():
    data = build_waybill_print_data({
        "waybill_no": "BY00041", "open_date": date(2026, 1, 2),
        "freight_fee": Decimal("0.00"), "quantity_lines": 0, "receipt_required": 0,
    })
    assert json.loads(json.dumps(data)) == data
    assert data["open_date"] == "2026-01-02"
    assert data["freight_fee"] == "0.00"
    assert data["quantity_lines"] == "0"
    assert data["receipt_required"] is False


def test_print_data_inherits_existing_authentication_gate():
    instance = object.__new__(LocalDocFlowApp)
    instance._handle_isolated_original_page_request = Mock(return_value=False)
    instance._active_original_page_proxy_disabled = Mock(return_value=False)
    instance._ensure_authorized = Mock(return_value=False)
    instance.routes = Mock()
    handler = SimpleNamespace(path="/waybills/41/print-data?source=manual")
    instance.handle_get(handler)
    instance._ensure_authorized.assert_called_once_with(handler)
    instance.routes.handle_get.assert_not_called()


PLATFORM_ROW = {"id": 41, "source": "ronghui", "waybill_no": "R00000000001", "open_date": date(2026, 1, 2),
                "source_account_id": "price_default", "status": "in_transit"}
LABEL_CALLS = [["PRINT_INITA", ["0mm", "0mm", "80mm", "100mm", "print"]]]


@pytest.fixture
def platform_app(app, monkeypatch):
    calls = []

    def fake_call(instance, operation, params, **kwargs):
        calls.append((operation, params, kwargs))
        if operation == "platform-waybill-print":
            return {"ok": True, "data": {"platform": params["platform"], "waybill_no": params["waybill_no"],
                                         "calls": LABEL_CALLS}}
        return {"ok": True, "data": {"verified": True, "submitted": True, "message": "融辉打印次数已记录（第 2 次）。"}}

    import console.services.waybills_receipts as module
    monkeypatch.setattr(module, "call_business", fake_call)
    app._receipt_attachment_context = Mock(return_value={"_console_principal": {"actor_id": "1"}})
    app.calls = calls
    return app


def test_platform_waybill_prints_its_original_site_label(platform_app):
    platform_app.repository.get_waybill.return_value = dict(PLATFORM_ROW)
    platform_app._handle_waybill_print_data(object(), 41, {"source": ["ronghui"]})
    operation, params, _ = platform_app.calls[0]
    assert operation == "platform-waybill-print"
    assert params == {"platform": "ronghui", "waybill_no": "R00000000001", "account_id": "price_default",
                      "open_date": "2026-01-02"}
    status, payload = platform_app._send_json.call_args.args[1:]
    assert status == HTTPStatus.OK
    assert payload == {"ok": True, "platform_label": {"platform": "ronghui", "platform_name": "融辉",
                                                      "waybill_no": "R00000000001", "calls": LABEL_CALLS}}


def test_boyi_waybill_linked_to_a_platform_prints_that_platform_label(platform_app):
    platform_app.repository.get_waybill.return_value = {
        "id": 41, "source": "manual", "waybill_no": "BY00041", "linked_platform": "yunda", "linked_waybill_no": "900000001"}
    platform_app.repository.get_linked_platform_waybill.return_value = {
        "waybill_no": "900000001", "open_date": "2026-01-02", "status": "signed", "source_account_id": "yunda_default"}
    platform_app._handle_waybill_print_data(object(), 41, {"source": ["manual"]})
    platform_app.repository.get_linked_platform_waybill.assert_called_once_with("yunda", "900000001")
    assert platform_app.calls[0][1]["platform"] == "yunda"
    # The entry frame asks for the Boyi label it just saved.
    platform_app._handle_waybill_print_data(object(), 41, {"source": ["manual"], "label": ["boyi"]})
    assert platform_app._send_json.call_args.args[2]["waybill"]["waybill_no"] == "BY00041"
    assert len(platform_app.calls) == 1


@pytest.mark.parametrize("row, linked", [
    ({**PLATFORM_ROW, "source_account_id": None}, None),
    ({"id": 41, "source": "manual", "waybill_no": "BY1", "linked_platform": "ronghui", "linked_waybill_no": "R1"},
     LookupError("绑定的平台运单尚未入库，请先在寄件运单查询中查询该平台运单。")),
])
def test_unresolved_platform_account_is_refused_without_guessing(platform_app, row, linked):
    platform_app.repository.get_waybill.return_value = row
    platform_app.repository.get_linked_platform_waybill.side_effect = linked
    platform_app._handle_waybill_print_data(object(), 41, {"source": [row["source"]]})
    status, payload = platform_app._send_json.call_args.args[1:]
    assert status == HTTPStatus.CONFLICT and payload["ok"] is False
    assert platform_app.calls == []


def test_boyi_waybill_linked_to_best_keeps_the_boyi_label(platform_app):
    platform_app.repository.get_waybill.return_value = {
        "id": 41, "source": "manual", "waybill_no": "BY00041", "linked_platform": "best", "linked_waybill_no": "B1"}
    platform_app._handle_waybill_print_data(object(), 41, {"source": ["manual"]})
    assert platform_app._send_json.call_args.args[2]["waybill"]["waybill_no"] == "BY00041"
    assert platform_app.calls == []


def _log_handler(request_id, body_id=None):
    payload = json.dumps({"request_id": body_id or request_id}).encode()
    return SimpleNamespace(headers={"X-Browser-Request-UUID": request_id, "Content-Length": str(len(payload))},
                           rfile=__import__("io").BytesIO(payload))


def test_print_log_is_written_through_the_signed_business_write(platform_app):
    platform_app.repository.get_waybill.return_value = dict(PLATFORM_ROW)
    platform_app._control_plane_write_context = Mock(return_value={"_console_principal": {"actor_id": "1"}})
    platform_app._normalize_browser_request_uuid = lambda value: value
    platform_app._parse_json_body = lambda handler: json.loads(handler.rfile.read())
    request_id = "00000000-0000-4000-8000-000000000001"
    platform_app._handle_waybill_platform_print_log(_log_handler(request_id), 41, {"source": ["ronghui"]})
    operation, params, kwargs = platform_app.calls[0]
    assert operation == "platform-waybill-print-log" and kwargs["write"] is True and kwargs["request_id"] == request_id
    assert params["waybill_no"] == "R00000000001"
    assert platform_app._send_json.call_args.args[1:] == (
        HTTPStatus.OK, {"ok": True, "submitted": True, "message": "融辉打印次数已记录（第 2 次）。"})
    # A body that does not repeat the header UUID is refused before anything is written.
    platform_app._handle_waybill_platform_print_log(
        _log_handler(request_id, "00000000-0000-4000-8000-000000000002"), 41, {"source": ["ronghui"]})
    assert platform_app._send_json.call_args.args[1] == HTTPStatus.BAD_REQUEST
    assert len(platform_app.calls) == 1
