"""Preserve reviewed Action-v1 preview contracts through signed ZIP updates."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import re
from typing import Any

from shared.automation_project_authorization import canonical_sha256


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SIGNED_SOURCES = frozenset({"ed25519_first_party", "ed25519_upload"})
_CONTRACT_FIELDS = (
    "governance_anchor", "tool_contract", "invocation_contracts",
    "runtime_permissions", "account_roles", "resource_roles",
)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _source(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


@lru_cache(maxsize=3)
def _reviewed_contract(plugin_id: str) -> str:
    # Reuse the actual core-owned contract generator; do not duplicate its
    # governance, preview schemas, dynamic resolvers, or Broker allowlist.
    from agent.automation_plugins.first_party import resolve_first_party_manifests
    from agent.tool_registry import ToolRegistry

    manifest = resolve_first_party_manifests(
        ToolRegistry(), _plugin_ids=frozenset({plugin_id}),
    )[plugin_id].to_mapping()
    return canonical_sha256({name: manifest[name] for name in _CONTRACT_FIELDS})


def signed_preview_contract_compatible(entry: Any) -> bool:
    """Validate maintenance compatibility after the authoritative catalog read.

    This does not verify uploads or confer first-party trust. Lifecycle verifies
    the signed installation; the catalog validates its immutable generation.
    Here an updated payload/version may keep the canonical preview only when
    all Host-facing contracts remain exactly the reviewed contract.
    """
    source = _source(getattr(entry, "trust_source", None))
    snapshot = getattr(entry, "committed_snapshot", None)
    generation = getattr(entry, "committed_generation", None)
    plugin_id = str(getattr(entry, "plugin_id", ""))
    if (
        source not in _SIGNED_SOURCES
        or getattr(entry, "runtime_model", "ACTION_V1") != "ACTION_V1"
        or plugin_id not in {
            "sync_scan_codes", "self_pickup_problem_upload", "split_pending_problem_upload",
        }
        or snapshot is None
        or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1
        or generation != getattr(entry, "target_generation", None)
        or generation != getattr(snapshot, "generation", None)
        or _source(getattr(snapshot, "trust_source", None)) != source
    ):
        return False
    for entry_field, snapshot_field in (
        ("automation_id", "automation_id"), ("plugin_id", "plugin_id"),
        ("installed_version", "plugin_version"), ("package_sha256", "package_sha256"),
        ("manifest_sha256", "manifest_sha256"),
    ):
        value = getattr(entry, entry_field, None)
        if not value or value != getattr(snapshot, snapshot_field, None):
            return False
    if any(not _DIGEST.fullmatch(str(getattr(entry, field, "")))
           for field in ("package_sha256", "manifest_sha256")):
        return False
    contract = {
        name: _plain(getattr(entry, "signed_runtime_permissions" if name == "runtime_permissions" else name, None))
        for name in _CONTRACT_FIELDS
    }
    if canonical_sha256(contract) != _reviewed_contract(plugin_id):
        return False
    for field in ("governance_anchor", "tool_contract", "invocation_contracts"):
        if canonical_sha256(contract[field]) != getattr(snapshot, field + "_sha256", None):
            return False
    return canonical_sha256(contract["governance_anchor"]) == getattr(entry, "governance_anchor_sha256", None)
