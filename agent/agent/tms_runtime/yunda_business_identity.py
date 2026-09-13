"""Fresh, non-secret Yunda organization context for bound-session readers."""
from __future__ import annotations

from typing import Any

from agent.tms_runtime.session_support import YUNDA_USER_INFO_URL


def read_yunda_business_identity(session: Any) -> dict[str, Any]:
    response = session.get(
        YUNDA_USER_INFO_URL, allow_redirects=False, timeout=15,
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    if response.status_code != 200:
        raise ValueError("YUNDA_SOURCE_CONTEXT_UNAVAILABLE")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("YUNDA_SOURCE_CONTEXT_UNVERIFIED") from exc
    details = payload.get("details") if isinstance(payload, dict) and payload.get("code") == 200 else None
    if not isinstance(details, dict):
        raise ValueError("YUNDA_SOURCE_CONTEXT_UNVERIFIED")
    context: dict[str, Any] = {}
    # Never copy the profile: it also contains authentication and personal fields.
    for key in ("orgCode", "websiteCode", "orgType"):
        value = details.get(key)
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
            raise ValueError("YUNDA_SOURCE_CONTEXT_UNVERIFIED")
        context[key] = str(value).strip()
    if type(details.get("superAdmin")) is not bool or "subPrincipal" not in details:
        raise ValueError("YUNDA_SOURCE_PERMISSION_MODE_UNVERIFIED")
    context["super_admin"] = details["superAdmin"]
    context["sub_principal"] = details["subPrincipal"] is not None
    return context
