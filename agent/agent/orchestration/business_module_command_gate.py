"""Lifecycle admission gate for new commands owned by manageable modules."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Callable

from agent.orchestration.models import Command, OrchestrationError
from shared.business_modules import BUSINESS_MODULE_BY_CODE, BUSINESS_MODULE_TOOL_OWNERS


_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class BusinessModuleCommandGate:
    """Lock lifecycle rows in the caller UoW before accepting a new command."""

    def __init__(self, *, project_governance_tool_resolver: Callable[[Command], str | None] | None = None) -> None:
        self._project_governance_tool_resolver = project_governance_tool_resolver

    def check_new_command(self, command: Command, uow: Any) -> None:
        if command.command_type not in {"tool.execute", "automation.project.invoke"}:
            return
        tool_name = str(command.parameters.get("tool_name") or "").strip()
        module_code = BUSINESS_MODULE_TOOL_OWNERS.get(tool_name)
        if command.command_type == "automation.project.invoke" and module_code is None:
            if self._project_governance_tool_resolver is None:
                raise OrchestrationError(
                    "MODULE_STATUS_BLOCKED",
                    "Automation project governance ownership is unavailable",
                )
            try:
                governance_tool = self._project_governance_tool_resolver(command)
            except Exception as exc:
                raise OrchestrationError(
                    "MODULE_STATUS_BLOCKED",
                    "Automation project governance ownership is blocked",
                ) from exc
            if governance_tool is None:
                return
            module_code = BUSINESS_MODULE_TOOL_OWNERS.get(str(governance_tool).strip())
        if not module_code:
            return
        module = BUSINESS_MODULE_BY_CODE[module_code]
        if not module.disable_allowed:
            return
        with uow.commands.cursor() as cursor:
            cursor.execute(
                "SELECT module_code, code_version, installed_version, lifecycle_state "
                "FROM business_modules WHERE module_code=%s FOR UPDATE",
                (module_code,),
            )
            rows = _rows(cursor)
        if len(rows) != 1 or str(rows[0].get("module_code") or "") != module_code:
            raise OrchestrationError("MODULE_STATUS_BLOCKED", "Business module lifecycle row is missing")
        row = rows[0]
        if not _SEMVER_RE.fullmatch(str(row.get("code_version") or "")) or not _SEMVER_RE.fullmatch(str(row.get("installed_version") or "")):
            raise OrchestrationError("MODULE_STATUS_BLOCKED", "Business module lifecycle version is malformed")
        if str(row.get("code_version") or "") != module.version or str(row.get("installed_version") or "") != module.version:
            raise OrchestrationError("MODULE_UPGRADE_REQUIRED", "The tool's business module requires an upgrade")
        if str(row.get("lifecycle_state") or "") != "ENABLED":
            raise OrchestrationError("MODULE_UNAVAILABLE", "The tool's business module is not enabled")


def _rows(cursor: Any) -> list[dict[str, Any]]:
    names = [str(item[0]) for item in (getattr(cursor, "description", None) or ())]
    values = cursor.fetchall() or []
    return [dict(row) if isinstance(row, Mapping) else dict(zip(names, row)) for row in values]
