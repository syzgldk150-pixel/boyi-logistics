"""计算规则 → 从手册起草 (方案 B, 2026-11-20): an AI reads a few manual pages and drafts fee rules.

The model sees the pages' text and pictures and answers with block-built rule contents
(``shared.fee_engine``). Nothing it says is trusted: ``drafting_spec`` builds a validator bound
to the pages that were read, and a draft is kept only when

* every content passes ``parse_rule`` and every amount has a source on itself or above;
* every source cites a page inside the range that was read (the document name is filled in
  by code, never by the model) and any quoted words really are on those pages;
* every number of the formula (amounts, rates, tiers, limits, compared values) is written on
  those pages, so a figure the manual does not contain cannot slip in;
* every question has 2 to 6 answer options.

What fails goes back to the model once with the reasons; what still fails is not saved.
Drafts become ``DRAFT`` versions and go through 核对 and approval like any other. Pure
functions: no environment or I/O.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from shared.ai_tasks import AITaskSpec
from shared.fee_engine import parse_rule, unsourced_blocks
from shared.fee_rule_lines import pages_of
from shared.handbook_fee_rules import FEE_NAME_LIMIT, FeeRuleError

MAX_PAGES = 8
MAX_FEES = 12
MAX_TEXT = 60_000
DRAFT_LABEL = "AI草稿"
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")
_CHINESE_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SPACE = re.compile(r"\s+")

SYSTEM_PROMPT = """你是物流公司的收费规则录入员。用户给你一份收费手册里的几页（文字和页面图片），请把这几页里写明的每一项收费，整理成公司系统的“积木式收费规则”。只输出 JSON。

输出格式：
{"fees": [{"name": "费用名称（不超过 20 字，用手册里的叫法）",
           "content": {规则内容，格式见下},
           "questions": [{"text": "手册没写清、需要人拍板的地方（写明页码）", "options": ["做法一", "做法二", "其他（写明做法）"]}]}],
 "skipped": ["这几页里看到但没法整理成规则的内容，一句话说明原因"]}

规则内容格式（schema 固定为 1）：
{"schema": 1,
 "params": [要人填写的参数],
 "formula": 计算结构（一个积木）,
 "definitions": {"名称": 积木}（可选，多处共用的部分，用 {"type": "use", "name": "名称"} 引用）,
 "group": "互斥组名"（可选，同组费用不同时收、取大）,
 "reminders": ["随结果提示给人的话，最多 10 条"]}

参数（key 用小写英文、数字、下划线，字母开头）：
- 数字：{"key": "settlement_weight", "label": "结算重量", "type": "number", "unit": "kg", "min": "0.01", "max": "100000", "prefill": "settlement_weight"}；整数加 "integer": true；可加 "default"、"required": false、"hint"
- 单选：{"key": "goods_type", "label": "货物类型", "type": "choice", "options": [{"value": "general", "label": "普货"}, ...]}
- 是/否：{"key": "elevator", "label": "有电梯", "type": "bool"}
- 逐件明细：{"key": "pieces", "label": "货物明细", "type": "pieces", "fields": [{"key": "weight", "label": "单件实际重量", "unit": "kg", "min": "0"}], "prefill": "pieces"}（每行自带件数 count）
- prefill 只能是 settlement_weight（运单结算重量）、pieces（运单件数）、actual_weight（运单实际重量）

取值写法（用在 of、by、left 等处）：参数 key；逐件计费里用 "piece.weight"、"piece.count"；{"const": "10"}；
{"count": "pieces", "where": 条件}（件数）；{"sum": "pieces", "field": "weight", "where": 条件}；{"max": "pieces", "field": "weight"}；
{"excess": 取值, "over": "3"}（超出 3 的部分）；{"ceil": "stairs", "per": "16", "min": "1"}（每 16 计 1，不足按 1）；
{"first": [取值, 取值]}（取第一个填了的）；{"add": [...]}；{"mul": [...]}

条件：{"left": 取值, "op": "eq|ne|gt|ge|lt|le|in", "value": 值}，或 {"all": [...]}、{"any": [...]}、{"not": 条件}

