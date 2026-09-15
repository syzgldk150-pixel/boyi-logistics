"""Data-only daily-sign timestamp and scalar normalization; no due-date policy."""
from __future__ import annotations
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
TARGET_STATION = "邵阳大祥S站"

def business_now() -> datetime:
    """Return a naive MySQL-compatible timestamp in the business timezone."""

    return datetime.now(BUSINESS_TIMEZONE).replace(tzinfo=None)


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def to_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(BUSINESS_TIMEZONE).replace(tzinfo=None)
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is not None:
            return parsed.astimezone(BUSINESS_TIMEZONE).replace(tzinfo=None)
        return parsed.replace(tzinfo=None)
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: Any) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def end_of_day(value: date) -> datetime:
    return datetime.combine(value, time(23, 59, 59))
