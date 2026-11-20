"""C line: fee rules managed in 业务手册 → 计算规则 (block-built, migrations 087/090) and manual chapters (C2)."""
import copy
import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from shared.fee_rule_lines import fee_lines, pages_of
from shared.fee_rule_repository import normalize_checks
from shared.handbook_fee_rules import (
    FeeRuleError, approval_blockers, quote_catalog, review_lines, rule_from_record, select_rule, submit_blockers,
)
from tools.feishu_knowledge_pdf import chapter_pages, read_outline

ROOT = Path(__file__).resolve().parents[1]
BATCH = json.loads((ROOT / "tests" / "fixtures" / "fee_rules" / "v61_first_batch.json").read_text(encoding="utf-8"))
UPSTAIRS = {"rule_id": "r-up", "fee_kind": "upstairs", "fee_name": "上楼费", "version_label": "V6.1-草稿1", "status": "DRAFT",
            "effective_from": None, "content": BATCH["upstairs"]["content"], "checks": BATCH["upstairs"]["checks"]}
RULE = rule_from_record(UPSTAIRS)


def test_rule_versions_by_effective_date():
    assert not RULE.reportable  # a draft never gives people an amount
    with pytest.raises(FeeRuleError, match="没有已审核生效"):
        select_rule([RULE], fee_kind="upstairs", business_date=date(2026, 11, 15))
    old = replace(RULE, version="V6.0", status="APPROVED", effective_from=date(2025, 1, 1))
    new = replace(RULE, version="V6.1", status="APPROVED", effective_from=date(2026, 10, 1))
    assert select_rule([old, new], fee_kind="upstairs", business_date=date(2026, 10, 1)).version == "V6.1"
    assert select_rule([old, new], fee_kind="upstairs", business_date=date(2026, 9, 30)).version == "V6.0"
    with pytest.raises(FeeRuleError, match="多版已生效"):
        select_rule([new, replace(new, version="dup")], fee_kind="upstairs", business_date=date(2026, 11, 1))
    with pytest.raises(ValueError):
        replace(RULE, status="APPROVED", effective_from=None)


def test_approval_needs_name_valid_content_sources_date_and_every_check_ticked():
    blockers = approval_blockers(UPSTAIRS)
    assert any("执行日期" in item for item in blockers) and any("未核对" in item for item in blockers)
    ticks = [{"kind": "line", "sig": line["sig"], "done": True} for line in fee_lines(UPSTAIRS["content"])]
    ready = {**UPSTAIRS, "effective_from": "2026-10-01", "checks": [{**item, "done": True} for item in UPSTAIRS["checks"]] + ticks}
    assert approval_blockers(ready) == []
    assert approval_blockers({**ready, "checks": ready["checks"][:-1]}) == ["收费表还有 1 行没有对照手册打勾。"]
    assert submit_blockers(ready, trial_count=0) == ["保存后还没有试算过，请至少试算一个场景。"]
    assert any("费用名称" in item for item in approval_blockers({**ready, "fee_name": " "}))
    broken = copy.deepcopy(ready["content"])
    broken["formula"] = {"type": "magic"}
    assert any("不是有效的积木" in item for item in approval_blockers({**ready, "content": broken}))
    unsourced = copy.deepcopy(BATCH["night_delivery"]["content"])
    unsourced["formula"].pop("source")
    assert any("没有出处" in item and "夜送费" in item
               for item in approval_blockers({**ready, "fee_kind": "night_delivery", "content": unsourced}))


def _approved(kind, effective="2026-10-01", **changes):
    return {"rule_id": f"r-{kind}", "fee_kind": kind, "fee_name": BATCH[kind]["name"], "version_label": "V6.1",
            "status": "APPROVED", "effective_from": effective, "content": BATCH[kind]["content"], "checks": [], **changes}


def test_quote_catalog_offers_one_version_per_fee_and_explains_the_rest():
    records = [_approved("upstairs"), _approved("unloading"), _approved("night_delivery", effective="2026-12-01")]
    rules, unavailable = quote_catalog(records, business_date=date(2026, 11, 19))
    assert [item.name for item in rules] == ["上楼费", "卸货费"]
    assert rules[0].version == "V6.1（执行日期 2026-10-01）"
    assert unavailable == [{"fee_kind": "night_delivery", "name": "夜送费", "reason": "这个日期没有已审核生效的收费规则，不能给出金额。"}]
    bad = _approved("revisit", content={"schema": 1, "params": [], "formula": {"type": "magic"}})
    _, unavailable = quote_catalog([bad], business_date=date(2026, 11, 19))
    assert unavailable[0]["fee_kind"] == "revisit" and "不是有效的积木" in unavailable[0]["reason"]


