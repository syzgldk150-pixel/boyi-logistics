"""Manual pages as pictures for 业务手册 → 计算规则 → 核对 (2026-11-20).

A 收费表 line cites its source by document title and page ("融辉物流网络管理手册 V6.1",
"第五篇第五章 第 50 页"). This finds the one authorized knowledge document with that title
and renders the page through the same renderer the AI assistant uses for table answers.
Nothing is guessed: no document, or more than one with the title, is a clear failure.
The document list and recent pages are kept in memory for a short while; callables are
injected by ``agent/main.py`` (this package may not import ``tools``).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from shared.handbook_fee_rules import FeeRuleError
from shared.knowledge_watch import comparable_title

_LIST_TTL = 600.0
_PAGE_CACHE = 24


class FeeSourcePages:
    def __init__(self, *, list_documents: Callable[[], list[dict[str, Any]]],
                 render_page: Callable[[str, int], bytes], clock: Callable[[], float] = time.monotonic) -> None:
        self._list_documents, self._render_page, self._clock = list_documents, render_page, clock
        self._lock = threading.Lock()
        self._documents: tuple[float, list[dict[str, Any]]] | None = None
        self._pages: OrderedDict[tuple[str, int], bytes] = OrderedDict()

    def _document_id(self, title: str) -> str:
        with self._lock:
            cached = self._documents
        if cached is None or self._clock() - cached[0] > _LIST_TTL:
            documents = self._list_documents()
            cached = (self._clock(), documents)
            with self._lock:
                self._documents = cached
        wanted = comparable_title(title)
        found = {str(item.get("obj_token") or "") for item in cached[1]
                 if comparable_title(item.get("title")) == wanted and item.get("obj_token")}
        if not found:
            raise FeeRuleError("FEE_RULE_SOURCE_PAGE_UNAVAILABLE", f"知识库里没有找到文档“{title}”，请核对出处的文档名称。")
        if len(found) > 1:
            raise FeeRuleError("FEE_RULE_SOURCE_PAGE_UNAVAILABLE", f"知识库里有多个名为“{title}”的文档，无法确定是哪一个。")
        return found.pop()

    def page(self, title: str, page: int) -> bytes:
        document_id = self._document_id(title)
        key = (document_id, page)
        with self._lock:
            if key in self._pages:
                self._pages.move_to_end(key)
                return self._pages[key]
        image = self._render_page(document_id, page)
        with self._lock:
            self._pages[key] = image
            while len(self._pages) > _PAGE_CACHE:
                self._pages.popitem(last=False)
        return image


__all__ = ["FeeSourcePages"]
