"""C line: upstairs fee rules computed by code (C1) and manual chapters read by bookmark (C2)."""
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from shared.handbook_fee_rules import (
    UPSTAIRS_V61_DRAFT, FeeRuleError, UpstairsInputs, boundary_table, calculate_upstairs, select_rule,
)
from tools.feishu_knowledge_pdf import chapter_pages, read_outline

W = Decimal("200")


def test_plan_example_no_elevator_fifth_floor_two_heavy_pieces():
    result = calculate_upstairs(UPSTAIRS_V61_DRAFT, UpstairsInputs(W, False, 5, "general", 2, 0, 0, total_pieces=2))
    assert (result.outcome, result.amount) == ("AMOUNT", Decimal("74.00"))
    assert result.steps == ("基础 10 + 0.12×200 = 34.00", "楼层加收（5 楼）0.10×200 = 20.00",
                            "单件加收 10×2 + 30×0 = 20", "合计 74.00")
    assert "第 50 页" in result.citations[0] and "卸货费" in result.reminders[0]
    assert not result.reportable  # a draft never gives people an amount


@pytest.mark.parametrize("floor,expected", [(2, "34.00"), (3, "44.00"), (4, "44.00"), (6, "54.00"), (7, "64.00")])
def test_floor_tiers(floor, expected):
    assert calculate_upstairs(UPSTAIRS_V61_DRAFT, UpstairsInputs(W, False, floor)).amount == Decimal(expected)


def test_elevator_and_situations_without_an_amount():
    assert calculate_upstairs(UPSTAIRS_V61_DRAFT, UpstairsInputs(W, True, 9)).amount == Decimal("17.00")
    assert calculate_upstairs(UPSTAIRS_V61_DRAFT, UpstairsInputs(W, False, 8)).outcome == "NEGOTIATE"
    assert calculate_upstairs(UPSTAIRS_V61_DRAFT, UpstairsInputs(W, False, 3, pieces_over_120=1)).outcome == "NEGOTIATE"
    assert calculate_upstairs(UPSTAIRS_V61_DRAFT, UpstairsInputs(W, False, 3, "mattress")).outcome == "MANUAL"
    assert calculate_upstairs(UPSTAIRS_V61_DRAFT, UpstairsInputs(W, False, 1)).outcome == "MANUAL"


def test_decimal_rounding_half_up():
    result = calculate_upstairs(UPSTAIRS_V61_DRAFT, UpstairsInputs(Decimal("123.45"), True, 3))
    assert result.amount == Decimal("12.41")  # 5 + 0.06×123.45 = 12.407


@pytest.mark.parametrize("inputs,message", [
    (UpstairsInputs(Decimal("80"), False, 3, pieces_50_100=2), "超过整票结算重量"),
    (UpstairsInputs(W, False, 3, pieces_50_100=3, total_pieces=2), "超过整票件数"),
    (UpstairsInputs(Decimal("0"), False, 3), "必须大于 0"),
    (UpstairsInputs(W, False, 0), "楼层无效"),
])
def test_unreasonable_inputs_stop_the_calculation(inputs, message):
    with pytest.raises(FeeRuleError, match=message):
        calculate_upstairs(UPSTAIRS_V61_DRAFT, inputs)


def test_rule_versions_by_effective_date():
    with pytest.raises(FeeRuleError, match="没有已审核生效"):
        select_rule([UPSTAIRS_V61_DRAFT], fee_kind="upstairs", business_date=date(2026, 11, 15))
    old = replace(UPSTAIRS_V61_DRAFT, version="V6.0", status="APPROVED", effective_from=date(2025, 1, 1))
    new = replace(UPSTAIRS_V61_DRAFT, version="V6.1", status="APPROVED", effective_from=date(2026, 10, 1))
    assert select_rule([old, new], fee_kind="upstairs", business_date=date(2026, 9, 30)).version == "V6.0"
    assert select_rule([old, new], fee_kind="upstairs", business_date=date(2026, 10, 1)).version == "V6.1"
    with pytest.raises(ValueError):
        replace(UPSTAIRS_V61_DRAFT, status="APPROVED")  # approval needs the headquarters effective date


def test_boundary_table_covers_every_combination_with_an_outcome():
    rows = boundary_table(UPSTAIRS_V61_DRAFT)
    assert len(rows) == 64 and all(row["outcome"] in {"AMOUNT", "NEGOTIATE", "MANUAL"} for row in rows)
    assert all((row["amount"] is None) == (row["outcome"] != "AMOUNT") for row in rows)


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
