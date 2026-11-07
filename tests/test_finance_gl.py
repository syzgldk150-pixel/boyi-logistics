"""Pure general-ledger rules (no database)."""
import datetime as dt
from decimal import Decimal

import pytest

from shared.finance import FinanceActor, FinanceRepository, FinanceValidationError
from shared.finance.gl.vouchers import parse_voucher
from shared.finance.models import month_bounds, parse_account_code, parse_amount, parse_month, parse_request_id


def payload(**overrides):
    base = {"posting_date": "2026-10-15", "summary": "收运费",
            "lines": [{"account_code": "1001", "debit": "12.50"}, {"account_code": "500101", "credit": "12.5"}]}
    base.update(overrides)
    return base


def test_parse_voucher_keeps_decimals_and_derives_period():
    voucher = parse_voucher(payload())
    assert voucher["period_month"] == "2026-10" and voucher["posting_date"] == dt.date(2026, 10, 15)
    assert [(line["debit"], line["credit"]) for line in voucher["lines"]] == [(Decimal("12.50"), 0), (0, Decimal("12.5"))]
    assert voucher["lines"][0]["summary"] == "收运费"


@pytest.mark.parametrize(("change", "code"), [
    ({"voucher_type": "closing"}, "VOUCHER_TYPE_INVALID"),
    ({"lines": []}, "VOUCHER_LINES_REQUIRED"),
    ({"lines": [{"account_code": "1001", "debit": "1", "credit": "1"}]}, "VOUCHER_LINE_SIDE"),
    ({"lines": [{"account_code": "1001"}]}, "VOUCHER_LINE_SIDE"),
    ({"lines": [{"account_code": "1001", "debit": "-1"}]}, "AMOUNT_INVALID"),
    ({"lines": [{"account_code": "1001", "debit": 0.1}]}, "AMOUNT_INVALID"),
    ({"lines": [{"account_code": "1001", "debit": "1", "party_id": "3"}]}, "FINANCE_INVALID_REQUEST"),
    ({"lines": [{"account_code": "1001", "debit": "1", "source_line_id": 3}]}, "FINANCE_INVALID_REQUEST"),
    ({"lines": [{"account_code": "10", "debit": "1"}]}, "FINANCE_INVALID_ACCOUNT_CODE"),
    ({"posting_date": "2026/10/15"}, "FINANCE_INVALID_DATE"),
])
def test_parse_voucher_refusals(change, code):
    with pytest.raises(FinanceValidationError) as refused:
        parse_voucher(payload(**change))
    assert refused.value.code == code


def test_value_parsers():
    assert parse_amount("1,234.5678", "金额") == Decimal("1234.5678")
    assert parse_amount("", "金额") == 0
    for bad in ("1.23456", "abc", True, "NaN", "1e20"):
        with pytest.raises(FinanceValidationError):
            parse_amount(bad, "金额")
    assert parse_month("2026-12") == "2026-12"
    assert month_bounds("2026-12") == (dt.date(2026, 12, 1), dt.date(2026, 12, 31))
    assert month_bounds("2028-02")[1] == dt.date(2028, 2, 29)
    assert parse_account_code("10120101") == "10120101"
    for bad in ("1012010", "7001", "abcd"):
        with pytest.raises(FinanceValidationError):
            parse_account_code(bad)
    request_id = "3F2504E0-4F89-41D3-9A0C-0305E82C3301"
    assert parse_request_id(request_id) == request_id.lower()
    with pytest.raises(FinanceValidationError):
        parse_request_id("not-a-uuid")


def test_actor_and_repository_construction():
    with pytest.raises(FinanceValidationError):
        FinanceActor("", "无名")
    assert FinanceActor("7", "").label == "7"
    with pytest.raises(TypeError):
        FinanceRepository(None)
