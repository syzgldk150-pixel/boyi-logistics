"""Read-only source-code registration status for Console modules."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType

from console.navigation import CONSOLE_MENU_REGISTRATIONS


CODE_REGISTERED = "code_registered"
_MODULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ConsoleModuleStatusRegistration:
    """State that one module identity is registered in this source build."""

    module_id: str
    code_status: str

    def __post_init__(self) -> None:
        if not _MODULE_ID_PATTERN.fullmatch(self.module_id):
            raise ValueError("module_id must be a lowercase snake_case identifier")
        if self.code_status != CODE_REGISTERED:
            raise ValueError("module code_status must be code_registered")


def register_console_module_statuses(
    registrations: Sequence[ConsoleModuleStatusRegistration],
    *,
    known_module_ids: Sequence[str],
) -> tuple[ConsoleModuleStatusRegistration, ...]:
    """Validate exact, ordered status coverage of the registered menu identities."""

    frozen = tuple(registrations)
    if not frozen:
        raise ValueError("at least one Console module status registration is required")
    if any(not isinstance(item, ConsoleModuleStatusRegistration) for item in frozen):
        raise TypeError("Console module statuses must use ConsoleModuleStatusRegistration")
    module_ids = tuple(item.module_id for item in frozen)
    if len(set(module_ids)) != len(module_ids):
        raise ValueError("Console module status IDs must be unique")
    known = tuple(known_module_ids)
    if len(set(known)) != len(known):
        raise ValueError("known Console module IDs must be unique")
    if module_ids != known:
        raise ValueError(
            "Console module statuses must preserve exact registered menu coverage and order"
        )
    return frozen


CONSOLE_MODULE_STATUS_REGISTRATIONS = register_console_module_statuses(
    tuple(
        ConsoleModuleStatusRegistration(item.menu_id, CODE_REGISTERED)
        for item in CONSOLE_MENU_REGISTRATIONS
    ),
    known_module_ids=tuple(item.menu_id for item in CONSOLE_MENU_REGISTRATIONS),
)

CONSOLE_MODULE_STATUS_BY_ID = MappingProxyType(
    {item.module_id: item for item in CONSOLE_MODULE_STATUS_REGISTRATIONS}
)
