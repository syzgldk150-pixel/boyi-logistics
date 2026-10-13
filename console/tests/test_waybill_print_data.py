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
