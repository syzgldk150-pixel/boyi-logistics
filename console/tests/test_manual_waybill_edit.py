"""Browser edit API contract and shared manual-entry validation."""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from console.app import LocalDocFlowApp
from console.app_support import DocumentService, compose_manual_weight_volume, split_manual_weight_volume
from console.database import MANUAL_WAYBILL_EDIT_LABELS, ManualWaybillEditError, manual_waybill_fingerprint
from console.routes import waybills
from console.services.waybill_entry_extensions import WAYBILL_ENTRY_ACTIVE_VALIDATORS_ENDPOINT


def edit_fields(**overrides):
    fields = {f"field_{name}": "" for name in MANUAL_WAYBILL_EDIT_LABELS if name != "weight_volume"}
    fields.update(field_open_date="2026-10-13", field_receiver_name="测试收件人",
                  field_receiver_phone="00000000000", field_receiver_address="测试收件地址",
                  field_destination_site="测试到站", field_goods_name_lines="测试货物",
                  field_quantity_lines="1", field_package_type_lines="纸箱", field_delivery_method="自提",
                  field_freight_fee="20.00", field_payment_method="寄付", field_receipt_required="0",
                  field_weight_kg="10", field_volume_m3="0.1")
    fields.update(overrides)
    return fields


def saved_row(**overrides):
    fields = {name.removeprefix("field_"): value for name, value in edit_fields().items()
              if name not in {"field_weight_kg", "field_volume_m3"}}
    fields.update(id=7, waybill_no="BY00007", source="manual", status="in_transit",
                  receipt_required=0, weight_volume="10kg / 0.1m³")
    fields.update(overrides)
    return fields


class JsonHandler:
    def __init__(self, payload=None, *, user=None, origin="http://console.test"):
        payload = payload or {}
        body = json.dumps(payload).encode()
        self.current_admin_user = user or {"id": 7, "username": "fixture", "display_name": "测试管理员"}
        self.headers = {"Host": "console.test", "Origin": origin,
                        "Content-Type": "application/json", "Content-Length": str(len(body)),
                        "X-Browser-Request-UUID": payload.get("request_id", "")}
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


def build_app():
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app.settings = SimpleNamespace(agent_timeout_seconds=2)
    required = {"open_date", "receiver_name", "receiver_phone", "receiver_address", "destination_site",
                "goods_name_lines", "package_type_lines", "quantity_lines", "delivery_method",
                "freight_fee", "payment_method"}
    template = {"fields": [{"name": name, "label": label, "required": name in required}
                           for name, label in MANUAL_WAYBILL_EDIT_LABELS.items()]}
    store = SimpleNamespace(get_active_template_spec=lambda: template)
    app.repository = Mock()
    app.repository.get_waybill.return_value = saved_row()
    app.repository.update_manual_waybill.return_value = {"outcome": "SAVED", "changes": []}

    def save_with_validation(*_args, validate_new_request=None, **_kwargs):
        if validate_new_request is not None:
            validate_new_request()
        return app.repository.update_manual_waybill.return_value

    app.repository.update_manual_waybill.side_effect = save_with_validation
    app.service = DocumentService(app.settings, app.repository, store, qwen_provider=None)
    app._validate_active_waybill_entry_extensions = Mock(return_value=(True, ""))
    return app


def update_body(**fields):
    return {"request_id": str(uuid4()), "fingerprint": manual_waybill_fingerprint(saved_row()),
            "fields": edit_fields(**fields)}


def test_edit_data_has_same_field_names_as_entry_and_current_fingerprint():
    app = build_app()
    handler = JsonHandler()
    assert waybills.handle_get(app, handler, "/waybills/7/edit-data", "", {"source": ["manual"]})
    result = handler.result()
    assert result["editable"] is True
    assert result["values"] == edit_fields()
    assert result["fingerprint"] == manual_waybill_fingerprint(saved_row())
    assert "field_waybill_no" not in result["values"]
    app.repository.get_waybill.assert_called_once_with(7, source="manual")


@pytest.mark.parametrize("source,status,expected", [("yunda", "in_transit", 400), ("manual", "cancelled", 200)])
def test_edit_data_rejects_platform_source_or_explains_cancelled(source, status, expected):
    app = build_app()
    app.repository.get_waybill.return_value = saved_row(status=status)
    handler = JsonHandler()
    app._handle_manual_waybill_edit_data(handler, 7, {"source": [source]})
    assert handler.status == expected
    assert not handler.result().get("editable")
    if source == "manual":
        assert "作废" in handler.result()["reason"]
    else:
        app.repository.get_waybill.assert_not_called()


def test_edit_history_is_loaded_only_for_existing_manual_waybill():
    app = build_app()
    app.repository.list_manual_waybill_edits.return_value = [{"actor": "测试管理员", "changes": []}]
    handler = JsonHandler()
    assert waybills.handle_get(app, handler, "/waybills/7/edit-history", "", {"source": ["manual"]})
    assert handler.result()["edits"] == [{"actor": "测试管理员", "changes": []}]
    app.repository.get_waybill.return_value = None
    missing = JsonHandler()
    app._handle_manual_waybill_edit_history(missing, 99, {"source": ["manual"]})
    assert missing.status == 404
    assert app.repository.list_manual_waybill_edits.call_count == 1


