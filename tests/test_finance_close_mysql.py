"""Period close, reopen and statements (migration 072) on loopback MySQL."""
from __future__ import annotations

import datetime as dt
import os

import pytest

from shared.finance import FinanceError
from tests.test_finance_gl_mysql import ACTOR, database, post, repo, require_loopback, rid, start, voucher  # noqa: F401
from tests.test_finance_intake_mysql import online_source, rows, submit, batch  # noqa: F401
from tests.test_finance_waybill_posting_mysql import waybills  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")


def october(repo):  # noqa: F811
    start(repo)
    post(repo, voucher(("1001", "1000", ""), ("3001", "", "1000"), date="2026-10-01"))
    post(repo, voucher(("1001", "300", "", {"waybill_no": "BY1"}), ("500101", "", "300", {"waybill_no": "BY1"}), date="2026-10-05"))
    post(repo, voucher(("540101", "120", "", {"waybill_no": "BY1"}), ("1001", "", "120"), date="2026-10-06"))
    post(repo, voucher(("560201", "30", ""), ("1001", "", "30"), date="2026-10-20"))


def close(repo, month):  # noqa: F811
    return repo.close_period(month, {}, request_id=rid(), actor=ACTOR)


def line(report, key, column="month"):
    return next(row[column] for row in report["rows"] if row["key"] == key)


def test_statements_reconcile_with_the_ledger(repo):  # noqa: F811
    october(repo)
    pl = repo.profit_loss("2026-10")
    assert (line(pl, "revenue"), line(pl, "cost"), line(pl, "admin_expense"), pl["net_profit"]) == (
        "300.0000", "120.0000", "30.0000", "150.0000")
    sheet = repo.balance_sheet("2026-10")
    assert sheet["balanced"] and sheet["total_assets"] == "1150.0000"
    assert next(r["amount"] for r in sheet["equity"] if r["key"] == "retained_earnings") == "150.0000"
    flow = repo.cash_flow("2026-10")
    by = {r["category"]: r for r in flow["rows"]}
    assert (by["operating"]["inflow"], by["operating"]["outflow"], by["financing"]["inflow"]) == ("300.0000", "150.0000", "1000.0000")
    assert flow["balanced"] and flow["net_change"] == "1150.0000"
    margin = repo.waybill_margin(start_date="2026-10-01", end_date="2026-10-31")
    assert margin["rows"] == [{"waybill_no": "BY1", "revenue": "300.0000", "cost": "120.0000", "margin": "180.0000"}]
    summary = repo.ledger_summary(start_date=dt.date(2026, 10, 1), end_date=dt.date(2026, 10, 31))
    assert (summary["revenue"], summary["cost"], summary["expense"], summary["profit"]) == ("300.0000", "120.0000", "30.0000", "150.0000")
    assert "所查期间尚未全部封账，金额可能变动" in summary["warnings"]


