"""业务手册 module routes (robot agent layer) and the 复核请求 page that stays under 智能模型.

Access comes from ``shared.identity_routes``: the 业务手册 module grants viewing
and editing; approve, resume and retire need a super administrator. The old
``/settings/llm/handbook`` address only redirects to the module.
"""

from __future__ import annotations

import re
from typing import Any

_ENTRY = re.compile(r"/handbook/entries/([^/]+)")
_ACTION = re.compile(r"/handbook/entries/([^/]+)/([a-z]+)")
_FEE_RULE = re.compile(r"/handbook/fee-rules/([^/]+)")
_FEE_ACTION = re.compile(r"/handbook/fee-rules/([^/]+)/([a-z]+)")
_DRAFT_JOB = re.compile(r"/handbook/fee-drafts/jobs/([^/]+)")
_DRAFT_SAVE = re.compile(r"/handbook/fee-drafts/jobs/([^/]+)/save")
_SOURCE_CHECK = re.compile(r"/handbook/sources/reminders/(\d{1,18})/check")
_REVIEW = re.compile(r"/settings/llm/reviews/([^/]+)/(approve|reject)")


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/handbook":
        app._render_handbook(handler)
        return True
    if path == "/settings/llm/handbook":
        app._redirect(handler, "/handbook")
        return True
    if path == "/settings/llm/reviews":
        app._render_reviews(handler)
        return True
    if path == "/settings/llm/reviews/items":
        app._handle_reviews_list(handler)
        return True
    if path == "/handbook/entries":
        app._handle_handbook_get(handler, None, query)
        return True
    match = _ENTRY.fullmatch(path)
    if match:
        app._handle_handbook_get(handler, match.group(1))
        return True
    if path == "/handbook/fee-rules":
        app._handle_fee_rules_get(handler, None)
        return True
    if path == "/handbook/fee-rules/source-page":
        app._handle_fee_rules_source_page(handler, query)
        return True
    if path == "/handbook/fee-drafts/documents":
        app._handle_fee_drafts(handler, "documents")
        return True
    if path == "/handbook/fee-drafts/outline":
        app._handle_fee_drafts(handler, "outline", query=query)
        return True
    match = _DRAFT_JOB.fullmatch(path)
    if match:
        app._handle_fee_drafts(handler, "job", match.group(1))
        return True
    if path == "/handbook/quote/catalog":
        app._handle_fee_quote(handler, "catalog", query)
        return True
    if path == "/handbook/quote/recent":
        app._handle_fee_quote(handler, "recent")
        return True
    if path == "/handbook/sources":
        app._handle_knowledge_sources(handler, "view", None)
        return True
    match = _FEE_RULE.fullmatch(path)
    if match:
        app._handle_fee_rules_get(handler, match.group(1))
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/handbook/try":
        app._handle_handbook_try(handler)
        return True
    if path == "/handbook/fee-rules":
        app._handle_fee_rules_create(handler)
        return True
    if path == "/handbook/fee-rules/effective-dates":
        app._handle_fee_rules_effective_dates(handler)
        return True
    if path == "/handbook/fee-drafts/jobs":
        app._handle_fee_drafts(handler, "start")
        return True
    match = _DRAFT_SAVE.fullmatch(path)
    if match:
        app._handle_fee_drafts(handler, "save", match.group(1))
        return True
    if path == "/handbook/quote":
        app._handle_fee_quote(handler, "quote")
        return True
    if path == "/handbook/quote/waybill":
        app._handle_fee_quote(handler, "waybill")
        return True
    if path == "/handbook/sources/scan":
        app._handle_knowledge_sources(handler, "scan", None)
        return True
    if path == "/handbook/sources/reminders/check-all":
        app._handle_knowledge_sources(handler, "check_all", None)
        return True
    match = _SOURCE_CHECK.fullmatch(path)
    if match:
        app._handle_knowledge_sources(handler, "check", int(match.group(1)))
        return True
    if path == "/handbook/entries":
        app._handle_handbook_post(handler, None, None)
        return True
    match = _ACTION.fullmatch(path)
    if match:
        app._handle_handbook_post(handler, match.group(1), match.group(2))
        return True
    match = _FEE_ACTION.fullmatch(path)
    if match:
        app._handle_fee_rules_post(handler, match.group(1), match.group(2))
        return True
    match = _REVIEW.fullmatch(path)
    if match:
        app._handle_review_action(handler, match.group(1), match.group(2))
        return True
    return False
