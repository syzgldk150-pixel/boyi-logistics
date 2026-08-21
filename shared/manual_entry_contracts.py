"""Closed contracts for the manually operated original-page entry surface."""

from __future__ import annotations

import re
from urllib.parse import unquote


_ENCODED_PATH_META = re.compile(r"%(?:2e|2f|5c|25)", re.IGNORECASE)


def canonical_manual_proxy_path(value: object) -> str:
    """Return one unambiguous absolute proxy path or an empty rejection.

    Upstream HTTP clients normalize dot segments before sending a request.  A
    raw prefix check would therefore authorize one path while executing a
    different one.  Reject encoded separators/dots, double-encoding,
    backslashes, control characters, network-path forms, and explicit dot
    segments before any allow-list comparison.
    """

    raw = str(value or "").strip()
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return ""
    if _ENCODED_PATH_META.search(raw):
        return ""
    try:
        decoded = unquote(raw, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return ""
    if not decoded.startswith("/") or decoded.startswith("//") or "\\" in decoded:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        return ""
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        return ""
    return decoded


YUNDA_MANUAL_ENTRY_ROUTE_ACTIONS = {
    "/ocr/yunda/bootstrap": "bootstrap",
    "/ocr/yunda/get-logistics-num": "get-logistics-num",
    "/ocr/yunda/address-analysis": "address-analysis",
    "/ocr/yunda/address-resolution": "address-resolution",
    "/ocr/yunda/quote-checks": "quote-checks",
    "/ocr/yunda/feedback/address": "feedback/address",
    "/ocr/yunda/feedback/cost": "feedback/cost",
    "/ocr/yunda/feedback/cost/upload": "feedback/cost/upload",
    "/ocr/yunda/return-upload": "return-upload",
    "/ocr/yunda/download-template": "download-template",
    "/ocr/yunda/save": "save",
    "/ocr/yunda/drafts/save": "drafts/save",
    "/ocr/yunda/drafts/list": "drafts/list",
    "/ocr/yunda/drafts/load": "drafts/load",
    "/ocr/yunda/drafts/delete": "drafts/delete",
    "/ocr/yunda/templates/save": "templates/save",
    "/ocr/yunda/templates/list": "templates/list",
    "/ocr/yunda/templates/load": "templates/load",
    "/ocr/yunda/templates/delete": "templates/delete",
    "/ocr/yunda/templates/set-default": "templates/set-default",
    "/ocr/yunda/print/child": "print/child",
    "/ocr/yunda/print/master": "print/master",
    "/ocr/yunda/print/triplicate": "print/triplicate",
    "/ocr/yunda/print/receipt-label": "print/receipt-label",
}

YUNDA_MANUAL_ENTRY_ACTIONS = frozenset(YUNDA_MANUAL_ENTRY_ROUTE_ACTIONS.values())

# The original pages were observed posting these two save endpoints.  No other
# remote write is authorized by the manual-entry exclusion.
YUNDA_MANUAL_PROXY_SAVE_PATH = "/ky_inms/public/index.php/business/waybill/entry/save.html"
RONGHUI_MANUAL_PROXY_SAVE_PATH = "/dataOperation/saveTables"

# These are the only remote path families observed and reviewed for the
# independent-origin manual-entry surface.  They are shared so Console and the
# runtime cannot drift into authorizing different paths.
YUNDA_MANUAL_PROXY_ALLOWED_PREFIXES = ("/ky_inms/public/",)
RONGHUI_MANUAL_PROXY_ALLOWED_PREFIXES = (
    "/widget/",
    "/static/",
    "/dataQuery/",
    "/dataOperation/",
    "/minic/",
    "/address/",
    "/advancePayment/",
    "/commonOption/",
    "/fhdquote/",
    "/file/",
    "/map/",
    "/userView/",
    "/unauth/download/",
    "/menuTreeExtend/",
    "/module/",
)
