"""Check every model-proposed argument against the person's own words (agent layer P1).

The model may only *copy* values out of what the person said. Code verifies it:

* free text (an address, a reply) must be a fragment of the person's message;
* a number must appear in the message as written digits;
* a waybill number must appear in the message and pass the local format check;
* an enum value must be one of the declared options;
* anything else (booleans, objects, arrays) is never accepted from the model.

A failed check never becomes a guess: it raises :class:`GroundingError`, whose
``question`` is put to the person (the action does not run).
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

# Readable names for argument fields shown to the person when we ask back.
FIELD_LABELS = {
    "address": "收货地址",
    "weight": "重量（公斤）",
    "volume": "体积（立方米）",
    "tracking_number": "运单号",
    "waybill_number": "运单号",
    "reply": "回复内容",
    "content": "回复内容",
}
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SPACE = re.compile(r"[\s，,。．.、;；:：]+")


class GroundingError(ValueError):
    """An argument the code could not tie to the person's words."""

    def __init__(self, question: str, *, field: str = "", code: str = "ARGUMENT_NOT_GROUNDED") -> None:
        super().__init__(question)
        self.question, self.field, self.code = question, field, code


def label(field: str, schema: Mapping[str, Any] | None = None) -> str:
    title = (schema or {}).get("title")
    return str(title) if isinstance(title, str) and title.strip() else FIELD_LABELS.get(field, field)


def normalize(text: Any) -> str:
    """Full-width → half-width, lower case, punctuation and spaces removed."""
    return _SPACE.sub("", unicodedata.normalize("NFKC", str(text or ""))).lower()


def numbers_in(message: str) -> frozenset[Decimal]:
    return frozenset(Decimal(item) for item in _NUMBER.findall(unicodedata.normalize("NFKC", message)))


def ground_text(field: str, value: Any, message: str, *, schema: Mapping[str, Any] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GroundingError(f"请告诉我{label(field, schema)}。", field=field, code="ARGUMENT_MISSING")
    if normalize(value) not in normalize(message):
        raise GroundingError(f"{label(field, schema)}需要用你原话里的内容，请直接说一下{label(field, schema)}。", field=field)
    return value.strip()


def ground_number(field: str, value: Any, message: str, *, schema: Mapping[str, Any] | None = None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise GroundingError(f"请用数字告诉我{label(field, schema)}。", field=field)
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise GroundingError(f"请用数字告诉我{label(field, schema)}。", field=field) from exc
    if number not in numbers_in(message):
        raise GroundingError(f"没有在你的话里找到{label(field, schema)}的数字，请直接说一下{label(field, schema)}。", field=field)
    minimum = (schema or {}).get("minimum")
    if isinstance(minimum, (int, float)) and number < Decimal(str(minimum)):
        raise GroundingError(f"{label(field, schema)}不能小于 {minimum}。", field=field, code="ARGUMENT_INVALID")
    return int(number) if (schema or {}).get("type") == "integer" or number == number.to_integral_value() else float(number)


def ground_tracking_number(field: str, value: Any, message: str) -> str:
    from agent.tracking_number_validation import normalize_tracking_number, validate_tracking_number

    text = normalize_tracking_number(value)
    if not text or text not in unicodedata.normalize("NFKC", message).upper():
        raise GroundingError("请把完整的运单号发给我。", field=field, code="ARGUMENT_MISSING")
    checked = validate_tracking_number(text)
    if not checked.ok:
        raise GroundingError(f"运单号 {text} 的格式不对，请核对后再发。", field=field, code="ARGUMENT_INVALID")
    return text


def ground_inputs(schema: Mapping[str, Any], arguments: Mapping[str, Any], message: str) -> dict[str, Any]:
    """Validate a whole argument object against a closed JSON object schema."""
    if not isinstance(arguments, Mapping):
        raise GroundingError("没有读懂要用的条件，请再说一遍。", code="ARGUMENT_INVALID")
    properties = schema.get("properties") or {}
    unknown = set(arguments) - set(properties)
    if unknown:
        raise GroundingError("包含系统不认识的条件，本次没有执行。", code="ARGUMENT_INVALID")
    missing = [field for field in schema.get("required") or () if arguments.get(field) in (None, "")]
    if missing:
        names = "、".join(label(field, properties.get(field)) for field in missing)
        raise GroundingError(f"还需要{names}，请补充。", field=missing[0], code="ARGUMENT_MISSING")
    grounded: dict[str, Any] = {}
    for field, value in arguments.items():
        if value is None:
            continue
        spec = properties[field] if isinstance(properties[field], Mapping) else {}
        kind = spec.get("type")
        if "enum" in spec:
            if value not in spec["enum"]:
                raise GroundingError(f"{label(field, spec)}只能是：{'、'.join(map(str, spec['enum']))}。", field=field,
                                     code="ARGUMENT_INVALID")
            grounded[field] = value
        elif field in {"tracking_number", "waybill_number"}:
            grounded[field] = ground_tracking_number(field, value, message)
        elif kind == "string":
            grounded[field] = ground_text(field, value, message, schema=spec)
        elif kind in {"number", "integer"}:
            grounded[field] = ground_number(field, value, message, schema=spec)
        else:
            raise GroundingError(f"{label(field, spec)}不能由 AI 填写，本次没有执行。", field=field, code="ARGUMENT_FORBIDDEN")
    return grounded


__all__ = ["FIELD_LABELS", "GroundingError", "ground_inputs", "ground_number", "ground_text",
           "ground_tracking_number", "label", "normalize", "numbers_in"]