def test_migration_090_seeds_exactly_the_first_batch_and_retires_the_old_format():
    sql = (ROOT / "agent" / "migrations" / "090_agent_fee_rule_blocks.sql").read_text(encoding="utf-8")
    values = sql.split("VALUES", 1)[1].split("CREATE TABLE", 1)[0]
    literals = values.split("'")[1::2]
    rows = [literals[index:index + 9] for index in range(0, len(literals), 9)]
    assert [row[1] for row in rows] == list(BATCH)
    for row in rows:
        kind = row[1]
        assert row[2] == BATCH[kind]["name"] and row[3:5] == ["V6.1-草稿1", "DRAFT"]
        assert json.loads(row[5]) == BATCH[kind]["content"] and json.loads(row[6]) == BATCH[kind]["checks"]
        rule_from_record({"fee_kind": kind, "fee_name": row[2], "version_label": row[3], "status": "DRAFT",
                          "effective_from": None, "content": json.loads(row[5])})
    assert "JSON_EXTRACT(content_json, '$.schema') IS NULL" in sql and "status='RETIRED'" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_fee_quotes" in sql


# -- 核对 (2026-11-20, 方案 A): 收费表 lines, questions, migration 091 -----------------------------

def test_fee_lines_read_the_rule_out_with_pages_and_clamps():
    lines = fee_lines(BATCH["count_check"]["content"])
    assert [line["text"] for line in lines] == [
        "开箱清点 › 开箱清点件数 ≤ 20：0 元",
        "开箱清点 › 开箱清点件数 > 20：0.1 元/件 × 开箱清点件数（最低 5 元，最高 100 元）"]
    assert {line["pages"][0] for line in lines} == {60} and lines[0]["document"].startswith("融辉物流网络管理手册")
    upstairs = [line["text"] for line in fee_lines(BATCH["upstairs"]["content"])]
    assert "公共部分“普货” › 有电梯 = 是 › 有电梯：5 元 + 0.06 元/kg × 结算重量" in upstairs  # leaves of a sum read as one line
    assert any("没有一件单件实际重量 > 150" in text for text in upstairs)
    assert pages_of("第五篇第五章 第 51–52 页") == [51, 52] and pages_of("第 63 页") == [63] and pages_of("附表") == []
    assert len({line["sig"] for line in lines}) == len(lines)


def test_changing_a_number_clears_only_that_lines_tick():
    content = copy.deepcopy(BATCH["count_check"]["content"])
    before = fee_lines(content)
    record = {**UPSTAIRS, "content": content, "checks": [{"kind": "line", "sig": line["sig"], "done": True} for line in before]}
    assert all(line["done"] for line in review_lines(record))
    content = copy.deepcopy(content)
    content["formula"]["tiers"][1]["then"]["max"] = "120"
    after = review_lines({**record, "content": content})
    assert [line["done"] for line in after] == [True, False] and "最高 120 元" in after[1]["text"]


def test_questions_must_be_answered_from_their_options():
    question = {"kind": "question", "text": "按哪个重量算？", "options": ["结算重量", "其他（写明做法）"], "answer": None}
    record = {**UPSTAIRS, "content": BATCH["night_delivery"]["content"], "effective_from": "2026-10-01",
              "checks": [question] + [{"kind": "line", "sig": line["sig"], "done": True} for line in fee_lines(BATCH["night_delivery"]["content"])]}
    assert approval_blockers(record) == ["还有需要拍板的问题没有回答：按哪个重量算？"]
    assert approval_blockers({**record, "checks": [{**question, "answer": "结算重量"}, *record["checks"][1:]]}) == []
    assert normalize_checks([{**question, "answer": "结算重量", "note": " "}])[0] == {**question, "answer": "结算重量", "note": ""}
    for bad, message in (({**question, "answer": "实际重量"}, "不在选项"), ({**question, "answer": "其他（写明做法）"}, "写明"),
                         ({**question, "options": ["只有一个"]}, "2 到 6 个"), ({"kind": "line", "sig": "x"}, "无效")):
        with pytest.raises(FeeRuleError, match=message):
            normalize_checks([bad])
    ticked = normalize_checks([{"kind": "line", "sig": "0123456789abcdef", "done": True}, {"kind": "line", "sig": "0123456789abcdef", "done": True},
                               {"kind": "line", "sig": "fedcba9876543210", "done": False}])
    assert ticked == [{"kind": "line", "sig": "0123456789abcdef", "done": True}]  # only ticked lines are kept, once