积木（每个都可加 "label" 名称和 "source" 出处）：
- {"type": "fixed", "amount": "50"}  固定金额
- {"type": "rate", "rate": "0.5", "unit": "元/件", "of": 取值}  单价 × 数值
- {"type": "amount", "of": 取值}  数值本身就是金额（如实报实销的发票金额）
- {"type": "sum", "items": [积木...]}  相加；{"type": "max", "items": [...]}  取大
- {"type": "tiers", "by": 取值, "tiers": [{"over": null, "up_to": "20", "then": 积木}, {"over": "20", "up_to": null, "then": 积木}]}  分档；默认“大于起点、不超过终点”，要“不小于起点、小于终点”时加 "closed": "left"；各档必须首尾相接
- {"type": "when", "cases": [{"if": 条件, "then": 积木}], "else": 积木}  按条件，取第一个满足的
- {"type": "lookup", "by": 单选参数key, "rows": [{"value": "general", "then": 积木}], "else": 积木}  按选项
- {"type": "clamp", "item": 积木, "min": "5", "max": "100"}  最低/最高
- {"type": "each_piece", "pieces": "pieces", "item": 积木}  逐件计费（每行算一次再乘件数）
- {"type": "none", "outcome": "NEGOTIATE|NOT_PROVIDED|MANUAL|ACTUAL", "reason": "原因"}  无金额：需协商 / 可不提供服务 / 需人工判断 / 实报实销

出处：{"location": "第五篇第三章 第 47 页", "quote": "手册原文里的一小句（可选，必须一字不差）"}。不要写文档名，系统会自动填。

