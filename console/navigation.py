"""Single source of truth for Console navigation and mobile preference validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


MOBILE_BOTTOM_NAV_SLOTS = 3
DEFAULT_MOBILE_BOTTOM_NAV = ("/tracking", "/receipts", "/automations")
_MENU_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_ICON_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ROUTE_PATTERN = re.compile(
    r"^/(?:[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*)?$"
)
_MENU_SECTIONS = frozenset({"primary", "system"})


@dataclass(frozen=True, slots=True)
class ConsoleMenuRegistration:
    """One static Console menu contribution, independent of access and state."""

    menu_id: str
    route: str
    label: str
    mobile_label: str
    icon: str
    section: str
    show_in_navigation: bool = True

    def __post_init__(self) -> None:
        if not _MENU_ID_PATTERN.fullmatch(self.menu_id):
            raise ValueError("menu_id must be a lowercase snake_case identifier")
        if not _ROUTE_PATTERN.fullmatch(self.route):
            raise ValueError("menu route must be one canonical absolute Console path")
        for name, value in (("label", self.label), ("mobile_label", self.mobile_label)):
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and already normalized")
        if not _ICON_PATTERN.fullmatch(self.icon):
            raise ValueError("menu icon must be a lowercase kebab-case name")
        if self.section not in _MENU_SECTIONS:
            raise ValueError("menu section must be primary or system")
        if not isinstance(self.show_in_navigation, bool):
            raise TypeError("show_in_navigation must be a boolean")

    def to_navigation_item(self) -> dict[str, str]:
        return {
            "route": self.route,
            "label": self.label,
            "mobile_label": self.mobile_label,
            "icon": self.icon,
            "section": self.section,
        }


def register_console_menus(
    registrations: Sequence[ConsoleMenuRegistration],
) -> tuple[ConsoleMenuRegistration, ...]:
    """Validate and freeze the static registration order."""

    frozen = tuple(registrations)
    if not frozen:
        raise ValueError("at least one Console menu registration is required")
    if any(not isinstance(item, ConsoleMenuRegistration) for item in frozen):
        raise TypeError("Console menus must use ConsoleMenuRegistration")
    menu_ids = [item.menu_id for item in frozen]
    routes = [item.route for item in frozen]
    if len(set(menu_ids)) != len(menu_ids):
        raise ValueError("Console menu IDs must be unique")
    if len(set(routes)) != len(routes):
        raise ValueError("Console menu routes must be unique")
    if any(
        item.section == "primary"
        for item in frozen[next((index for index, item in enumerate(frozen) if item.section == "system"), len(frozen)) :]
    ):
        raise ValueError("primary Console menus must precede system menus")
    return frozen


CONSOLE_MENU_REGISTRATIONS = register_console_menus(
    (
        ConsoleMenuRegistration("overview", "/", "概览", "首页", "grid", "primary"),
        ConsoleMenuRegistration("waybill_entry", "/ocr", "运单录入", "录单", "file-text", "primary"),
        ConsoleMenuRegistration("waybill_query", "/waybills", "寄件运单查询", "运单", "list", "primary"),
        ConsoleMenuRegistration("tracking", "/tracking", "物流跟踪", "跟踪", "search", "primary"),
        ConsoleMenuRegistration("receipts", "/receipts", "回单管理", "回单", "image", "primary"),
        ConsoleMenuRegistration("customer_service", "/modules/customer-service", "客户服务", "客服", "headphones", "primary"),
        ConsoleMenuRegistration("finance", "/modules/finance", "财务模块", "财务", "dollar-sign", "primary"),
        ConsoleMenuRegistration("dispatch", "/dispatch", "货拉拉调度", "调度", "map-pin", "primary"),
        ConsoleMenuRegistration("line_haul", "/line-haul-contacts", "专线分流", "专线", "map", "primary"),
        ConsoleMenuRegistration("automations", "/automations", "自动化", "自动化", "sliders", "primary"),
        ConsoleMenuRegistration("harness", "/harness", "Harness 助手", "助手", "message-square", "primary"),
        ConsoleMenuRegistration("automation_accounts", "/automation-accounts", "业务账号", "账号", "users", "primary"),
        ConsoleMenuRegistration("llm_settings", "/settings/llm", "智能模型", "模型", "cpu", "system"),
        ConsoleMenuRegistration(
            "work_items",
            "/work-items",
            "事项中心",
            "事项",
            "inbox",
            "system",
            show_in_navigation=False,
        ),
        ConsoleMenuRegistration("system_settings", "/settings/accounts", "系统管理", "设置", "settings", "system"),
    )
)

# This is a Console control-plane entry, not a fifteenth fixed business module.
CONSOLE_CONTROL_PLANE_MENU_REGISTRATIONS = register_console_menus(
    (
        ConsoleMenuRegistration("extensions", "/extensions", "扩展中心", "扩展", "package", "system"),
        ConsoleMenuRegistration("system_status", "/settings/system-status", "系统状态", "状态", "activity", "system"),
    )
)
CONSOLE_NAVIGATION_REGISTRATIONS = register_console_menus(
    (*CONSOLE_MENU_REGISTRATIONS, *CONSOLE_CONTROL_PLANE_MENU_REGISTRATIONS)
)

# The static registration remains the fifteen fixed module identities. The
# visible projection omits internal-only control surfaces such as work items.
CONSOLE_NAVIGATION: tuple[dict[str, str], ...] = tuple(
    registration.to_navigation_item()
    for registration in CONSOLE_MENU_REGISTRATIONS
    if registration.show_in_navigation
)
CONSOLE_CONTROL_PLANE_NAVIGATION: tuple[dict[str, str], ...] = tuple(
    registration.to_navigation_item() for registration in CONSOLE_CONTROL_PLANE_MENU_REGISTRATIONS
)

NAVIGATION_BY_ROUTE = {
    registration.route: registration.to_navigation_item()
    for registration in CONSOLE_NAVIGATION_REGISTRATIONS
}
MOBILE_NAVIGATION_CANDIDATES = tuple(
    registration.to_navigation_item()
    for registration in CONSOLE_NAVIGATION_REGISTRATIONS
    if registration.route != "/" and registration.show_in_navigation
)
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


if validate_mobile_bottom_nav(DEFAULT_MOBILE_BOTTOM_NAV) != DEFAULT_MOBILE_BOTTOM_NAV:
    raise RuntimeError("default mobile navigation must be a valid registered menu selection")
