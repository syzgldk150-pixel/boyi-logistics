from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from agent.automation_plugins.daily_send_handlers import _date_text
from agent.tms_runtime.scripts import Send_order
from tests.test_sync_daily_send_orders_plugin_action import _load_action
from tools.send_order_sync_tool import _console_waybill_records


DAY = "2026-05-12"


def _source(**changes):
    return {
        "BILL_CODE": "R-SYNTHETIC-001",
        "REGISTER_DATE": f"{DAY} 06:30:00",
        "SEND_MAN_ADDRESS": "合成寄件地址",
        "DESTINATION": "合成目的地",
        "DISPATCH_UNDERLING_SITE": "合成目的网点",
        "BILL_WEIGHT": "7.25",
        "FEE_WEIGHT": "10.50",
        "VOLUME": "0.25",
        "SETTLEMENT_WEIGHT": "11",
        "VOLUME_WEIGHT": "12.75",
        "PIECE_NUMBER": "2",
        "GUEST_FREIGHT": "31.50",
        "FREIGHT": "20.25",
        "TOPAYMENT": "4.75",
        "GOODS_PAYMENT": "6.50",
        "INSURANCE": "100",
        "BL_INDENTURE": "1",
        "R_BILLCODE": "HR-SYNTHETIC-001",
        "SCAN_TYPE": "到件扫描",
        **changes,
    }


def _direct_record(raw):
    row = Send_order.normalize_record(raw)
    return row, _console_waybill_records([row], target_date=date.fromisoformat(DAY))[0]


def test_direct_refresh_and_plugin_keep_distinct_source_amounts_and_weights():
    source = _source()
    row, direct = _direct_record(source)
    fields, projected = _load_action()._normalize_source_row(source, target_date=DAY)
    assert direct == projected
    assert {key: direct[key] for key in (
        "recorded_cost", "freight_fee", "to_pay_amount", "cod_amount", "insurance_amount"
    )} == {
        "recorded_cost": "31.50", "freight_fee": "20.25", "to_pay_amount": "4.75",
        "cod_amount": "6.50", "insurance_amount": "100",
    }
    assert direct["weight_volume"] == (
        "实际重量 7.25 / 计费重量 10.50 / 体积 0.25 / 结算重量 11 / 体积重 12.75"
    )
    assert direct["sender_address"] == "合成寄件地址"
    assert direct["destination_site"] == fields["目的网点"] == "合成目的网点"
    assert direct["receipt_required"] == "1"
    assert direct["receipt_number"] == "HR-SYNTHETIC-001"
    assert direct["scan_status"] == "到件扫描"
    assert row["录单运费"] == fields["录单运费"] == "31.50"
    assert "录单金额" not in row and "录单金额" not in fields
    assert fields["需要回单"] == "是"


def test_missing_actual_weight_does_not_reuse_billing_weight():
    source = _source(BILL_WEIGHT=None)
    row, direct = _direct_record(source)
    fields, projected = _load_action()._normalize_source_row(source, target_date=DAY)
    assert row["实际重量"] is None and fields["实际重量"] is None
    assert direct == projected
    assert "实际重量" not in direct["weight_volume"]
    assert "计费重量 10.50" in direct["weight_volume"]


def test_missing_destination_site_does_not_reuse_geographic_destination():
    source = _source(DISPATCH_UNDERLING_SITE=None)
    _, direct = _direct_record(source)
    fields, projected = _load_action()._normalize_source_row(source, target_date=DAY)
    assert direct["destination_site"] == ""
    assert projected["destination_site"] == ""
    assert fields["目的网点"] == ""


def test_numeric_zero_and_missing_are_distinct_in_both_paths():
    source = _source(BILL_WEIGHT=0, FEE_WEIGHT=0, PIECE_NUMBER=0, GUEST_FREIGHT=0,
                     FREIGHT=0, TOPAYMENT=0, GOODS_PAYMENT=0, INSURANCE=0, BL_INDENTURE=0)
    row, direct = _direct_record(source)
    fields, projected = _load_action()._normalize_source_row(source, target_date=DAY)
    assert direct == projected
    for key in ("quantity_lines", "recorded_cost", "freight_fee", "to_pay_amount",
                "cod_amount", "insurance_amount", "receipt_required"):
        assert direct[key] == "0"
    assert row["录单运费"] == fields["录单运费"] == 0
    assert fields["需要回单"] == "否"
    _, missing = _direct_record(_source(GOODS_PAYMENT=None, BL_INDENTURE=None))
    assert missing["cod_amount"] == missing["receipt_required"] == ""


