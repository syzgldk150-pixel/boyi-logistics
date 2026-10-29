"""Runtime definition of one installed plugin project and its trusted entrypoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


TRUSTED_AUTOMATION_ENTRYPOINTS = frozenset({"scheduler", "console", "feishu", "webhook"})


@dataclass(frozen=True)
class AutomationProjectInstanceDefinition:
    """Runtime definition assembled from one installed instance and system config."""

    automation_id: str
    plugin_id: str
    tool_name: str
    argument_templates: Mapping[str, Mapping[str, Any]]
    dynamic_argument_resolvers: Mapping[str, Mapping[str, str]]
    account_bindings: Mapping[str, str | tuple[str, ...]]
    allowed_entrypoints: frozenset[str]
    project_config: Mapping[str, Any] = field(default_factory=dict)
    resource_bindings: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict)


__all__ = ["AutomationProjectInstanceDefinition", "TRUSTED_AUTOMATION_ENTRYPOINTS"]
