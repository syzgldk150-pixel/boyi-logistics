"""PDF text-layer extraction in a disposable child; no network or credentials."""
from __future__ import annotations

import hashlib
import json
import re
import sys

MAX_OUTLINE_ENTRIES = 500


def extract_pages(path):
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("encrypted")
    if not 1 <= len(reader.pages) <= 200:
        raise ValueError("page_limit")
    pages = []
    total = 0
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if not text.strip():
            raise ValueError("missing_text_layer")
        total += len(text)
        if total > 500_000:
            raise ValueError("text_limit")
        pages.append({"page": number, "text": text})
    return {"pages": pages, "page_count": len(pages), "text_layer_complete": True,
            "images_read": False, "outline": read_outline(reader)}


def read_outline(reader):
    """Bookmarks as ``[{title, page, level}]`` in document order; empty when the PDF has none."""
    entries = []

    def walk(items, level):
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            if len(entries) >= MAX_OUTLINE_ENTRIES:
                raise ValueError("outline_limit")
            page_index = reader.get_destination_page_number(item)
            title = str(getattr(item, "title", "") or "").strip()
            if title and page_index is not None and page_index >= 0:
                entries.append({"title": title[:200], "page": page_index + 1, "level": level})

    walk(reader.outline or [], 0)
    return entries


def _normalized(text):
    return re.sub(r"\s+", "", text)


def chapter_pages(body, title):
    """Text of one bookmarked chapter: from its page to the page before the next bookmark at the
    same or a higher level (inclusive of a shared boundary page). The title must match one
    bookmark exactly, or be contained in exactly one; anything else fails rather than guessing.
    Returns (text, meta) where meta carries the page range and a fingerprint of the chapter text
    (whitespace removed), so a later edition can be compared chapter by chapter.
    """
    outline, pages = body.get("outline"), body.get("pages")
    if not isinstance(outline, list) or not outline:
        raise ValueError("PDF 没有章节书签，不能按章读取。")
    exact = [i for i, item in enumerate(outline) if item["title"] == title]
    matches = exact or [i for i, item in enumerate(outline) if title in item["title"]]
    if len(matches) != 1:
        raise ValueError("没有找到这个章节。" if not matches else "章节名称对应多个书签，请写得更完整。")
    index = matches[0]
    start, level = outline[index]["page"], outline[index]["level"]
    end = body.get("page_count") or len(pages)
    for item in outline[index + 1:]:
        if item["level"] <= level:
            end = max(start, item["page"])  # the next chapter may begin mid-page
            break
    selected = [page for page in pages if start <= page["page"] <= end]
    text = "\n\n".join(f"[PDF 第 {page['page']} 页]\n{page['text']}" for page in selected)
    fingerprint = hashlib.sha256(_normalized("".join(page["text"] for page in selected)).encode("utf-8")).hexdigest()
    return text, {"title": outline[index]["title"], "start_page": start, "end_page": end,
                  "fingerprint": fingerprint, "images_read": False,
                  "scope_note": "按书签取整章文字层；表格单元格可能被拆散，表格版面未经识别。"}


def select_pages(body, query):
    """Literal search of every page; return complete hit pages plus neighbours."""
    pages, count = body.get("pages"), body.get("page_count")
    if (body.get("text_layer_complete") is not True or not isinstance(pages, list)
            or type(count) is not int or count != len(pages) or not 1 <= count <= 200):
        raise ValueError("PDF 页数或文字层完整性未核实。")
    for number, page in enumerate(pages, 1):
        if not isinstance(page, dict) or page.get("page") != number or not isinstance(page.get("text"), str) or not page["text"].strip():
            raise ValueError("PDF 页码不连续或正文为空。")
    hits = [i for i, page in enumerate(pages) if query.casefold() in page["text"].casefold()]
    selected = sorted({j for i in hits for j in (i-1, i, i+1) if 0 <= j < count})
    excerpt = "\n\n".join(f"[PDF 第 {i+1} 页]\n{pages[i]['text']}" for i in selected)
    if len(excerpt) > 12000:
        raise ValueError("该主题命中的 PDF 页数较多，请改用更具体的主题；未截断正文。")
    return excerpt, {"format": "pdf", "page_count": count,
                     "selected_pages": [i+1 for i in selected], "matched_pages": [i+1 for i in hits],
                     "text_layer_complete": True, "images_read": False, "excerpt_only": True,
                     "scope_note": "已检索全部页文字层；返回命中页及相邻页。图示、扫描文字及表格版面未经识别。"}


MAX_PAGE_IMAGE_BYTES = 1_300_000  # base64 + JSON stays under the parent 2 MB output cap


def render_page(path, page):
    """One page as a PNG for a table answer (业务手册 收尾第 4b 步); smaller scale if the picture is large."""
    import base64
    import io

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(path)
    try:
        if not 1 <= len(document) <= 200 or not 1 <= page <= len(document):
            raise ValueError("page_out_of_range")
        for scale in (2.0, 1.5, 1.2):
            image = document[page - 1].render(scale=scale).to_pil().convert("L")
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            if len(output.getvalue()) <= MAX_PAGE_IMAGE_BYTES:
                return {"page": page, "width": image.width, "height": image.height,
                        "png_base64": base64.b64encode(output.getvalue()).decode("ascii")}
        raise ValueError("image_limit")
    finally:
        document.close()


MAX_BATCH_IMAGE_BYTES = 1_400_000  # all pages together, base64 + JSON under the parent 2 MB output cap
MAX_BATCH_PAGES = 4


def render_pages(path, pages):
    """Several pages as smaller grey PNGs for an AI draft (业务手册 → 计算规则 → 从手册起草, 2026-11-20).

    Tables come out of the text layer scrambled, so the model also sees the pages. One download
    serves the whole batch; the scale drops until every page fits the shared output cap.
    """
    import base64
    import io

    import pypdfium2 as pdfium

    if not 1 <= len(pages) <= MAX_BATCH_PAGES or len(set(pages)) != len(pages):
        raise ValueError("page_batch")
    document = pdfium.PdfDocument(path)
    try:
        if not 1 <= len(document) <= 200 or any(not 1 <= page <= len(document) for page in pages):
            raise ValueError("page_out_of_range")
        for scale in (1.4, 1.1, 0.9):
            images, total = [], 0
            for page in pages:
                image = document[page - 1].render(scale=scale).to_pil().convert("L")
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=True)
                encoded = base64.b64encode(output.getvalue()).decode("ascii")
                total += len(encoded)
                images.append({"page": page, "width": image.width, "height": image.height, "png_base64": encoded})
            if total <= MAX_BATCH_IMAGE_BYTES:
                return {"pages": images}
        raise ValueError("image_limit")
    finally:
        document.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        # Bound malformed content in addition to the parent's wall-clock limit.
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
        if len(sys.argv) == 4 and sys.argv[1] == "--render":
            result = {"data": render_page(sys.argv[3], int(sys.argv[2]))}
        elif len(sys.argv) == 4 and sys.argv[1] == "--render-batch":
            result = {"data": render_pages(sys.argv[3], [int(item) for item in sys.argv[2].split(",")])}
        else:
            result = {"data": extract_pages(sys.argv[1])}
    except Exception:
        result = {"code": "KNOWLEDGE_PDF_EXTRACTION_FAILED"}
    print(json.dumps(result, ensure_ascii=False))
