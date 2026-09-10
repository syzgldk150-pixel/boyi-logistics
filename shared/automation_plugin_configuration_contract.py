"""Closed configuration witnesses and exact first-party upgrade targets."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.orchestration_repository_support import (
    ConcurrentUpdateError, OrchestrationPersistenceError, _required_text, _row_dict,
)

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
