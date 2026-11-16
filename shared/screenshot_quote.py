"""Quote from a screenshot: the AI task that reads the delivery address (收尾 截图报价).

A person sends a screenshot (a chat, a note) to the robot. The active model,
through the shared AI task gateway, returns the delivery address, any weight
and volume written in the picture, and the text it read. Code checks the
address and the numbers are in that text, shows the address to the person and
hands the existing three-platform quote the usual ``报价 地址，重量[，体积]`` text,
so permissions, the plugin call and the reply stay exactly as for typed quotes.
A missing weight is asked; a missing volume is left to the quote plugin, which
uses 0.1 m³ and says so. Pure functions: no environment or I/O.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from shared.ai_tasks import AITaskSpec

PENDING_TTL_SECONDS = 900
MAX_IMAGE_BYTES = 8_000_000
_SPACE = re.compile(r"[\s，,；;。．.、:：\-—_（）()\[\]【】]+")
_PHONE = re.compile(r"1\d{10}")

SYSTEM_PROMPT = (
    "你在物流公司帮同事报价。用户发来一张截图（聊天记录、便签、单据等），请找出要送货的收货地址，"
    "以及截图里明确写出的货物重量（公斤）和体积（立方米）。\n"
    "规则：1. 地址写省、市、区县和详细地址，不要姓名、电话、公司名以外的内容；截图里有多个地址时，选收货/送货的那个，无法判断就填 null 并在 note 说明。"
    "2. 重量、体积只填截图里明确写出的数字，没写就填 null，不要按货物种类估算；车长（如 13 米挂车）不是体积。"
    "3. source_text 照抄你读到的、与地址和重量体积有关的原文（可以多行），不要改写。"
    "只输出 JSON：{\"address\": \"…或 null\", \"weight_kg\": 数字或 null, \"volume_m3\": 数字或 null, "
    "\"source_text\": \"…\", \"note\": \"…\"}")


class ScreenshotQuoteError(ValueError):
    pass


def compact(text: Any) -> str:
    return _SPACE.sub("", unicodedata.normalize("NFKC", str(text or ""))).lower()


def _number(value: Any, label: str, upper: Decimal) -> Decimal | None:
    if value in (None, "", "null"):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} 不是数字") from exc
    if not number.is_finite() or number <= 0 or number > upper:
        raise ValueError(f"{label} 超出合理范围")
    return number


def _figures(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


def validate_reading(answer: Any) -> dict[str, Any]:
    """The model's reading, kept only when its address and numbers are in the text it says it read."""
    if not isinstance(answer, Mapping):
        raise ValueError("回答必须是 JSON 对象")
    source = str(answer.get("source_text") or "").strip()
    raw_address = answer.get("address")
    address = None if raw_address in (None, "", "null") else str(raw_address).strip()
    if address is not None:
        address = _PHONE.sub("", address).strip()
        if not 4 <= len(address) <= 150:
            raise ValueError("地址长度不合理")
        if not compact(address) or compact(address) not in compact(source):
            raise ValueError("address 必须是 source_text 里原样出现的文字")
    weight = _number(answer.get("weight_kg"), "weight_kg", Decimal("100000"))
    volume = _number(answer.get("volume_m3"), "volume_m3", Decimal("1000"))
    flat = compact(source)
    for label, number in (("weight_kg", weight), ("volume_m3", volume)):
        if number is not None and _figures(number) not in flat:
            raise ValueError(f"{label} 的数字必须出现在 source_text 里")
    return {"address": address, "weight_kg": None if weight is None else _figures(weight),
            "volume_m3": None if volume is None else _figures(volume),
            "note": str(answer.get("note") or "")[:200]}


READ_SCREENSHOT = AITaskSpec(task_id="quote.read_screenshot", prompt_version="2026-11-16.1",
                             system_prompt=SYSTEM_PROMPT, validate=validate_reading, max_retries=1,
                             timeout_seconds=45.0)


FOLLOW_UP_INTENTS = ("quote", "address", "cancel", "other")
_WEIGHT_UNITS = {"公斤": Decimal("1"), "千克": Decimal("1"), "kg": Decimal("1"), "斤": Decimal("0.5"),
                 "吨": Decimal("1000")}

FOLLOW_UP_PROMPT = (
    "你在物流公司帮同事报价。机器人刚从一张截图里识别出收货地址（waiting_address），正在等这位同事补充货物重量；"
    "waiting_volume_m3 是截图里写的体积（可能为空）。reply 是同事接下来发的一句话，请判断它的意思：\n"
    "intent 只能是：quote（给出了重量，可能同时给体积或更正地址）、address（只更正了收货地址，没有给重量）、"
    "cancel（不报价了，如“算了”“不用了”）、other（与这次报价无关，如查单号、问别的事）。\n"
    "规则：1. weight_value 照抄 reply 里写的重量数字，weight_unit 填它的单位（公斤、千克、kg、斤、吨之一；只写数字没写单位就填公斤）；"
    "没写重量填 null，不要估算。2. volume_m3 只填 reply 里明确写的体积数字（方、立方、立方米都是立方米），没写填 null；车长不是体积。"
    "3. address 只在 reply 里写了新的收货地址时照抄那段地址，否则填 null。4. 拿不准就填 other。"
    "只输出 JSON：{\"intent\": \"quote|address|cancel|other\", \"weight_value\": 数字或 null, "
    "\"weight_unit\": \"…或 null\", \"volume_m3\": 数字或 null, \"address\": \"…或 null\", \"note\": \"…\"}")


