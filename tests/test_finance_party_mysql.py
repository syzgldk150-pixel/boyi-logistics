"""Party layer (migration 069): open items, allocation, aging and statements on loopback MySQL."""
from __future__ import annotations

import os
import threading
from decimal import Decimal

import pytest

from shared.finance import FinanceError, FinanceValidationError
from tests.test_finance_gl_mysql import ACTOR, database, post, repo, require_loopback, rid, start, voucher  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")


def party(repo, name="测试月结客户", party_type="customer", **extra):  # noqa: F811
    return repo.create_party({"party_type": party_type, "name": name, **extra}, request_id=rid(), actor=ACTOR)


def line(code, debit, credit, **extra):
    return (code, debit, credit, extra)


def outstanding(repo, party_id, account="112201", as_of="2026-12-31"):  # noqa: F811
    return {item["item_id"]: item["outstanding"] for item in repo.open_items(party_id=party_id, account_code=account, as_of=as_of)["items"]}


def test_migration_marks_party_accounts_and_party_crud(repo):  # noqa: F811
    accounts = {row["code"]: row for row in repo.list_accounts()}
    assert accounts["112201"]["requires_party"] and accounts["224101"]["requires_party"]
    assert not accounts["224199"]["requires_party"] and not accounts["1001"]["requires_party"]
    customer = party(repo, boyi_sender_name="示例寄件人", boyi_sender_phone="00000000000")
    with pytest.raises(FinanceError) as duplicate:
        party(repo)
    assert duplicate.value.code == "PARTY_EXISTS"
    with pytest.raises(FinanceError) as linked:
        party(repo, name="另一客户", boyi_sender_name="示例寄件人", boyi_sender_phone="00000000000")
    assert linked.value.code == "PARTY_SENDER_LINKED"
    with pytest.raises(FinanceValidationError):
        party(repo, name="半填", boyi_sender_name="只有姓名")
    renamed = repo.update_party(customer["id"], {"name": "改名客户", "expected_revision": customer["revision"]}, request_id=rid(), actor=ACTOR)
    assert renamed["name"] == "改名客户" and renamed["revision"] == 2
    assert [p["name"] for p in repo.list_parties(party_type="customer")] == ["寄付散客", "改名客户"]


