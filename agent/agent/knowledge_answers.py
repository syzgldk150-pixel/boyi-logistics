"""Render only citations actually returned by the bounded knowledge reader."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence


def cite_knowledge(answer: str, results: Sequence[Mapping]) -> str:
    if not results:
        return answer
    failures = [item for item in results if item.get("ok") is False]
    if failures:
        return "尚未取得可核验的知识依据。" + str(failures[-1].get("message") or "知识读取失败。")
    evidence = {}
    for result in results:
        for item in result.get("items", ()):
            if not isinstance(item, Mapping) or not item.get("source_id"):
                continue
            previous = evidence.get(item["source_id"])
            if previous is not None and any(previous.get(key) != item.get(key)
                    for key in ("document_id", "updated_at", "title")):
                return "本轮检索期间知识文件发生变化，请重新查询后再核对条款。"
            merged = dict(item)
            if previous is not None and item.get("format") == "pdf":
                merged["selected_pages"] = sorted(set(previous.get("selected_pages", ())) |
                                                  set(item.get("selected_pages", ())))
            evidence[item["source_id"]] = merged
    if not evidence:
        return "本次在授权知识库内未找到匹配依据，暂不能据此判断规则。"
    urls = {item.get("url") for item in evidence.values() if item.get("url")}
    answer = re.sub(r"https?://[^\s)\]>]+", lambda match: match.group(0) if match.group(0) in urls else "（链接未核实）", answer)
    citations = []
    for item in evidence.values():
        title = str(item["title"]).replace("[", "［").replace("]", "］")
        source = f"《{title}》"
        if item.get("url"):
            source = f"[{source}]({item['url']})"
        pages = item.get("selected_pages") if item.get("format") == "pdf" else None
        page_note = "，PDF 页码 " + "、".join(map(str, pages)) if pages else ""
        citations.append(f"{source}（{item['space']}{page_note}，来源编号 {item['source_id']}）")
    limitation = ("PDF 依据限所列页的文字层；图示、扫描文字及表格版面未核验。"
                  if any(item.get("format") == "pdf" for item in evidence.values())
                  else "附件和嵌入表格未读取。")
    return answer + "\n\n依据：" + "；".join(citations) + "。" + limitation


# -- answering with the original text (业务手册 收尾第 4 步) ----------------------------------
#
# People asked that knowledge answers be the document's own words, never a
# model paraphrase. The model only points at one passage (source and the exact
# words); code checks the words are one continuous piece of the body that was
# actually read in this turn, takes that piece from the body itself, and adds one
# line naming where it comes from plus the link.

QUOTE_TOOL = "quote_knowledge"
QUOTE_MIN_CHARS = 6
QUOTE_MAX_CHARS = 1200
NO_QUOTE_REPLY = "知识库里找到了相关文档，但没有定位到能直接回答这个问题的原文段落。"
_PAGE_MARK = re.compile(r"\[PDF 第 (\d+) 页\]")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_SKIP = re.compile(r"[\s　*_`>|#]")


def page_text(body: str, page: int) -> str:
    """The text of one page inside a PDF excerpt (between its marker and the next)."""
    marks = list(_PAGE_MARK.finditer(body))
    for index, mark in enumerate(marks):
        if int(mark.group(1)) == page:
            end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
            return body[mark.end():end]
    return ""


def looks_like_table(text: str) -> bool:
    """A table read through the text layer comes out as many short lines, many with figures."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 6:
        return False
    short = sum(len(line) <= 14 for line in lines) / len(lines)
    figures = sum(bool(re.search(r"\d", line)) for line in lines) / len(lines)
    return short >= 0.5 and figures >= 0.3


@dataclass(frozen=True)
class PageImage:
    """A table page to send as a picture; ``fallback`` is sent instead if the picture cannot be."""
    document_id: str
    page: int
    caption: str
    fallback: str


class QuoteError(ValueError):
    """The proposed passage is not original text of a document read in this turn."""


def _comparable(text: str) -> tuple[str, list[int]]:
    """Text without spaces and markdown marks, with each kept character's position in the original."""
    import unicodedata

    kept, positions = [], []
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKC", char)
        if not folded or _SKIP.fullmatch(folded[0]):
            continue
        kept.append(folded.lower())
        positions.append(index)
    return "".join(kept), positions


def _evidence(results: Sequence[Mapping]) -> dict[str, Mapping]:
    items = {}
    for result in results:
        if result.get("ok") is not True:
            continue
        for item in result.get("items", ()):
            if isinstance(item, Mapping) and item.get("source_id") and isinstance(item.get("body"), str):
                items[str(item["source_id"])] = item
    return items


