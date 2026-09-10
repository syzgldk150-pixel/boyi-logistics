"""Observe the authenticated provider's current query profile, not all organisations.

Only reviewed business context is retained. Account credentials remain inside
the session broker. A profile describes the existing collector's exact query
mode and effective upstream business context; it is not an organisation-wide
permission certificate.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Mapping

from shared.automation_project_authorization import canonical_sha256
from shared.waybill_source_coverage import WaybillSourceScope, native_waybill_source_scope


class _BusinessInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.site_codes: list[str] = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and fields.get("id") == "loginSiteCode":
            self.site_codes.append(str(fields.get("value") or "").strip())


def _response_body(response: Any) -> str:
    if response.status_code != 200:
        raise ValueError("WAYBILL_SOURCE_CONTEXT_UNAVAILABLE")
    body = str(response.text or "")
    if not body.strip() or re.search(r"<input\b[^>]*type=[\"']password[\"']", body, re.I):
        raise ValueError("WAYBILL_SOURCE_LOGIN_REQUIRED")
    return body


def _literal(html: str, name: str) -> str:
    # The native page embeds its editor and repeats an identical share-mode
    # declaration. $user_type is the second item in a comma-separated var.
    # Accept only one distinct literal; conflicting copies are schema drift.
    pattern = r"(?<![\w$])" + re.escape(name) + r"\s*=(?!=)\s*(['\"])([^'\"\r\n]*)\1\s*[,;]"
    values = [match[1] for match in re.findall(pattern, html)]
    if len(set(values)) != 1:
        raise ValueError("WAYBILL_SOURCE_PERMISSION_MODE_UNVERIFIED")
    return values[0]


def _nonempty(mapping: Mapping[str, Any], name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        raise ValueError("WAYBILL_SOURCE_CONTEXT_UNVERIFIED")
    return str(value).strip()


def observe_query_scope(source: str, account_id: str, session: Any) -> WaybillSourceScope:
    """Read fresh upstream context using the already bound authenticated session."""
    if source == "ronghui":
        from agent.tms_runtime.scripts.Send_order import CALL_ID, build_payload
        response = session.get("https://tms.ronghuiwl.com/module/index?mv=index",
                               allow_redirects=False, timeout=15)
        parser = _BusinessInputs()
        parser.feed(_response_body(response))
        if len(parser.site_codes) != 1 or not parser.site_codes[0]:
            raise ValueError("WAYBILL_SOURCE_CONTEXT_UNVERIFIED")
        # Date and pagination change between requests, while business filters do not.
        filters = build_payload({"start": "", "end": ""})
        for key in ("SEARCH_DATE_RANGE1", "SEARCH_DATE_RANGE2", "SEARCH_DATE_RANGE", "REGISTER_DATE",
                    "pageIndex", "pageSize", "totalColumns"):
            filters.pop(key, None)
        context = {"login_site_code": parser.site_codes[0], "endpoint": CALL_ID, "filters": filters}
    elif source == "yunda":
        from agent.tms_runtime.scripts.yunda_send_waybills import SEND_INDEX_URL, _send_list_form, _special_line_list_form
        from agent.tms_runtime.session_support import YUNDA_USER_INFO_URL
        from datetime import date
        response = session.get(YUNDA_USER_INFO_URL, allow_redirects=False, timeout=15,
                               headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        _response_body(response)
        payload = response.json()
        details = payload.get("details") if isinstance(payload, dict) and payload.get("code") == 200 else None
        if not isinstance(details, dict):
            raise ValueError("WAYBILL_SOURCE_CONTEXT_UNVERIFIED")
        context = {key: _nonempty(details, key) for key in ("orgCode", "websiteCode", "orgType")}
        if type(details.get("superAdmin")) is not bool or "subPrincipal" not in details:
            raise ValueError("WAYBILL_SOURCE_PERMISSION_MODE_UNVERIFIED")
        # Never retain profile/user names, phone numbers or authentication fields.
        context["super_admin"] = details["superAdmin"]
        context["sub_principal"] = details["subPrincipal"] is not None
        body = _response_body(session.get(SEND_INDEX_URL, allow_redirects=False, timeout=15))
        context.update({"share_mode": _literal(body, "is_share_user"),
                        "user_type": _literal(body, "$user_type"),
                        "creator_restriction": canonical_sha256(_literal(body, "$Created_By_Code"))})
        filters = []
        for build in (_send_list_form, _special_line_list_form):
            form = build(date(2000, 1, 1), page=1, rows=1)
            for key in ("start_date", "start_time", "end_date", "end_time", "page", "rows"):
                form.pop(key, None)
            filters.append(form)
        context["filters"] = filters
    else:
        raise ValueError("WAYBILL_SOURCE_UNSUPPORTED")
    # Platform waybill numbers are the lookup identities used by each native
    # exact-detail endpoint. A new account or query profile reuses those entities.
    return WaybillSourceScope(source, native_waybill_source_scope(source), "query-v1:" + canonical_sha256(context), account_id)


def source_session(descriptor: Mapping[str, Any]):
    from agent.tms_runtime.session_broker import get_session_broker
    if descriptor.get("system") not in {"ronghui", "yunda"} or not descriptor.get("session_profile"):
        raise ValueError("WAYBILL_ACCOUNT_BINDING_MISMATCH")
    return get_session_broker(str(descriptor["session_profile"])).build_requests_session(validate=False)
