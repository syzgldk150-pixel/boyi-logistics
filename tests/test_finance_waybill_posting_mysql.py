"""Boyi waybill confirmation and posting (migration 070) on loopback MySQL."""
from __future__ import annotations

import os
from decimal import Decimal

import pytest

from shared.finance import FinanceError
from tests.test_finance_gl_mysql import ACTOR, database, post, repo, require_loopback, rid, start, voucher  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")


@pytest.fixture
def waybills(database):  # noqa: F811
    created = []

    def make(waybill_no, *, open_date="2026-10-05", payment_method="寄付", freight_fee="20.00", transfer_fee="",
             sender_name="示例寄件人", sender_phone="00000000000", linked_platform=None, status="pending"):
        with database._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO boyi_waybills (waybill_no, source, open_date, sender_name, sender_phone, receiver_name,"
                " receiver_address, goods_name_lines, package_type_lines, quantity_lines, remark, payment_method,"
                " freight_fee, transfer_fee, status, linked_platform, created_at, updated_at)"
                " VALUES (%s, 'manual', %s, %s, %s, '测试收件人', '测试地址', '测试货物', '纸箱', '1', '', %s, %s, %s, %s, %s, NOW(), NOW())",
                (waybill_no, open_date, sender_name, sender_phone, payment_method, freight_fee, transfer_fee, status, linked_platform),
            )
            connection.commit()
            created.append(cursor.lastrowid)
            return cursor.lastrowid

    yield make
    with database._connection() as connection, connection.cursor() as cursor:
        for waybill_id in created:
            cursor.execute("DELETE FROM boyi_waybills WHERE id=%s", (waybill_id,))
        connection.commit()


def edit(database, waybill_id, **fields):  # noqa: F811
    with database._connection() as connection, connection.cursor() as cursor:
        assignments = ", ".join(f"{key}=%s" for key in fields)
        cursor.execute(f"UPDATE boyi_waybills SET {assignments} WHERE id=%s", (*fields.values(), waybill_id))
        connection.commit()


def row_of(repo, waybill_id, month="2026-10"):  # noqa: F811
    return next(r for r in repo.list_waybill_postings(month=month)["rows"] if r["waybill_id"] == waybill_id)


def confirm(repo, waybill_id, month="2026-10", **extra):  # noqa: F811
    row = row_of(repo, waybill_id, month)
    return repo.confirm_waybill(waybill_id, {"expected_fingerprint": row["fingerprint"], **extra}, request_id=rid(), actor=ACTOR)


def gl(repo, month="2026-10"):  # noqa: F811
    return {row["code"]: row for row in repo.trial_balance(month)["rows"]}


def test_seeded_rules_and_parties(repo):  # noqa: F811
    rules = {r["payment_method"]: r for r in repo.list_doc_rules()}
    assert (rules["寄付"]["receivable_account"], rules["寄付"]["party_mode"]) == ("112203", "fixed")
    assert (rules["到付"]["party_mode"], rules["月结"]["party_mode"]) == ("linked_platform", "sender_customer")
    platforms = {p["platform_code"]: p["name"] for p in repo.list_parties(party_type="platform")}
    assert platforms == {"best": "百世", "ronghui": "融辉", "yunda": "韵达"}


def test_every_waybill_waits_for_confirmation_and_blank_amounts_cannot_post(repo, waybills, database):  # noqa: F811
    start(repo)
    blank = waybills("BYT0001", freight_fee="")
    filled = waybills("BYT0002", freight_fee="20", transfer_fee="5.5")
    listing = repo.list_waybill_postings(month="2026-10")
    assert listing["counts"]["pending"] == 2 and gl(repo) == {}
    assert row_of(repo, blank)["problems"] == ["运费和中转费都未填写，请先补录金额"]
    with pytest.raises(FinanceError) as refused:
        confirm(repo, blank)
    assert refused.value.code == "WAYBILL_NOT_READY"
    result = confirm(repo, filled)
    assert result["status"] == "posted" and result["total_amount"] == "25.5000"
    balance = gl(repo)
    assert balance["112203"]["closing_debit"] == "25.5000"
    assert balance["500101"]["closing_credit"] == "20.0000" and balance["500102"]["closing_credit"] == "5.5000"
    jifu = next(p for p in repo.list_parties(party_type="customer") if p["name"] == "寄付散客")
    assert [i["waybill_no"] for i in repo.open_items(party_id=jifu["id"], as_of="2026-10-31")["items"]] == ["BYT0002"]
    # Amount filled in later through the waybill edit, then confirmed.
    edit(database, blank, freight_fee="30")
    assert confirm(repo, blank)["total_amount"] == "30.0000"
    assert row_of(repo, blank)["status"] == "confirmed"
    with pytest.raises(FinanceError) as twice:
        confirm(repo, blank)
    assert twice.value.code == "ALREADY_CONFIRMED"