def test_close_moves_profit_snapshots_and_locks_the_month(repo):  # noqa: F811
    october(repo)
    status = repo.period_status("2026-10")
    assert status["can_close"], [c for c in status["checks"] if not c["ok"]]
    closed = close(repo, "2026-10")
    assert closed["net_profit"] == "150.0000" and closed["closing_voucher_id"]
    tb = {row["code"]: row for row in repo.trial_balance("2026-10")["rows"]}
    assert tb["3103"]["closing_credit"] == "150.0000" and tb["5001"]["closing_credit"] == "0.0000"
    assert repo.profit_loss("2026-10")["net_profit"] == "150.0000"  # closing vouchers are not revenue or cost
    assert repo.balance_sheet("2026-10")["balanced"]
    after = repo.period_status("2026-10")
    assert after["status"] == "closed" and after["snapshot_matches"] is True and after["closes"][0]["status"] == "closed"
    draft = repo.create_draft(voucher(("1001", "1", ""), ("500101", "", "1"), date="2026-10-31"), request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as locked:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert locked.value.code == "PERIOD_CLOSED"
    repo.delete_draft(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as twice:
        close(repo, "2026-10")
    assert twice.value.code == "PERIOD_ALREADY_CLOSED"


def test_close_order_reopen_and_close_again(repo):  # noqa: F811
    october(repo)
    with pytest.raises(FinanceError) as order:
        close(repo, "2026-11")
    assert order.value.code == "PERIOD_CHECKS_FAILED"
    assert any(c["key"] == "previous_closed" for c in order.value.details["checks"] if not c["ok"])
    close(repo, "2026-10")
    close(repo, "2026-11")
    with pytest.raises(FinanceError) as reopen_order:
        repo.reopen_period("2026-10", {"reason": "补记"}, request_id=rid(), actor=ACTOR)
    assert reopen_order.value.code == "PERIOD_REOPEN_ORDER"
    with pytest.raises(FinanceError):
        repo.reopen_period("2026-11", {}, request_id=rid(), actor=ACTOR)
    repo.reopen_period("2026-11", {"reason": "补记十一月"}, request_id=rid(), actor=ACTOR)
    reopened = repo.reopen_period("2026-10", {"reason": "补记十月费用"}, request_id=rid(), actor=ACTOR)
    assert reopened["version"] == 2
    tb = {row["code"]: row for row in repo.trial_balance("2026-10")["rows"]}
    assert "3103" not in tb and tb["5001"]["closing_credit"] == "300.0000"
    post(repo, voucher(("560201", "10", ""), ("1001", "", "10"), date="2026-10-25"))
    again = close(repo, "2026-10")
    assert again["version"] == 2 and again["net_profit"] == "140.0000"
    history = repo.period_status("2026-10")["closes"]
    assert [(c["version"], c["status"]) for c in history] == [(2, "closed"), (1, "reopened")]


def test_failed_checks_refuse_the_close(repo, waybills):  # noqa: F811
    october(repo)
    repo.create_draft(voucher(("1001", "5", ""), ("500101", "", "5"), date="2026-10-09"), request_id=rid(), actor=ACTOR)
    online_source(repo)
    submit(repo, batch(rows(("Z1", "2026-10-10", "8", "未知项目")), start_="2026-10-01", end="2026-10-31"))
    waybills("BYC001", open_date="2026-10-11")
    status = repo.period_status("2026-10")
    failed = {c["key"] for c in status["checks"] if not c["ok"]}
    assert {"no_drafts", "suspense_zero", "waybills_confirmed", "no_open_items"} <= failed
    assert not status["can_close"]
    with pytest.raises(FinanceError) as refused:
        close(repo, "2026-10")
    assert refused.value.code == "PERIOD_CHECKS_FAILED"


def test_source_coverage_and_fund_reconciliation(repo):  # noqa: F811
    start(repo)
    online_source(repo)
    repo.save_line_rule({"provider": "ronghui", "item_l1": "利息", "direction": "in", "counter_account": "560302"},
                        request_id=rid(), actor=ACTOR)
    partial = batch(rows(("I1", "2026-10-03", "2", "利息")), start_="2026-10-01", end="2026-10-15")
    submit(repo, partial)
    checks = {c["key"]: c for c in repo.period_status("2026-10")["checks"]}
    assert not checks["sources_complete"]["ok"] and checks["fund_unverifiable"]["blocking"] is False
    rest = batch(rows(("I2", "2026-10-20", "3", "利息")), start_="2026-10-16", end="2026-10-31")
    rest["declares"]["opening_closing"] = True
    rest["opening_balance"], rest["closing_balance"] = "3.0000", "6.0000"
    submit(repo, rest)
    checks = {c["key"]: c for c in repo.period_status("2026-10")["checks"]}
    assert checks["sources_complete"]["ok"] and not checks["fund_reconciled"]["ok"]
    assert "101203" in checks["fund_reconciled"]["detail"]


def test_december_close_moves_the_year_profit(repo):  # noqa: F811
    start(repo, "2026-12")
    post(repo, voucher(("1001", "80", ""), ("500101", "", "80"), date="2026-12-03"))
    closed = close(repo, "2026-12")
    assert closed["year_end_voucher_id"]
    tb = {row["code"]: row for row in repo.trial_balance("2026-12")["rows"]}
    assert tb["310401"]["closing_credit"] == "80.0000" and tb["3103"]["closing_credit"] == "0.0000"
    assert repo.balance_sheet("2026-12")["balanced"]
    repo.reopen_period("2026-12", {"reason": "复核"}, request_id=rid(), actor=ACTOR)
    assert "310401" not in {row["code"] for row in repo.trial_balance("2026-12")["rows"]}
