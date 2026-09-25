"""PDF text-layer extraction in a disposable child; no network or credentials."""
from __future__ import annotations

import json
import sys


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
            "images_read": False}


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


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        # Bound malformed content in addition to the parent's wall-clock limit.
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
        result = {"data": extract_pages(sys.argv[1])}
    except Exception:
        result = {"code": "KNOWLEDGE_PDF_EXTRACTION_FAILED"}
    print(json.dumps(result, ensure_ascii=False))
