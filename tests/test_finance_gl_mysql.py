"""General ledger (migration 068 + shared.finance) on the isolated loopback MySQL."""
from __future__ import annotations

import os
import threading
from decimal import Decimal
from uuid import uuid4

import pytest

from shared.finance import FinanceActor, FinanceError, FinanceRepository, FinanceValidationError
from tests.mysql_migrated_database import database  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")

ACTOR = FinanceActor("7", "测试会计")
CLEAN = (
    "DELETE FROM finance_account_balances", "DELETE FROM finance_period_closes",
    "DELETE FROM finance_reviews", "DELETE FROM finance_anomalies", "DELETE FROM finance_source_lines",
    "DELETE FROM finance_source_batches", "DELETE FROM finance_source_file_chunks", "DELETE FROM finance_source_files",
    "DELETE FROM finance_line_rules", "DELETE FROM finance_sources",
    "DELETE FROM finance_doc_postings",
    "DELETE FROM finance_party_ledger WHERE against_item_id IS NOT NULL", "DELETE FROM finance_party_ledger",
    "DELETE FROM finance_allocations", "UPDATE finance_voucher_lines SET party_id=NULL",
    "DELETE FROM finance_gl_entries", "DELETE FROM finance_voucher_lines", "DELETE FROM finance_vouchers",
    "DELETE FROM finance_voucher_sequences", "DELETE FROM finance_periods", "DELETE FROM finance_events",
    "DELETE FROM finance_parties WHERE created_by <> 'migration_070'",
)


@pytest.fixture(autouse=True)
def require_loopback():
    if os.environ.get("AGENT_DB_HOST") != "127.0.0.1" or os.environ.get("AGENT_DB_PORT") != "33330":
        pytest.fail("finance GL tests require the isolated loopback MySQL on port 33330")


@pytest.fixture
def repo(database):  # noqa: F811
    with database._connection() as connection, connection.cursor() as cursor:
        for statement in CLEAN:
            cursor.execute(statement)
        cursor.execute("DELETE FROM finance_accounts WHERE code IN ('100201', '100202', '560206')")
        cursor.execute("UPDATE finance_accounts SET is_group=0, requires_waybill=0, frozen=0, is_active=1 WHERE code IN ('1002', '540101', '5602')")
        cursor.execute("UPDATE finance_settings SET start_month=NULL, revision=1 WHERE id=1")
        connection.commit()
    return FinanceRepository(lambda: database._connection())


def rid() -> str:
    return str(uuid4())


def voucher(*lines, date="2026-10-15", voucher_type="manual", summary="测试凭证"):
    return {"voucher_type": voucher_type, "posting_date": date, "summary": summary,
            "lines": [{"account_code": code, "debit": debit, "credit": credit, **extra} for code, debit, credit, *rest in lines
                      for extra in [rest[0] if rest else {}]]}


def start(repo, month="2026-10"):
    settings = repo.get_settings()
    return repo.update_settings({"start_month": month, "expected_revision": settings["revision"]}, request_id=rid(), actor=ACTOR)


def post(repo, payload):
    draft = repo.create_draft(payload, request_id=rid(), actor=ACTOR)
    return repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)


def gl_rows(database, voucher_id=None):  # noqa: F811
    with database._connection() as connection, connection.cursor() as cursor:
        if voucher_id is None:
            cursor.execute("SELECT * FROM finance_gl_entries ORDER BY id")
        else:
            cursor.execute("SELECT * FROM finance_gl_entries WHERE voucher_id=%s ORDER BY id", (voucher_id,))
        return list(cursor.fetchall())


def test_seeded_chart_and_settings(repo):
    accounts = {row["code"]: row for row in repo.list_accounts()}
    assert accounts["1012"]["is_group"] and accounts["101299"]["account_type"] == "transit"
    assert accounts["224199"]["account_type"] == "suspense" and accounts["224199"]["parent_code"] == "2241"
    assert accounts["5001"]["normal_balance"] == "credit" and accounts["5401"]["root_type"] == "profit_loss"
    settings = repo.get_settings()
    assert settings["start_month"] is None and not settings["start_month_locked"]
    assert (settings["suspense_account"], settings["transit_account"]) == ("224199", "101299")


