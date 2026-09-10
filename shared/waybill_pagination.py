"""Strict collection of existing page-number protocols; no invented cursors."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping


def authoritative_total(value: Any) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError("WAYBILL_PAGE_TOTAL_MISSING")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("WAYBILL_PAGE_TOTAL_INVALID") from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("WAYBILL_PAGE_TOTAL_INVALID")
    return int(number)


def collect_complete_pages(fetch: Callable[[int], Any], *, rows_from: Callable[[Any], Any],
                           total_from: Callable[[Any], Any], identity: str,
                           first_page: int, page_size: int, max_pages: int) -> tuple[list[dict[str, Any]], int]:
    if page_size <= 0 or max_pages <= 0:
        raise ValueError("WAYBILL_PAGE_LIMIT_INVALID")
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    total: int | None = None
    for page in range(first_page, first_page + max_pages):
        payload = fetch(page)
        page_total = authoritative_total(total_from(payload))
        if total is not None and page_total != total:
            raise ValueError("WAYBILL_SOURCE_CHANGED_DURING_PAGINATION")
        total = page_total
        rows = rows_from(payload)
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise ValueError("WAYBILL_PAGE_ROWS_INVALID")
        if len(rows) > page_size:
            raise ValueError("WAYBILL_PAGE_SIZE_EXCEEDED")
        for row in rows:
            entity = str(row.get(identity) or "").strip()
            if not entity or entity in seen:
                raise ValueError("WAYBILL_PAGE_IDENTITY_MISSING_OR_DUPLICATED")
            seen.add(entity)
            collected.append(dict(row))
        if len(collected) > total:
            raise ValueError("WAYBILL_PAGE_COUNT_EXCEEDED")
        if len(collected) == total:
            return collected, total
        if len(rows) < page_size:
            raise ValueError("WAYBILL_PAGINATION_INCOMPLETE")
    raise ValueError("WAYBILL_PAGINATION_LIMIT_REACHED")
