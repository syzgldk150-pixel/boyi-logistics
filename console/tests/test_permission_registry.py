from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from console.navigation import CONSOLE_MENU_REGISTRATIONS
from console.permission_registry import (
    CONSOLE_PERMISSION_BY_ID,
    CONSOLE_PERMISSION_BY_MENU_ID,
    CONSOLE_PERMISSION_REGISTRATIONS,
    ConsolePermissionRegistration,
    has_console_permission,
    register_console_permissions,
)
from shared.service_identity import (
    CONSOLE_ADMIN_ROLES,
    ConsoleIdentityError,
    normalize_console_principal,
)


def _permission(
    menu_id: str = "example",
    *,
    permission_id: str | None = None,
    allowed_roles: tuple[str, ...] = ("admin", "super_admin"),
) -> ConsolePermissionRegistration:
    return ConsolePermissionRegistration(
        permission_id=permission_id or f"console.menu.{menu_id}.view",
        menu_id=menu_id,
        label="查看示例",
        allowed_roles=allowed_roles,
    )


def test_permission_registry_covers_every_registered_menu_exactly_once() -> None:
    menu_ids = tuple(item.menu_id for item in CONSOLE_MENU_REGISTRATIONS)

    assert tuple(item.menu_id for item in CONSOLE_PERMISSION_REGISTRATIONS) == menu_ids
    assert set(CONSOLE_PERMISSION_BY_MENU_ID) == set(menu_ids)
    assert set(CONSOLE_PERMISSION_BY_ID) == {
        f"console.menu.{menu_id}.view" for menu_id in menu_ids
    }


def test_existing_administrator_roles_retain_all_registered_menu_views() -> None:
    assert CONSOLE_ADMIN_ROLES == frozenset({"admin", "super_admin"})
    for role in CONSOLE_ADMIN_ROLES:
        assert all(
            has_console_permission(role, item.permission_id)
            for item in CONSOLE_PERMISSION_REGISTRATIONS
        )


def test_unknown_or_legacy_roles_and_unknown_permissions_fail_closed() -> None:
    permission_id = CONSOLE_PERMISSION_REGISTRATIONS[0].permission_id

    assert not has_console_permission("legacy_admin", permission_id)
    assert not has_console_permission("Admin", permission_id)
    assert not has_console_permission("admin", "console.menu.missing.view")


def test_registered_roles_match_the_signed_console_principal_contract() -> None:
    for role in CONSOLE_ADMIN_ROLES:
        principal = normalize_console_principal(
            {
                "actor_type": "console_admin",
                "actor_id": "17",
                "roles": [role],
                "display_name": "Reviewer",
                "authenticated_by": "mysql_admin_session",
            }
        )
        assert principal["roles"] == [role]

    with pytest.raises(ConsoleIdentityError, match="roles are invalid"):
        normalize_console_principal(
            {
                "actor_type": "console_admin",
                "actor_id": "17",
                "roles": ["module_owner"],
                "authenticated_by": "mysql_admin_session",
            }
        )


def test_permission_registration_is_immutable_and_rejects_drift() -> None:
    permission = _permission()
    with pytest.raises(FrozenInstanceError):
        permission.label = "changed"
    with pytest.raises(ValueError, match="match menu_id"):
        _permission(permission_id="console.menu.other.view")
    with pytest.raises(ValueError, match="unknown"):
        _permission(allowed_roles=("module_owner",))
    with pytest.raises(ValueError, match="duplicates"):
        _permission(allowed_roles=("admin", "admin"))


def test_permission_registry_rejects_duplicate_missing_and_orphan_menus() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        register_console_permissions(
            (_permission(), _permission()),
            known_menu_ids=("example",),
        )
    with pytest.raises(ValueError, match="cover"):
        register_console_permissions(
            (_permission(),),
            known_menu_ids=("example", "missing"),
        )


def test_permission_contract_has_no_runtime_state_fields() -> None:
    fields = ConsolePermissionRegistration.__dataclass_fields__

    assert "enabled" not in fields
    assert "status" not in fields