def test_submit_requires_start_month_then_posts_numbered_balanced_entries(repo, database):  # noqa: F811
    draft = repo.create_draft(voucher(("1001", "100.50", ""), ("500101", "", "100.50")), request_id=rid(), actor=ACTOR)
    assert draft["status"] == "draft" and draft["voucher_no"] is None
    with pytest.raises(FinanceError) as refused:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "START_MONTH_REQUIRED"
    start(repo)
    posted = repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert (posted["status"], posted["voucher_no"]) == ("submitted", "记-2026-10-0001")
    assert posted["total_debit"] == posted["total_credit"] == "100.5000"
    rows = gl_rows(database, posted["id"])
    assert [(r["account_code"], r["debit"], r["credit"]) for r in rows] == [
        ("1001", Decimal("100.5000"), Decimal("0.0000")), ("500101", Decimal("0.0000"), Decimal("100.5000"))]
    second = post(repo, voucher(("1001", "1", ""), ("500101", "", "1")))
    assert second["voucher_no"] == "记-2026-10-0002"
    balance = repo.trial_balance("2026-10")
    assert balance["balanced"]
    by_code = {row["code"]: row for row in balance["rows"]}
    assert by_code["1001"]["closing_debit"] == "101.5000"
    assert by_code["5001"]["period_credit"] == "101.5000" and by_code["5001"]["closing_credit"] == "101.5000"


