"""C line: fee rules managed in 业务手册 → 计算规则 (structure in code, numbers stored) and manual chapters (C2)."""
import copy
import json
import os
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from shared.handbook_fee_rules import (
    FeeRuleError, UpstairsInputs, approval_blockers, boundary_table, calculate_upstairs, parse_upstairs_content,
    rule_from_record, select_rule,
)
from tools.feishu_knowledge_pdf import chapter_pages, read_outline

ROOT = Path(__file__).resolve().parents[1]
DRAFT = json.loads((ROOT / "tests" / "fixtures" / "fee_rules" / "upstairs_v61_draft.json").read_text(encoding="utf-8"))
RULE = rule_from_record({**DRAFT, "status": "DRAFT", "effective_from": None})
W = Decimal("200")


def inputs(weight=W, elevator=False, floor=3, goods="general", counts=(0, 0), over=0, total=None):
    return UpstairsInputs(weight, elevator, floor, goods, counts, over, total)


def test_plan_example_no_elevator_fifth_floor_two_heavy_pieces():
    result = calculate_upstairs(RULE, inputs(floor=5, counts=(2, 0), total=2))
    assert (result.outcome, result.amount) == ("AMOUNT", Decimal("74.00"))
    assert result.steps == ("基础 10 + 0.12×200 = 34.00", "楼层加收（5 楼）0.10×200 = 20.00",
                            "单件加收 10×2 = 20", "合计 74.00")
    assert result.citations == ("融辉物流网络管理手册 V6.1 第五篇第五章 第 50 页",)
    assert "卸货费" in result.reminders[0]
    assert not result.reportable  # a draft never gives people an amount


@pytest.mark.parametrize("floor,expected", [(2, "34.00"), (3, "44.00"), (4, "44.00"), (6, "54.00"), (7, "64.00")])
def test_floor_tiers(floor, expected):
    assert calculate_upstairs(RULE, inputs(floor=floor)).amount == Decimal(expected)


def test_elevator_and_situations_without_an_amount():
    assert calculate_upstairs(RULE, inputs(elevator=True, floor=9)).amount == Decimal("17.00")
    assert calculate_upstairs(RULE, inputs(floor=8)).outcome == "NEGOTIATE"
    assert calculate_upstairs(RULE, inputs(over=1)).outcome == "NEGOTIATE"
    assert calculate_upstairs(RULE, inputs(goods="mattress")).outcome == "MANUAL"
    assert calculate_upstairs(RULE, inputs(floor=1)).outcome == "MANUAL"


def test_first_floor_choice_comes_from_the_stored_rule():
    for choice, outcome, amount in (("NO_CHARGE", "AMOUNT", Decimal("0.00")), ("BASE", "AMOUNT", Decimal("34.00"))):
        content = {**DRAFT["content"], "first_floor": choice}
        rule = replace(RULE, content=parse_upstairs_content(content))
        result = calculate_upstairs(rule, inputs(floor=1))
        assert (result.outcome, result.amount) == (outcome, amount)


def test_decimal_rounding_half_up():
    result = calculate_upstairs(RULE, inputs(weight=Decimal("123.45"), elevator=True))
    assert result.amount == Decimal("12.41")  # 5 + 0.06×123.45 = 12.407


@pytest.mark.parametrize("given,message", [
    (inputs(weight=Decimal("80"), counts=(2, 0)), "超过整票结算重量"),
    (inputs(counts=(3, 0), total=2), "超过整票件数"),
    (inputs(weight=Decimal("0")), "必须大于 0"),
    (inputs(floor=0), "楼层无效"),
    (inputs(counts=(1,)), "分档件数应有 2 项"),
])
def test_unreasonable_inputs_stop_the_calculation(given, message):
    with pytest.raises(FeeRuleError, match=message):
        calculate_upstairs(RULE, given)


def test_rule_versions_by_effective_date():
    with pytest.raises(FeeRuleError, match="没有已审核生效"):
        select_rule([RULE], fee_kind="upstairs", business_date=date(2026, 11, 15))
    old = replace(RULE, version="V6.0", status="APPROVED", effective_from=date(2025, 1, 1))
    new = replace(RULE, version="V6.1", status="APPROVED", effective_from=date(2026, 10, 1))
    assert select_rule([old, new], fee_kind="upstairs", business_date=date(2026, 9, 30)).version == "V6.0"
    assert select_rule([old, new], fee_kind="upstairs", business_date=date(2026, 10, 1)).version == "V6.1"
    with pytest.raises(ValueError):
        replace(RULE, status="APPROVED")  # approval needs the headquarters effective date


