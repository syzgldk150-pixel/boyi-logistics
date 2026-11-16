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
_REVIEW = re.compile(r"/settings/llm/reviews/([^/]+)/(approve|reject)")


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
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
        app._handle_handbook_get(handler, None)
        return True
    match = _ENTRY.fullmatch(path)
    if match:
        app._handle_handbook_get(handler, match.group(1))
        return True
    if path == "/handbook/fee-rules":
        app._handle_fee_rules_get(handler, None)
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