def test_start_month_locks_once_entries_exist(repo):
    settings = start(repo)
    post(repo, voucher(("1001", "5", ""), ("3001", "", "5")))
    assert repo.get_settings()["start_month_locked"]
    with pytest.raises(FinanceError) as refused:
        repo.update_settings({"start_month": "2026-09", "expected_revision": settings["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "START_MONTH_LOCKED"


@pytest.mark.parametrize(("lines", "code"), [
    ((("1001", "10", ""), ("500101", "", "9")), "VOUCHER_UNBALANCED"),
    ((("1012", "10", ""), ("500101", "", "10")), "ACCOUNT_NOT_LEAF"),
    ((("1001", "10", ""),), "VOUCHER_TOO_FEW_LINES"),
])
def test_submit_refusals_leave_no_gl(repo, database, lines, code):  # noqa: F811
    start(repo)
    draft = repo.create_draft(voucher(*lines), request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as refused:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == code
    assert gl_rows(database) == []
    assert repo.get_voucher(draft["id"])["status"] == "draft"


def test_line_must_use_exactly_one_side_and_text_amounts(repo):
    with pytest.raises(FinanceValidationError) as both:
        repo.create_draft(voucher(("1001", "1", "1"), ("500101", "", "1")), request_id=rid(), actor=ACTOR)
    assert both.value.code == "VOUCHER_LINE_SIDE"
    with pytest.raises(FinanceValidationError) as floating:
        repo.create_draft(voucher(("1001", 1.5, ""), ("500101", "", "1.5")), request_id=rid(), actor=ACTOR)
    assert floating.value.code == "AMOUNT_INVALID"
    with pytest.raises(FinanceValidationError) as precise:
        repo.create_draft(voucher(("1001", "1.00001", ""), ("500101", "", "1.00001")), request_id=rid(), actor=ACTOR)
    assert precise.value.code == "AMOUNT_INVALID"


def test_closed_period_refuses_posting_and_cancelling(repo, database):  # noqa: F811
    start(repo)
    posted = post(repo, voucher(("1001", "8", ""), ("500101", "", "8")))
    draft = repo.create_draft(voucher(("1001", "2", ""), ("500101", "", "2")), request_id=rid(), actor=ACTOR)
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE finance_periods SET status='closed' WHERE period_month='2026-10'")
        connection.commit()
    with pytest.raises(FinanceError) as refused:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "PERIOD_CLOSED"
    with pytest.raises(FinanceError) as cancel:
        repo.cancel_voucher(posted["id"], {"expected_revision": posted["revision"], "reason": "错账"}, request_id=rid(), actor=ACTOR)
    assert cancel.value.code == "PERIOD_CLOSED"
    assert len(gl_rows(database)) == 2


def test_cancel_reverses_to_zero_and_keeps_history(repo, database):  # noqa: F811
    start(repo)
    posted = post(repo, voucher(("1001", "30", ""), ("500101", "", "30")))
    with pytest.raises(FinanceValidationError):
        repo.cancel_voucher(posted["id"], {"expected_revision": posted["revision"]}, request_id=rid(), actor=ACTOR)
    cancelled = repo.cancel_voucher(posted["id"], {"expected_revision": posted["revision"], "reason": "录错科目"},
                                    request_id=rid(), actor=ACTOR)
    assert cancelled["status"] == "cancelled" and cancelled["voucher_no"] == "记-2026-10-0001"
    rows = gl_rows(database, posted["id"])
    assert len(rows) == 4 and all(row["is_cancelled"] for row in rows)
    assert sum(row["debit"] - row["credit"] for row in rows) == 0
    assert repo.trial_balance("2026-10")["rows"] == []
    ledger = repo.account_ledger("1001", start_date="2026-10-01", end_date="2026-10-31")
    assert ledger["rows"] == [] and ledger["closing"] == {"direction": "平", "balance": "0.0000"}
    assert [v["status"] for v in repo.journal("2026-10")["vouchers"]] == ["cancelled"]
    with pytest.raises(FinanceError) as again:
        repo.cancel_voucher(posted["id"], {"expected_revision": cancelled["revision"], "reason": "再次"}, request_id=rid(), actor=ACTOR)
    assert again.value.code == "VOUCHER_NOT_SUBMITTED"


def test_request_id_replays_and_refuses_reuse(repo, database):  # noqa: F811
    start(repo)
    request_id = rid()
    payload = voucher(("1001", "3", ""), ("500101", "", "3"))
    first = repo.create_draft(payload, request_id=request_id, actor=ACTOR)
    assert repo.create_draft(payload, request_id=request_id, actor=ACTOR) == first
    with pytest.raises(FinanceError) as reused:
        repo.create_draft(voucher(("1001", "4", ""), ("500101", "", "4")), request_id=request_id, actor=ACTOR)
    assert reused.value.code == "REQUEST_ID_REUSED"
    submit_id = rid()
    posted = repo.submit_voucher(first["id"], {"expected_revision": first["revision"]}, request_id=submit_id, actor=ACTOR)
    assert repo.submit_voucher(first["id"], {"expected_revision": first["revision"]}, request_id=submit_id, actor=ACTOR) == posted
    assert len(gl_rows(database)) == 2


def test_concurrent_submits_post_once(repo, database):  # noqa: F811
    start(repo)
    draft = repo.create_draft(voucher(("1001", "9", ""), ("500101", "", "9")), request_id=rid(), actor=ACTOR)
    barrier, outcomes = threading.Barrier(4), []

    def submit(request_id):
        barrier.wait()
        try:
            outcomes.append(repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=request_id, actor=ACTOR)["voucher_no"])
        except FinanceError as exc:
            outcomes.append(exc.code)

    shared_id = rid()
    threads = [threading.Thread(target=submit, args=(request_id,)) for request_id in (rid(), rid(), shared_id, shared_id)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(gl_rows(database)) == 2
    numbers = [item for item in outcomes if item.startswith("记-")]
    assert set(numbers) == {"记-2026-10-0001"}
    assert all(item in {"VOUCHER_NOT_DRAFT", "FINANCE_REVISION_CONFLICT"} for item in outcomes if not item.startswith("记-"))


def test_opening_voucher_only_in_start_month(repo):
    start(repo)
    payload = voucher(("1001", "500", ""), ("310401", "", "500"), date="2026-11-01", voucher_type="opening")
    draft = repo.create_draft(payload, request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as refused:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "OPENING_OUTSIDE_START_MONTH"
    opening = post(repo, voucher(("1001", "500", ""), ("310401", "", "500"), date="2026-10-01", voucher_type="opening"))
    assert opening["voucher_type"] == "opening"
    with pytest.raises(FinanceError) as early:
        repo.create_draft(voucher(("1001", "1", ""), ("500101", "", "1"), date="2026-09-30"), request_id=rid(), actor=ACTOR)
    assert early.value.code == "BEFORE_START_MONTH"


def test_account_tree_rules(repo):
    created = repo.create_account({"code": "100201", "name": "工商银行"}, request_id=rid(), actor=ACTOR)
    assert (created["root_type"], created["normal_balance"], created["account_type"]) == ("asset", "debit", "bank")
    accounts = {row["code"]: row for row in repo.list_accounts()}
    assert accounts["1002"]["is_group"]
    start(repo)
    post(repo, voucher(("100201", "10", ""), ("3001", "", "10")))
    with pytest.raises(FinanceError) as used:
        repo.create_account({"code": "10020101", "name": "子户", "parent_code": "100201"}, request_id=rid(), actor=ACTOR)
    assert used.value.code == "ACCOUNT_HAS_ENTRIES"
    with pytest.raises(FinanceError) as delete:
        repo.delete_account("100201", {"expected_revision": created["revision"]}, request_id=rid(), actor=ACTOR)
    assert delete.value.code == "ACCOUNT_HAS_ENTRIES"
    with pytest.raises(FinanceValidationError):
        repo.create_account({"code": "100302", "name": "错位", "parent_code": "1002"}, request_id=rid(), actor=ACTOR)
    spare = repo.create_account({"code": "100202", "name": "建设银行"}, request_id=rid(), actor=ACTOR)
    repo.delete_account("100202", {"expected_revision": spare["revision"]}, request_id=rid(), actor=ACTOR)
    frozen = repo.update_account("100201", {"frozen": True, "expected_revision": created["revision"]}, request_id=rid(), actor=ACTOR)
    assert frozen["frozen"]
    draft = repo.create_draft(voucher(("100201", "1", ""), ("3001", "", "1")), request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as refused:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "ACCOUNT_FROZEN"


def test_waybill_dimension_and_ledger_running_balance(repo):
    account = next(row for row in repo.list_accounts() if row["code"] == "540101")
    repo.update_account("540101", {"requires_waybill": True, "expected_revision": account["revision"]}, request_id=rid(), actor=ACTOR)
    start(repo)
    draft = repo.create_draft(voucher(("540101", "6", ""), ("1001", "", "6")), request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as refused:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "DIMENSION_REQUIRED"
    post(repo, voucher(("1001", "100", ""), ("3001", "", "100"), date="2026-10-01"))
    post(repo, voucher(("540101", "6", "", {"waybill_no": "BY0001"}), ("1001", "", "6"), date="2026-10-02"))
    post(repo, voucher(("1001", "20", ""), ("500101", "", "20"), date="2026-11-03"))
    ledger = repo.account_ledger("1001", start_date="2026-10-02", end_date="2026-11-30")
    assert ledger["opening"] == {"direction": "借", "balance": "100.0000"}
    assert [(r["credit"], r["debit"], r["balance"]) for r in ledger["rows"]] == [
        ("6.0000", "0.0000", "94.0000"), ("0.0000", "20.0000", "114.0000")]
    november = {row["code"]: row for row in repo.trial_balance("2026-11")["rows"]}
    assert november["1001"]["opening_debit"] == "94.0000" and november["1001"]["closing_debit"] == "114.0000"
    assert november["3001"]["opening_credit"] == "100.0000"


def test_inactive_parent_blocks_posting_to_its_children(repo):
    start(repo)
    group = next(row for row in repo.list_accounts() if row["code"] == "5602")
    repo.update_account("5602", {"is_active": False, "expected_revision": group["revision"]}, request_id=rid(), actor=ACTOR)
    draft = repo.create_draft(voucher(("560201", "50", ""), ("1001", "", "50")), request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as refused:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "ACCOUNT_INACTIVE"


def test_default_accounts_are_protected_and_typed(repo):
    settings = repo.get_settings()
    with pytest.raises(FinanceValidationError) as typed:
        repo.update_settings({"suspense_account": "1001", "expected_revision": settings["revision"]}, request_id=rid(), actor=ACTOR)
    assert typed.value.code == "FINANCE_INVALID_DEFAULT_ACCOUNT"
    suspense = next(row for row in repo.list_accounts() if row["code"] == "224199")
    with pytest.raises(FinanceError) as protected:
        repo.update_account("224199", {"is_active": False, "expected_revision": suspense["revision"]}, request_id=rid(), actor=ACTOR)
    assert protected.value.code == "ACCOUNT_IS_DEFAULT"