@pytest.mark.parametrize("value, expected", [
    ("1,234.50", "1234.50"), ("9007199254740993.01", "9007199254740993.01"),
])
def test_source_decimal_values_keep_all_digits(value, expected):
    source = _source(GUEST_FREIGHT=value)
    _, direct = _direct_record(source)
    fields, projected = _load_action()._normalize_source_row(source, target_date=DAY)
    assert direct["recorded_cost"] == projected["recorded_cost"] == expected
    assert fields["录单运费"] == expected


@pytest.mark.parametrize("value", ["not-a-number", "NaN", "Infinity", "***", True, {"amount": 1}])
def test_invalid_source_money_fails_instead_of_becoming_empty_or_zero(value):
    with pytest.raises(ValueError):
        _direct_record(_source(GUEST_FREIGHT=value))
    with pytest.raises(ValueError):
        _load_action()._normalize_source_row(_source(GUEST_FREIGHT=value), target_date=DAY)


@pytest.mark.parametrize("value", [None, "", "   ", "null", "missing-field"])
def test_missing_recorded_cost_aborts_before_any_publication(value):
    source = _source(GUEST_FREIGHT=value)
    if value == "missing-field":
        source.pop("GUEST_FREIGHT")
    with pytest.raises(ValueError, match="GUEST_FREIGHT is missing"):
        _direct_record(source)
    calls = []

    def broker(_operation, *, action, role, arguments):
        calls.append(action)
        evidence = f"synthetic-evidence:{len(calls)}"
        if action == "sync_daily_send_orders.lock.acquire":
            return {"acquired": True, "lease_ref": "synthetic-lease", "evidence_ref": evidence}
        if action == "ronghui.send_order.read_page":
            return {"items": [_source(BILL_CODE="valid-cost"), source], "total": 2, "evidence_ref": evidence}
        if action == "sync_daily_send_orders.lock.release":
            return {"committed": True, "released": True, "evidence_ref": evidence}
        pytest.fail("a missing cost must block all publication reads and writes")

    with pytest.raises(ValueError, match="GUEST_FREIGHT is missing"):
        _load_action().run_action({"target_date": DAY}, broker)
    assert calls[-1] == "sync_daily_send_orders.lock.release"


@pytest.mark.parametrize("value, expected", [(0, "0"), (1, "1"), ("否", "0"), ("是", "1"),
                                            (None, ""), ("", "")])
def test_receipt_requirement_is_explicit_and_nullable(value, expected):
    _, direct = _direct_record(_source(BL_INDENTURE=value))
    fields, projected = _load_action()._normalize_source_row(_source(BL_INDENTURE=value), target_date=DAY)
    assert direct["receipt_required"] == projected["receipt_required"] == expected
    assert fields["需要回单"] == {"1": "是", "0": "否", "": ""}[expected]


@pytest.mark.parametrize("value", [2, "unknown", True])
def test_unknown_receipt_requirement_is_not_reported_as_no(value):
    with pytest.raises(ValueError):
        _direct_record(_source(BL_INDENTURE=value))
    with pytest.raises(ValueError):
        _load_action()._normalize_source_row(_source(BL_INDENTURE=value), target_date=DAY)


@pytest.mark.parametrize("rows", [[_source(BILL_CODE="")], [_source(), _source(GUEST_FREIGHT="99.50")]])
def test_bad_source_identity_aborts_before_any_publication(rows):
    calls = []

    def broker(_operation, *, action, role, arguments):
        calls.append(action)
        evidence = f"synthetic-evidence:{len(calls)}"
        if action == "sync_daily_send_orders.lock.acquire":
            return {"acquired": True, "lease_ref": "synthetic-lease", "evidence_ref": evidence}
        if action == "ronghui.send_order.read_page":
            return {"items": rows, "total": len(rows), "evidence_ref": evidence}
        if action == "sync_daily_send_orders.lock.release":
            return {"committed": True, "released": True, "evidence_ref": evidence}
        pytest.fail("invalid source must not read or mutate a publication target")

    with pytest.raises(ValueError, match="identity"):
        _load_action().run_action({"target_date": DAY}, broker)
    assert calls[-1] == "sync_daily_send_orders.lock.release"
    with pytest.raises(ValueError, match="identity"):
        _console_waybill_records([Send_order.normalize_record(row) for row in rows],
                                target_date=date.fromisoformat(DAY))


