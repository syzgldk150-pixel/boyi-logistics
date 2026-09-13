"""Release metadata after every original business instance has migrated to V2.

No old executable is needed to start a fully migrated host.  Historical rows
remain intact; this index never authorizes an unfinished migration to retire.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from agent.automation_plugins.errors import PluginPackageError
from agent.automation_plugins.first_party import (
    FirstPartyReleasePreflight,
    release_first_party_automation_ids,
)
from agent.automation_plugins.manifest import canonical_json_bytes


def retired_release_index(release_sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{7,64}", release_sha):
        raise PluginPackageError("retired plugin release SHA is invalid")
    return {
        "schema_version": 2,
        "release_sha": release_sha,
        "runtime_model": "SERVICE_V2",
        "plugins": {},
        "retired_instance_ids": sorted(release_first_party_automation_ids()),
    }


def read_retired_release(root: Path, release_sha: str) -> FirstPartyReleasePreflight | None:
    root = Path(root)
    path = root / "release-index.json"
    if root.is_symlink() or not root.is_dir() or path.is_symlink() or not path.is_file():
        raise PluginPackageError("plugin release index is missing or unsafe")
    if path.stat().st_size > 1024 * 1024:
        raise PluginPackageError("plugin release index is too large")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise PluginPackageError("plugin release index is invalid") from exc
    if isinstance(value, dict) and value.get("schema_version") == 1:
        return None  # Existing signed-release validator owns the V1 format.
    expected = retired_release_index(release_sha)
    encoded = canonical_json_bytes(expected)
    if value != expected or raw not in (encoded, encoded + b"\n"):
        raise PluginPackageError("retired plugin release scope or SHA is invalid")
    if {item.name for item in root.iterdir()} != {"release-index.json"}:
        raise PluginPackageError("retired release must contain no legacy packages")
    return FirstPartyReleasePreflight(
        release_sha=release_sha, package_count=0,
        instance_count=len(expected["retired_instance_ids"]),
        contracts_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def require_completed_migrations(orchestration_repository) -> None:
    """Check the authoritative database before accepting a package-free release."""
    with orchestration_repository.unit_of_work() as uow:
        repository = uow.automation_plugins
        for identity in sorted(release_first_party_automation_ids()):
            pair = repository.get_authoritative_plugin_migration_pair_for_automation(identity, for_update=False)
            if (not pair or pair.get("source_automation_id") != identity
                    or pair.get("state") != "COMPLETED" or pair.get("rolled_back_at") is not None):
                raise PluginPackageError("first-party migration is not completed")
            source = repository.get_project(identity, for_update=False)
            target = repository.get_project(pair["target_automation_id"], for_update=False)
            if source is not None and (source.get("enabled") or source.get("state") != "DISABLED"):
                raise PluginPackageError("retired first-party source is still enabled")
            # An explicitly uninstalled V2 target must not resurrect V1 or
            # prevent the rest of the host from starting.
            if target is not None:
                version = repository.get_version(target["plugin_id"], target["plugin_version"])
                if not version or version.get("runtime_model") != "SERVICE_V2":
                    raise PluginPackageError("migrated target is not a V2 plugin")
