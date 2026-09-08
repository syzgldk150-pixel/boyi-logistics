"""Stable account identifiers shared by account management and task runtimes."""

from agent.tms_runtime.errors import TMSAuthStateError


def require_session_profile(params: dict) -> str:
    """Require the explicit account profile resolved by the dispatcher."""
    value = params.get("session_profile")
    if not isinstance(value, str) or not value.strip():
        raise TMSAuthStateError(
            "ACCOUNT_SESSION_PROFILE_REQUIRED",
            "原页未获取业务账号会话，请检查默认账号设置。",
        )
    return value.strip()


PRICE_ACCOUNT_ID = "price_default"
PRICE_SESSION_PROFILE = PRICE_ACCOUNT_ID

FINANCE_ACCOUNT_ROLES: tuple[tuple[str, str], ...] = (
    ("ronghui", PRICE_ACCOUNT_ID),
    ("ronghui", "ronghui_daxiang_s"),
    ("ronghui", "ronghui_self_pickup_problem"),
    ("yunda", "yunda_default"),
)