def test_migration_091_turns_untouched_seed_items_into_questions():
    sql = (ROOT / "agent" / "migrations" / "091_agent_fee_rule_review_lines.sql").read_text(encoding="utf-8")
    updates = sql.split("UPDATE agent_fee_rule_versions")[1:]
    seeded = [item["checks"] for item in BATCH.values()]
    assert len(updates) == len(BATCH)
    questions = 0
    for update in updates:
        literals = update.split("'")[1::2]
        new_checks, rule_id, old_checks = json.loads(literals[0]), literals[2], json.loads(literals[4])
        assert "status='DRAFT'" in update and literals[1] == "migration:091" and old_checks in seeded
        assert normalize_checks(new_checks) == new_checks
        questions += len(new_checks)
        assert rule_id and all(item["kind"] == "question" and item["answer"] is None for item in new_checks)
    assert questions == 3


# -- C2: chapters by bookmark --------------------------------------------------------------

BODY = {"page_count": 6, "pages": [{"page": n, "text": f"第{n}页 内容"} for n in range(1, 7)], "outline": [
    {"title": "第五篇 收费标准", "page": 2, "level": 0},
    {"title": "第四章 卸货服务收费标准", "page": 2, "level": 1},
    {"title": "第五章 送货上下楼服务收费标准", "page": 3, "level": 1},
    {"title": "第六章 进仓服务收费标准", "page": 5, "level": 1},
    {"title": "第六篇 附则", "page": 6, "level": 0},
]}


def test_chapter_is_cut_at_the_next_bookmark_of_the_same_level():
    text, meta = chapter_pages(BODY, "送货上下楼")
    assert (meta["start_page"], meta["end_page"]) == (3, 5)  # next chapter starts on page 5, which may be shared
    assert "[PDF 第 3 页]" in text and "[PDF 第 6 页]" not in text
    _, whole = chapter_pages(BODY, "第五篇 收费标准")
    assert (whole["start_page"], whole["end_page"]) == (2, 6)


def test_chapter_fingerprint_changes_only_with_its_own_text():
    _, before = chapter_pages(BODY, "送货上下楼")
    edited = {**BODY, "pages": [dict(page, text=page["text"] + ("改" if page["page"] == 6 else "")) for page in BODY["pages"]]}
    assert chapter_pages(edited, "送货上下楼")[1]["fingerprint"] == before["fingerprint"]
    edited = {**BODY, "pages": [dict(page, text=page["text"] + ("改" if page["page"] == 4 else "")) for page in BODY["pages"]]}
    assert chapter_pages(edited, "送货上下楼")[1]["fingerprint"] != before["fingerprint"]


@pytest.mark.parametrize("title,message", [("收费标准", "多个书签"), ("第九章", "没有找到")])
def test_unclear_chapter_titles_fail(title, message):
    with pytest.raises(ValueError, match=message):
        chapter_pages(BODY, title)
    with pytest.raises(ValueError, match="没有章节书签"):
        chapter_pages({**BODY, "outline": []}, "第五章")


def test_outline_is_read_with_levels(tmp_path):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=200, height=200)
    part = writer.add_outline_item("第五篇 收费标准", 1)
    writer.add_outline_item("第五章 送货上下楼服务收费标准", 2, parent=part)
    path = tmp_path / "manual.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    assert read_outline(PdfReader(path)) == [{"title": "第五篇 收费标准", "page": 2, "level": 0},
                                              {"title": "第五章 送货上下楼服务收费标准", "page": 3, "level": 1}]


# -- 计算规则 storage (real migrations 087/090) and the Agent API ----------------------------

from tests.mysql_migrated_database import database  # noqa: E402,F401