def test_host_uses_the_china_business_date_for_midnight_timestamp():
    value = int(datetime(2026, 5, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    assert _date_text(value) == DAY


@pytest.mark.parametrize("rows", [
    [{"record_ref": "one", "fields": {"运单编号": "", "发件日期": DAY}}],
    [{"record_ref": "one", "fields": {"运单编号": "R001", "发件日期": DAY}},
     {"record_ref": "two", "fields": {"运单编号": "R001", "发件日期": "2026-05-11"}}],
])
def test_existing_bitable_missing_or_ambiguous_identity_is_not_arbitrarily_updated(rows):
    with pytest.raises(ValueError, match="identity"):
        _load_action()._existing_waybill_records(rows, {"R001"}, target_date=DAY)


@pytest.mark.parametrize("explicit_page", [False, True])
def test_runtime_excludes_receipt_rows_before_required_cost_parsing(monkeypatch, explicit_page):
    rows = [
        _source(GUEST_FREIGHT=0),
        {"BILL_CODE": "H001", "REGISTER_DATE": DAY},
        {"BILL_CODE": " = 'hr002' ", "REGISTER_DATE": DAY, "GUEST_FREIGHT": "***"},
    ]
    monkeypatch.setattr(Send_order, "login_as_daxiang", lambda **_: object())
    monkeypatch.setattr(Send_order, "fetch_send_orders", lambda *_, **__: {"data": rows, "total": 3})
    params = {"target_date": DAY}
    if explicit_page:
        params["page_index"] = 0
    result = Send_order.run_once(params)
    assert len(result) == 1 and result[0]["运单编号"] == "R-SYNTHETIC-001"
    assert result[0]["录单运费"] == 0


@pytest.mark.parametrize("payload", [
    {}, {"data": None}, {"data": {}}, {"data": [None]}, {"data": [{}]},
    {"data": [{"BILL_CODE": " "}]}, {"data": [{"BILL_CODE": {"text": "R001"}}]},
    {"data": [{"BILL_CODE": "H001"}, {"BILL_CODE": " = 'H001' "}]},
])
def test_runtime_source_validation_does_not_silently_drop_invalid_rows(payload):
    with pytest.raises(ValueError):
        Send_order.normalize_records(payload)


@pytest.mark.parametrize("receipt_date", [DAY, "2026-05-11"])
def test_direct_day_source_filters_receipts_but_still_validates_all_source_dates(monkeypatch, receipt_date):
    from types import SimpleNamespace
    from plugin_core_adapters.waybill_query import build_reviewed_provider_source
    from shared.waybill_source_coverage import WaybillSourceScope

    scope = WaybillSourceScope("ronghui", "fixture/send", "fixture/all", "fixture-account")
    rows = [_source(), {"BILL_CODE": "H001", "REGISTER_DATE": receipt_date}]
    monkeypatch.setattr(Send_order, "TMSAuth", lambda **_: SimpleNamespace(login_and_get_session=lambda: object()))
    monkeypatch.setattr(Send_order, "fetch_send_orders", lambda *_, **__: {"data": rows, "total": 2})
    source = build_reviewed_provider_source(
        scope=scope,
        descriptor={"account_id": scope.account_id, "system": "ronghui", "session_profile": "fixture"},
        verify_scope=lambda: scope,
    )
    if receipt_date != DAY:
        with pytest.raises(ValueError, match="SOURCE_DATE_MISMATCH"):
            source.fetch_day(date.fromisoformat(DAY))
    else:
        result, total = source.fetch_day(date.fromisoformat(DAY))
        assert total == len(result) == 1
        assert result[0]["waybill_no"] == "R-SYNTHETIC-001"
