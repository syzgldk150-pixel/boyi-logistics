"""Shared, deterministic legacy customer-problem queue policy.

The shadow projection must compare against the Console queue definition that
is actually in production.  Keeping the field aliases and site filter here
prevents the Agent collector and Console renderer from drifting.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CUSTOMER_SERVICE_RESOURCE_KEY = "customer_service.problem_settings"
CUSTOMER_SERVICE_ALLOWED_ACCOUNT_SYSTEMS = frozenset({"ronghui", "yunda"})
CUSTOMER_SERVICE_DEFAULT_SETTINGS = {
    "ronghui_account_ids": [],
    "yunda_account_ids": [],
    "poll_interval_sec": 60,
}
CUSTOMER_SERVICE_SITE_FILTER_LOGIN = "739010002"
CUSTOMER_SERVICE_SITE_FILTER_SITE = "\u90b5\u9633\u64cd\u4f5c\u573a"
CUSTOMER_SERVICE_PUBLISH_SITE_KEYS = (
    "REGISTER_SITE",
    "register_site",
    "REGISTER_SITE_NAME",
    "register_site_name",
    "site_id",
    "site_name",
    "publish_site",
    "publisher_site",
)
CUSTOMER_SERVICE_NOTIFIED_SITE_KEYS = (
    "SEND_SITE",
    "send_site",
    "SEND_SITE_NAME",
    "send_site_name",
    "recv_site_id",
    "notice_site",
    "notify_site",
    "notified_site",
    "rec_comp",
    "inform_site_name",
)


def customer_problem_clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "、".join(
            text
            for text in (customer_problem_clean_text(item) for item in value)
            if text
        )
    if isinstance(value, Mapping):
        return ""
    return str(value).strip()


def customer_problem_field(row: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    sources: list[Mapping[str, Any]] = [row]
    raw = row.get("raw")
    if isinstance(raw, Mapping):
        sources.append(raw)
    for source in sources:
        for key in keys:
            value = customer_problem_clean_text(source.get(key))
            if value:
                return value
    return ""


def legacy_customer_problem_included(
    row: Mapping[str, Any],
    *,
    account_login: str,
) -> bool:
    """Apply the exact historical Console site filter to one source row."""

    if customer_problem_clean_text(account_login) != CUSTOMER_SERVICE_SITE_FILTER_LOGIN:
        return True
    publish_site = customer_problem_field(row, CUSTOMER_SERVICE_PUBLISH_SITE_KEYS)
    notified_site = customer_problem_field(row, CUSTOMER_SERVICE_NOTIFIED_SITE_KEYS)
    return (
        publish_site == CUSTOMER_SERVICE_SITE_FILTER_SITE
        and notified_site == CUSTOMER_SERVICE_SITE_FILTER_SITE
    )