OLD_SEED_ID = "0f6a1c2e-5b7d-4e3a-9c41-61f2e8d0a087"
UPSTAIRS_ID = "29e79711-0f15-5421-9cbc-ec74d21d0e3d"


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")
def test_migrations_087_090_repository_lifecycle(database):  # noqa: F811
    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    import pymysql

    from shared.fee_rule_repository import FeeRuleRepository

    def connect():
        return pymysql.connect(host=database.host, port=database.port, user=database.user, password=database.password,
                               database=database.database, charset="utf8mb4", autocommit=False)

    repo = FeeRuleRepository(connect)
    old = repo.get(OLD_SEED_ID)
    assert old["status"] == "RETIRED" and old["fee_name"] == "上楼费"  # the old format can no longer be computed
    seed = repo.get(UPSTAIRS_ID)
    assert (seed["status"], seed["fee_name"], seed["content"]) == ("DRAFT", "上楼费", BATCH["upstairs"]["content"])
    with pytest.raises(FeeRuleError, match="别人修改"):
        repo.submit(UPSTAIRS_ID, actor_id="7", expected_row_version=seed["row_version"] + 1)
    assert [item["kind"] for item in seed["checks"]] == ["question", "question"]  # migration 091
    answered = [{**item, "answer": item["options"][0]} for item in seed["checks"]]
    repo.save_draft(UPSTAIRS_ID, version_label="V6.1", effective_from="2026-10-01", content=seed["content"], checks=answered,
                    actor_id="7", expected_row_version=seed["row_version"])
    saved = repo.get(UPSTAIRS_ID)
    assert any("收费表还有" in item for item in saved["submit_blockers"])
    with pytest.raises(FeeRuleError, match="已经变了"):
        repo.set_checks(UPSTAIRS_ID, checks=[*answered, {"kind": "line", "sig": "0" * 16, "done": True}], actor_id="7",
                        expected_row_version=saved["row_version"])
    with pytest.raises(FeeRuleError, match="只能勾选或回答"):
        repo.set_checks(UPSTAIRS_ID, checks=answered[:1], actor_id="7", expected_row_version=saved["row_version"])
    ticks = [{"kind": "line", "sig": line["sig"], "done": True} for line in saved["lines"]]
    version = repo.set_checks(UPSTAIRS_ID, checks=[*answered, *ticks], actor_id="7", expected_row_version=saved["row_version"])
    assert all(line["done"] for line in repo.get(UPSTAIRS_ID)["lines"])
    with pytest.raises(FeeRuleError, match="别人修改"):
        repo.set_effective_dates([{"rule_id": UPSTAIRS_ID, "row_version": version - 1}], effective_from="2026-10-01", actor_id="7")
    assert repo.set_effective_dates([{"rule_id": UPSTAIRS_ID, "row_version": version}], effective_from="2026-10-01", actor_id="7") == 1
    version = repo.get(UPSTAIRS_ID)["row_version"]
    with pytest.raises(FeeRuleError, match="试算"):
        repo.submit(UPSTAIRS_ID, actor_id="7", expected_row_version=version)
    repo.record_trial(UPSTAIRS_ID, actor_id="7", detail={"outcome": "AMOUNT"})
    repo.submit(UPSTAIRS_ID, actor_id="7", expected_row_version=version)
    pending = repo.get(UPSTAIRS_ID)
    repo.approve(UPSTAIRS_ID, actor_id="1", expected_row_version=pending["row_version"])
    approved = repo.approved()
    assert [item["rule_id"] for item in approved] == [UPSTAIRS_ID]
    rules, _ = quote_catalog(approved, business_date=date(2026, 11, 12))
    assert [item.fee_kind for item in rules] == ["upstairs"]

    created = repo.create_fee(fee_name="进仓服务费", actor_id="7")
    fresh = repo.get(created)
    assert fresh["fee_kind"].startswith("fee_") and fresh["status"] == "DRAFT" and fresh["trial_count"] == 0
    with pytest.raises(FeeRuleError, match="已经有名为"):
        repo.create_fee(fee_name="进仓服务费", actor_id="7")
    repo.save_draft(created, fee_name="进仓费", version_label="草稿1", effective_from=None, content=fresh["content"],
                    checks=fresh["checks"], actor_id="7", expected_row_version=fresh["row_version"])
    assert repo.get(created)["fee_name"] == "进仓费"
    with pytest.raises(FeeRuleError, match="已经有名为"):
        repo.save_draft(created, fee_name="上楼费", version_label="草稿1", effective_from=None, content=fresh["content"],
                        checks=fresh["checks"], actor_id="7", expected_row_version=repo.get(created)["row_version"])

    quote_id = repo.record_quote(actor_id="7", channel="console", billing_date=date(2026, 11, 12), fee_kinds=["upstairs"],
                                 values={"floor": 3}, result={"total": "44.00", "complete": True, "items": []},
                                 tracking_number="R00099990001")
    recent = repo.recent_quotes()
    assert recent[0]["quote_id"] == quote_id and recent[0]["total"] == "44.00" and recent[0]["complete"] is True