def test_party_account_requires_party_and_posts_open_items(repo):  # noqa: F811
    start(repo)
    customer = party(repo)
    draft = repo.create_draft(voucher(("112201", "300", ""), ("500101", "", "300")), request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as refused:
        repo.submit_voucher(draft["id"], {"expected_revision": draft["revision"]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "DIMENSION_REQUIRED"
    invoice = post(repo, voucher(("112201", "300", "", {"party_id": customer["id"], "waybill_no": "BY0001"}), ("500101", "", "300")))
    assert invoice["lines"][0]["party_name"] == "测试月结客户"
    items = repo.open_items(party_id=customer["id"], as_of="2026-10-31")
    assert [(i["amount"], i["outstanding"], i["waybill_no"]) for i in items["items"]] == [("300.0000", "300.0000", "BY0001")]
    assert items["totals"] == {"112201": "300.0000"}
    assert repo.party_balance_check() == []


def test_manual_allocation_settles_and_reversal_restores(repo):  # noqa: F811
    start(repo)
    customer = party(repo)
    pid = customer["id"]
    post(repo, voucher(("112201", "300", "", {"party_id": pid}), ("500101", "", "300"), date="2026-10-01"))
    post(repo, voucher(("112201", "200", "", {"party_id": pid}), ("500101", "", "200"), date="2026-10-02"))
    post(repo, voucher(("1001", "350", ""), ("112201", "", "350", {"party_id": pid}), date="2026-10-10"))
    items = repo.open_items(party_id=pid, as_of="2026-10-31")["items"]
    first, second, receipt = (item["item_id"] for item in items)
    assert [i["outstanding"] for i in items] == ["300.0000", "200.0000", "-350.0000"]
    with pytest.raises(FinanceError) as unbalanced:
        repo.allocate({"party_id": pid, "account_code": "112201", "allocation_date": "2026-10-10",
                       "items": [{"item_id": first, "amount": "300"}, {"item_id": receipt, "amount": "200"}]}, request_id=rid(), actor=ACTOR)
    assert unbalanced.value.code == "ALLOCATION_UNBALANCED"
    with pytest.raises(FinanceError) as too_much:
        repo.allocate({"party_id": pid, "account_code": "112201", "allocation_date": "2026-10-10",
                       "items": [{"item_id": first, "amount": "400"}, {"item_id": receipt, "amount": "400"}]}, request_id=rid(), actor=ACTOR)
    assert too_much.value.code == "ALLOCATION_EXCEEDS_OUTSTANDING"
    allocation = repo.allocate({"party_id": pid, "account_code": "112201", "allocation_date": "2026-10-10", "remark": "十月回款",
                                "items": [{"item_id": first, "amount": "300"}, {"item_id": second, "amount": "50"},
                                          {"item_id": receipt, "amount": "350"}]}, request_id=rid(), actor=ACTOR)
    assert allocation["total_amount"] == "350.0000" and allocation["pairs"] == 2
    assert outstanding(repo, pid) == {second: "150.0000"}
    # The account balance always equals the open items.
    assert repo.party_balance_check() == []
    tb = {row["code"]: row for row in repo.trial_balance("2026-10")["rows"]}
    assert tb["112201"]["closing_debit"] == "150.0000"
    with pytest.raises(FinanceError) as allocated:
        voucher_id = next(v for v in repo.list_vouchers(month="2026-10") if v["total_debit"] == "350.0000")
        repo.cancel_voucher(voucher_id["id"], {"expected_revision": voucher_id["revision"], "reason": "测试"}, request_id=rid(), actor=ACTOR)
    assert allocated.value.code == "VOUCHER_HAS_ALLOCATIONS"
    repo.reverse_allocation(allocation["id"], {"reason": "核销错误"}, request_id=rid(), actor=ACTOR)
    assert outstanding(repo, pid) == {first: "300.0000", second: "200.0000", receipt: "-350.0000"}
    assert [a["status"] for a in repo.list_allocations(party_id=pid)] == ["reversed"]
    assert repo.party_balance_check() == []


def test_cancelling_an_unallocated_voucher_removes_its_open_items(repo):  # noqa: F811
    start(repo)
    pid = party(repo)["id"]
    invoice = post(repo, voucher(("112201", "80", "", {"party_id": pid}), ("500101", "", "80")))
    repo.cancel_voucher(invoice["id"], {"expected_revision": invoice["revision"], "reason": "开错"}, request_id=rid(), actor=ACTOR)
    assert repo.open_items(party_id=pid, as_of="2026-10-31")["items"] == []
    assert repo.party_balance_check() == []


def test_auto_allocation_by_waybill(repo):  # noqa: F811
    start(repo)
    platform = party(repo, name="测试平台", party_type="platform")["id"]
    post(repo, voucher(("112202", "20", "", {"party_id": platform, "waybill_no": "W1"}),
                       ("112202", "35", "", {"party_id": platform, "waybill_no": "W2"}), ("500101", "", "55"), date="2026-10-03"))
    post(repo, voucher(("101203", "20", ""), ("112202", "", "20", {"party_id": platform, "waybill_no": "W1"}),
                       ("101203", "30", ""), ("112202", "", "30", {"party_id": platform, "waybill_no": "W2"}), date="2026-10-08"))
    result = repo.auto_allocate_by_waybill({"party_id": platform, "account_code": "112202", "allocation_date": "2026-10-08"},
                                           request_id=rid(), actor=ACTOR)
    assert sorted((a["waybill_no"], a["total_amount"]) for a in result["allocations"]) == [("W1", "20.0000"), ("W2", "30.0000")]
    remaining = repo.open_items(party_id=platform, account_code="112202", as_of="2026-10-31")["items"]
    assert [(i["waybill_no"], i["outstanding"]) for i in remaining] == [("W2", "5.0000")]
    assert all(a["method"] == "auto_waybill" for a in repo.list_allocations(party_id=platform, month="2026-10"))
    assert repo.party_balance_check() == []


def test_closed_period_refuses_allocation(repo, database):  # noqa: F811
    start(repo)
    pid = party(repo)["id"]
    post(repo, voucher(("112201", "10", "", {"party_id": pid}), ("500101", "", "10")))
    post(repo, voucher(("1001", "10", ""), ("112201", "", "10", {"party_id": pid})))
    items = [item["item_id"] for item in repo.open_items(party_id=pid, as_of="2026-10-31")["items"]]
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE finance_periods SET status='closed' WHERE period_month='2026-10'")
        connection.commit()
    with pytest.raises(FinanceError) as refused:
        repo.allocate({"party_id": pid, "account_code": "112201", "allocation_date": "2026-10-20",
                       "items": [{"item_id": items[0], "amount": "10"}, {"item_id": items[1], "amount": "10"}]}, request_id=rid(), actor=ACTOR)
    assert refused.value.code == "PERIOD_CLOSED"


def test_concurrent_allocations_never_over_settle(repo):  # noqa: F811
    start(repo)
    pid = party(repo)["id"]
    post(repo, voucher(("112201", "100", "", {"party_id": pid}), ("500101", "", "100")))
    post(repo, voucher(("1001", "100", ""), ("112201", "", "100", {"party_id": pid})))
    post(repo, voucher(("1001", "100", ""), ("112201", "", "100", {"party_id": pid})))
    invoice, receipt_a, receipt_b = (item["item_id"] for item in repo.open_items(party_id=pid, as_of="2026-10-31")["items"])
    barrier, outcomes = threading.Barrier(2), []

    def settle(receipt):
        barrier.wait()
        try:
            repo.allocate({"party_id": pid, "account_code": "112201", "allocation_date": "2026-10-15",
                           "items": [{"item_id": invoice, "amount": "100"}, {"item_id": receipt, "amount": "100"}]}, request_id=rid(), actor=ACTOR)
            outcomes.append("ok")
        except FinanceError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=settle, args=(receipt,)) for receipt in (receipt_a, receipt_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["ALLOCATION_EXCEEDS_OUTSTANDING", "ok"]
    assert sum(Decimal(v) for v in outstanding(repo, pid).values()) == Decimal("-100")
    assert repo.party_balance_check() == []


def test_aging_and_statement(repo):  # noqa: F811
    start(repo, "2026-08")
    pid = party(repo)["id"]
    post(repo, voucher(("112201", "100", "", {"party_id": pid}), ("500101", "", "100"), date="2026-08-01"))
    post(repo, voucher(("112201", "40", "", {"party_id": pid}), ("500101", "", "40"), date="2026-09-20"))
    post(repo, voucher(("112201", "7", "", {"party_id": pid}), ("500101", "", "7"), date="2026-10-25"))
    post(repo, voucher(("1001", "15", ""), ("112201", "", "15", {"party_id": pid}), date="2026-10-28"))
    aging = repo.aging(as_of="2026-10-31", kind="receivable")
    row = aging["rows"][0]
    assert (row["0_30"], row["31_60"], row["61_90"], row["over_90"], row["unapplied"], row["total"]) == (
        "7.0000", "40.0000", "0.0000", "100.0000", "-15.0000", "132.0000")
    assert aging["totals"]["total"] == "132.0000"
    statement = repo.party_statement(party_id=pid, start_date="2026-09-01", end_date="2026-10-31")
    assert statement["opening"] == {"112201": "100.0000"}
    assert [(r["increase"], r["decrease"], r["balance"]) for r in statement["rows"]] == [
        ("40.0000", "0.0000", "140.0000"), ("7.0000", "0.0000", "147.0000"), ("0.0000", "15.0000", "132.0000")]
    assert statement["closing"] == {"112201": "132.0000"}
