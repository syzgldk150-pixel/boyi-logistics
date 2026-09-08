"""Closed contracts for the manually operated original-page entry surface."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode


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

# Observed original-entry initialization calls. These shared endpoints also
# dispatch other operations, so neither their path prefix nor FIND_* is a read
# permission. Parameter names are the observed filters for each exact selector.
_RONGHUI_INITIALIZATION_FIELDS = {
    "FIND_SYS_DATE": frozenset(),
    "FIND_SITE_INFO_BY_SITE_CODE": frozenset({"SITE_CODE"}),
    "FIND_SITE_AND_CENTER": frozenset({"SITE_CODE"}),
    "FIND_TAB_SITE_BY_AGENT": frozenset({"SITE_CODE"}),
    "FIND_TAB_QUOTE_SWITCH_SITE": frozenset({"SITE_CODE"}),
    "FIND_TMS_SYS_SHARE_SET": frozenset({"SHARE_CODE_IN"}),
    "FIND_TAB_SITE_BUSINESS_TYPE": frozenset({"SITE_CODE"}),
    "FIND_BILL_CHECK": frozenset({"CREATE_MAN_CODE"}),
    "FIND_TAB_COLLAR_CURRENT_SITE": frozenset({"BELONG_SITE_CODE", "COLLAR_STATUS"}),
}
_YUNDA_EMPTY_INITIALIZATION_PATHS = frozenset({
    "/ky_inms/public/index.php/elecStock.html",
    "/ky_inms/public/index.php/getCostInfoPrompt.html",
})
_YUNDA_TEMPLATE_LIST_PATH = (
    "/ky_inms/public/index.php/business/waybill/entry/getTemplateList.html"
)
_YUNDA_TEMPLATE_FIELDS = frozenset({"CreatedDotCode", "IsNew", "queryType"})


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in pairs:
        if key in fields or not isinstance(key, str) or isinstance(value, (dict, list)):
            raise ValueError("ambiguous proxy parameter")
        fields[key] = value
    return fields


def _initialization_fields(params: Mapping[str, Any]) -> dict[str, Any]:
    """Inspect the same body/header representation the proxy will transmit."""
    headers = params.get("headers")
    content_types = [
        str(value).strip()
        for key, value in headers.items()
        if str(key or "").strip().lower() == "content-type"
    ] if isinstance(headers, dict) else []
    if len(content_types) > 1:
        raise ValueError("duplicate content type")
    declared_type = str(params.get("content_type") or "").strip()
    if content_types and declared_type and content_types[0].lower() != declared_type.lower():
        raise ValueError("conflicting content type")
    content_type = content_types[0] if content_types else declared_type

    # Both existing runtime adapters prefer non-empty base64 to the text body.
    # Reject conflicting representations rather than checking the unused one.
    raw_base64 = str(params.get("body_base64") or "").strip()
    text_body = params.get("body")
    body = str(text_body).encode("utf-8") if text_body is not None else b""
    if raw_base64:
        decoded = base64.b64decode(raw_base64, validate=True)
        if text_body is not None and body != decoded:
            raise ValueError("conflicting body representation")
        body = decoded

    query_value = params.get("query")
    if isinstance(query_value, dict):
        query = urlencode([
            (str(key), str(value)) for key, value in query_value.items() if value is not None
        ], doseq=True)
    else:
        query = str(query_value or "").strip().lstrip("?")
    pairs = parse_qsl(query, keep_blank_values=True, errors="strict")
    if body:
        media_type, *parameters = content_type.lower().split(";")
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if name == "charset" and (not separator or value.strip('" ') not in {"utf-8", "utf8"}):
                raise ValueError("unsupported request charset")
        text_body = body.decode("utf-8")
        if media_type.strip() == "application/x-www-form-urlencoded":
            pairs.extend(parse_qsl(text_body, keep_blank_values=True, errors="strict"))
        elif media_type.strip() == "application/json":
            fields = json.loads(text_body, object_pairs_hook=_unique_fields)
            if not isinstance(fields, dict):
                raise ValueError("proxy JSON must be an object")
            pairs.extend(fields.items())
        else:
            raise ValueError("unsupported request body")
    return _unique_fields(pairs)


def manual_proxy_request_allowed(provider: str, params: Mapping[str, Any]) -> bool:
    """Authorize the closed request contract; caller verifies origin/principal.

    Pass the final transport params, including headers and body_base64. Parsing
    failures, duplicate selectors and query/body conflicts never grant a read.
    Existing GET paths and the two explicitly reviewed manual saves are retained.
    """
    if provider not in {"ronghui", "yunda"}:
        return False
    raw_path = str(params.get("path") or "").strip()
    path = canonical_manual_proxy_path(raw_path) if raw_path else ""
    if raw_path and not path:
        return False
    method = str(params.get("method") or "GET").strip().upper()
    if method == "GET":
        if provider == "yunda":
            return path.startswith(YUNDA_MANUAL_PROXY_ALLOWED_PREFIXES)
        return not path or path.startswith(RONGHUI_MANUAL_PROXY_ALLOWED_PREFIXES)
    if method != "POST":
        return False
    save_path = (
        YUNDA_MANUAL_PROXY_SAVE_PATH if provider == "yunda" else RONGHUI_MANUAL_PROXY_SAVE_PATH
    )
    if path == save_path:
        return True
    try:
        fields = _initialization_fields(params)
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        return False
    if provider == "yunda":
        if path in _YUNDA_EMPTY_INITIALIZATION_PATHS:
            return not fields and not params.get("body") and not params.get("body_base64")
        return path == _YUNDA_TEMPLATE_LIST_PATH and fields.keys() == _YUNDA_TEMPLATE_FIELDS
    if path == "/minic/combobox":
        return fields == {"optionCode": "WEIGHT_RATIO"}
    if path != "/dataQuery/findAllByCallId":
        return False
    selector = fields.get("id")
    if not isinstance(selector, str) or selector not in _RONGHUI_INITIALIZATION_FIELDS:
        return False
    return fields.keys() == _RONGHUI_INITIALIZATION_FIELDS[selector] | {"id"}
