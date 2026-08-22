"""Pure production-readiness contract for finance source accounts.

This module intentionally contains no environment access or runtime imports.
Every finance consumer must derive enabled platforms and account roles from
this contract instead of treating a generic automation account as a live
finance source.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FinanceSourceSpec:
    """One account role that may eventually provide finance transactions."""

    platform: str
    platform_label: str
    account_id: str
    account_label: str
    production_ready: bool
    status: str
    reason: str


FINANCE_SOURCE_SPECS: tuple[FinanceSourceSpec, ...] = (
    FinanceSourceSpec(
        platform="ronghui",
        platform_label="融辉",
        account_id="price_default",
        account_label="大祥报价",
        production_ready=True,
        status="enabled",
        reason="融辉真实财务页面采集已上线",
    ),
    FinanceSourceSpec(
        platform="ronghui",
        platform_label="融辉",
        account_id="ronghui_daxiang_s",
        account_label="大祥S站",
        production_ready=True,
        status="enabled",
        reason="融辉真实财务页面采集已上线",
    ),
    FinanceSourceSpec(
        platform="ronghui",
        platform_label="融辉",
        account_id="ronghui_self_pickup_problem",
        account_label="自提部",
        production_ready=True,
        status="enabled",
        reason="融辉真实财务页面采集已上线",
    ),
    FinanceSourceSpec(
        platform="yunda",
        platform_label="韵达",
        account_id="yunda_default",
        account_label="韵达默认账号",
        production_ready=False,
        status="not_launched",
        reason="韵达财务真实页面采集尚未上线",
    ),
)


def enabled_finance_source_specs() -> tuple[FinanceSourceSpec, ...]:
    """Return production-ready source roles in stable declaration order."""

    return tuple(spec for spec in FINANCE_SOURCE_SPECS if spec.production_ready)


def enabled_finance_platforms() -> tuple[str, ...]:
    """Return production-ready platforms once each, preserving stable order."""

    return tuple(dict.fromkeys(spec.platform for spec in enabled_finance_source_specs()))


def enabled_finance_account_ids() -> tuple[str, ...]:
    """Return production-ready account role IDs in stable declaration order."""

    return tuple(spec.account_id for spec in enabled_finance_source_specs())


def finance_source_spec(platform: str, account_id: str) -> FinanceSourceSpec | None:
    """Return the exact declared source role, including not-launched roles."""

    normalized_platform = str(platform or "").strip().lower()
    normalized_account_id = str(account_id or "").strip()
    for spec in FINANCE_SOURCE_SPECS:
        if spec.platform == normalized_platform and spec.account_id == normalized_account_id:
            return spec
    return None


def is_finance_source_enabled(platform: str, account_id: str) -> bool:
    """Return whether an exact platform/account finance source is live."""

    spec = finance_source_spec(platform, account_id)
    return bool(spec and spec.production_ready)


__all__ = [
    "FINANCE_SOURCE_SPECS",
    "FinanceSourceSpec",
    "enabled_finance_account_ids",
    "enabled_finance_platforms",
    "enabled_finance_source_specs",
    "finance_source_spec",
    "is_finance_source_enabled",
]
