"""Single source of truth for Console navigation and mobile preference validation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


MOBILE_BOTTOM_NAV_SLOTS = 3
DEFAULT_MOBILE_BOTTOM_NAV = ("/tracking", "/receipts", "/automations")

CONSOLE_NAVIGATION: tuple[dict[str, str], ...] = (
    {"route": "/", "label": "概览", "mobile_label": "首页", "icon": "grid", "section": "primary"},
    {
        "route": "/work-items",
        "label": "事项中心",
        "mobile_label": "事项",
        "icon": "inbox",
        "section": "primary",
    },
    {"route": "/ocr", "label": "运单录入", "mobile_label": "录单", "icon": "file-text", "section": "primary"},
    {"route": "/waybills", "label": "寄件运单查询", "mobile_label": "运单", "icon": "list", "section": "primary"},
    {"route": "/tracking", "label": "物流跟踪", "mobile_label": "跟踪", "icon": "search", "section": "primary"},
    {"route": "/receipts", "label": "回单管理", "mobile_label": "回单", "icon": "image", "section": "primary"},
    {"route": "/modules/customer-service", "label": "客户服务", "mobile_label": "客服", "icon": "headphones", "section": "primary"},
    {"route": "/modules/finance", "label": "财务模块", "mobile_label": "财务", "icon": "dollar-sign", "section": "primary"},
    {"route": "/dispatch", "label": "货拉拉调度", "mobile_label": "调度", "icon": "map-pin", "section": "primary"},
    {"route": "/line-haul-contacts", "label": "专线分流", "mobile_label": "专线", "icon": "map", "section": "primary"},
    {"route": "/automations", "label": "自动化", "mobile_label": "自动化", "icon": "sliders", "section": "primary"},
    {"route": "/automation-accounts", "label": "业务账号", "mobile_label": "账号", "icon": "users", "section": "primary"},
    {"route": "/templates/new", "label": "模板配置", "mobile_label": "模板", "icon": "layout", "section": "primary"},
    {"route": "/settings/accounts", "label": "系统管理", "mobile_label": "设置", "icon": "settings", "section": "system"},
)

NAVIGATION_BY_ROUTE = {item["route"]: item for item in CONSOLE_NAVIGATION}
MOBILE_NAVIGATION_CANDIDATES = tuple(item for item in CONSOLE_NAVIGATION if item["route"] != "/")
MOBILE_NAVIGATION_ROUTES = frozenset(item["route"] for item in MOBILE_NAVIGATION_CANDIDATES)


class MobileNavigationValidationError(ValueError):
    """Raised when a stored or submitted bottom navigation is invalid."""


def validate_mobile_bottom_nav(routes: object) -> tuple[str, ...]:
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes, bytearray)):
        raise MobileNavigationValidationError("routes 必须是包含三个模块路径的列表。")
    normalized = tuple(str(route or "").strip() for route in routes)
    if len(normalized) != MOBILE_BOTTOM_NAV_SLOTS:
        raise MobileNavigationValidationError("移动底栏必须恰好包含三个模块。")
    if len(set(normalized)) != MOBILE_BOTTOM_NAV_SLOTS:
        raise MobileNavigationValidationError("移动底栏中的模块不能重复。")
    unknown = [route for route in normalized if route not in MOBILE_NAVIGATION_ROUTES]
    if unknown:
        raise MobileNavigationValidationError("移动底栏包含不支持的模块。")
    return normalized


def parse_ui_preferences(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def mobile_bottom_nav_for_user(user: Mapping[str, Any] | None) -> tuple[str, ...]:
    preferences = parse_ui_preferences((user or {}).get("ui_preferences_json", "{}"))
    try:
        return validate_mobile_bottom_nav(preferences.get("mobile_bottom_nav"))
    except MobileNavigationValidationError:
        return DEFAULT_MOBILE_BOTTOM_NAV


def serialize_mobile_bottom_nav(existing_preferences: object, routes: object) -> str:
    preferences = parse_ui_preferences(existing_preferences)
    preferences["mobile_bottom_nav"] = list(validate_mobile_bottom_nav(routes))
    return json.dumps(preferences, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


if len(NAVIGATION_BY_ROUTE) != len(CONSOLE_NAVIGATION):
    raise RuntimeError("Console navigation routes must be unique.")
