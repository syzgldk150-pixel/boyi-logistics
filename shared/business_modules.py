"""Immutable, code-owned business-module catalog.

The lifecycle database records installation state only.  It never supplies a
module implementation, menu entry, permission, or runtime extension.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable


_MODULE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PATH_PREFIX_RE = re.compile(r"^/(?:[a-z0-9_]+(?:[-/][a-z0-9_]+)*)?$")
_EXTENSION_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

CORE_MODULE_CODES = frozenset(
    {
        "overview",
        "automations",
        "automation_accounts",
        "llm_settings",
        "work_items",
        "system_settings",
    }
)


@dataclass(frozen=True, slots=True)
class BusinessModuleCode:
    """The complete static contract for one Console menu identity."""

    module_code: str
    version: str
    name: str
    menu_contributions: tuple[str, ...]
    page_contributions: tuple[str, ...]
    api_contributions: tuple[str, ...]
    permission_contributions: tuple[str, ...]
    disable_allowed: bool
    internal_extensions: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _MODULE_CODE_RE.fullmatch(self.module_code):
            raise ValueError("module_code must be a lowercase snake_case identifier")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ValueError("module version must be semantic major.minor.patch")
        if not self.name or self.name != self.name.strip():
            raise ValueError("module name must be non-empty and normalized")
        for field_name in (
            "menu_contributions",
            "page_contributions",
            "api_contributions",
            "permission_contributions",
            "internal_extensions",
            "tool_names",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(not item or item != item.strip() for item in values):
                raise ValueError(f"{field_name} must be an immutable normalized tuple")
        for field_name in ("page_contributions", "api_contributions"):
            if any(not _PATH_PREFIX_RE.fullmatch(item) for item in getattr(self, field_name)):
                raise ValueError(f"{field_name} must contain canonical absolute route prefixes")
        if any(not _EXTENSION_RE.fullmatch(item) for item in self.internal_extensions):
            raise ValueError("internal_extensions must contain canonical dotted identities")
        if any(not _TOOL_NAME_RE.fullmatch(item) for item in self.tool_names):
            raise ValueError("tool_names must contain canonical tool identifiers")

    @property
    def code_registered(self) -> bool:
        return True

    def public_dict(self) -> dict[str, Any]:
        return {
            "module_code": self.module_code,
            "version": self.version,
            "code_registered": True,
            "name": self.name,
            "menu_contributions": list(self.menu_contributions),
            "page_contributions": list(self.page_contributions),
            "api_contributions": list(self.api_contributions),
            "permission_contributions": list(self.permission_contributions),
            "disable_allowed": self.disable_allowed,
            "internal_extensions": list(self.internal_extensions),
            "tool_names": list(self.tool_names),
        }


def _module(
    module_code: str,
    route: str,
    name: str,
    *,
    disable_allowed: bool,
    api_prefixes: tuple[str, ...] = (),
    page_prefixes: tuple[str, ...] = (),
    internal_extensions: tuple[str, ...] = (),
    tool_names: tuple[str, ...] = (),
) -> BusinessModuleCode:
    return BusinessModuleCode(
        module_code=module_code,
        version="1.0.0",
        name=name,
        menu_contributions=(module_code,),
        page_contributions=(route, *page_prefixes),
        api_contributions=api_prefixes,
        permission_contributions=(f"console.menu.{module_code}.view",),
        disable_allowed=disable_allowed,
        internal_extensions=internal_extensions,
        tool_names=tool_names,
    )


def register_business_modules(items: Iterable[BusinessModuleCode]) -> tuple[BusinessModuleCode, ...]:
    """Validate the exact source-owned module set once, at import time."""

    catalog = tuple(items)
    if len(catalog) != 14:
        raise ValueError("business module catalog must contain exactly 14 current menu identities")
    if any(not isinstance(item, BusinessModuleCode) for item in catalog):
        raise TypeError("business module catalog entries must be BusinessModuleCode")
    codes = tuple(item.module_code for item in catalog)
    if len(set(codes)) != len(codes):
        raise ValueError("business module codes must be unique")
    if set(codes) != {
        "overview", "waybill_entry", "waybill_query", "tracking", "receipts", "customer_service",
        "finance", "dispatch", "line_haul", "automations", "automation_accounts", "llm_settings",
        "work_items", "system_settings",
    }:
        raise ValueError("business module catalog must cover the exact current Console menu identities")
    if {item.module_code for item in catalog if not item.disable_allowed} != CORE_MODULE_CODES:
        raise ValueError("only the six core modules may be non-disableable")
    for field_name in ("menu_contributions", "page_contributions", "api_contributions", "permission_contributions"):
        contributions = [value for item in catalog for value in getattr(item, field_name)]
        if len(contributions) != len(set(contributions)):
            raise ValueError(f"business module {field_name} must be unique across the catalog")
    extensions = [value for item in catalog for value in item.internal_extensions]
    if len(extensions) != len(set(extensions)):
        raise ValueError("business module internal_extensions must be unique across the catalog")
    tools = [value for item in catalog for value in item.tool_names]
    if len(tools) != len(set(tools)):
        raise ValueError("business module tool_names must have one exact owner")
    return catalog


BUSINESS_MODULE_CATALOG = register_business_modules(
    (
        _module("overview", "/", "概览", disable_allowed=False, api_prefixes=("/monitoring",)),
        _module("waybill_entry", "/ocr", "运单录入", disable_allowed=True, api_prefixes=("/ocr", "/upload", "/documents", "/templates", "/runtime/originals", "/runtime/artifacts", "/runtime/logs", "/waybills/manual", "/waybills/quote-options", "/original-pages"), page_prefixes=("/workspaces/ocr",), tool_names=("ocr_recognize", "get_price")),
        _module("waybill_query", "/waybills", "寄件运单查询", disable_allowed=True, api_prefixes=("/waybills",), tool_names=("query_waybill",)),
        _module("tracking", "/tracking", "物流跟踪", disable_allowed=True, api_prefixes=("/tracking",), internal_extensions=("tracking.ronghui.source_adapter", "tracking.yunda.source_adapter", "tracking.line_haul.source_adapter"), tool_names=("track_waybill",)),
        _module("receipts", "/receipts", "回单管理", disable_allowed=True, api_prefixes=("/receipts",), tool_names=("query_receipt_feishu_detail", "receipts_sync", "receipts_audit")),
        _module("customer_service", "/modules/customer-service", "客户服务", disable_allowed=True, api_prefixes=("/customer-service",), internal_extensions=("customer_service.ronghui.problem_source_adapter", "customer_service.yunda.problem_source_adapter"), tool_names=("sync_customer_service_problems", "customer_service_problem_query", "customer_service_problem_detail", "customer_service_problem_fetch_attachment", "customer_service_problem_mark_read", "customer_service_problem_reply", "customer_service_problem_publish", "customer_service_problem_upload_attachment", "preview_self_pickup_problems", "self_pickup_problem_upload", "preview_split_pending_problems", "split_pending_problem_upload")),
        _module("finance", "/modules/finance", "财务模块", disable_allowed=True, api_prefixes=("/finance", "/runtime/finance_knowledge"), internal_extensions=("finance.ronghui.source.enabled", "finance.yunda.source.not_launched", "finance.sync", "finance.bi"), tool_names=("query_business_finance", "sync_finance_bills", "analyze_finance_reviews")),
        _module("dispatch", "/dispatch", "货拉拉调度", disable_allowed=True, api_prefixes=("/dispatch",), tool_names=("sync_yunda_dispatch_forecast",)),
        _module("line_haul", "/line-haul-contacts", "专线分流", disable_allowed=True, api_prefixes=("/line-haul-contacts",)),
        _module("automations", "/automations", "自动化", disable_allowed=False, api_prefixes=("/automations",), page_prefixes=("/workspaces/automations",), internal_extensions=("automations.signed_action_package_platform", "notification.feishu.background")),
        _module("automation_accounts", "/automation-accounts", "业务账号", disable_allowed=False, api_prefixes=("/automation-accounts",)),
        _module("llm_settings", "/settings/llm", "智能模型", disable_allowed=False, api_prefixes=("/settings/llm",)),
        _module("work_items", "/work-items", "事项中心", disable_allowed=False, api_prefixes=("/control-plane",)),
        _module("system_settings", "/settings/accounts", "系统管理", disable_allowed=False, api_prefixes=("/settings/accounts",)),
    )
)
BUSINESS_MODULE_BY_CODE = MappingProxyType({item.module_code: item for item in BUSINESS_MODULE_CATALOG})
BUSINESS_MODULE_TOOL_OWNERS = MappingProxyType({tool: item.module_code for item in BUSINESS_MODULE_CATALOG for tool in item.tool_names})
