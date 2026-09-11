"""Release scope after a durable V1-to-V2 ownership transfer.

The original 018 marker remains immutable. Only its still-owned V1 instances
need the legacy runtime/policy checks; installed V2 instances use the normal
signed release health and generation checks.
"""
from __future__ import annotations

import hashlib
import json


def validate_bootstrap_sources(contract, items_by_id, *, error_class):
    """Keep validating the complete immutable 018 receipt after retirement."""
    evidence = contract["bootstrap_evidence"]
    snapshots = {}
    source_task_ids = set()
    source_enabled_count = 0
    for identity in sorted(contract["release_projects"]):
        try:
            source = evidence["validate_automation_project_bootstrap_source_snapshot"](
                items_by_id[identity].get("source_snapshot_json"))
        except evidence["error_class"] as exc:
            raise error_class("AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_INVALID") from exc
        task_ids = {str(task.get("task_id") or "") for task in source.get("scheduled_tasks", [])}
        if source.get("automation_id") != identity or task_ids != contract["templates"][identity]["task_ids"]:
            raise error_class("AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_TASK_SET_MISMATCH")
        source_task_ids.update(task_ids)
        source_enabled_count += sum(task.get("enabled") is True for task in source.get("scheduled_tasks", []))
        snapshots[identity] = source
    if source_task_ids != contract["release_tasks"] or source_enabled_count != 55:
        raise error_class("AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_TASK_SET_MISMATCH")
    return snapshots


def remaining_contract(contract, superseded):
    identities = contract["release_projects"] - set(superseded)
    return {**contract, "release_projects": identities,
            "release_tasks": frozenset(task for identity in identities
                                       for task in contract["templates"][identity]["task_ids"])}


def read_superseded_sources(cursor, contract, *, error_class):
    identities = tuple(sorted(contract["release_projects"]))
    if not identities:
        return frozenset()
    markers = ",".join("%s" for _ in identities)
    cursor.execute(f"""SELECT * FROM automation_plugin_migration_pairs
        WHERE source_automation_id IN ({markers}) OR target_automation_id IN ({markers})
        ORDER BY created_at,migration_pair_id""", (*identities, *identities))
    rows = []
    for raw in cursor.fetchall():
        row = dict(raw)
        snapshot = row["entrypoint_snapshot_json"]
        if isinstance(snapshot, (str, bytes)):
            snapshot = json.loads(snapshot)
        digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        if digest != row["entrypoint_snapshot_sha256"]:
            raise error_class("PLUGIN_MIGRATION_RELEASE_SNAPSHOT_INVALID")
        row["entrypoint_snapshot_json"] = snapshot
        rows.append(row)
    superseded = set()
    for identity in identities:
        candidates = [row for row in rows if identity in (row["source_automation_id"], row["target_automation_id"])]
        try:
            pair = contract["select_migration_pair"](candidates, automation_id=identity)
            replaced = contract["source_is_superseded"](pair, identity)
        except (RuntimeError, ValueError, TypeError) as exc:
            raise error_class("PLUGIN_MIGRATION_RELEASE_OWNERSHIP_INVALID") from exc
        if not replaced:
            continue
        cursor.execute("""SELECT to_state,to_record_version,entrypoint_snapshot_sha256,actor_role
            FROM automation_plugin_migration_pair_events
            WHERE migration_pair_id=%s AND request_id=%s""",
            (pair["migration_pair_id"], pair["last_transition_request_id"]))
        events = cursor.fetchall()
        if len(events) != 1 or any(events[0].get(key) != expected for key, expected in {
            "to_state": pair["state"], "to_record_version": pair["record_version"],
            "entrypoint_snapshot_sha256": pair["entrypoint_snapshot_sha256"], "actor_role": "super_admin",
        }.items()):
            raise error_class("PLUGIN_MIGRATION_RELEASE_EVENT_INVALID")
        cursor.execute("SELECT enabled FROM automation_projects WHERE automation_id=%s", (identity,))
        source = cursor.fetchone()
        if source is not None and source["enabled"] != 0:
            raise error_class("PLUGIN_MIGRATION_SOURCE_STILL_ENABLED")
        cursor.execute("SELECT id FROM scheduled_tasks WHERE automation_id=%s AND enabled=TRUE", (identity,))
        if cursor.fetchall():
            raise error_class("PLUGIN_MIGRATION_SOURCE_SCHEDULE_STILL_ENABLED")
        cursor.execute("SELECT automation_id FROM automation_projects WHERE automation_id=%s", (pair["target_automation_id"],))
        if cursor.fetchone() is None and pair["state"] != "COMPLETED":
            raise error_class("PLUGIN_MIGRATION_TARGET_MISSING")
        superseded.add(identity)
    return frozenset(superseded)
