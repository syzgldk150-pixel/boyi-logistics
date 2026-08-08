"""Pure response validators used by the TMS session façade."""

from __future__ import annotations

from typing import Any


def looks_like_ronghui_login(response: Any) -> bool:
    """Return whether a Ronghui HTTP response is a login redirect or page."""

    headers = getattr(response, "headers", {}) or {}
    location = str(headers.get("Location") or headers.get("location") or "")
    if any(keyword in location for keyword in ("/system/login", "system/login")):
        return True
    try:
        body = str(getattr(response, "text", "") or "")
    except Exception:
        body = ""
    body_lower = body.lower()
    return any(
        marker in body_lower
        for marker in (
            "/system/login",
            "system/login",
            "validatecode",
            "loinform",
            "#loinform",
        )
    )
