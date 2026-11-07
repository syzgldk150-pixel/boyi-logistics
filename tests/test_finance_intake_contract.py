"""Standard source contract v1 validation (pure)."""
import copy
from decimal import Decimal

import pytest

from shared.finance.intake.contract import IntakeError, validate_batch


def batch(**overrides):
    base = {
        "contract": "finance.source_batch/v1", "source_key": "demo_bank",
        "period": {"start": "2026-10-01", "end": "2026-10-31"},
        "declares": {"balance_chain": True, "summary": True, "opening_closing": True, "complete_period": True},
        "opening_balance": "100.0000", "closing_balance": "87.5000",
        "summary": [{"item_l1": "运费", "item_l2": "", "direction": "out", "count": 1, "amount": "12.5"}],
        "rows": [{"source_key": "T1", "business_date": "2026-10-03", "amount": "-12.5", "item_l1": "运费",
                  "balance_before": "100", "balance_after": "87.5", "raw": {"原始": "行"}}],
        "evidence": {"kind": "http_observation", "refs": ["ref-1"]},
    }
    base.update(overrides)
    return base


def test_valid_batch_records_every_check():
    result = validate_batch(batch())
    assert result["inflow"] == 0 and result["outflow"] == Decimal("12.5")
    assert [c["check"] for c in result["checks"] if c["ran"]] == ["schema", "source_key_unique", "balance_chain", "opening_closing",
                                                                  "summary", "complete_period"]
    assert len(result["content_sha256"]) == 64 and result["rows"][0]["waybill_no"] is None


def test_undeclared_checks_do_not_run():
    payload = batch(declares={"balance_chain": False, "summary": False, "opening_closing": False, "complete_period": False},
                    opening_balance=None, closing_balance=None, summary=None)
    payload["rows"][0].pop("balance_before")
    result = validate_batch(payload)
    assert {c["check"]: c["ran"] for c in result["checks"]}["balance_chain"] is False


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda b: b["rows"][0].update(balance_after="80"), "INTAKE_BALANCE_CHAIN"),
    (lambda b: b.update(closing_balance="90"), "INTAKE_OPENING_CLOSING"),
    (lambda b: b["summary"][0].update(amount="10"), "INTAKE_SUMMARY"),
    (lambda b: b["rows"].append(dict(b["rows"][0])), "INTAKE_DUPLICATE"),
    (lambda b: b["rows"][0].update(amount=12.5), "AMOUNT_INVALID"),
    (lambda b: b["rows"][0].update(amount="1.00001"), "AMOUNT_INVALID"),
    (lambda b: b["rows"][0].update(business_date="2026-11-01"), "INTAKE_SCHEMA"),
    (lambda b: b.update(contract="other/v1"), "INTAKE_SCHEMA"),
    (lambda b: b["declares"].pop("summary"), "INTAKE_SCHEMA"),
    (lambda b: b.update(evidence={"kind": "screenshot", "refs": ["x"]}), "INTAKE_EVIDENCE"),
])
def test_refusals(mutate, code):
    payload = copy.deepcopy(batch())
    mutate(payload)
    with pytest.raises(IntakeError) as refused:
        validate_batch(payload)
    assert refused.value.code == code


def test_chain_must_be_continuous():
    payload = batch(declares={"balance_chain": True, "summary": False, "opening_closing": False, "complete_period": True},
                    opening_balance=None, closing_balance=None, summary=None)
    payload["rows"].append({"source_key": "T2", "business_date": "2026-10-04", "amount": "5",
                            "balance_before": "90", "balance_after": "95"})
    with pytest.raises(IntakeError) as refused:
        validate_batch(payload)
    assert refused.value.code == "INTAKE_BALANCE_CHAIN"
