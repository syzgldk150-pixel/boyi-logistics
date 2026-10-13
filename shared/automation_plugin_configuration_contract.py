"""Closed configuration witnesses and exact upgrade targets."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from shared.orchestration_repository_support import (
    ConcurrentUpdateError, OrchestrationPersistenceError, _required_text, _row_dict,
)

from shared.plugin_json import plugin_json_digest

PLUGIN_RUNTIME_MODELS = frozenset({"ACTION_V1", "SERVICE_V2"})


def configuration_contract_witness(value: Mapping[str, Any]) -> dict[str, Any]:
    """Close the catalog proof, compared to immutable persisted version digests."""
    if not isinstance(value, Mapping) or set(value) != {
        "runtime_model", "allowed_entrypoints", "invocation_contracts", "scheduling",
    }:
        raise ValueError("project configuration contract witness is invalid")
    runtime_model = _required_text(value.get("runtime_model"), "runtime_model")
    if runtime_model not in PLUGIN_RUNTIME_MODELS:
        raise ValueError("project configuration runtime_model is invalid")
    raw_entrypoints = value.get("allowed_entrypoints")
    if not isinstance(raw_entrypoints, list):
        raise ValueError("project configuration allowed_entrypoints are invalid")
    allowed_entrypoints = [_required_text(item, "allowed_entrypoint") for item in raw_entrypoints]
    if len(allowed_entrypoints) != len(set(allowed_entrypoints)):
        raise ValueError("project configuration allowed_entrypoints are duplicated")
    raw_contracts = value.get("invocation_contracts")
    if not isinstance(raw_contracts, Mapping) or set(raw_contracts) != set(allowed_entrypoints):
        raise ValueError("project configuration invocation contracts are invalid")
    invocation_contracts: dict[str, dict[str, Any]] = {}
    for entrypoint in allowed_entrypoints:
        contract = raw_contracts.get(entrypoint)
        if not isinstance(contract, Mapping):
            raise ValueError("project configuration invocation contract is invalid")
        invocation_contracts[entrypoint] = dict(contract)
    scheduling = value.get("scheduling")
    if not isinstance(scheduling, Mapping):
        raise ValueError("project configuration scheduling witness is invalid")
    return {
        "runtime_model": runtime_model,
        "allowed_entrypoints": allowed_entrypoints,
        "invocation_contracts": invocation_contracts,
        "scheduling": dict(scheduling),
    }


def lock_upgrade_configuration_contract(cursor, project: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    """Read one immutable target while preserving the current project pointers."""
    if project.get("plugin_version") != target["from_version"]:
        raise ConcurrentUpdateError("automation project changed before upgrade configuration")
    cursor.execute(
        """
        SELECT manifest_json, runtime_model, allowed_entrypoints_sha256,
               invocation_contracts_sha256, scheduling_sha256, package_sha256
        FROM automation_plugin_versions
        WHERE plugin_id=%s AND version=%s FOR UPDATE
        """,
        (project["plugin_id"], target["to_version"]),
    )
    target_contract = _row_dict(cursor, cursor.fetchone())
    if target_contract is None or target_contract.get("package_sha256") != target["package_sha256"]:
        raise OrchestrationPersistenceError("immutable upgrade configuration target is not registered")
    # Only the local validation view changes. Version and committed generation
    # switch through the existing audited upgrade path after preparation.
    return {**project, **target_contract}


def require_configuration_contract(project: Mapping[str, Any], witness: Mapping[str, Any]) -> None:
    """Bind the already-compiled witness to the immutable installed version."""
    if str(project.get("runtime_model") or "ACTION_V1") != witness["runtime_model"]:
        raise OrchestrationPersistenceError("persisted plugin runtime model does not match contract witness")
    if any(project.get(f"{field}_sha256") != plugin_json_digest(witness[field])
           for field in ("allowed_entrypoints", "invocation_contracts", "scheduling")):
        raise OrchestrationPersistenceError("persisted plugin contract does not match configuration witness")


def optional_console_input_additions(
    previous: Mapping[str, Any], target: Mapping[str, Any],
) -> dict[str, frozenset[str]]:
    """Accept only additive optional inputs on existing Console contributions.

    Resolver identities come from the immutable, Host-compiled witnesses, not
    from a prefix allowlist supplied by an upgrade caller.
    """
    old = configuration_contract_witness(previous)
    new = configuration_contract_witness(target)
    if (old["runtime_model"] != "SERVICE_V2" or new["runtime_model"] != "SERVICE_V2"
            or plugin_json_digest(old["allowed_entrypoints"]) != plugin_json_digest(new["allowed_entrypoints"])
            or plugin_json_digest(old["scheduling"]) != plugin_json_digest(new["scheduling"])):
        raise ValueError("optional Console upgrade changes the project contract")
    additions = {}
    for source, before in old["invocation_contracts"].items():
        after = copy.deepcopy(new["invocation_contracts"][source])
        old_schema, new_schema = before["input_schema"], after["input_schema"]
        fields = frozenset(set(new_schema["properties"]) - set(old_schema["properties"]))
        if fields:
            if (before.get("contribution_kind") != "console"
                    or fields.intersection(new_schema["required"])
                    or fields.intersection(before["dynamic_resolvers"])
                    or fields.intersection(after["argument_template"])):
                raise ValueError("upgrade input is not a new optional Console input")
            for field in fields:
                if not after["dynamic_resolvers"].pop(field, None):
                    raise ValueError("upgrade input has no compiled resolver")
                del new_schema["properties"][field]
            additions[source] = fields
        if plugin_json_digest(after) != plugin_json_digest(before):
            raise ValueError("optional Console upgrade changes an existing invocation contract")
    return additions


def validate_optional_console_upgrade_configuration(
    previous: Mapping[str, Any], target: Mapping[str, Any],
    current: Mapping[str, Any], request: Mapping[str, Any],
) -> None:
    """Keep administrator settings and existing materialization byte-equivalent."""
    additions = optional_console_input_additions(previous, target)
    for requested, saved in (
        ("config", "config_json"), ("account_bindings", "account_bindings_json"),
        ("resource_bindings", "resource_bindings_json"), ("schedule", "desired_schedule_json"),
        ("device_id", "device_id"),
    ):
        if plugin_json_digest(request[requested]) != plugin_json_digest(current[saved]):
            raise ValueError("optional Console upgrade must preserve saved settings")
    if sorted(request["enabled_entrypoints"]) != sorted(current["enabled_entrypoints_json"]):
        raise ValueError("optional Console upgrade must preserve enabled entrypoints")
    before, after = current["compiled_invocations_json"], request["compiled_invocations"]
    if set(before) != set(after) or not set(before).intersection(additions):
        raise ValueError("optional Console upgrade has no enabled input addition")
    for source, original in before.items():
        projected = copy.deepcopy(after[source])
        for field in additions.get(source, ()):
            resolver = projected["dynamic_resolvers"].pop(field, None)
            if resolver != target["invocation_contracts"][source]["dynamic_resolvers"][field]:
                raise ValueError("optional Console resolver differs from the target contract")
        if plugin_json_digest(projected) != plugin_json_digest(original):
            raise ValueError("optional Console upgrade changes the saved invocation template")
