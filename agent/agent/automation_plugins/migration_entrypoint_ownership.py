"""Exact migration ownership for Console, Scheduler, and fixed Feishu routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.automation_plugins.errors import PluginConflictError
from agent.direct_tool_router import (
    FIRST_PARTY_FEISHU_ROUTE_KEYS,
    direct_tool_request_from_text,
)
from shared.automation_plugin_migration_ownership import (
    MIGRATION_ENTRYPOINT_OWNERSHIP_SCHEMA,
    MIGRATION_OWNERSHIP_STATES,
    MIGRATION_PERSISTED_PAIR_STATES,
    migration_effective_ownership_state,
    migration_entrypoint_owner,
    normalize_migration_entrypoint_ownership,
)
from shared.automation_project_manifest import (
    FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES,
)


FIXED_FEISHU_OWNER_V1 = "ACTION_V1"
FIXED_FEISHU_OWNER_V2 = "SERVICE_V2"
FIXED_FEISHU_OWNER_BLOCKED = "BLOCKED"


def _contribution_ids(entry: Any, kind: str) -> tuple[str, ...]:
    declarations = getattr(entry, "contributions", {}).get(kind, ())
    if not isinstance(declarations, (list, tuple)):
        raise PluginConflictError(
            f"migration target {kind} contributions are invalid",
            code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
        )
    result = tuple(
        str(item.get("id") or "")
        for item in declarations
        if isinstance(item, Mapping)
    )
    if len(result) != len(declarations) or any(not item for item in result):
        raise PluginConflictError(
            f"migration target {kind} contributions are invalid",
            code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
        )
    return result


def _single_target_contribution(entry: Any, kind: str) -> str:
    candidates = _contribution_ids(entry, kind)
    if len(candidates) != 1:
        raise PluginConflictError(
            f"migration target must declare exactly one {kind} entrypoint",
            code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
        )
    return candidates[0]


def _source_fixed_feishu_identity(source: Any) -> tuple[str, str]:
    source_id = str(getattr(source, "automation_id", "") or "")
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES.get(source_id)
    tool_name = str(getattr(template, "tool_name", "") or "")
    source_plugin_id = str(getattr(source, "plugin_id", "") or "")
    route_key = str(FIRST_PARTY_FEISHU_ROUTE_KEYS.get(tool_name) or "")
    if (
        template is None
        or not tool_name
        or source_plugin_id != tool_name
        or not route_key
    ):
        raise PluginConflictError(
            "migration source has no exact fixed Feishu route",
            code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
        )
    return tool_name, route_key


def _target_feishu_commands(
    target: Any,
    *,
    contribution_id: str,
    source_tool_name: str,
    source_route_key: str,
) -> tuple[str, ...]:
    declarations = getattr(target, "contributions", {}).get("feishu", ())
    matches = tuple(
        item
        for item in declarations
        if isinstance(item, Mapping)
        and str(item.get("id") or "") == contribution_id
    )
    if len(matches) != 1:
        raise PluginConflictError(
            "migration target Feishu contribution is ambiguous",
            code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
        )
    if matches[0].get("selection_preview_operation") is not None:
        raise PluginConflictError(
            "migration of an enabled Action-v1 Feishu selection-preview route is production gated",
            code="PLUGIN_MIGRATION_FEISHU_SELECTION_PREVIEW_PRODUCTION_GATED",
        )
    commands = matches[0].get("commands")
    if (
        not isinstance(commands, (list, tuple))
        or not commands
        or any(
            not isinstance(command, str)
            or not command
            or command != command.strip()
            for command in commands
        )
        or len(set(commands)) != len(commands)
    ):
        raise PluginConflictError(
            "migration target Feishu commands are invalid",
            code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
        )
    for command in commands:
        request = direct_tool_request_from_text(command)
        if (
            not isinstance(request, Mapping)
            or request.get("mode") != "automation_project"
            or request.get("tool_name") != source_tool_name
            or request.get("automation_route_key") != source_route_key
        ):
            raise PluginConflictError(
                "migration target Feishu command does not exactly match its v1 route",
                code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
            )
    return tuple(commands)


def migration_target_entrypoints_and_ownership(
    *,
    source: Any,
    target: Any,
    source_enabled_entrypoints: Sequence[str],
    source_schedule: Mapping[str, Any],
    source_resource_bindings: Mapping[str, Any],
) -> tuple[tuple[str, ...], dict[str, Any], frozenset[str]]:
    """Build the target config and immutable ownership from signed catalogs."""

    source_enabled = tuple(str(item or "") for item in source_enabled_entrypoints)
    if any(not item for item in source_enabled) or len(source_enabled) != len(
        set(source_enabled)
    ):
        raise PluginConflictError(
            "migration source entrypoints are invalid",
            code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
        )
    unsupported_entrypoints = set(source_enabled) - {
        "console",
        "scheduler",
        "feishu",
        "webhook",
    }
    if unsupported_entrypoints:
        raise PluginConflictError(
            "migration source has entrypoints outside the reviewed ownership contract",
            code="PLUGIN_MIGRATION_ENTRYPOINT_PRODUCTION_GATED",
        )
    console_id = _single_target_contribution(target, "console")
    entrypoints = [console_id]

    has_real_schedule = (
        source_schedule.get("kind") != "none"
        and source_schedule.get("enabled") is True
    )
    if "scheduler" in source_enabled or has_real_schedule:
        raise PluginConflictError(
            "migration of an enabled Action-v1 Scheduler is production gated",
            code="PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED",
        )
    scheduler_enabled = False
    scheduler_id = None

    feishu_enabled = "feishu" in source_enabled
    webhook_enabled = "webhook" in source_enabled
    if webhook_enabled:
        raise PluginConflictError(
            "migration of an enabled Action-v1 Webhook is production gated",
            code="PLUGIN_MIGRATION_WEBHOOK_PRODUCTION_GATED",
        )
    source_template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES.get(
        str(getattr(source, "automation_id", "") or "")
    )
    consumed_route_bindings: set[str] = set()
    for role in ("feishu_route", "webhook_route"):
        if role not in source_resource_bindings:
            continue
        expected_resource = (
            source_template.resource_bindings.get(role)
            if source_template is not None
            else None
        )
        if (
            not isinstance(expected_resource, str)
            or source_resource_bindings.get(role) != expected_resource
        ):
            raise PluginConflictError(
                f"migration source {role} binding is not the reviewed route",
                code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
            )
        consumed_route_bindings.add(role)
    if feishu_enabled and "feishu_route" not in consumed_route_bindings:
        raise PluginConflictError(
            "migration source Feishu route binding is missing",
            code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
        )
    source_tool_name = None
    source_route_key = None
    source_feishu_resource_id = None
    feishu_id = None
    commands: tuple[str, ...] = ()
    if feishu_enabled:
        source_tool_name, source_route_key = _source_fixed_feishu_identity(source)
        source_feishu_resource_id = str(
            source_resource_bindings["feishu_route"]
        )
        feishu_id = _single_target_contribution(target, "feishu")
        commands = _target_feishu_commands(
            target,
            contribution_id=feishu_id,
            source_tool_name=source_tool_name,
            source_route_key=source_route_key,
        )
        entrypoints.append(feishu_id)

    route_enabled = {
        "console": True,
        "scheduler": scheduler_enabled,
        "feishu": feishu_enabled,
    }
    owners = {
        state: {
            kind: (
                "NONE"
                if not route_enabled[kind]
                else "SERVICE_V2" if state == "CUTOVER" else "ACTION_V1"
            )
            for kind in ("console", "scheduler", "feishu")
        }
        for state in MIGRATION_OWNERSHIP_STATES
    }
    ownership = normalize_migration_entrypoint_ownership(
        {
            "schema": MIGRATION_ENTRYPOINT_OWNERSHIP_SCHEMA,
            "console": {
                "source_enabled": True,
                "target_contribution_id": console_id,
            },
            "scheduler": {
                "source_enabled": scheduler_enabled,
                "target_contribution_id": scheduler_id,
                "schedule_mode": "COPY_SOURCE" if scheduler_enabled else "NONE",
            },
            "feishu": {
                "source_enabled": feishu_enabled,
                "source_tool_name": source_tool_name,
                "source_route_key": source_route_key,
                "source_resource_id": source_feishu_resource_id,
                "target_contribution_id": feishu_id,
                "commands": list(commands),
            },
            "owners": owners,
        }
    )
    return tuple(entrypoints), ownership, frozenset(consumed_route_bindings)


def _reviewed_source_automation_id(tool_name: str) -> str | None:
    matches = tuple(
        automation_id
        for automation_id, template in FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES.items()
        if template.tool_name == tool_name
    )
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class _FeishuPairIdentity:
    state: str
    source_automation_id: str
    target_automation_id: str
    target_generation: int
    target_contribution_id: str
    source_tool_name: str
    source_route_key: str
    commands: tuple[str, ...]


class MigrationEntrypointOwnershipResolver:
    """Read one immutable pair; never infer ownership from project.enabled."""

    def __init__(self, repository: Any) -> None:
        reader = getattr(
            repository,
            "get_authoritative_plugin_migration_pair_for_automation",
            None,
        )
        if not callable(reader):
            raise TypeError("repository must expose authoritative migration pairs")
        self._read_pair = reader

    @staticmethod
    def _identity(pair: object) -> _FeishuPairIdentity:
        if not isinstance(pair, Mapping):
            raise ValueError("migration pair is invalid")
        state = str(pair.get("state") or "")
        source_id = str(pair.get("source_automation_id") or "")
        target_id = str(pair.get("target_automation_id") or "")
        snapshot = pair.get("entrypoint_snapshot_json")
        if (
            state not in MIGRATION_PERSISTED_PAIR_STATES
            or not source_id
            or not target_id
            or not isinstance(snapshot, Mapping)
        ):
            raise ValueError("migration pair is invalid")
        ownership = normalize_migration_entrypoint_ownership(
            snapshot.get("entrypoint_ownership")
        )
        feishu = ownership["feishu"]
        target = snapshot.get("target")
        if target is None and state == "PREPARING":
            target_generation = 0
        elif not isinstance(target, Mapping):
            raise ValueError("migration target snapshot is invalid")
        else:
            if target.get("automation_id") != target_id:
                raise ValueError("migration target snapshot is invalid")
            target_generation = int(
                target.get("generation") or target.get("pending_generation") or 0
            )
        target_contribution_id = str(
            feishu.get("target_contribution_id") or ""
        )
        source_tool_name = str(feishu.get("source_tool_name") or "")
        source_route_key = str(feishu.get("source_route_key") or "")
        source_resource_id = str(feishu.get("source_resource_id") or "")
        commands = tuple(str(item) for item in feishu.get("commands") or ())
        source_template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES.get(source_id)
        if (
            (state != "PREPARING" and target_generation < 1)
            or not target_contribution_id
            or not source_tool_name
            or not source_route_key
            or not source_resource_id
            or not commands
            or source_template is None
            or source_template.tool_name != source_tool_name
            or source_template.resource_bindings.get("feishu_route")
            != source_resource_id
            or FIRST_PARTY_FEISHU_ROUTE_KEYS.get(source_tool_name)
            != source_route_key
        ):
            raise ValueError("migration Feishu ownership is unavailable")
        return _FeishuPairIdentity(
            state=state,
            source_automation_id=source_id,
            target_automation_id=target_id,
            target_generation=target_generation,
            target_contribution_id=target_contribution_id,
            source_tool_name=source_tool_name,
            source_route_key=source_route_key,
            commands=commands,
        )

    @staticmethod
    def _command_matches(identity: _FeishuPairIdentity, command: str) -> bool:
        request = direct_tool_request_from_text(command)
        return bool(
            command in identity.commands
            and isinstance(request, Mapping)
            and request.get("mode") == "automation_project"
            and request.get("tool_name") == identity.source_tool_name
            and request.get("automation_route_key") == identity.source_route_key
        )

    def allow_reserved_feishu_target(
        self,
        automation_id: str,
        generation: int,
        contribution_id: str,
        command: str,
    ) -> bool:
        """Allow staging only for the exact target frozen by an open pair."""

        try:
            pair = self._read_pair(automation_id)
            if not isinstance(pair, Mapping):
                return False
            effective_state = migration_effective_ownership_state(
                pair.get("state"),
                rolled_back_at=pair.get("rolled_back_at"),
            )
            if effective_state not in {"TESTING", "READY", "CUTOVER"}:
                return False
            identity = self._identity(pair)
            generation_matches = (
                generation >= identity.target_generation
                if identity.state == "COMPLETED"
                else generation == identity.target_generation
            )
            return bool(
                identity.target_automation_id == automation_id
                and generation_matches
                and identity.target_contribution_id == contribution_id
                and self._command_matches(identity, command)
            )
        except Exception:
            return False

    def fixed_feishu_owner(
        self,
        *,
        source_tool_name: str,
        source_route_key: str,
        command: str,
    ) -> str:
        """Return the exact current owner; malformed pair state is BLOCKED."""

        source_id = _reviewed_source_automation_id(source_tool_name)
        if source_id is None:
            return FIXED_FEISHU_OWNER_V1
        try:
            pair = self._read_pair(source_id)
        except Exception:
            return FIXED_FEISHU_OWNER_BLOCKED
        if pair is None:
            return FIXED_FEISHU_OWNER_V1
        try:
            effective_state = migration_effective_ownership_state(
                pair.get("state"),
                rolled_back_at=pair.get("rolled_back_at"),
            )
            if effective_state is None:
                return FIXED_FEISHU_OWNER_BLOCKED
            identity = self._identity(pair)
            if (
                identity.source_automation_id != source_id
                or identity.source_tool_name != source_tool_name
                or identity.source_route_key != source_route_key
                or not self._command_matches(identity, command)
            ):
                return FIXED_FEISHU_OWNER_BLOCKED
            owner = migration_entrypoint_owner(
                pair["entrypoint_snapshot_json"]["entrypoint_ownership"],
                state=effective_state,
                kind="feishu",
            )
            if owner in {FIXED_FEISHU_OWNER_V1, FIXED_FEISHU_OWNER_V2}:
                return owner
        except Exception:
            pass
        return FIXED_FEISHU_OWNER_BLOCKED


__all__ = [
    "FIXED_FEISHU_OWNER_BLOCKED",
    "FIXED_FEISHU_OWNER_V1",
    "FIXED_FEISHU_OWNER_V2",
    "MigrationEntrypointOwnershipResolver",
    "migration_target_entrypoints_and_ownership",
]