def test_boundary_table_covers_every_combination_with_an_outcome():
    rows = boundary_table(RULE)
    assert len(rows) == 2 * 8 * 4 and all(row["outcome"] in {"AMOUNT", "NEGOTIATE", "MANUAL"} for row in rows)
    assert all((row["amount"] is None) == (row["outcome"] != "AMOUNT") for row in rows)


def _broken(change):
    content = copy.deepcopy(DRAFT["content"])
    change(content)
    return content


@pytest.mark.parametrize("change,message", [
    (lambda c: c["base"].update(per_kg="abc"), "基础费每公斤单价不是有效数字"),
    (lambda c: c["base"].update(fixed="-1"), "基础费固定金额必须是 0 到"),
    (lambda c: c["elevator"]["source"].update(document=""), "有电梯收费的出处文档必须填写"),
    (lambda c: c["floor_tiers"].pop(1), "第 2 档应从 3 楼开始"),
    (lambda c: c.update(max_floor_without_elevator=9), "最后一档应到无电梯最高楼层 9 楼"),
    (lambda c: c["piece_tiers"][1].update(over_kg="90"), "重量区间无效"),
    (lambda c: c.update(piece_limit_kg="150"), "最后一档应到单件重量上限 150 公斤"),
    (lambda c: c.update(first_floor="FREE"), "送到 1 楼的处理方式无效"),
    (lambda c: c["base"]["source"].update(link="http://x"), "必须以 https:// 开头"),
])
def test_content_validation_names_each_problem(change, message):
    with pytest.raises(FeeRuleError, match=message):
        parse_upstairs_content(_broken(change))


def test_validation_lists_all_problems_at_once():
    def change(content):
        content["base"]["per_kg"] = ""
        content["elevator"]["fixed"] = "x"
    with pytest.raises(FeeRuleError) as caught:
        parse_upstairs_content(_broken(change))
    assert "基础费每公斤" in caught.value.message and "有电梯收费固定金额" in caught.value.message


def test_approval_needs_valid_content_effective_date_and_every_check_ticked():
    record = {**DRAFT, "effective_from": None}
    blockers = approval_blockers(record)
    assert any("执行日期" in item for item in blockers) and any("未核对" in item for item in blockers)
    ready = {**DRAFT, "effective_from": "2026-10-01", "checks": [{**item, "done": True} for item in DRAFT["checks"]]}
    assert approval_blockers(ready) == []
    assert approval_blockers({**ready, "content": _broken(lambda c: c.pop("base"))})


def test_migration_087_seeds_exactly_the_reviewed_draft():
    sql = (ROOT / "agent" / "migrations" / "087_agent_fee_rules.sql").read_text(encoding="utf-8")
    values = sql.split("VALUES", 1)[1]
    literals = values.split("'")[1::2]
    assert literals[1:4] == ["upstairs", DRAFT["version_label"], "DRAFT"]
    assert json.loads(literals[4]) == DRAFT["content"] and json.loads(literals[5]) == DRAFT["checks"]
    parse_upstairs_content(json.loads(literals[4]))


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


# -- 计算规则 storage (real migration 087) and the Agent API --------------------------------

from tests.mysql_migrated_database import database  # noqa: E402,F401

