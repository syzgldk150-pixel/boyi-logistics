"""Project only approved, non-credential context from an exact account binding."""

from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.ronghui_user_context import (
    RonghuiUserContextError,
    unique_ronghui_public_context,
)
from agent.tms_runtime.session_broker import get_session_broker
from shared.redaction import redact_sensitive


def _error(code):
    raise PluginExecutionError("account context is unavailable", code=code)


def _ronghui_context(session):
    try:
        result = unique_ronghui_public_context(session.cookies)
    except RonghuiUserContextError as exc:
        _error(exc.code)
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
