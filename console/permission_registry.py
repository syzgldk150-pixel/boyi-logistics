"""Static Console module permission metadata for the existing administrator roles."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType

from console.navigation import CONSOLE_MENU_REGISTRATIONS, CONSOLE_NAVIGATION_REGISTRATIONS
from shared.service_identity import CONSOLE_ADMIN_ROLES


_PERMISSION_ID_PATTERN = re.compile(r"^console\.menu\.[a-z][a-z0-9_]*\.view$")


@dataclass(frozen=True, slots=True)
class ConsolePermissionRegistration:
    """One existing menu-view permission; enforcement remains at route boundaries."""

    permission_id: str
    menu_id: str
    label: str
    allowed_roles: tuple[str, ...]

    def __post_init__(self) -> None:
        expected_id = f"console.menu.{self.menu_id}.view"
        if not _PERMISSION_ID_PATTERN.fullmatch(self.permission_id):
            raise ValueError("permission_id must be a canonical Console menu-view permission")
        if self.permission_id != expected_id:
            raise ValueError("permission_id must match menu_id")
        if not self.label or self.label != self.label.strip():
            raise ValueError("permission label must be non-empty and already normalized")
        if not isinstance(self.allowed_roles, tuple) or not self.allowed_roles:
            raise ValueError("allowed_roles must be one non-empty immutable tuple")
        if len(set(self.allowed_roles)) != len(self.allowed_roles):
            raise ValueError("allowed_roles must not contain duplicates")
        if any(role not in CONSOLE_ADMIN_ROLES for role in self.allowed_roles):
            raise ValueError("allowed_roles contains an unknown Console administrator role")


def register_console_permissions(
    registrations: Sequence[ConsolePermissionRegistration],
    *,
    known_menu_ids: Sequence[str],
) -> tuple[ConsolePermissionRegistration, ...]:
    """Validate one explicit view permission for every registered Console menu."""

    frozen = tuple(registrations)
    if not frozen:
        raise ValueError("at least one Console permission registration is required")
    if any(not isinstance(item, ConsolePermissionRegistration) for item in frozen):
        raise TypeError("Console permissions must use ConsolePermissionRegistration")
    menu_ids = [item.menu_id for item in frozen]
    if len(set(menu_ids)) != len(menu_ids):
        raise ValueError("each Console menu must have exactly one view permission")
    known = tuple(known_menu_ids)
    if len(set(known)) != len(known):
        raise ValueError("known Console menu IDs must be unique")
    if set(menu_ids) != set(known):
        raise ValueError("Console permissions must cover the registered menu IDs exactly")
    return frozen


_ALL_ADMIN_ROLES = tuple(sorted(CONSOLE_ADMIN_ROLES))
CONSOLE_PERMISSION_REGISTRATIONS = register_console_permissions(
    (
        ConsolePermissionRegistration("console.menu.overview.view", "overview", "查看概览", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.waybill_entry.view", "waybill_entry", "查看运单录入", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.waybill_query.view", "waybill_query", "查看寄件运单查询", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.tracking.view", "tracking", "查看物流跟踪", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.receipts.view", "receipts", "查看回单管理", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.customer_service.view", "customer_service", "查看客户服务", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.finance.view", "finance", "查看财务模块", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.dispatch.view", "dispatch", "查看货拉拉调度", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.line_haul.view", "line_haul", "查看专线分流", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.automations.view", "automations", "查看自动化", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.automation_accounts.view", "automation_accounts", "查看业务账号", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.llm_settings.view", "llm_settings", "查看智能模型", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.work_items.view", "work_items", "查看事项中心", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.system_settings.view", "system_settings", "查看系统管理", _ALL_ADMIN_ROLES),
        ConsolePermissionRegistration("console.menu.system_status.view", "system_status", "查看系统状态", ("super_admin",)),
    ),
    known_menu_ids=tuple(item.menu_id for item in CONSOLE_NAVIGATION_REGISTRATIONS),
)

CONSOLE_PERMISSION_BY_ID = MappingProxyType(
    {item.permission_id: item for item in CONSOLE_PERMISSION_REGISTRATIONS}
)
CONSOLE_PERMISSION_BY_MENU_ID = MappingProxyType(
    {item.menu_id: item for item in CONSOLE_PERMISSION_REGISTRATIONS}
)


def has_console_permission(role: str, permission_id: str) -> bool:
    """Return the registered fact for an already authenticated exact role."""

    registration = CONSOLE_PERMISSION_BY_ID.get(permission_id)
    return registration is not None and role in registration.allowed_roles
