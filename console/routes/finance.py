"""Finance workspace routes."""

from __future__ import annotations

import re
from typing import Any


_GET_ACTIONS = {
    "/finance/summary": "summary",
    "/finance/trend": "trend",
    "/finance/entries": "entries",
    "/finance/fee-mappings": "fee_mappings",
    "/finance/sync-batches": "sync_batches",
    "/finance/review-cases": "review_cases",
    "/finance/waybill-facts": "waybill_facts",
    "/finance/knowledge": "knowledge",
}


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/modules/finance":
        app._render_finance(handler, query)
        return True
    action = _GET_ACTIONS.get(path)
    if action:
        app._handle_finance_get(handler, action, query)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/finance/sync":
        app._handle_finance_post(handler, "sync")
        return True
    if path == "/finance/backfill":
        app._handle_finance_post(handler, "backfill")
        return True
    if path == "/finance/reviews/analyze":
        app._handle_finance_post(handler, "analyze_reviews")
        return True
    if re.fullmatch(r"/finance/fee-mappings/\d+", path):
        app._handle_finance_post(handler, "save_mapping", path=path)
        return True
    if re.fullmatch(r"/finance/review-cases/\d+/reject", path):
        app._handle_finance_post(handler, "reject_review", path=path)
        return True
    if re.fullmatch(r"/finance/sync-batches/\d+/retry", path):
        app._handle_finance_post(handler, "retry_batch", path=path)
        return True
    return False
