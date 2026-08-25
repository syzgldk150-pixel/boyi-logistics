from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from console.navigation import (
    CONSOLE_MENU_REGISTRATIONS,
    CONSOLE_NAVIGATION,
    DEFAULT_MOBILE_BOTTOM_NAV,
    ConsoleMenuRegistration,
    register_console_menus,
    validate_mobile_bottom_nav,
)


def _menu(menu_id: str = "example", route: str = "/example", **changes: str):
    values = {
        "menu_id": menu_id,
        "route": route,
        "label": "示例",
        "mobile_label": "示例",
        "icon": "box",
        "section": "primary",
    }
    values.update(changes)
    return ConsoleMenuRegistration(**values)


def test_registered_menu_projection_preserves_existing_console_contract() -> None:
    assert tuple(item.menu_id for item in CONSOLE_MENU_REGISTRATIONS) == (
        "overview",
        "waybill_entry",
        "waybill_query",
        "tracking",
        "receipts",
        "customer_service",
        "finance",
        "dispatch",
        "line_haul",
        "automations",
        "automation_accounts",
        "llm_settings",
        "work_items",
        "system_settings",
    )
    assert CONSOLE_NAVIGATION == tuple(
        item.to_navigation_item() for item in CONSOLE_MENU_REGISTRATIONS
    )
    assert all(
        set(item) == {"route", "label", "mobile_label", "icon", "section"}
        for item in CONSOLE_NAVIGATION
    )


def test_menu_registration_is_immutable_and_freezes_input_order() -> None:
    primary = _menu()
    source = [primary]
    registered = register_console_menus(source)
    source.append(_menu("later", "/later"))

    assert registered == (primary,)
    with pytest.raises(FrozenInstanceError):
        primary.label = "changed"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"menu_id": "Not-valid"}, "menu_id"),
        ({"route": "relative"}, "canonical absolute"),
        ({"route": "/nested/"}, "canonical absolute"),
        ({"route": "/nested?mode=1"}, "canonical absolute"),
        ({"route": "/a/../b"}, "canonical absolute"),
        ({"route": "/a\\b"}, "canonical absolute"),
        ({"route": "/a b"}, "canonical absolute"),
        ({"label": " "}, "label"),
        ({"mobile_label": " 示例"}, "mobile_label"),
        ({"icon": "Bad Icon"}, "icon"),
        ({"section": "admin"}, "section"),
    ],
)
def test_menu_registration_rejects_invalid_fields(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        _menu(**changes)


def test_menu_registry_rejects_duplicate_identity_and_route() -> None:
    with pytest.raises(ValueError, match="IDs must be unique"):
        register_console_menus((_menu(), _menu(route="/other")))
    with pytest.raises(ValueError, match="routes must be unique"):
        register_console_menus((_menu(), _menu("other")))


def test_menu_registry_rejects_primary_item_after_system_section() -> None:
    with pytest.raises(ValueError, match="precede system"):
        register_console_menus(
            (
                _menu(section="system"),
                _menu("other", "/other"),
            )
        )


def test_menu_contract_has_no_permission_or_runtime_state_fields() -> None:
    fields = ConsoleMenuRegistration.__dataclass_fields__

    assert "permission" not in fields
    assert "enabled" not in fields
    assert "status" not in fields


def test_default_mobile_navigation_is_valid_against_registered_routes() -> None:
    assert validate_mobile_bottom_nav(DEFAULT_MOBILE_BOTTOM_NAV) == DEFAULT_MOBILE_BOTTOM_NAV