def _locate(body: str, quote: str) -> tuple[int, int]:
    haystack, positions = _comparable(_PAGE_MARK.sub(lambda m: " " * len(m.group(0)), body))
    needle, _ = _comparable(quote)
    if len(needle) < QUOTE_MIN_CHARS:
        raise QuoteError(f"引用太短，请复制至少 {QUOTE_MIN_CHARS} 个字的完整原文。")
    if len(needle) > QUOTE_MAX_CHARS:
        raise QuoteError(f"引用太长，请只复制回答问题的那一段（不超过 {QUOTE_MAX_CHARS} 字）。")
    start = haystack.find(needle)
    if start < 0:
        raise QuoteError("引用必须一字不差地复制正文中连续的一段，不能改写、拼接或省略。")
    if haystack.find(needle, start + 1) >= 0 and len(needle) < 20:
        raise QuoteError("这段文字在正文里出现多次，请多复制几个字，让它只对应一处。")
    return positions[start], positions[start + len(needle) - 1] + 1


def _location(item: Mapping, body: str, start: int, end: int) -> str:
    if item.get("format") == "pdf":
        before = [int(page) for page in _PAGE_MARK.findall(body[:start])]
        inside = [int(page) for page in _PAGE_MARK.findall(body[start:end])]
        pages = sorted({*(before[-1:]), *inside})
        if not pages:
            return ""
        return f"第 {pages[0]} 页" if len(pages) == 1 else f"第 {pages[0]}–{pages[-1]} 页"
    headings = _HEADING.findall(body[:start])
    return f"“{headings[-1].strip()}”部分" if headings else ""


@dataclass(frozen=True)
class Quote:
    source_id: str
    title: str
    location: str
    text: str
    url: str | None
    pages: tuple[int, ...]
    document_id: str | None = None
    table_pages: tuple[int, ...] = ()

    def _title(self) -> str:
        return self.title.replace("[", "［").replace("]", "］")

    def render(self) -> str:
        if self.table_pages:
            # A table's text layer comes out scrambled: the page picture is the original.
            lines = [f"以下是《{self._title()}》{self.location}的原文（表格页，原文见下方截图）："]
        else:
            lines = [f"以下是《{self._title()}》{self.location}的原文：", "", self.text]
        if self.url:
            lines += ["", f"全文：{self.url}"]
        return "\n".join(lines)

    def images(self) -> tuple[PageImage, ...]:
        if not self.table_pages or not self.document_id:
            return ()
        fallback = f"这一页的文字如下（表格的文字可能错位，请以原表为准）：\n\n{self.text}"
        return tuple(PageImage(self.document_id, page, f"《{self._title()}》第 {page} 页", fallback)
                     for page in self.table_pages)


def quote_knowledge(arguments: Mapping, results: Sequence[Mapping]) -> Quote:
    """Check one proposed passage against the bodies read in this turn and return it verbatim."""
    source_id, proposed = arguments.get("source_id"), arguments.get("quote")
    if not isinstance(source_id, str) or not isinstance(proposed, str):
        raise QuoteError("请给出 source_id 和要引用的原文。")
    item = _evidence(results).get(source_id)
    if item is None:
        raise QuoteError("只能引用本轮知识查询实际返回的文档。")
    body = item["body"]
    start, end = _locate(body, proposed)
    text = _PAGE_MARK.sub("", body[start:end]).strip()
    pdf = item.get("format") == "pdf"
    pages = tuple(sorted({int(page) for page in _PAGE_MARK.findall(body[:start])[-1:]}
                         | {int(page) for page in _PAGE_MARK.findall(body[start:end])})) if pdf else ()
    hinted = arguments.get("table") is True
    table_pages = tuple(page for page in pages if hinted or looks_like_table(page_text(body, page)))[:2]
    document_id = item.get("document_id") if isinstance(item.get("document_id"), str) else None
    return Quote(source_id, str(item.get("title") or ""), _location(item, body, start, end), text,
                 item.get("url") if isinstance(item.get("url"), str) else None, pages,
                 document_id if pdf else None, table_pages if pdf and document_id else ())


def unquoted_reply(results: Sequence[Mapping]) -> str:
    """When the model would only paraphrase: say so and list the documents, never the paraphrase."""
    return cite_knowledge(NO_QUOTE_REPLY, results)