def _client(record, super_admin=None, approved=()):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent.fee_rules_api import create_fee_quotes_router, create_fee_rules_router

    calls = []

    class Repository:
        def get(self, rule_id):
            return {**record, "trial_count": sum(1 for item in calls if item[0] == "trial")}

        def record_trial(self, rule_id, *, actor_id, detail):
            calls.append(("trial", actor_id, detail["outcome"]))

        def submit(self, rule_id, *, actor_id, expected_row_version):
            calls.append(("submit", actor_id, expected_row_version))

        def approve(self, rule_id, **kwargs):
            calls.append("approve")

        def retire(self, rule_id, **kwargs):
            calls.append("retire")

        def set_checks(self, rule_id, *, checks, actor_id, expected_row_version):
            calls.append(("checks", len(checks), expected_row_version))
            return expected_row_version + 1

        def set_effective_dates(self, versions, *, effective_from, actor_id):
            calls.append(("dates", effective_from, [item["rule_id"] for item in versions]))
            return len(versions)

        def create_fee(self, *, fee_name, actor_id):
            calls.append(("create", fee_name, actor_id))
            return "new-id"

        def approved(self, fee_kind=None):
            return list(approved)

        def record_quote(self, **kwargs):
            calls.append(("quote", kwargs["channel"], kwargs["fee_kinds"], kwargs["result"]["total"], kwargs["tracking_number"]))
            return "q-1"

    def deny(request):
        raise PermissionError("super administrator required")

    async def waybill(tracking, actor):
        return {"tracking_number": tracking, "billing_date": "2026-11-12", "settlement_weight": "200", "pieces": 2,
                "actual_weight": None}

    app = FastAPI()
    admin = lambda request: SimpleNamespace(actor_id="7")  # noqa: E731
    def page(title, number):
        if title != "融辉物流网络管理手册 V6.1":
            raise RuntimeError("没有这个文档")
        return b"\x89PNG\r\n\x1a\n" + bytes([number])

    app.include_router(create_fee_rules_router(repository_provider=Repository, admin_provider=admin,
                                               super_admin_provider=super_admin or deny, source_page_provider=page))
    app.include_router(create_fee_quotes_router(repository_provider=Repository, admin_provider=admin, waybill_reader=waybill))
    return TestClient(app, raise_server_exceptions=False), calls


