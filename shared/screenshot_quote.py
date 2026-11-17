"""Quote from a screenshot: the AI task that reads the delivery address (收尾 截图报价).

A person sends a screenshot (a chat, a note, an order) to the robot. The active
model, through the shared AI task gateway, copies what the picture says: the
delivery address (and the pieces of text it is made of), the weight and volume
*as written* (value, unit, per piece or in total, or the dimensions), anything it
is unsure about, and the text it read. Code checks every copied piece and number
is in that text, then does the arithmetic itself (tons and 斤 to kg, per piece ×
pieces, dimensions to m³), so screenshots may be worded in many ways without the
model inventing numbers. A reading with doubts (a weight range, an unclear piece
count) is shown and waits for "按这个报" or a corrected weight instead of quoting.
The usual ``报价 地址，重量[，体积]`` text then goes to the existing three-platform
quote, so permissions, the plugin call and the reply stay as for typed quotes.
A missing weight is asked; a missing volume is left to the quote plugin, which
uses 0.1 m³ and says so. Pure functions: no environment or I/O.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping

from shared.ai_tasks import AITaskSpec

PENDING_TTL_SECONDS = 900
MAX_IMAGE_BYTES = 8_000_000
_SPACE = re.compile(r"[\s，,；;。．.、:：\-—_（）()\[\]【】]+")
_PHONE = re.compile(r"1\d{10}")

SYSTEM_PROMPT = (
    "你在物流公司帮同事报价。用户发来一张截图（聊天记录、便签、单据等），请照抄截图里的收货地址、货物重量和体积，不要自己计算或换算。\n"
    "规则：1. address 写收货地址（省、市、区县和详细地址，不要姓名、电话），只用截图里写的文字拼成，不要自己补省市；"
    "address_parts 按顺序列出拼成地址的原文片段（地址分几栏写时，如“省：江苏 市：南通 详细地址：…”，每栏一个片段）。"
    "截图里有多个地址时选收货/送货的那个，无法判断就填 null 并写进 doubts。\n"
    "2. weight 照抄截图里写的重量：value 是原数字（“0.35吨”填 0.35，“1,200kg”填 1200，“三百公斤”填 300），"
    "unit 是原单位（公斤、千克、kg、斤、吨、t 之一，只写数字没写单位填公斤）；写的是每件重量时 pieces 填件数，写的是总重时 pieces 填 null。没写重量 weight 填 null，不要按货物种类估算。\n"
    "3. volume 照抄截图里写的体积：写了体积就填 {\"value\": 原数字, \"pieces\": 每件体积时的件数或 null}（方、立方、立方米都是立方米）；"
    "没写体积但写了长宽高就填 {\"length\": 数字, \"width\": 数字, \"height\": 数字, \"unit\": \"cm|mm|m\", \"pieces\": 件数或 null}；都没写填 null。车长（如 13 米挂车）不是货物尺寸。\n"
    "4. doubts 列出拿不准的地方（例如重量写的是范围“300-400公斤”、看不出是每件还是总重、有两个重量），没有就填 []；有范围时 value 填较大的数。\n"
    "5. source_text 照抄你读到的、与地址和重量体积有关的原文（可以多行），不要改写。\n"
    "只输出 JSON：{\"address\": \"…或 null\", \"address_parts\": [\"…\"], "
    "\"weight\": {\"value\": 数字, \"unit\": \"…\", \"pieces\": 数字或 null} 或 null, "
    "\"volume\": {…} 或 null, \"doubts\": [\"…\"], \"source_text\": \"…\", \"note\": \"…\"}")


class ScreenshotQuoteError(ValueError):
    pass


def compact(text: Any) -> str:
    return _SPACE.sub("", unicodedata.normalize("NFKC", str(text or ""))).lower()


def _number(value: Any, label: str, upper: Decimal) -> Decimal | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} 不是数字")
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} 不是数字") from exc
    if not number.is_finite() or number <= 0 or number > upper:
        raise ValueError(f"{label} 超出合理范围")
    return number


def _figures(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


_WEIGHT_UNITS = {"公斤": Decimal("1"), "千克": Decimal("1"), "kg": Decimal("1"), "kgs": Decimal("1"),
                 "斤": Decimal("0.5"), "吨": Decimal("1000"), "t": Decimal("1000")}
_LENGTH_UNITS = {"cm": Decimal("0.01"), "厘米": Decimal("0.01"), "mm": Decimal("0.001"), "毫米": Decimal("0.001"),
                 "m": Decimal("1"), "米": Decimal("1")}
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_NUMBER = re.compile(r"[零〇一二两三四五六七八九十百千万]+(?:点[零〇一二三四五六七八九]+)?")
_THOUSANDS = re.compile(r"(?<=\d)[,，](?=\d{3}(?!\d))")


def _chinese_number(token: str) -> str:
    """Arabic figures for a Chinese numeral ("三百五十" → "350", "一点五" → "1.5")."""
    whole, _, fraction = token.partition("点")
    if whole and all(ch in _CN_DIGITS for ch in whole):  # digit by digit ("一二〇")
        number = "".join(str(_CN_DIGITS[ch]) for ch in whole)
    else:
        total, section, digit = 0, 0, None
        for ch in whole:
            if ch in _CN_DIGITS:
                digit = _CN_DIGITS[ch]
            elif ch == "万":
                total += (section + (digit or 0)) * 10000
                section, digit = 0, None
            else:
                section += (1 if digit is None else digit) * _CN_UNITS[ch]
                digit = None
        number = str(total + section + (digit or 0))
    if fraction:
        number += "." + "".join(str(_CN_DIGITS[ch]) for ch in fraction)
    return number


def numbers_text(text: Any) -> str:
    """``text`` with numbers in one spelling: no spaces, no thousands commas, Chinese numerals as figures."""
    flat = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))
    flat = _THOUSANDS.sub("", flat)
    return _CN_NUMBER.sub(lambda match: _chinese_number(match.group(0)), flat)


def _number_in_text(number: Decimal, text: str) -> bool:
    """Whether ``number`` is written in ``text`` with its decimal point kept ("0.35吨" holds 0.35, not 35)."""
    return re.search(rf"(?<![\d.]){re.escape(_figures(number))}(?![\d]|\.\d)", numbers_text(text)) is not None


def _written(value: Any, label: str, upper: Decimal, source: str) -> Decimal | None:
    number = _number(value, label, upper)
    if number is not None and not _number_in_text(number, source):
        raise ValueError(f"{label} 的数字必须出现在 source_text 里")
    return number


def _kilograms(value: Decimal | None, unit: Any) -> Decimal | None:
    if value is None:
        return None
    unit_text = str(unit or "公斤").strip().lower()
    if unit_text not in _WEIGHT_UNITS:
        raise ValueError("weight_unit 只能是 公斤、千克、kg、斤、吨、t")
    weight = value * _WEIGHT_UNITS[unit_text]
    if weight > Decimal("100000"):
        raise ValueError("重量超出合理范围")
    return weight


def _pieces(raw: Mapping[str, Any], label: str, source: str) -> Decimal | None:
    pieces = _written(raw.get("pieces"), f"{label}.pieces", Decimal("10000"), source)
    if pieces is not None and pieces != pieces.to_integral_value():
        raise ValueError(f"{label}.pieces 必须是整数")
    return None if pieces in (None, Decimal(1)) else pieces


def _object(raw: Any, label: str) -> Mapping[str, Any] | None:
    if raw in (None, "", "null"):
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} 必须是对象或 null")
    return raw


def _read_weight(raw: Any, source: str) -> tuple[Decimal | None, str | None]:
    """Kilograms and how they were worked out (``None`` when written plainly in kg)."""
    weight = _object(raw, "weight")
    if weight is None:
        return None, None
    value = _written(weight.get("value"), "weight.value", Decimal("200000"), source)
    if value is None:
        return None, None
    unit = str(weight.get("unit") or "公斤").strip()
    pieces = _pieces(weight, "weight", source)
    kilograms = _kilograms(value * (pieces or 1), unit)
    total = _figures(kilograms)
    if pieces is not None:
        return kilograms, f"每件 {_figures(value)}{unit} × {_figures(pieces)} 件 = {total} 公斤"
    if _WEIGHT_UNITS[unit.lower()] != 1:
        return kilograms, f"{_figures(value)}{unit} = {total} 公斤"
    return kilograms, None


def _cubic(value: Decimal) -> Decimal:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return max(rounded, Decimal("0.01"))


def _read_volume(raw: Any, source: str) -> tuple[Decimal | None, str | None]:
    """Cubic metres and how they were worked out (``None`` when written plainly)."""
    volume = _object(raw, "volume")
    if volume is None:
        return None, None
    pieces = _pieces(volume, "volume", source)
    if volume.get("value") not in (None, "", "null"):
        value = _written(volume.get("value"), "volume.value", Decimal("1000"), source)
        if pieces is None:
            return value, None
        total = _cubic(value * pieces)
        if total > 1000:
            raise ValueError("体积超出合理范围")
        return total, f"每件 {_figures(value)} 立方米 × {_figures(pieces)} 件 = {_figures(total)} 立方米"
    sides = [_written(volume.get(name), f"volume.{name}", Decimal("100000"), source)
             for name in ("length", "width", "height")]
    if any(side is None for side in sides):
        if any(side is not None for side in sides):
            raise ValueError("volume 的长宽高必须写全")
        return None, None
    unit = str(volume.get("unit") or "cm").strip().lower()
    if unit not in _LENGTH_UNITS:
        raise ValueError("volume.unit 只能是 cm、mm、m")
    scale = _LENGTH_UNITS[unit]
    total = _cubic(sides[0] * sides[1] * sides[2] * scale ** 3 * (pieces or 1))
    if total > 1000:
        raise ValueError("体积超出合理范围")
    size = "×".join(_figures(side) for side in sides)
    times = f" × {_figures(pieces)} 件" if pieces is not None else ""
    return total, f"尺寸 {size}{unit}{times} = {_figures(total)} 立方米"


def _read_address(answer: Mapping[str, Any], source: str) -> str | None:
    raw_address = answer.get("address")
    if raw_address in (None, "", "null"):
        return None
    address = _PHONE.sub("", str(raw_address)).strip()
    if not 4 <= len(address) <= 150:
        raise ValueError("地址长度不合理")
    raw_parts = answer.get("address_parts")
    parts = [raw_address] if raw_parts in (None, [], "") else raw_parts
    if not isinstance(parts, list) or not 1 <= len(parts) <= 8 or not all(isinstance(part, str) for part in parts):
        raise ValueError("address_parts 必须是 1–8 个文字片段")
    flat_source = compact(source)
    for part in parts:
        piece = compact(_PHONE.sub("", part))
        if not piece or piece not in flat_source:
            raise ValueError("address_parts 的每个片段都必须是 source_text 里原样出现的文字")
    if compact("".join(_PHONE.sub("", part) for part in parts)) != compact(address):
        raise ValueError("address 必须由 address_parts 按顺序拼成")
    return address


def validate_reading(answer: Any) -> dict[str, Any]:
    """The model's reading, kept only when every piece it copied is in the text it says it read.

    The model copies; code computes. Each address piece and every number (weight,
    pieces, volume or each side) must be written in ``source_text`` (decimal points
    kept, thousands commas and Chinese numerals read as figures); the kilograms and
    cubic metres are then worked out here. 2026-11-17: the old check wanted the final
    numbers in the text, so "0.35吨", "每件175kg 共2件", dimensions only or an address
    written in fields could never be quoted.
    """
    if not isinstance(answer, Mapping):
        raise ValueError("回答必须是 JSON 对象")
    source = str(answer.get("source_text") or "").strip()
    address = _read_address(answer, source)
    weight, weight_basis = _read_weight(answer.get("weight"), source)
    volume, volume_basis = _read_volume(answer.get("volume"), source)
    raw_doubts = answer.get("doubts") or []
    if not isinstance(raw_doubts, list):
        raise ValueError("doubts 必须是列表")
    doubts = tuple(text for text in (str(item or "").strip()[:80] for item in raw_doubts) if text)[:3]
    return {"address": address, "weight_kg": None if weight is None else _figures(weight),
            "volume_m3": None if volume is None else _figures(volume),
            "basis": tuple(line for line in (weight_basis, volume_basis) if line), "doubts": doubts,
            "note": str(answer.get("note") or "")[:200]}


READ_SCREENSHOT = AITaskSpec(task_id="quote.read_screenshot", prompt_version="2026-11-17.2",
                             system_prompt=SYSTEM_PROMPT, validate=validate_reading, max_retries=1,
                             timeout_seconds=45.0)


FOLLOW_UP_INTENTS = ("quote", "address", "cancel", "other")

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
    for label, number in (("weight_value", value), ("volume_m3", volume)):
        if number is not None and not _number_in_text(number, reply):
            raise ValueError(f"{label} 的数字必须出现在 reply 里")
    weight = _kilograms(value, answer.get("weight_unit"))
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
    """A recognised address waiting for the same person: for a weight, or (``weight`` set) for "按这个报"."""
    address: str
    volume: str | None = None
    weight: str | None = None
    basis: tuple[str, ...] = ()


GO_AHEAD_TEXTS = frozenset({"按这个报", "按这个报价", "就按这个报", "就按这个报价", "按这个", "就这样报", "报吧", "可以报"})


def is_go_ahead(text: str) -> bool:
    """The reply that quotes a reading shown with doubts. Not "确认": that word belongs to plugin confirmations."""
    return re.sub(r"[\s，,。.!！~]+", "", unicodedata.normalize("NFKC", str(text or ""))) in GO_AHEAD_TEXTS


def recognised_line(address: str) -> str:
    return f"识别到的收货地址：{address}"


def _recognised(address: str, basis: tuple[str, ...] = ()) -> str:
    return "\n".join([recognised_line(address), *(f"按截图计算：{line}" for line in basis)])


def ask_weight_reply(address: str, volume: str | None, basis: tuple[str, ...] = (), doubts: tuple[str, ...] = ()) -> str:
    volume_hint = f"体积按截图 {volume} 立方米。" if volume else "体积没写会按 0.1 立方米计算，也可以一起发，例如“300公斤 2方”。"
    lines = [_recognised(address, basis)]
    if doubts:
        lines.append("拿不准的地方：" + "；".join(doubts))
    lines.append(f"请告诉我重量（公斤），例如“300公斤”；{volume_hint}")
    lines.append("地址不对的话，直接发正确的“报价 地址，重量”。")
    return "\n".join(lines)


def confirm_reply(address: str, weight: str, volume: str | None, basis: tuple[str, ...], doubts: tuple[str, ...]) -> str:
    volume_text = f"体积 {volume} 立方米" if volume else "体积没写，按 0.1 立方米"
    return "\n".join([
        _recognised(address, basis),
        "拿不准的地方：" + "；".join(doubts),
        f"回复“按这个报”就按重量 {weight} 公斤、{volume_text}报价；不对的话直接发正确的重量和体积，例如“300公斤 2方”。",
        "地址不对的话，直接发正确的“报价 地址，重量”。"])


def quoting_reply(address: str, weight: str, volume: str | None, basis: tuple[str, ...] = ()) -> str:
    volume_text = f"体积 {volume} 立方米" if volume else "体积没写，按 0.1 立方米"
    return (f"{_recognised(address, basis)}\n重量 {weight} 公斤，{volume_text}，正在报价……\n"
            "地址不对的话，直接发正确的“报价 地址，重量”。")


CANCELLED_REPLY = "好的，这张截图的报价已取消。"
NO_ADDRESS_REPLY = "截图里没有找到收货地址。可以直接发“报价 地址，重量”，例如“报价 长沙市岳麓区麓谷大道，300”。"


__all__ = ["CANCELLED_REPLY", "FOLLOW_UP_INTENTS", "MAX_IMAGE_BYTES", "NO_ADDRESS_REPLY", "PENDING_TTL_SECONDS",
           "PendingQuote", "READ_FOLLOW_UP", "READ_SCREENSHOT", "follow_up_task", "validate_follow_up",
           "ScreenshotQuoteError", "ask_weight_reply", "compact", "confirm_reply", "is_go_ahead", "numbers_text",
           "quote_address", "quote_text", "quoting_reply", "recognised_line", "validate_reading"]