def test_stale_fingerprint_is_refused(repo, waybills, database):  # noqa: F811
    start(repo)
    waybill = waybills("BYT0003")
    seen = row_of(repo, waybill)["fingerprint"]
    edit(database, waybill, freight_fee="99")
    with pytest.raises(FinanceError) as refused:
        repo.confirm_waybill(waybill, {"expected_fingerprint": seen}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "WAYBILL_CHANGED"
    # Unrelated fields do not change the finance fingerprint.
    fingerprint = row_of(repo, waybill)["fingerprint"]
    edit(database, waybill, receiver_name="另一个收件人")
    assert row_of(repo, waybill)["fingerprint"] == fingerprint


def test_party_resolution_for_collect_and_monthly(repo, waybills):  # noqa: F811
    start(repo)
    collect = waybills("BYT0004", payment_method="到付", linked_platform="ronghui")
    unlinked = waybills("BYT0005", payment_method="到付")
    monthly = waybills("BYT0006", payment_method="月结", sender_name="月结甲", sender_phone="00000000001")
    assert row_of(repo, collect)["suggested_party_name"] == "融辉"
    confirm(repo, collect)
    with pytest.raises(FinanceError) as no_platform:
        confirm(repo, unlinked)
    assert no_platform.value.code == "PARTY_REQUIRED"
    yunda = next(p for p in repo.list_parties(party_type="platform") if p["platform_code"] == "yunda")
    assert confirm(repo, unlinked, party_id=yunda["id"])["party_id"] == yunda["id"]
    with pytest.raises(FinanceError) as no_customer:
        confirm(repo, monthly)
    assert no_customer.value.code == "PARTY_REQUIRED"
    customer = repo.create_party({"party_type": "customer", "name": "月结甲公司", "boyi_sender_name": "月结甲",
                                  "boyi_sender_phone": "00000000001"}, request_id=rid(), actor=ACTOR)
    assert row_of(repo, monthly)["suggested_party_id"] == customer["id"]
    confirm(repo, monthly)
    assert gl(repo)["112201"]["closing_debit"] == "20.0000" and gl(repo)["112202"]["closing_debit"] == "40.0000"
    assert repo.party_balance_check() == []


def test_edit_after_confirmation_reposts_and_cancellation_reverses(repo, waybills, database):  # noqa: F811
    start(repo)
    waybill = waybills("BYT0007", freight_fee="20")
    first = confirm(repo, waybill)
    edit(database, waybill, freight_fee="35")
    assert row_of(repo, waybill)["status"] == "changed"
    second = confirm(repo, waybill)
    assert second["voucher_id"] != first["voucher_id"] and second["total_amount"] == "35.0000"
    assert repo.get_voucher(first["voucher_id"])["status"] == "cancelled"
    assert gl(repo)["500101"]["closing_credit"] == "35.0000"
    edit(database, waybill, status="cancelled")
    assert row_of(repo, waybill)["status"] == "cancelled_posted"
    assert confirm(repo, waybill)["status"] == "reversed"
    assert gl(repo) == {} and row_of(repo, waybill)["status"] == "reversed"
    assert repo.party_balance_check() == []


def test_allocated_waybill_must_be_unallocated_before_repost(repo, waybills, database):  # noqa: F811
    start(repo)
    waybill = waybills("BYT0008", freight_fee="20")
    confirm(repo, waybill)
    jifu = next(p for p in repo.list_parties(party_type="customer") if p["name"] == "寄付散客")["id"]
    post(repo, voucher(("1001", "20", ""), ("112203", "", "20", {"party_id": jifu}), date="2026-10-05"))
    daily = repo.allocate_daily({"party_id": jifu, "account_code": "112203", "date": "2026-10-05"}, request_id=rid(), actor=ACTOR)
    assert daily["allocation"]["method"] == "daily_aggregate" and daily["difference"] == "0.0000"
    edit(database, waybill, freight_fee="25")
    with pytest.raises(FinanceError) as allocated:
        confirm(repo, waybill)
    assert allocated.value.code == "VOUCHER_HAS_ALLOCATIONS"
    repo.reverse_allocation(daily["allocation"]["id"], {"reason": "改单"}, request_id=rid(), actor=ACTOR)
    assert confirm(repo, waybill)["total_amount"] == "25.0000"


def test_daily_aggregate_leaves_the_difference_open(repo, waybills):  # noqa: F811
    start(repo)
    for number, fee in (("BYT0009", "20"), ("BYT0010", "15")):
        confirm(repo, waybills(number, freight_fee=fee, open_date="2026-10-06"))
    jifu = next(p for p in repo.list_parties(party_type="customer") if p["name"] == "寄付散客")["id"]
    post(repo, voucher(("101201", "30", ""), ("112203", "", "30", {"party_id": jifu}), date="2026-10-06"))
    daily = repo.allocate_daily({"party_id": jifu, "account_code": "112203", "date": "2026-10-06"}, request_id=rid(), actor=ACTOR)
    assert (daily["receivable"], daily["received"], daily["difference"]) == ("35.0000", "30.0000", "5.0000")
    left = repo.open_items(party_id=jifu, account_code="112203", as_of="2026-10-31")["items"]
    assert sum(Decimal(i["outstanding"]) for i in left) == Decimal("5")
    assert repo.party_balance_check() == []


def test_zero_amount_waybill_is_confirmed_without_voucher_and_batch_reports_each(repo, waybills):  # noqa: F811
    start(repo)
    free = waybills("BYT0011", freight_fee="0")
    good = waybills("BYT0012", freight_fee="12")
    blank = waybills("BYT0013", freight_fee="")
    rows = {r["waybill_id"]: r for r in repo.list_waybill_postings(month="2026-10")["rows"]}
    result = repo.confirm_waybills({"items": [{"waybill_id": i, "expected_fingerprint": rows[i]["fingerprint"]} for i in (free, good, blank)]},
                                   request_id=rid(), actor=ACTOR)
    assert (result["succeeded"], result["failed"]) == (2, 1)
    by_id = {r["waybill_id"]: r for r in result["results"]}
    assert by_id[free]["status"] == "zero" and by_id[free]["voucher_id"] is None
    assert by_id[blank]["error_code"] == "WAYBILL_NOT_READY"
    assert gl(repo)["112203"]["closing_debit"] == "12.0000"


def test_rule_update_validates_accounts(repo):  # noqa: F811
    rule = next(r for r in repo.list_doc_rules() if r["payment_method"] == "月结")
    body = {key: rule[key] for key in ("receivable_account", "freight_income_account", "transfer_income_account", "party_mode")}
    with pytest.raises(FinanceError) as bad:
        repo.update_doc_rule("月结", {**body, "receivable_account": "1001", "expected_revision": rule["revision"]}, request_id=rid(), actor=ACTOR)
    assert bad.value.code == "DOC_RULE_INVALID_ACCOUNT"
    updated = repo.update_doc_rule("月结", {**body, "party_mode": "choose", "expected_revision": rule["revision"]}, request_id=rid(), actor=ACTOR)
    assert updated["party_mode"] == "choose" and updated["revision"] == rule["revision"] + 1
    repo.update_doc_rule("月结", {**body, "expected_revision": updated["revision"]}, request_id=rid(), actor=ACTOR)