def test_update_normalizes_fields_and_records_authenticated_actor():
    app = build_app()
    body = update_body(field_freight_fee="20.5", field_receipt_required="1")
    handler = JsonHandler(body)
    assert waybills.handle_post(app, handler, "/waybills/7/manual-update", "", {})
    assert handler.status == 200 and handler.result()["outcome"] == "SAVED"
    args, kwargs = app.repository.update_manual_waybill.call_args
    assert args[0] == 7
    assert args[1]["freight_fee"] == "20.50"
    assert args[1]["weight_volume"] == "10kg / 0.1m³"
    assert args[1]["open_date"] == "2026/10/13"
    assert args[1]["receipt_required"] == "1"
    assert kwargs["actor_id"] == "7" and kwargs["actor_name"] == "测试管理员"
    assert kwargs["request_id"] == body["request_id"]
    assert "重新打印" in handler.result()["message"]


@pytest.mark.parametrize("changes,message", [
    ({"field_receiver_name": ""}, "必填"), ({"field_payment_method": "现付"}, "只能选择"),
    ({"field_freight_fee": "二十"}, "金额格式无效"), ({"field_freight_fee": "NaN"}, "金额格式无效"),
    ({"field_open_date": "2026-02-30"}, "日期格式无效"), ({"field_receipt_required": "true"}, "回单"),
    ({"field_weight_kg": "-1"}, "不能为负数"), ({"field_volume_m3": "Infinity"}, "不能为负数"),
])
def test_update_reuses_entry_validation_and_never_saves_invalid_values(changes, message):
    app = build_app()
    handler = JsonHandler(update_body(**changes))
    app._handle_manual_waybill_update(handler, 7)
    assert handler.status == 422 and message in handler.result()["message"]
    app.repository.update_manual_waybill.assert_not_called()


@pytest.mark.parametrize("mutation", ["different_uuid", "missing", "extra", "unprefixed", "numeric", "fingerprint"])
def test_update_rejects_invalid_browser_contract(mutation):
    app = build_app()
    body = update_body()
    if mutation == "missing":
        del body["fields"]["field_remark"]
    elif mutation == "extra":
        body["fields"]["field_waybill_no"] = "FORGED"
    elif mutation == "unprefixed":
        body["fields"]["remark"] = body["fields"].pop("field_remark")
    elif mutation == "numeric":
        body["fields"]["field_freight_fee"] = 20
    elif mutation == "fingerprint":
        body["fingerprint"] = "not-a-fingerprint"
    handler = JsonHandler(body)
    if mutation == "different_uuid":
        handler.headers["X-Browser-Request-UUID"] = str(uuid4())
    app._handle_manual_waybill_update(handler, 7)
    assert handler.status == 400
    app.repository.update_manual_waybill.assert_not_called()


@pytest.mark.parametrize("user,origin", [
    ({"id": 7, "username": "fixture", "is_legacy_basic_auth": True}, "http://console.test"),
    ({"id": 0, "username": "fixture"}, "http://console.test"),
    ({"id": 7, "username": "fixture"}, "http://other.test"),
])
def test_update_requires_mysql_admin_and_same_origin(user, origin):
    app = build_app()
    handler = JsonHandler(update_body(), user=user, origin=origin)
    app._handle_manual_waybill_update(handler, 7)
    assert handler.status == 403
    app.repository.update_manual_waybill.assert_not_called()


@pytest.mark.parametrize("code,status", [("STALE", 409), ("CANCELLED", 409), ("NOT_FOUND", 404), ("REQUEST_REUSED", 409)])
def test_update_reports_repository_conflict_without_success(code, status):
    app = build_app()
    app.repository.update_manual_waybill.side_effect = ManualWaybillEditError(code, "测试拒绝原因")
    handler = JsonHandler(update_body())
    app._handle_manual_waybill_update(handler, 7)
    assert handler.status == status and handler.result()["error_code"] == code


def test_active_extension_failure_blocks_edit_and_passes_same_entry_draft():
    app = build_app()
    del app._validate_active_waybill_entry_extensions
    app._agent_request = Mock(return_value={"ok": False})
    handler = JsonHandler(update_body())
    app._handle_manual_waybill_update(handler, 7)
    assert handler.status == 422
    assert handler.result()["error_code"] == "WAYBILL_ENTRY_VALIDATION_FAILED"
    app.repository.update_manual_waybill.assert_called_once()
    args, kwargs = app._agent_request.call_args
    assert args == ("POST", WAYBILL_ENTRY_ACTIVE_VALIDATORS_ENDPOINT)
    assert kwargs["payload"]["waybill"]["receiver_name"] == "测试收件人"
    assert kwargs["console_principal"]["authenticated_by"] == "mysql_admin_session"


@pytest.mark.parametrize("outcome,message", [("UNCHANGED", "未修改任何内容"), ("REPLAYED", "已修改")])
def test_unchanged_and_replayed_updates_have_clear_result(outcome, message):
    app = build_app()
    app.repository.update_manual_waybill.return_value = {"outcome": outcome, "changes": []}
    handler = JsonHandler(update_body())
    app._handle_manual_waybill_update(handler, 7)
    assert handler.result()["outcome"] == outcome and message in handler.result()["message"]


def test_weight_volume_decimal_input_round_trips_without_exponent():
    value = compose_manual_weight_volume("1e3", "0.20")
    assert value == "1000kg / 0.20m³"
    assert split_manual_weight_volume(value) == ("1000", "0.20")
    assert split_manual_weight_volume("unrecognized stored text") is None
