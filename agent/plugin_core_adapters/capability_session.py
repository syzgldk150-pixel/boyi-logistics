"""One target-scoped online session check for privileged core actions."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker


CapabilityAuthorizer = Callable[[Mapping[str, Any], str], None]


def authorize_target_capability(
    descriptor: Mapping[str, Any],
    capability: str,
) -> None:
    """Validate exactly one bound target capability without exposing a session.

    Generic broker admission remains local-only.  Privileged handlers call this
    function after validating their closed arguments and immediately before the
    durable write-start boundary (or before a live finance capture).
    """

    profile = str(descriptor.get("session_profile") or "").strip()
    if not profile:
        raise PluginExecutionError(
            "the exact target account has no session profile",
            code="BLOCKED_LOGIN",
        )
    try:
        session = get_session_broker(profile).open_capability_session(capability)
    except TMSAuthStateError as exc:
        code = (
            "BLOCKED_LOGIN"
            if exc.code in {"AUTH_REQUIRED", "BLOCKED_LOGIN"}
            else str(exc.code or "CAPABILITY_UNAVAILABLE")
        )
        raise PluginExecutionError(
            "the exact target capability is unavailable",
            code=code,
        ) from exc
    session.close()


__all__ = ["CapabilityAuthorizer", "authorize_target_capability"]
