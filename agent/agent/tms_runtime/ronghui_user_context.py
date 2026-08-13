"""Shared parsing and validation for Ronghui's browser-side user context."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import unquote


RONGHUI_USER_INFO_COOKIE = "userInfo"
RONGHUI_USER_CONTEXT_FIELDS = (
    "loginUserName",
    "loginUserAccount",
    "loginSiteName",
    "loginSiteCode",
)


def decode_js_cookie_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    def replace_unicode_escape(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)

    return unquote(re.sub(r"%u([0-9A-Fa-f]{4})", replace_unicode_escape, text))


def parse_ronghui_user_info_cookie(value: Any) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    for candidate in (raw, decode_js_cookie_value(raw)):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def ronghui_user_context_signature(value: Any) -> tuple[str, ...] | None:
    payload = value if isinstance(value, dict) else parse_ronghui_user_info_cookie(value)
    if not isinstance(payload, dict):
        return None
    signature = tuple(str(payload.get(field) or "").strip() for field in RONGHUI_USER_CONTEXT_FIELDS)
    return signature if all(signature) else None


def normalize_ronghui_user_info_storage_state(
    storage_state: dict[str, Any],
    *,
    host: str,
) -> tuple[bool, str]:
    """Make the client cookie JS-readable and report whether its identity is usable.

    Returns ``(changed, status)``. Status is structural only and never contains
    user or cookie values.
    """

    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list):
        return False, "missing"

    normalized_host = str(host or "").strip().lower().lstrip(".")
    changed = False
    matched = 0
    signatures: set[tuple[str, ...]] = set()
    incomplete = False

    for cookie in cookies:
        if not isinstance(cookie, dict) or str(cookie.get("name") or "") != RONGHUI_USER_INFO_COOKIE:
            continue
        domain = str(cookie.get("domain") or normalized_host).strip().lower().lstrip(".")
        if normalized_host and domain and not (
            normalized_host == domain or normalized_host.endswith(f".{domain}")
        ):
            continue
        matched += 1
        if cookie.get("httpOnly") is not False:
            cookie["httpOnly"] = False
            changed = True
        path = str(cookie.get("path") or "/").strip() or "/"
        expires = cookie.get("expires")
        if path != "/":
            incomplete = True
            continue
        if expires not in (None, "", -1, "-1"):
            try:
                if float(expires) <= time.time():
                    incomplete = True
                    continue
            except (TypeError, ValueError):
                incomplete = True
                continue
        signature = ronghui_user_context_signature(cookie.get("value"))
        if signature is None:
            incomplete = True
        else:
            signatures.add(signature)

    if matched == 0:
        return changed, "missing"
    if incomplete:
        return changed, "incomplete"
    if len(signatures) != 1:
        return changed, "conflicting"
    return changed, "ready"


__all__ = [
    "RONGHUI_USER_CONTEXT_FIELDS",
    "RONGHUI_USER_INFO_COOKIE",
    "decode_js_cookie_value",
    "normalize_ronghui_user_info_storage_state",
    "parse_ronghui_user_info_cookie",
    "ronghui_user_context_signature",
]
