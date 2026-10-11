"""Project only approved, non-credential context from an exact account binding."""

import time

from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.ronghui_user_context import (
    RONGHUI_PUBLIC_CONTEXT_FIELDS,
    RONGHUI_USER_INFO_COOKIE,
    parse_ronghui_user_info_cookie,
)
from agent.tms_runtime.session_broker import get_session_broker
from shared.redaction import redact_sensitive


def _error(code):
    raise PluginExecutionError("account context is unavailable", code=code)


def _ronghui_context(session):
    projections = set()
    host = "tms.ronghuiwl.com"
    for cookie in session.cookies:
        if cookie.name != RONGHUI_USER_INFO_COOKIE:
            continue
        domain = str(cookie.domain or "").lower().lstrip(".")
        if not domain or not (host == domain or host.endswith("." + domain)):
            continue
        # The page reads a root-scoped browser cookie. Ambiguous or incomplete
        # saved identities must not silently select an arbitrary employee.
        if cookie.path != "/" or cookie.is_expired(time.time()):
            _error("CONTEXT_DATA_MISSING")
        payload = parse_ronghui_user_info_cookie(cookie.value)
        values = tuple(payload.get(field) for field in RONGHUI_PUBLIC_CONTEXT_FIELDS)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 191 for value in values):
            _error("CONTEXT_DATA_MISSING")
        projections.add(values)
    if not projections:
        _error("CONTEXT_DATA_MISSING")
    if len(projections) != 1:
        _error("CONTEXT_DATA_AMBIGUOUS")
    result = dict(zip(RONGHUI_PUBLIC_CONTEXT_FIELDS, next(iter(projections))))
    if redact_sensitive(result) != result:
        _error("CONTEXT_DATA_INVALID")
    return result


def account_context_read(context, arguments, manifest):
    roles = [item for item in manifest.account_roles if item["role"] == context.role]
    if context.action != "read" or arguments or len(context.account_ids) != 1 or len(roles) != 1:
        _error("CONTEXT_BINDING_MISMATCH")
    systems = tuple(roles[0]["allowed_systems"])
    if systems != ("ronghui",):
        _error("CONTEXT_ACCOUNT_UNSUPPORTED")
    try:
        binding = get_account_manager().require_active_binding_descriptor(context.account_ids[0])
        if binding["system"] != systems[0] or not binding["session_profile"]:
            _error("CONTEXT_BINDING_MISMATCH")
        session = get_session_broker(binding["session_profile"]).build_requests_session(validate=False)
    except TMSAuthStateError:
        _error("BLOCKED_LOGIN")
    try:
        return _ronghui_context(session)
    finally:
        session.close()
