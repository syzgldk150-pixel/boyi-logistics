"""Connector intake (migration 071): sources, files, batches, rules, reviews and posting on loopback MySQL."""
from __future__ import annotations

import copy
import os
import pytest

from shared.finance import FinanceError
from tests.test_finance_gl_mysql import ACTOR, database, post, repo, require_loopback, rid, start, voucher  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")
PLUGIN = "finance-demo-1"


def online_source(repo, *, key="rh_main", fund="101203", provider="ronghui"):  # noqa: F811
    return repo.create_source({"source_key": key, "display_name": "融辉主发货", "kind": "online", "provider": provider,
                               "producer_instance_id": PLUGIN, "fund_account": fund}, request_id=rid(), actor=ACTOR)


def rows(*items):
    return [{"source_key": key, "business_date": day, "amount": amount, "item_l1": item, **extra}
            for key, day, amount, item, *rest in items for extra in [rest[0] if rest else {}]]


def batch(rows_, *, key="rh_main", start_="2026-10-01", end="2026-10-31", refs=("ref-1",), kind="http_observation"):
    return {"contract": "finance.source_batch/v1", "source_key": key, "period": {"start": start_, "end": end},
            "declares": {"balance_chain": False, "summary": False, "opening_closing": False, "complete_period": True},
            "rows": rows_, "evidence": {"kind": kind, "refs": list(refs)}}


def submit(repo, payload, observed=("ref-1",), automation=PLUGIN):  # noqa: F811
    return repo.submit_source_batch(automation_id=automation, plugin_version="1.0.0", payload=payload, observed_read_refs=observed)


def tb(repo, month="2026-10"):  # noqa: F811
    return {row["code"]: row for row in repo.trial_balance(month)["rows"]}


def lines_of(repo, batch_id):  # noqa: F811
    return {row["source_key"]: row for row in repo.list_source_lines(batch_id)["rows"]}


BASE = rows(("A1", "2026-10-03", "-12.5", "运费"), ("A2", "2026-10-03", "30", "到付回款"), ("A3", "2026-10-04", "-2", "短信费"))


def test_unmatched_lines_go_to_suspense_and_review_resolution_reposts(repo):  # noqa: F811
    start(repo)
    online_source(repo)
    result = submit(repo, batch(BASE))
    assert result["status"] == "accepted" and result["posted_days"] == ["2026-10-03", "2026-10-04"]
    balance = tb(repo)
    assert balance["101203"]["closing_debit"] == "15.5000"
    assert balance["224199"]["closing_credit"] == "15.5000"  # inflow 30 credit, outflows 14.5 debit
    reviews = {(r["item_l1"], r["direction"]): r for r in repo.list_reviews()}
    assert set(reviews) == {("运费", "out"), ("到付回款", "in"), ("短信费", "out")}
    freight = reviews[("运费", "out")]
    resolved = repo.resolve_review(freight["id"], {"counter_account": "540101"}, request_id=rid(), actor=ACTOR)
    assert resolved["reposted_days"] == ["2026-10-03"]
    balance = tb(repo)
    assert balance["540101"]["closing_debit"] == "12.5000"
    assert balance["224199"]["closing_credit"] == "28.0000"
    assert {(r["item_l1"]) for r in repo.list_reviews()} == {"到付回款", "短信费"}
    assert resolved["still_open"] is False
    assert [r["resolved_by"] for r in repo.list_reviews(status="resolved")] == ["测试会计"]
    line = lines_of(repo, result["batch_id"])["A1"]
    assert line["posting_status"] == "posted" and line["voucher_no"].startswith("记-2026-10-")


def test_source_producer_and_evidence_are_enforced(repo):  # noqa: F811
    start(repo)
    online_source(repo)
    wrong = submit(repo, batch(BASE), automation="someone-else")
    assert (wrong["status"], wrong["error_code"], wrong["batch_id"]) == ("rejected", "INTAKE_SOURCE", None)
    unseen = submit(repo, batch(BASE, refs=("forged",)))
    assert (unseen["status"], unseen["error_code"]) == ("rejected", "INTAKE_EVIDENCE")
    assert [b["status"] for b in repo.list_source_batches(repo.list_sources()[0]["id"])] == ["rejected"]
    assert tb(repo) == {}


