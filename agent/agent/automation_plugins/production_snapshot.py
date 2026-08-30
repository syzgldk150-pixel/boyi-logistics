"""Closed runtime-generation snapshot compilation for production plugins."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from agent.automation_plugins.catalog import PluginCatalogEntry
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeGenerationSnapshot,
)


_POLICY_PROJECTION_FIELDS = (
    "automation_id",
    "project_generation",
    "mode",
    "project_configuration_version",
    "version",
)
_CONFIG_JSON_FIELDS = (
    "config_json",
    "account_bindings_json",
    "resource_bindings_json",
    "enabled_entrypoints_json",
    "desired_schedule_json",
    "compiled_invocations_json",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _required_sha(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PluginConflictError(f"persisted {field} is not a SHA-256 digest")
    return text


def _required_policy_generation(policy: Mapping[str, Any]) -> int:
    value = policy.get("project_generation")
    if type(value) is not int or value <= 0:
        raise PluginConflictError(
            "project policy is not bound to the desired runtime generation",
            code="PLUGIN_POLICY_GENERATION_MISMATCH",
        )
    return value


def _closed_policy_projection(
    policy: Mapping[str, Any],
    *,
    automation_id: str,
    generation: int,
    project_configuration_version: int,
) -> dict[str, Any]:
    projection = {field: policy.get(field) for field in _POLICY_PROJECTION_FIELDS}
    policy_generation = _required_policy_generation(policy)
    if (
        str(projection["automation_id"] or "") != automation_id
        or policy_generation != generation
        or not str(projection["mode"] or "")
        or type(projection["project_configuration_version"]) is not int
        or int(projection["project_configuration_version"])
        != project_configuration_version
        or type(projection["version"]) is not int
        or int(projection["version"]) <= 0
    ):
        raise PluginConflictError(
            "project policy is not bound to the desired runtime generation",
            code="PLUGIN_POLICY_GENERATION_MISMATCH",
        )
    return projection


def _closed_config_row(
    row: Mapping[str, Any],
    *,
    automation_id: str,
) -> dict[str, Any]:
    if str(row.get("automation_id") or "") != automation_id:
        raise PluginConflictError("project configuration identity changed")
    result: dict[str, Any] = {}
    for field in _CONFIG_JSON_FIELDS:
        value = row.get(field)
        expected_type = list if field == "enabled_entrypoints_json" else Mapping
        if not isinstance(value, expected_type):
            raise PluginConflictError(f"project configuration field is invalid: {field}")
        result[field] = copy.deepcopy(
            list(value) if isinstance(value, list) else dict(value)
        )
    version = row.get("config_version")
    if type(version) is not int or version <= 0 or row.get("configured") not in {True, 1}:
        raise PluginConflictError(
            "project configuration is incomplete",
            code="PLUGIN_PROJECT_NOT_CONFIGURED",
        )
    result["config_version"] = version
    result["device_id"] = str(row.get("device_id") or "").strip() or None
    hash_pairs = (
        ("config_json", "config_sha256"),
        ("account_bindings_json", "account_bindings_sha256"),
        ("resource_bindings_json", "resource_bindings_sha256"),
        ("enabled_entrypoints_json", "enabled_entrypoints_sha256"),
        ("desired_schedule_json", "desired_schedule_sha256"),
        ("compiled_invocations_json", "compiled_invocations_sha256"),
    )
    for value_field, hash_field in hash_pairs:
        persisted = _required_sha(row.get(hash_field), hash_field)
        if _digest(result[value_field]) != persisted:
            raise PluginConflictError(
                f"project configuration digest changed: {value_field}"
            )
        result[hash_field] = persisted
    result["device_binding_sha256"] = _required_sha(
        row.get("device_binding_sha256"),
        "device_binding_sha256",
    )
    return result


def build_runtime_generation_snapshot(
    entry: PluginCatalogEntry,
    *,
    desired_config_row: Mapping[str, Any],
    policy_row: Mapping[str, Any],
    generation: int,
    core_catalog: Any,
    created_at: datetime | None = None,
) -> RuntimeGenerationSnapshot:
    """Compile one closed non-secret generation from desired core-owned state."""

    if type(generation) is not int or generation <= 0:
        raise PluginConflictError("runtime generation must be a positive integer")
    is_v2 = entry.runtime_model == PluginRuntimeModel.SERVICE_V2.value
    allowed_trust_sources = (
        {
            PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
            PluginTrustSource.BUILTIN_BUNDLE.value,
        }
        if is_v2
        else {
            PluginTrustSource.ED25519_FIRST_PARTY.value,
            PluginTrustSource.ED25519_UPLOAD.value,
        }
    )
    if entry.trust_source not in allowed_trust_sources:
        raise PluginConflictError(
            "plugin trust source does not match its runtime model",
            code="PLUGIN_TRUST_SOURCE_INVALID",
        )
    if entry.runtime.get("kind") != "python_subprocess":
        raise PluginConflictError(
            "production generations require a subprocess action payload",
            code="PLUGIN_RUNTIME_FORBIDDEN",
        )
    desired = _closed_config_row(
        desired_config_row,
        automation_id=entry.automation_id,
    )
    entrypoints = tuple(str(item) for item in desired["enabled_entrypoints_json"])
    if (
        len(entrypoints) != len(set(entrypoints))
        or not set(entrypoints) <= set(entry.allowed_entrypoints)
        or set(desired["compiled_invocations_json"]) != set(entrypoints)
    ):
        raise PluginConflictError("desired entrypoint route is not closed")
    if desired["device_id"] is not None:
        # Worker snapshots require the exact paired key fingerprint and
        # capability revision.  A bare mutable device_id is not enough.
        raise PluginConflictError(
            "worker generation needs a closed immutable device descriptor",
            code="PLUGIN_DEVICE_SNAPSHOT_UNAVAILABLE",
        )
    if desired["device_binding_sha256"] != _digest(None):
        raise PluginConflictError("server plugin device binding digest is invalid")

    anchor = copy.deepcopy(dict(entry.governance_anchor))
    if not is_v2:
        core_capability = core_catalog.get_capability(str(anchor.get("name") or ""))
        if not isinstance(core_capability, Mapping):
            raise PluginConflictError("governed core capability disappeared")
        if any(
            key not in core_capability
            or canonical_json_bytes(core_capability[key])
            != canonical_json_bytes(value)
            for key, value in anchor.items()
        ):
            raise PluginConflictError(
                "governed core capability changed beneath the signed action",
                code="PLUGIN_GOVERNANCE_ANCHOR_MISMATCH",
            )
    if _digest(anchor) != entry.governance_anchor_sha256:
        raise PluginConflictError("signed governance anchor digest is invalid")

    project_config = desired["config_json"]
    account_bindings = desired["account_bindings_json"]
    resource_bindings = desired["resource_bindings_json"]
    schedule = desired["desired_schedule_json"]
    compiled_invocations = desired["compiled_invocations_json"]
    declared_resource_roles = {
        str(item.get("role") or ""): item
        for item in entry.resource_roles
        if isinstance(item, Mapping)
    }
    if (
        "" in declared_resource_roles
        or not set(resource_bindings) <= set(declared_resource_roles)
        or any(
            role.get("required") is True and role_name not in resource_bindings
            for role_name, role in declared_resource_roles.items()
        )
    ):
        raise PluginConflictError(
            "desired managed-resource bindings are incomplete",
            code="PLUGIN_RESOURCE_BINDING_INVALID",
        )
    webhook_enabled = (
        any(
            isinstance(entry.invocation_contracts.get(source), Mapping)
            and entry.invocation_contracts[source].get("contribution_kind") == "webhook"
            for source in entrypoints
        )
        if is_v2
        else "webhook" in entrypoints
    )
    if (
        not is_v2
        and webhook_enabled
        and (
            "webhook_route" not in declared_resource_roles
            or "webhook_route" not in resource_bindings
        )
    ):
        raise PluginConflictError(
            "an enabled Webhook entrypoint requires an explicit route resource",
            code="PLUGIN_WEBHOOK_ROUTE_REQUIRED",
        )
    action_contract = copy.deepcopy(dict(entry.tool_contract))
    runtime_descriptor = {
        "install_metadata": {
            **copy.deepcopy(dict(entry.install_metadata)),
            "install_root": entry.install_root,
        },
        "runtime": copy.deepcopy(dict(entry.runtime)),
        # Generation identity follows the exact signed manifest bytes.  Older
        # schema-v1 packages omitted broker ``effect``; Catalog keeps a
        # conservative normalized view for execution separately.
        "runtime_permissions": copy.deepcopy(dict(entry.signed_runtime_permissions)),
        "account_roles": [copy.deepcopy(dict(item)) for item in entry.account_roles],
        "resource_roles": [copy.deepcopy(dict(item)) for item in entry.resource_roles],
    }
    if not entry.install_root or not runtime_descriptor["install_metadata"].get(
        "python_relative"
    ):
        raise PluginConflictError(
            "plugin version is not materialized",
            code="PLUGIN_NOT_MATERIALIZED",
        )
    policy = _closed_policy_projection(
        policy_row,
        automation_id=entry.automation_id,
        generation=generation,
        project_configuration_version=int(desired["config_version"]),
    )
    execution_metadata = {
        "project_config_version": int(desired["config_version"]),
        "project_config": project_config,
        "account_bindings": account_bindings,
        "resource_bindings": resource_bindings,
        "device_binding": None,
        "schedule": schedule,
        "compiled_invocations": compiled_invocations,
        "runtime_descriptor": runtime_descriptor,
        "action_contract": action_contract,
        "governance_anchor": anchor,
    }
    if is_v2:
        execution_metadata.update(
            {
                "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
                "plugin_api": entry.plugin_api,
                "service_contracts": copy.deepcopy(dict(entry.service_contracts)),
                "contributions": copy.deepcopy(dict(entry.contributions)),
                "storage_contract": copy.deepcopy(dict(entry.storage_contract)),
            }
        )
    observed_at = created_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise PluginConflictError("runtime generation timestamp must be timezone-aware")
    return RuntimeGenerationSnapshot(
        automation_id=entry.automation_id,
        generation=generation,
        plugin_id=entry.plugin_id,
        plugin_version=entry.installed_version,
        package_sha256=_required_sha(entry.package_sha256, "package_sha256"),
        manifest_sha256=_required_sha(entry.manifest_sha256, "manifest_sha256"),
        trust_source=PluginTrustSource(entry.trust_source),
        project_config_sha256=_digest(project_config),
        account_bindings_sha256=_digest(account_bindings),
        resource_bindings_sha256=_digest(resource_bindings),
        device_binding_sha256=_digest(None),
        schedule_sha256=_digest(schedule),
        # The signed governance projection is the exact core registry slice
        # used by this action; unrelated tools do not invalidate a generation.
        core_registry_sha256=_digest(anchor),
        tool_contract_sha256=_digest(action_contract),
        invocation_contracts_sha256=_digest(dict(entry.invocation_contracts)),
        compiled_invocations_sha256=_digest(compiled_invocations),
        runtime_descriptor_sha256=_digest(runtime_descriptor),
        governance_anchor_sha256=_digest(anchor),
        policy_contract_sha256=_digest(policy),
        enabled_entrypoints=entrypoints,
        execution_metadata=execution_metadata,
        created_at=observed_at.astimezone(timezone.utc),
        runtime_model=PluginRuntimeModel(entry.runtime_model),
        plugin_api=entry.plugin_api,
    )


__all__ = ["build_runtime_generation_snapshot"]
