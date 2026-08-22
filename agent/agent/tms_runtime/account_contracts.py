"""Stable account identifiers shared by account management and task runtimes."""

PRICE_ACCOUNT_ID = "price_default"
PRICE_SESSION_PROFILE = PRICE_ACCOUNT_ID

FINANCE_ACCOUNT_ROLES: tuple[tuple[str, str], ...] = (
    ("ronghui", PRICE_ACCOUNT_ID),
    ("ronghui", "ronghui_daxiang_s"),
    ("ronghui", "ronghui_self_pickup_problem"),
    ("yunda", "yunda_default"),
)
