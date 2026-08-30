from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from console.module_status_registry import (
    CODE_REGISTERED,
    CONSOLE_MODULE_STATUS_BY_ID,
    CONSOLE_MODULE_STATUS_REGISTRATIONS,
    ConsoleModuleStatusRegistration,
    register_console_module_statuses,
)
from console.navigation import CONSOLE_MENU_REGISTRATIONS
from console.permission_registry import CONSOLE_PERMISSION_BY_ID, CONSOLE_PERMISSION_BY_MENU_ID
from console.services.documents import DocumentServiceMixin


def _status(
    module_id: str = "example",
    code_status: str = CODE_REGISTERED,
) -> ConsoleModuleStatusRegistration:
    return ConsoleModuleStatusRegistration(module_id, code_status)


def test_status_registry_preserves_fixed_module_coverage_and_excludes_control_plane_menu() -> None:
    lifecycle_menu_ids = tuple(item.menu_id for item in CONSOLE_MENU_REGISTRATIONS)

    assert tuple(item.module_id for item in CONSOLE_MODULE_STATUS_REGISTRATIONS) == lifecycle_menu_ids
    assert tuple(CONSOLE_MODULE_STATUS_BY_ID) == lifecycle_menu_ids
    assert set(lifecycle_menu_ids).issubset(CONSOLE_PERMISSION_BY_MENU_ID)
    assert "system_status" in CONSOLE_PERMISSION_BY_MENU_ID
    assert "console.menu.system_status.view" in CONSOLE_PERMISSION_BY_ID
    assert "system_status" not in CONSOLE_MODULE_STATUS_BY_ID
    assert all(
        item.code_status == CODE_REGISTERED
        for item in CONSOLE_MODULE_STATUS_REGISTRATIONS
    )


def test_status_registry_and_entries_are_immutable() -> None:
    registration = CONSOLE_MODULE_STATUS_REGISTRATIONS[0]

    with pytest.raises(FrozenInstanceError):
        registration.code_status = "changed"
    with pytest.raises(TypeError):
        CONSOLE_MODULE_STATUS_BY_ID[registration.module_id] = registration


def test_status_registration_rejects_invalid_identity_and_runtime_meaning() -> None:
    with pytest.raises(ValueError, match="module_id"):
        _status("Not-valid")
    for status in ("enabled", "healthy", "ready", "disabled", "planned"):
        with pytest.raises(ValueError, match="code_registered"):
            _status(code_status=status)


def test_status_registry_rejects_duplicates_missing_orphans_and_reordering() -> None:
    with pytest.raises(ValueError, match="unique"):
        register_console_module_statuses(
            (_status(), _status()),
            known_module_ids=("example",),
        )
    with pytest.raises(ValueError, match="coverage and order"):
        register_console_module_statuses(
            (_status(),),
            known_module_ids=("example", "missing"),
        )
    with pytest.raises(ValueError, match="coverage and order"):
        register_console_module_statuses(
            (_status(), _status("orphan")),
            known_module_ids=("example",),
        )
    with pytest.raises(ValueError, match="coverage and order"):
        register_console_module_statuses(
            (_status("second"), _status("first")),
            known_module_ids=("first", "second"),
        )


def test_code_registration_contract_has_no_runtime_state_fields() -> None:
    fields = ConsoleModuleStatusRegistration.__dataclass_fields__

    assert "enabled" not in fields
    assert "healthy" not in fields
    assert "runtime_status" not in fields
    assert "project_status" not in fields


def test_legacy_project_module_maturity_statuses_remain_independent() -> None:
    project_modules = DocumentServiceMixin._build_project_modules(None)

    assert {slug: module.status for slug, module in project_modules.items()} == {
        "ocr": "ready",
        "pricing": "maintained",
        "finance": "ready",
        "customer-service": "in-progress",
        "ai-service": "planned",
        "dispatch": "in-progress",
    }
