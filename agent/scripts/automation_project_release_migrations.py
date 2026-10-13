"""Release scope after a durable V1-to-V2 ownership transfer.

The original 018 marker remains immutable. Only its still-owned V1 instances
need the legacy runtime/policy checks; installed V2 instances use the normal
signed release health and generation checks.
"""
from __future__ import annotations

import hashlib
import json


def read_project_schedule_rows(cursor):
    """Include disabled V2 tasks and projects missing their physical tasks."""
    cursor.execute("""
        SELECT task.id AS task_id, project.automation_id,
               task.automation_generation, task.tool_name, task.cron_expression, task.enabled,
               task.configuration_version, config.config_version,
               config.desired_schedule_json, config.desired_schedule_sha256,
               project.committed_generation, project.target_generation,
               project.enabled AS project_enabled, project.state AS project_state,
               project.reconcile_state, policy.mode AS policy_mode,
               policy.project_generation AS policy_project_generation,
               generation.state AS generation_state, generation.error_code AS generation_error_code,
               target_generation.state AS target_generation_state,
               target_generation.base_committed_generation AS target_base_generation,
               (SELECT CAST( COALESCE(MAX(history.generation), 0) AS UNSIGNED)
                FROM automation_project_generations AS history
                WHERE BINARY history.automation_id=BINARY project.automation_id) AS max_generation,
               (SELECT COUNT(*) FROM automation_project_generations AS history
                WHERE BINARY history.automation_id=BINARY project.automation_id
                  AND history.generation<>project.committed_generation
                  AND history.generation<>project.target_generation AND history.state<>'DISPOSED'
                  AND NOT (history.generation<project.committed_generation
                    AND history.state='BLOCKED' AND history.error_code='WRITE_OUTCOME_UNKNOWN'
                    AND EXISTS (SELECT 1 FROM automation_project_generation_leases AS archive_lease
                      WHERE BINARY archive_lease.automation_id=BINARY history.automation_id
                        AND archive_lease.generation=history.generation
                        AND archive_lease.outcome='WRITE_OUTCOME_UNKNOWN'))) AS unsafe_non_disposed_other_count,
               (SELECT COUNT(*) FROM automation_project_generation_leases AS lease
                WHERE BINARY lease.automation_id=BINARY project.automation_id
                  AND lease.generation=project.committed_generation
                  AND lease.outcome IN ('RUNNING', 'VERIFYING')) AS active_current_lease_count,
               (SELECT COUNT(*) FROM automation_project_generation_leases AS lease
                WHERE BINARY lease.automation_id=BINARY project.automation_id
                  AND lease.generation=project.committed_generation
                  AND lease.outcome='WRITE_OUTCOME_UNKNOWN') AS unknown_write_count,
               generation.snapshot_json
        FROM automation_projects AS project
        LEFT JOIN scheduled_tasks AS task ON BINARY project.automation_id=BINARY task.automation_id
        LEFT JOIN automation_project_configs AS config ON BINARY config.automation_id=BINARY project.automation_id
        INNER JOIN automation_project_policies AS policy ON BINARY policy.automation_id=BINARY project.automation_id
        INNER JOIN automation_project_generations AS generation
          ON BINARY generation.automation_id=BINARY project.automation_id
         AND generation.generation=project.committed_generation
        LEFT JOIN automation_project_generations AS target_generation
          ON BINARY target_generation.automation_id=BINARY project.automation_id
         AND target_generation.generation=project.target_generation
        WHERE generation.runtime_model='SERVICE_V2'
           OR (task.enabled=TRUE AND task.tool_name LIKE 'automation.%.run')
        ORDER BY BINARY task.id
        """)
    return list(cursor.fetchall())


def validate_service_v2_schedule_projections(rows, contract, *, normalize_schedule,
                                             staged_runtime, error_class):
    """Check current host times against the complete committed V2 task set."""
    grouped = {}
    for row, snapshot in rows:
        grouped.setdefault(row["automation_id"], []).append((row, snapshot))
    for project_rows in grouped.values():
        project, snapshot = project_rows[0]
        # An interrupted configuration change intentionally retains the prior
        # committed projection while current configuration describes its target.
        if staged_runtime(project):
            continue
        metadata = snapshot.get("execution_metadata") or {}
        desired = project.get("desired_schedule_json")
        if isinstance(desired, (str, bytes)):
            try:
                desired = json.loads(desired)
            except (ValueError, TypeError) as exc:
                raise error_class("PROJECT_SCHEDULE_CONFIG_INVALID") from exc
        expressions, _enabled = normalize_schedule(desired)
        normalize_schedule(metadata.get("schedule"))
        digest = hashlib.sha256(json.dumps(desired, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
        if (project.get("desired_schedule_sha256") != digest
                or contract["schedule_generation_shape"](desired)
                != contract["schedule_generation_shape"](metadata.get("schedule"))
                or project.get("config_version") != metadata.get("project_config_version")):
            raise error_class("PROJECT_SCHEDULE_CONFIG_INVALID")
        tasks = [row for row, _snapshot in project_rows if row.get("task_id") is not None]
        if tuple(sorted(expressions)) != tuple(sorted(str(row.get("cron_expression") or "") for row in tasks)):
            raise error_class("PROJECT_SCHEDULE_ROWS_MISMATCH")
        for row in tasks:
            if (row.get("automation_generation") != project.get("committed_generation")
                    or row.get("configuration_version") != project.get("config_version")
                    or row.get("tool_name") != f"automation.{project['automation_id']}.run"):
                raise error_class("AUTOMATION_PROJECT_TASK_RUNTIME_MISMATCH")


def validate_service_v2_scheduler_contract(snapshot, contract, *, cron_expression, error_class):
    """Use the runtime's unique scheduler binding for V2 release windows."""
    enabled_entrypoints = snapshot.get("enabled_entrypoints")
    if not isinstance(enabled_entrypoints, list) or any(
        type(item) is not str for item in enabled_entrypoints
    ):
        raise error_class("PROJECT_SCHEDULE_CONTRACT_INVALID")
    execution_metadata = snapshot["execution_metadata"]
    try:
        entrypoint, enabled = contract["scheduler_contribution_binding"](
            snapshot=snapshot,
            execution_metadata=execution_metadata,
            enabled_entrypoints=enabled_entrypoints,
            schedule_expressions=(cron_expression,),
        )
    except (ValueError, contract["persistence_error_class"]) as exc:
        raise error_class("PROJECT_SCHEDULE_CONTRACT_INVALID") from exc
    if not enabled or entrypoint not in execution_metadata["compiled_invocations"]:
        raise error_class("PROJECT_SCHEDULE_CONTRACT_INVALID")


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