SEED_ID = "0f6a1c2e-5b7d-4e3a-9c41-61f2e8d0a087"


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")
def test_migration_087_repository_lifecycle(database):  # noqa: F811
    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    import pymysql

    from shared.fee_rule_repository import FeeRuleRepository

    def connect():
        return pymysql.connect(host=database.host, port=database.port, user=database.user, password=database.password,
                               database=database.database, charset="utf8mb4", autocommit=False)

    repo = FeeRuleRepository(connect)
    seed = repo.get(SEED_ID)
    assert (seed["status"], seed["content"], seed["checks"]) == ("DRAFT", DRAFT["content"], DRAFT["checks"])
    rule_from_record(seed)
    with pytest.raises(FeeRuleError, match="别人修改"):
        repo.submit(SEED_ID, actor_id="7", expected_row_version=seed["row_version"] + 1)
    ticked = [{**item, "done": True} for item in seed["checks"]]
    repo.save_draft(SEED_ID, version_label="V6.1", effective_from="2026-10-01", content=seed["content"], checks=ticked,
                    actor_id="7", expected_row_version=seed["row_version"])
    version = repo.get(SEED_ID)["row_version"]
    repo.submit(SEED_ID, actor_id="7", expected_row_version=version)
    pending = repo.get(SEED_ID)
    assert pending["status"] == "PENDING" and pending["submitted_by"] == "7"
    with pytest.raises(FeeRuleError, match="当前状态不能"):
        repo.save_draft(SEED_ID, version_label="x", effective_from=None, content=seed["content"], checks=ticked,
                        actor_id="7", expected_row_version=pending["row_version"])
    with pytest.raises(FeeRuleError, match="只能勾选已有"):
        repo.set_checks(SEED_ID, checks=[{"text": "新事项", "done": True}], actor_id="7",
                        expected_row_version=pending["row_version"])
    repo.approve(SEED_ID, actor_id="1", expected_row_version=pending["row_version"])
    approved = repo.approved("upstairs")
    assert [item["rule_id"] for item in approved] == [SEED_ID] and approved[0]["effective_from"] == "2026-10-01"
    assert select_rule([rule_from_record(item) for item in approved], fee_kind="upstairs",
                       business_date=date(2026, 11, 12)).version == "V6.1"

    copy_id = repo.copy_to_draft(SEED_ID, actor_id="7")
    copied = repo.get(copy_id)
    assert copied["status"] == "DRAFT" and copied["based_on"] == SEED_ID
    assert not any(item["done"] for item in copied["checks"])  # a copy is checked again
    same_day = [{**item, "done": True} for item in copied["checks"]]
    repo.save_draft(copy_id, version_label="V6.1-改", effective_from="2026-10-01", content=copied["content"], checks=same_day,
                    actor_id="7", expected_row_version=copied["row_version"])
    repo.submit(copy_id, actor_id="7", expected_row_version=repo.get(copy_id)["row_version"])
    with pytest.raises(FeeRuleError, match="同一执行日期已有生效版本"):
        repo.approve(copy_id, actor_id="1", expected_row_version=repo.get(copy_id)["row_version"])
    repo.retire(SEED_ID, actor_id="1", expected_row_version=repo.get(SEED_ID)["row_version"])
    repo.approve(copy_id, actor_id="1", expected_row_version=repo.get(copy_id)["row_version"])
    assert [item["rule_id"] for item in repo.approved("upstairs")] == [copy_id]
    assert [item["action"] for item in repo.get(copy_id)["events"]] == ["APPROVE", "SUBMIT", "SAVE", "COPY"]


def test_agent_api_lets_editors_work_but_only_super_admins_approve_or_retire():
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent.fee_rules_api import create_fee_rules_router

    calls = []
    record = {**DRAFT, "rule_id": SEED_ID, "status": "DRAFT", "effective_from": None, "row_version": 1}

    class Repository:
        def get(self, rule_id):
            return record

        def submit(self, rule_id, *, actor_id, expected_row_version):
            calls.append(("submit", actor_id, expected_row_version))

        def approve(self, rule_id, **kwargs):
            calls.append("approve")

        def retire(self, rule_id, **kwargs):
            calls.append("retire")

    def super_admin(request):
        raise PermissionError("super administrator required")

    app = FastAPI()
    app.include_router(create_fee_rules_router(repository_provider=Repository,
                                               admin_provider=lambda request: SimpleNamespace(actor_id="7"),
                                               super_admin_provider=super_admin))
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/internal/v1/admin/fee-rules/{SEED_ID}"
    assert client.post(f"{base}/submit", json={"row_version": 1}).status_code == 200
    for action in ("approve", "retire"):
        assert client.post(f"{base}/{action}", json={"row_version": 1}).status_code == 500
    assert calls == [("submit", "7", 1)]
    trial = client.post(f"{base}/trial", json={"settlement_weight_kg": "200", "elevator": False, "floor": 5,
                                               "piece_counts": [2, 0], "boundary": True})
    data = trial.json()["data"]
    assert data["result"]["amount"] == "74.00" and data["result"]["reportable"] is False
    assert len(data["boundary"]) == 64
    bad = client.post(f"{base}/trial", json={"settlement_weight_kg": "abc", "elevator": False, "floor": 5,
                                             "piece_counts": [0, 0]})
    assert bad.status_code == 422 and "结算重量" in bad.json()["error"]["message"]