硬性要求：
1. 只整理这几页里写明的收费；每个金额、单价、档位、上下限、比较的数都必须是这几页原文里写着的数字，不许推算、换算或编造。
2. 每个有金额或无金额结果的积木，自己或上层必须有出处；出处页码必须在给你的页码范围内。
3. 手册没写清的情况（比如两种条件同时出现怎么办），规则里用 {"type": "none", "outcome": "MANUAL"} 处理，并在 questions 里提一个问题，第一个选项写“需人工判断（现在的做法）”，最后一个选项写“其他（写明做法）”。
4. 一页里有几种独立的费用就拆成几项；同一种费用的不同情况（有无电梯、不同货物类型）放在同一项里用 when/lookup 区分。
5. 费用名称尽量和“已有费用”里的名称一致，方便系统存成新版本。
6. 金额、数字一律写成字符串，例如 "0.5"。"""

_EXAMPLE = {"fees": [{"name": "示例清点费", "content": {
    "schema": 1,
    "params": [{"key": "check_count", "label": "清点件数", "type": "number", "unit": "件", "integer": True, "min": "0", "max": "100000"}],
    "formula": {"type": "tiers", "label": "示例清点费", "by": "check_count",
                "source": {"location": "第二章 第 9 页", "quote": "10件以内免费"},
                "tiers": [{"over": None, "up_to": "10", "then": {"type": "fixed", "amount": "0"}},
                          {"over": "10", "up_to": None, "then": {"type": "clamp", "min": "3", "max": "80", "item": {
                              "type": "rate", "rate": "0.2", "unit": "元/件", "of": "check_count"}}}]},
    "reminders": ["件数以签收底单为准。"]},
    "questions": []}], "skipped": []}


def chapters(outline: Any, page_count: int) -> list[dict[str, Any]]:
    """Bookmarked chapters with their page ranges: from a bookmark's page to the page where the
    next bookmark of the same or a higher level starts (that page is shared: chapters start mid-page)."""
    entries = [item for item in (outline or []) if isinstance(item, Mapping) and isinstance(item.get("page"), int)
               and isinstance(item.get("level"), int) and str(item.get("title") or "").strip()]
    result = []
    for index, item in enumerate(entries):
        end = page_count
        for later in entries[index + 1:]:
            if later["level"] <= item["level"]:
                end = max(item["page"], later["page"])  # the next chapter often starts mid-page
                break
        result.append({"title": str(item["title"]).strip()[:200], "level": item["level"], "start": item["page"], "end": end})
    return result


def page_range(start: Any, end: Any, page_count: int) -> tuple[int, int]:
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= page_count:
        raise FeeRuleError("FEE_DRAFT_PAGES_INVALID", f"页码范围无效（这份文档共 {page_count} 页）。")
    if end - start + 1 > MAX_PAGES:
        raise FeeRuleError("FEE_DRAFT_PAGES_INVALID", f"一次最多起草 {MAX_PAGES} 页，请选小一点的章节或页码范围。")
    return start, end


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number.normalize() if number.is_finite() else None


def _chinese_numbers(text: str) -> set[Decimal]:
    """Small numbers written in Chinese (三件、十元、二十公斤、两次): enough for counts and tiers."""
    found = set()
    for match in re.finditer(r"[零一二两三四五六七八九十百]+", text):
        word, total, current = match.group(0), 0, 0
        for char in word:
            if char in _CHINESE_DIGITS:
                current = _CHINESE_DIGITS[char]
            elif char == "十":
                total += (current or 1) * 10
                current = 0
            elif char == "百":
                total += (current or 1) * 100
                current = 0
        found.add(Decimal(total + current))
    return found


def page_numbers(text: str) -> set[Decimal]:
    numbers = {Decimal(item).normalize() for item in _NUMBER.findall(text.replace("，", ",").replace(",", ""))}
    return numbers | {item.normalize() for item in _chinese_numbers(text)}


def formula_numbers(content: Mapping[str, Any]) -> list[tuple[str, Decimal]]:
    """Every number the formula itself uses (not parameter input limits), with where it is."""
    found: list[tuple[str, Decimal]] = []

    def add(value: Any, where: str) -> None:
        number = _decimal(value)
        if number is not None:
            found.append((where, number))

    def ref(value: Any, where: str) -> None:
        if not isinstance(value, Mapping):
            return
        if "const" in value:
            add(value["const"], where)
        for key in ("over", "per", "min"):
            if key in value:
                add(value[key], where)
        for key in ("excess", "ceil"):
            ref(value.get(key), where)
        for key in ("first", "add", "mul"):
            for item in value.get(key) or []:
                ref(item, where)
        if value.get("where"):
            cond(value["where"], where)

    def cond(value: Any, where: str) -> None:
        if not isinstance(value, Mapping):
            return
        for key in ("all", "any"):
            for item in value.get(key) or []:
                cond(item, where)
        if "not" in value:
            cond(value["not"], where)
        if "left" in value:
            ref(value["left"], where)
            if not isinstance(value.get("value"), (bool, list)):
                add(value.get("value"), where)

    def node(value: Any, where: str) -> None:
        if not isinstance(value, Mapping):
            return
        here = f"{where} › {value.get('label') or value.get('type')}"
        for key in ("amount", "rate", "min", "max"):
            if key in value:
                add(value[key], here)
        for key in ("of", "by"):
            ref(value.get(key), here)
        for item in value.get("items") or []:
            node(item, here)
        for key in ("item", "else"):
            node(value.get(key), here)
        for tier in value.get("tiers") or []:
            if isinstance(tier, Mapping):
                add(tier.get("over"), here)
                add(tier.get("up_to"), here)
                node(tier.get("then"), here)
        for case in value.get("cases") or []:
            if isinstance(case, Mapping):
                cond(case.get("if"), here)
                node(case.get("then"), here)
        for row in value.get("rows") or []:
            if isinstance(row, Mapping):
                node(row.get("then"), here)

    node(content.get("formula"), "计算结构")
    for name, value in (content.get("definitions") or {}).items():
        node(value, f"公共部分“{name}”")
    return found


def _sources(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "source" and isinstance(item, Mapping):
                yield item
            else:
                yield from _sources(item)
    elif isinstance(value, list):
        for item in value:
            yield from _sources(item)


def _squash(text: str) -> str:
    return _SPACE.sub("", text).replace("，", ",").replace("。", ".").replace("（", "(").replace("）", ")")


def check_draft(answer: Any, *, document: str, pages: Sequence[Mapping[str, Any]],
                existing: Sequence[str] = ()) -> dict[str, Any]:
    """Validate and normalize one model answer against the pages it was given; raise ValueError with
    every problem in Chinese (the gateway hands them back to the model once)."""
    if not isinstance(answer, Mapping) or not isinstance(answer.get("fees"), list):
        raise ValueError("回答必须是 {\"fees\": [...], \"skipped\": [...]}。")
    fees = answer["fees"]
    if not 1 <= len(fees) <= MAX_FEES:
        raise ValueError(f"fees 要有 1 到 {MAX_FEES} 项；这几页里没有收费时，请把原因写在 skipped 里并给出最接近的一项。")
    texts = {int(page["page"]): str(page.get("text") or "") for page in pages}
    low, high = min(texts), max(texts)
    allowed = {Decimal(0), Decimal(1)}
    for text in texts.values():
        allowed |= page_numbers(text)
    problems, result, names = [], [], set()
    for index, fee in enumerate(fees, 1):
        fee = fee if isinstance(fee, Mapping) else {}
        name = str(fee.get("name") or "").strip()
        where = f"第 {index} 项“{name or '未命名'}”"
        if not name or len(name) > FEE_NAME_LIMIT:
            problems.append(f"{where}的名称必须填写，且不超过 {FEE_NAME_LIMIT} 字。")
        elif name in names:
            problems.append(f"{where}与前面的费用重名，请合并或改名。")
        names.add(name)
        content = fee.get("content")
        if not isinstance(content, Mapping):
            problems.append(f"{where}缺少 content。")
            continue
        content = dict(content)
        content.setdefault("schema", 1)
        content.setdefault("reminders", [])
        for source in _sources(content):
            if not isinstance(source, dict):
                continue
            source["document"] = document
            cited = pages_of(source.get("location"))
            if not cited:
                problems.append(f"{where}有出处没写页码（例如“第 47 页”）：{source.get('location')!r}。")
            elif any(not low <= page <= high for page in cited):
                problems.append(f"{where}的出处“{source.get('location')}”不在本次读取的第 {low}–{high} 页内。")
            quote = str(source.get("quote") or "").strip()
            if quote and cited and not any(_squash(quote) in _squash(texts.get(page, "")) for page in cited):
                problems.append(f"{where}的原文摘句“{quote[:40]}”在第 {'、'.join(map(str, cited))} 页原文里找不到，请一字不差地摘，或者不写摘句。")
        try:
            parse_rule(content)
        except FeeRuleError as exc:
            problems.append(f"{where}的规则有问题：{exc.message}")
        missing = unsourced_blocks(content)
        if missing:
            problems.append(f"{where}这些积木没有出处：{'、'.join(missing[:6])}。")
        made_up = sorted({f"{number:f}（{place}）" for place, number in formula_numbers(content) if number not in allowed})
        if made_up:
            problems.append(f"{where}用了原文里没有的数字：{'、'.join(made_up[:6])}。只能用这几页写着的数字。")
        questions = []
        for item in fee.get("questions") or []:
            item = item if isinstance(item, Mapping) else {}
            text = str(item.get("text") or "").strip()
            options = [str(option).strip() for option in item.get("options") or [] if str(option).strip()]
            if not text or len(text) > 300 or not 2 <= len(options) <= 6 or len(set(options)) != len(options) \
                    or any(len(option) > 60 for option in options):
                problems.append(f"{where}的问题“{text[:20]}”要有内容（不超过 300 字）和 2 到 6 个不重复的选项（每个不超过 60 字）。")
                continue
            questions.append({"kind": "question", "text": text, "options": options, "answer": None, "note": ""})
        if len(questions) > 10:
            problems.append(f"{where}的问题太多（最多 10 个），请只留真正需要拍板的。")
        result.append({"name": name, "content": content, "questions": questions, "existing": name in set(existing)})
    if problems:
        raise ValueError("；".join(problems))
    skipped = [str(item).strip()[:200] for item in answer.get("skipped") or [] if str(item).strip()][:10]
    return {"fees": result, "skipped": skipped}


def drafting_payload(*, document: str, pages: Sequence[Mapping[str, Any]], existing: Sequence[str],
                     chapter: str = "") -> dict[str, Any]:
    text_pages = [{"page": int(page["page"]), "text": str(page.get("text") or "")} for page in pages]
    if sum(len(item["text"]) for item in text_pages) > MAX_TEXT:
        raise FeeRuleError("FEE_DRAFT_TOO_LONG", "这几页文字太多，请选小一点的范围。")
    return {"document": document, "chapter": chapter, "pages": text_pages,
            "page_range": f"第 {text_pages[0]['page']}–{text_pages[-1]['page']} 页",
            "existing_fees": list(existing)[:60],
            "note": "页面图片按页码顺序附在前面；表格以图片为准，文字层里的表格可能是乱序的。",
            "format_example": _EXAMPLE}


def drafting_spec(*, document: str, pages: Sequence[Mapping[str, Any]], existing: Sequence[str]) -> AITaskSpec:
    """The AI task for one read: its validator is bound to these pages."""
    return AITaskSpec(task_id="handbook.fee_draft", prompt_version="2026-11-20.1", system_prompt=SYSTEM_PROMPT,
                      validate=lambda answer: check_draft(answer, document=document, pages=pages, existing=existing),
                      max_retries=1, timeout_seconds=240)


__all__ = ["DRAFT_LABEL", "MAX_PAGES", "SYSTEM_PROMPT", "chapters", "check_draft", "drafting_payload", "drafting_spec",
           "formula_numbers", "page_numbers", "page_range"]