def test_identical_resubmission_replays_and_changed_day_is_reposted_only(repo):  # noqa: F811
    start(repo)
    online_source(repo)
    first = submit(repo, batch(BASE))
    again = submit(repo, batch(BASE))
    assert again["replayed"] and again["batch_id"] == first["batch_id"]
    kept_voucher = lines_of(repo, first["batch_id"])["A3"]["voucher_id"]
    changed = copy.deepcopy(BASE)
    changed[0]["amount"] = "-15"
    second = submit(repo, batch(changed))
    assert second["superseded_batch_id"] == first["batch_id"]
    assert second["posted_days"] == ["2026-10-03"] and second["kept_days"] == ["2026-10-04"]
    assert lines_of(repo, second["batch_id"])["A3"]["voucher_id"] == kept_voucher
    # A resubmission without the 短信费 line closes its review automatically.
    third = submit(repo, batch([row for row in changed if row["source_key"] != "A3"]))
    assert third["status"] == "accepted"
    assert "短信费" not in {r["item_l1"] for r in repo.list_reviews()}
    assert ("短信费", "system") in {(r["item_l1"], r["resolved_by"]) for r in repo.list_reviews(status="resolved")}
    submit(repo, batch(changed))
    assert tb(repo)["101203"]["closing_debit"] == "13.0000"
    assert [b["status"] for b in repo.list_source_batches(repo.list_sources()[0]["id"])] == ["accepted", "superseded", "superseded", "superseded"]


def test_conflicts_and_overlaps_are_refused(repo):  # noqa: F811
    start(repo)
    online_source(repo)
    submit(repo, batch(BASE))
    overlap = submit(repo, batch(rows(("B1", "2026-10-20", "1", "运费")), start_="2026-10-15", end="2026-11-14"))
    assert overlap["error_code"] == "INTAKE_PERIOD_OVERLAP"
    conflict = submit(repo, batch(rows(("A1", "2026-11-02", "-1", "运费")), start_="2026-11-01", end="2026-11-30"))
    assert conflict["error_code"] == "INTAKE_CONFLICT"


def test_closed_period_drift_is_recorded_without_changing_the_ledger(repo, database):  # noqa: F811
    start(repo)
    online_source(repo)
    submit(repo, batch(BASE))
    before = tb(repo)
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE finance_periods SET status='closed' WHERE period_month='2026-10'")
        connection.commit()
    changed = copy.deepcopy(BASE)
    changed[2]["amount"] = "-3"
    result = submit(repo, batch(changed))
    assert result["status"] == "accepted" and result["closed_period_days"] == ["2026-10-04"]
    assert result["kept_days"] == ["2026-10-03"]
    assert tb(repo) == before
    anomalies = repo.list_anomalies()
    assert [a["kind"] for a in anomalies] == ["CLOSED_PERIOD_DRIFT"]
    repo.acknowledge_anomaly(anomalies[0]["id"], {"reason": "平台事后调整，下月处理"}, request_id=rid(), actor=ACTOR)
    assert repo.list_anomalies() == []


def test_settle_by_waybill_rule_allocates_and_repost_reallocates(repo):  # noqa: F811
    start(repo)
    platform = next(p for p in repo.list_parties(party_type="platform") if p["platform_code"] == "ronghui")["id"]
    post(repo, voucher(("112202", "30", "", {"party_id": platform, "waybill_no": "W1"}), ("500101", "", "30"), date="2026-10-02"))
    repo.save_line_rule({"provider": "ronghui", "item_l1": "到付回款", "direction": "in", "counter_account": "112202",
                         "party_action": "settle_by_waybill", "party_id": platform}, request_id=rid(), actor=ACTOR)
    online_source(repo)
    payload = batch(rows(("C1", "2026-10-05", "30", "到付回款", {"waybill_no": "W1"})))
    submit(repo, payload)
    assert repo.open_items(party_id=platform, account_code="112202", as_of="2026-10-31")["items"] == []
    allocations = repo.list_allocations(party_id=platform)
    assert [(a["method"], a["total_amount"], a["status"]) for a in allocations] == [("auto_waybill", "30.0000", "active")]
    changed = copy.deepcopy(payload)
    changed["rows"][0]["amount"] = "25"
    submit(repo, changed)
    left = repo.open_items(party_id=platform, account_code="112202", as_of="2026-10-31")["items"]
    assert [i["outstanding"] for i in left] == ["5.0000"]
    assert sorted(a["status"] for a in repo.list_allocations(party_id=platform)) == ["active", "reversed"]
    assert repo.party_balance_check() == []


