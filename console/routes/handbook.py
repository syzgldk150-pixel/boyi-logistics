"""Business handbook (P2) and review request (P4) routes, under 智能模型; super administrators only."""

from __future__ import annotations

import re
from typing import Any

_ENTRY = re.compile(r"/settings/llm/handbook/entries/([^/]+)")
_ACTION = re.compile(r"/settings/llm/handbook/entries/([^/]+)/([a-z]+)")
_REVIEW = re.compile(r"/settings/llm/reviews/([^/]+)/(approve|reject)")


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/settings/llm/handbook":
        app._render_handbook(handler)
        return True
    if path == "/settings/llm/reviews":
        app._render_reviews(handler)
        return True
    if path == "/settings/llm/reviews/items":
        app._handle_reviews_list(handler)
        return True
    if path == "/settings/llm/handbook/entries":
        app._handle_handbook_get(handler, None)
        return True
    match = _ENTRY.fullmatch(path)
    if match:
        app._handle_handbook_get(handler, match.group(1))
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/settings/llm/handbook/entries":
        app._handle_handbook_post(handler, None, None)
        return True
    match = _ACTION.fullmatch(path)
    if match:
        app._handle_handbook_post(handler, match.group(1), match.group(2))
        return True
    match = _REVIEW.fullmatch(path)
    if match:
        app._handle_review_action(handler, match.group(1), match.group(2))
        return True
    return False
