"""Authority for restoring a package actually committed by this instance."""

from __future__ import annotations

from typing import Any, Mapping

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.models import PluginVersionRecord
from agent.automation_plugins.runtime_repository import snapshot_from_row


def require_committed_history(
    repository: Any,
    automation_id: str,
    project: Mapping[str, Any],
    version: PluginVersionRecord,
) -> None:
    committed = project.get("committed_generation")
    if type(committed) is not int or committed <= 0:
        raise PluginConflictError(
            "rollback requires a committed project history",
            code="PLUGIN_ROLLBACK_HISTORY_REQUIRED",
        )
    history = repository.lock_project_generation_history(
        automation_id, committed_generation=committed,
    )
    matched = False
    for item in history:
        generation = item.get("generation")
        if (
            type(generation) is not int or not 0 < generation <= committed
            or item.get("state") not in {"COMMITTED", "DISPOSED"}
        ):
            continue
        row = repository.get_generation_row(
            automation_id, generation, for_update=True,
        )
        if row is None or row.get("committed_at") is None:
            continue  # A disposed preparation that never committed is not history.
        try:
            snapshot = snapshot_from_row(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise PluginConflictError(
                "rollback history contains an invalid immutable snapshot",
                code="PLUGIN_ROLLBACK_HISTORY_INVALID",
            ) from exc
        matched |= (
            snapshot.automation_id == automation_id
            and snapshot.plugin_id == version.plugin_id
            and snapshot.plugin_version == version.version
            and snapshot.package_sha256 == version.package_sha256
            and snapshot.manifest_sha256 == version.manifest_sha256
            and snapshot.trust_source == version.trust_source
            and snapshot.runtime_model == version.runtime_model
        )
    if not matched:
        raise PluginConflictError(
            "rollback target must be the exact signed package previously committed by this instance",
            code="PLUGIN_ROLLBACK_HISTORY_REQUIRED",
        )