def test_internal_transfer_and_source_without_fund_account(repo):  # noqa: F811
    start(repo)
    repo.save_line_rule({"provider": "bank_demo", "item_l1": "转出至融辉", "direction": "out", "party_action": "internal_transfer"},
                        request_id=rid(), actor=ACTOR)
    online_source(repo, key="bank_card", fund="1002", provider="bank_demo")
    submit(repo, batch(rows(("D1", "2026-10-06", "-500", "转出至融辉")), key="bank_card"))
    assert tb(repo)["101299"]["closing_debit"] == "500.0000"
    online_source(repo, key="no_fund", fund=None, provider="bank_demo")
    result = submit(repo, batch(rows(("E1", "2026-10-06", "8", "利息")), key="no_fund"))
    assert lines_of(repo, result["batch_id"])["E1"]["posting_status"] == "no_account"


def test_file_source_upload_read_and_consume(repo):  # noqa: F811
    start(repo)
    source = repo.create_source({"source_key": "wechat_shop", "display_name": "微信商户", "kind": "file", "provider": "wechat",
                                 "producer_instance_id": PLUGIN, "fund_account": "101201"}, request_id=rid(), actor=ACTOR)
    content = ("交易时间,金额\n" + "2026-10-07,9.90\n" * 120_000).encode("utf-8")
    uploaded = repo.upload_source_file(source["id"], file_name="账单.csv", content_type="text/csv", data=content,
                                       request_id=rid(), actor=ACTOR)
    with pytest.raises(FinanceError) as duplicate:
        repo.upload_source_file(source["id"], file_name="账单2.csv", content_type="text/csv", data=content, request_id=rid(), actor=ACTOR)
    assert duplicate.value.code == "SOURCE_FILE_DUPLICATE"
    listed = repo.producer_sources(PLUGIN)
    assert [f["id"] for f in listed[0]["pending_files"]] == [uploaded["id"]]
    assert repo.producer_sources("other") == []
    chunks, offset = [], 0
    while True:
        part = repo.read_source_file(PLUGIN, uploaded["id"], offset, 700_000)
        chunks.append(part["data"])
        offset += len(part["data"])
        if part["eof"]:
            break
    assert b"".join(chunks) == content
    with pytest.raises(FinanceError) as forbidden:
        repo.read_source_file("other", uploaded["id"], 0, 10)
    assert forbidden.value.code == "SOURCE_FILE_FORBIDDEN"
    result = submit(repo, batch(rows(("F1", "2026-10-07", "9.9", "收款")), key="wechat_shop", refs=(str(uploaded["id"]),), kind="file"),
                    observed=())
    assert result["status"] == "accepted"
    assert submit(repo, batch(rows(("F1", "2026-10-07", "9.9", "收款")), key="wechat_shop", refs=(str(uploaded["id"]),), kind="file"),
                  observed=())["replayed"]
    with pytest.raises(FinanceError) as consumed:
        repo.read_source_file(PLUGIN, uploaded["id"], 0, 10)
    assert consumed.value.code == "SOURCE_FILE_NOT_PENDING"
    assert tb(repo)["101201"]["closing_debit"] == "9.9000"


def test_rule_validation(repo):  # noqa: F811
    with pytest.raises(FinanceError) as needs_party:
        repo.save_line_rule({"provider": "ronghui", "item_l1": "x", "direction": "in", "counter_account": "112202"}, request_id=rid(), actor=ACTOR)
    assert needs_party.value.code == "LINE_RULE_PARTY_REQUIRED"
    with pytest.raises(FinanceError) as group:
        repo.save_line_rule({"provider": "ronghui", "item_l1": "x", "direction": "in", "counter_account": "5001"}, request_id=rid(), actor=ACTOR)
    assert group.value.code == "LINE_RULE_INVALID_ACCOUNT"