def validate_follow_up(answer: Any, reply: str) -> dict[str, Any]:
    """The model's reading of a follow-up, kept only when its numbers and address are in the reply itself."""
    if not isinstance(answer, Mapping):
        raise ValueError("回答必须是 JSON 对象")
    intent = str(answer.get("intent") or "").strip()
    if intent not in FOLLOW_UP_INTENTS:
        raise ValueError("intent 只能是 quote/address/cancel/other")
    if intent in {"cancel", "other"}:
        return {"intent": intent, "address": None, "weight_kg": None, "volume_m3": None}
    flat = compact(reply)
    raw_address = answer.get("address")
    address = None if raw_address in (None, "", "null") else _PHONE.sub("", str(raw_address)).strip()
    if address is not None:
        if not 4 <= len(address) <= 150:
            raise ValueError("地址长度不合理")
        if not compact(address) or compact(address) not in flat:
            raise ValueError("address 必须是 reply 里原样出现的文字")
    value = _number(answer.get("weight_value"), "weight_value", Decimal("200000"))
    volume = _number(answer.get("volume_m3"), "volume_m3", Decimal("1000"))
    # Numbers are matched with their decimal point kept ("1.5吨" is not "15").
    figures_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(reply or "")))
    for label, number in (("weight_value", value), ("volume_m3", volume)):
        if number is not None and not re.search(rf"(?<![\d.]){re.escape(_figures(number))}(?![\d]|\.\d)", figures_text):
            raise ValueError(f"{label} 的数字必须出现在 reply 里")
    weight = None
    if value is not None:
        unit = str(answer.get("weight_unit") or "公斤").strip().lower()
        if unit not in _WEIGHT_UNITS:
            raise ValueError("weight_unit 只能是 公斤、千克、kg、斤、吨")
        weight = value * _WEIGHT_UNITS[unit]
        if weight > Decimal("100000"):
            raise ValueError("重量超出合理范围")
    if intent == "quote" and weight is None:
        raise ValueError("intent=quote 必须给出 reply 里写的重量")
    if intent == "address" and address is None:
        raise ValueError("intent=address 必须给出 reply 里写的地址")
    return {"intent": intent, "address": address, "weight_kg": None if weight is None else _figures(weight),
            "volume_m3": None if volume is None else _figures(volume)}


READ_FOLLOW_UP = AITaskSpec(task_id="quote.read_follow_up", prompt_version="2026-11-16.1",
                            system_prompt=FOLLOW_UP_PROMPT, validate=lambda answer: validate_follow_up(answer, ""),
                            max_retries=1, timeout_seconds=30.0)


def follow_up_task(reply: str) -> AITaskSpec:
    """The follow-up task whose validator checks the answer against this very reply."""
    return replace(READ_FOLLOW_UP, validate=lambda answer: validate_follow_up(answer, reply))


def quote_address(address: str) -> str:
    """The address as one field of the quote text (its separators and spaces removed)."""
    return re.sub(r"[\s，,；;]+", "", unicodedata.normalize("NFKC", address))


def quote_text(address: str, weight: str, volume: str | None = None) -> str:
    """The same text a person would type, for the existing quote route."""
    parts = [quote_address(address), str(weight).strip()]
    if volume:
        parts.append(str(volume).strip())
    return "报价 " + "，".join(parts)


@dataclass(frozen=True)
class PendingQuote:
    """A recognised address waiting for its weight (and optional volume) from the same person."""
    address: str
    volume: str | None = None


def recognised_line(address: str) -> str:
    return f"识别到的收货地址：{address}"


def ask_weight_reply(address: str, volume: str | None) -> str:
    volume_hint = f"体积按截图写的 {volume} 立方米。" if volume else "体积没写会按 0.1 立方米计算，也可以一起发，例如“300公斤 2方”。"
    return (f"{recognised_line(address)}\n请告诉我重量（公斤），例如“300公斤”；{volume_hint}\n"
            "地址不对的话，直接发正确的“报价 地址，重量”。")


def quoting_reply(address: str, weight: str, volume: str | None) -> str:
    volume_text = f"体积 {volume} 立方米" if volume else "体积没写，按 0.1 立方米"
    return f"{recognised_line(address)}\n重量 {weight} 公斤，{volume_text}，正在报价……\n地址不对的话，直接发正确的“报价 地址，重量”。"


CANCELLED_REPLY = "好的，这张截图的报价已取消。"
NO_ADDRESS_REPLY = "截图里没有找到收货地址。可以直接发“报价 地址，重量”，例如“报价 长沙市岳麓区麓谷大道，300”。"


__all__ = ["CANCELLED_REPLY", "FOLLOW_UP_INTENTS", "MAX_IMAGE_BYTES", "NO_ADDRESS_REPLY", "PENDING_TTL_SECONDS",
           "PendingQuote", "READ_FOLLOW_UP", "READ_SCREENSHOT", "follow_up_task", "validate_follow_up",
           "ScreenshotQuoteError", "ask_weight_reply", "compact", "quote_address", "quote_text", "quoting_reply",
           "recognised_line", "validate_reading"]
