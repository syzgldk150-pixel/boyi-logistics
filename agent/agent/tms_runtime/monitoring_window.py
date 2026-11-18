"""首页消息中心的显示时间范围（用户确认 2026-11-18）。

整个消息中心只显示最近 3 天（今天、昨天、前天）内有动态的未完成记录；
动态时间取登记/发布、催单、回复、处理等原系统时间中最新的一个。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

RECENT_DAYS = 3
RECENT_NOTE = "只显示最近3天（今天、昨天、前天）有新登记、催单、回复或处理的未完成记录。"


def today() -> dt.date:
    """Single clock for the window; tests replace this function."""
    return dt.date.today()


def cutoff_date(current: dt.date | None = None) -> dt.date:
    return (current or today()) - dt.timedelta(days=RECENT_DAYS - 1)


def _date(value: Any) -> dt.date | None:
    text = str(value or "").strip()[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def latest_activity(values: Iterable[Any]) -> str:
    """Return the latest parseable original timestamp text, or an empty string."""
    dated = [str(value).strip() for value in values if _date(value) is not None]
    return max(dated) if dated else ""


def is_recent(values: Iterable[Any]) -> bool:
    """A record without any readable time stays visible: it cannot be proven stale."""
    dates = [day for day in (_date(value) for value in values) if day is not None]
    return not dates or max(dates) >= cutoff_date()