def test_agent_api_lets_editors_work_but_only_super_admins_approve_or_retire():
    record = {**UPSTAIRS, "row_version": 1}
    client, calls = _client(record)
    base = f"/internal/v1/admin/fee-rules/{UPSTAIRS['rule_id']}"
    assert client.post(f"{base}/submit", json={"row_version": 1}).status_code == 200
    for action in ("approve", "retire"):
        assert client.post(f"{base}/{action}", json={"row_version": 1}).status_code == 500
    assert calls == [("submit", "7", 1)]
    detail = client.get(base).json()["data"]
    assert [item["key"] for item in detail["params"]] == ["settlement_weight", "floor", "stairs", "elevator", "goods_type", "pieces"]
    assert detail["content_problems"] == []
    values = {"settlement_weight": "200", "floor": 5, "elevator": False, "goods_type": "general",
              "pieces": [{"count": 2, "weight": "80"}]}
    trial = client.post(f"{base}/trial", json={"values": values, "boundary": True})
    data = trial.json()["data"]
    assert data["result"]["amount"] == "74.00" and data["result"]["reportable"] is False
    assert any(row["param"] == "送到几楼" and row["value"] == "8" and row["outcome"] == "NEGOTIATE" for row in data["boundary"])
    assert data["trial_count"] == 1 and calls[-1] == ("trial", "7", "AMOUNT")  # trials are logged and counted
    missing = client.post(f"{base}/trial", json={"values": {"floor": 5}}).json()["data"]["result"]
    assert missing["outcome"] == "MISSING" and "结算重量" in missing["missing"]
    bad = client.post(f"{base}/trial", json={"values": {**values, "settlement_weight": "abc"}})
    assert bad.status_code == 422 and "结算重量" in bad.json()["error"]["message"]
    assert client.post("/internal/v1/admin/fee-rules", json={"fee_name": "进仓服务费"}).json()["data"] == {"rule_id": "new-id"}
    assert client.post(f"{base}/checks", json={"checks": [], "row_version": 4}).json()["data"] == {"saved": True, "row_version": 5}
    dates = client.post("/internal/v1/admin/fee-rules/effective-dates",
                        json={"effective_from": "2026-12-01", "versions": [{"rule_id": "a", "row_version": 1}]})
    assert dates.json()["data"] == {"updated": 1} and calls[-1] == ("dates", "2026-12-01", ["a"])
    page = client.get("/internal/v1/admin/fee-rules/source-page", params={"document": "融辉物流网络管理手册 V6.1", "page": 50})
    assert page.json()["data"]["page"] == 50 and page.json()["data"]["png_base64"].startswith("iVBORw0KGgo")
    missing = client.get("/internal/v1/admin/fee-rules/source-page", params={"document": "别的手册", "page": 50})
    assert missing.status_code == 503 and "没有这个文档" in missing.json()["error"]["message"]
    assert client.get("/internal/v1/admin/fee-rules/source-page", params={"document": "", "page": 1}).status_code == 422


def test_agent_api_shows_problems_of_a_saved_rule_instead_of_failing():
    broken = {**UPSTAIRS, "row_version": 1, "content": {"schema": 1, "params": [], "formula": {"type": "magic"}}}
    client, _ = _client(broken)
    detail = client.get(f"/internal/v1/admin/fee-rules/{UPSTAIRS['rule_id']}").json()["data"]
    assert detail["params"] == [] and any("不是有效的积木" in item for item in detail["content_problems"])


def test_quote_api_uses_approved_rules_by_billing_date_and_keeps_the_quote():
    client, calls = _client(UPSTAIRS, approved=[_approved("upstairs"), _approved("unloading"),
                                                _approved("night_delivery", effective="2026-12-01")])
    catalog = client.get("/internal/v1/admin/fee-quotes/catalog?billing_date=2026-11-19").json()["data"]
    assert [fee["fee_kind"] for fee in catalog["fees"]] == ["upstairs", "unloading"]
    assert catalog["unavailable"][0]["fee_kind"] == "night_delivery" and catalog["conflict"] == ""
    assert {param["key"] for param in catalog["params"]} >= {"settlement_weight", "pieces", "floor"}
    values = {"settlement_weight": "1600", "floor": 3, "elevator": True, "goods_type": "general",
              "pieces": [{"count": 4, "weight": "100"}]}
    result = client.post("/internal/v1/admin/fee-quotes", json={
        "billing_date": "2026-11-19", "fee_kinds": ["upstairs", "unloading", "night_delivery"], "values": values,
        "tracking_number": "R00099990001"}).json()["data"]
    assert result["total"] == "101.00" and result["quote_id"] == "q-1"
    assert [item["counted"] for item in result["items"]] == [True, False]
    assert result["unavailable"][0]["name"] == "夜送费"
    assert calls[-1] == ("quote", "console", ["upstairs", "unloading"], "101.00", "R00099990001")
    nothing = client.post("/internal/v1/admin/fee-quotes", json={"billing_date": "2026-11-19", "fee_kinds": ["night_delivery"]})
    assert nothing.status_code == 422 and "都没有已生效的规则" in nothing.json()["error"]["message"]
    unknown = client.post("/internal/v1/admin/fee-quotes", json={"billing_date": "2026-11-19", "fee_kinds": ["nope"]})
    assert unknown.status_code == 422
    prefill = client.post("/internal/v1/admin/fee-quotes/waybill", json={"tracking_number": "r00099990001"}).json()["data"]
    assert prefill["tracking_number"] == "R00099990001" and prefill["pieces"] == 2
    assert client.post("/internal/v1/admin/fee-quotes/waybill", json={"tracking_number": "x"}).status_code == 422
