"""Finance follow-ups (migration 073): P&L split by dimension, AI review advice storage, xlsx exports."""
from __future__ import annotations

import io
import os

import pytest

from shared.finance import FinanceError
from shared.finance.exports import build_export
from tests.test_finance_gl_mysql import ACTOR, database, post, repo, require_loopback, rid, start, voucher  # noqa: F401
from tests.test_finance_intake_mysql import batch, rows, submit  # noqa: F401
from tests.test_finance_waybill_posting_mysql import confirm, waybills  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")


def line(report, key):
    return next(r for r in report["rows"] if r["key"] == key)


def test_profit_loss_split_by_site_and_business_line(repo, waybills):  # noqa: F811
    start(repo)
    post(repo, voucher(("1001", "100", "", {"site_code": "甲站"}), ("500101", "", "100", {"site_code": "甲站", "business_line": "零担"})))
    post(repo, voucher(("1001", "60", ""), ("500101", "", "60", {"site_code": "乙站", "business_line": "整车"})))
    post(repo, voucher(("560201", "25", ""), ("1001", "", "25")))
    # Source vouchers take the source's default dimensions.
    repo.create_source({"source_key": "rh_split", "display_name": "融辉拆分", "kind": "online", "provider": "ronghui",
                        "producer_instance_id": "plugin-x", "fund_account": "101203", "site_code": "甲站", "business_line": "平台"},
                       request_id=rid(), actor=ACTOR)
    repo.save_line_rule({"provider": "ronghui", "item_l1": "运费", "direction": "out", "counter_account": "540101"}, request_id=rid(), actor=ACTOR)
    submit(repo, batch(rows(("S1", "2026-10-07", "-10", "运费")), key="rh_split"), automation="plugin-x")
    # Boyi waybill vouchers take the posting rule's dimensions.
    rule = next(r for r in repo.list_doc_rules() if r["payment_method"] == "寄付")
    repo.update_doc_rule("寄付", {**{k: rule[k] for k in ("receivable_account", "freight_income_account", "transfer_income_account",
                                                          "party_mode", "fixed_party_id")},
                                 "site_code": "乙站", "business_line": "零担", "expected_revision": rule["revision"]},
                         request_id=rid(), actor=ACTOR)
    confirm(repo, waybills("BYS001", freight_fee="40"))

    by_site = repo.profit_loss("2026-10", split="site_code")
    assert [c["label"] for c in by_site["columns"]] == ["乙站", "甲站", "未指定"]
    revenue = line(by_site, "revenue")
    assert revenue["values"] == {"乙站": "100.0000", "甲站": "100.0000", "": "0.0000"} and revenue["total"] == "200.0000"
    assert line(by_site, "cost")["values"]["甲站"] == "10.0000"
    assert line(by_site, "admin_expense")["values"][""] == "25.0000"
    assert by_site["net_profit"] == repo.profit_loss("2026-10")["net_profit"] == "165.0000"
    by_line = repo.profit_loss("2026-10", split="business_line", scope="year")
    assert {c["label"] for c in by_line["columns"]} == {"零担", "整车", "平台", "未指定"}
    assert line(by_line, "revenue")["values"]["零担"] == "140.0000"
    with pytest.raises(FinanceError):
        repo.profit_loss("2026-10", split="party_id")


def test_review_evidence_and_advice_storage(repo):  # noqa: F811
    start(repo)
    repo.create_source({"source_key": "rh_ai", "display_name": "融辉 AI", "kind": "online", "provider": "ronghui",
                        "producer_instance_id": "plugin-y", "fund_account": "101203"}, request_id=rid(), actor=ACTOR)
    submit(repo, batch(rows(("A1", "2026-10-03", "-3", "短信费", {"counterparty": "平台"}), ("A2", "2026-10-04", "-2", "短信费")),
                       key="rh_ai"), automation="plugin-y")
    review = repo.list_reviews()[0]
    evidence = repo.review_evidence(review["id"])
    assert evidence["review"]["item_l1"] == "短信费" and evidence["review"]["line_count"] == 2
    codes = {a["code"] for a in evidence["candidate_accounts"]}
    assert "540105" in codes and "224199" not in codes and "101203" not in codes
    assert set(evidence["_accounts"]) == codes
    saved = repo.save_review_suggestion(review["id"], {"counter_account": "540105", "party_action": "none", "party_id": None,
                                                       "confidence": "0.86", "reason": "平台短信费属于运单相关成本"}, model="test-model")
    assert saved["model"] == "test-model"
    listed = repo.list_reviews()[0]
    assert listed["suggestion"]["counter_account"] == "540105" and listed["suggested_by_model"] == "test-model"
    repo.resolve_review(review["id"], {"counter_account": "540105"}, request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as closed:
        repo.save_review_suggestion(review["id"], saved["suggestion"], model="test-model")
    assert closed.value.code == "REVIEW_NOT_OPEN"


def test_xlsx_exports_open_with_amounts_as_numbers(repo):  # noqa: F811
    openpyxl = pytest.importorskip("openpyxl")
    start(repo)
    post(repo, voucher(("1001", "88.5", "", {"site_code": "甲站"}), ("500101", "", "88.5", {"site_code": "甲站"})))
    for kind, params in (("trial-balance", {"month": "2026-10"}), ("journal", {"month": "2026-10"}),
                         ("ledger", {"account": "1001", "start": "2026-10-01", "end": "2026-10-31"}),
                         ("profit-loss", {"month": "2026-10"}), ("profit-loss", {"month": "2026-10", "split": "site_code"}),
                         ("balance-sheet", {"month": "2026-10"}), ("cash-flow", {"month": "2026-10"}),
                         ("waybill-margin", {"start": "2026-10-01", "end": "2026-10-31"}),
                         ("aging", {"as_of": "2026-10-31", "kind": "receivable"})):
        name, data = build_export(repo, kind, params)
        sheet = openpyxl.load_workbook(io.BytesIO(data)).active
        assert name.endswith(".xlsx") and sheet.max_row >= 2, kind
    _, data = build_export(repo, "trial-balance", {"month": "2026-10"})
    sheet = openpyxl.load_workbook(io.BytesIO(data)).active
    row = next(r for r in sheet.iter_rows(values_only=True) if r[0] == "1001")
    assert row[6] == 88.5 and sheet["A1"].font.b
