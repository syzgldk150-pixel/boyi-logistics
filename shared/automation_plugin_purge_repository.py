"""Hard-uninstall purge persistence mixin.

Imported only by ``shared.automation_plugin_repository`` so the public
repository type and method names stay in one place.
"""

from __future__ import annotations

from shared import automation_plugin_repository as _repository

Any = _repository.Any
AutomationPluginPurgeBlocked = _repository.AutomationPluginPurgeBlocked
ConcurrentUpdateError = _repository.ConcurrentUpdateError
IdempotencyConflict = _repository.IdempotencyConflict
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
_decode_row = _repository._decode_row
_json_hash = _repository._json_hash
_json_param = _repository._json_param
_required_text = _repository._required_text
_row_dict = _repository._row_dict
_rows = _repository._rows
_safe_error = _repository._safe_error
_selected_ids = _repository._selected_ids
_sql_placeholders = _repository._sql_placeholders

# An accepted command retains RECEIVED after its run is created. Only commands
# without a run are pending on that status; otherwise the durable run decides.
_CONTROL_PLANE_UNINSTALL_BLOCK_PREDICATE = """(
    (command.status='RECEIVED' AND run.run_id IS NULL)
    OR run.status NOT IN ('COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED')
)"""


def _delete_disposed_execution_references(cursor: Any, automation_id: str) -> None:
    cursor.execute("""SELECT generation FROM automation_project_generations
        WHERE automation_id=%s AND state<>'DISPOSED' FOR UPDATE""", (automation_id,))
    if _rows(cursor):
        raise AutomationPluginPurgeBlocked("runtime generation effects were not fully disposed")
    cursor.execute("""SELECT lease_id FROM automation_project_generation_leases
        WHERE automation_id=%s AND outcome IN ('RUNNING','VERIFYING','WRITE_OUTCOME_UNKNOWN') FOR UPDATE""", (automation_id,))
    if _rows(cursor):
        raise AutomationPluginPurgeBlocked("active or uncertain runtime lease blocks uninstall cleanup")
    cursor.execute("""SELECT receipt_id FROM automation_write_attempt_receipts
        WHERE automation_id=%s AND outcome IN ('STARTED','WRITE_OUTCOME_UNKNOWN') FOR UPDATE""", (automation_id,))
    if _rows(cursor):
        raise AutomationPluginPurgeBlocked("unresolved write receipt blocks uninstall cleanup")
    # Migration 025 receipts reference leases and runs; migration 019 leases
    # reference runs. Remove only resolved control-plane children in that order.
    cursor.execute("DELETE FROM automation_write_attempt_receipts WHERE automation_id=%s", (automation_id,))
    cursor.execute("DELETE FROM automation_project_generation_leases WHERE automation_id=%s", (automation_id,))


class AutomationPluginPurgeRepositoryMixin:
    """Uninstall journal, execution blocks and exact application-state deletion."""

    def prepare_instance_purge(
        self,
        automation_id: str,
        *,
        purge_id: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
        cleanup_scope: str,
    ) -> dict[str, Any]:
        scope = str(cleanup_scope or "").upper()
        if scope not in {"INSTANCE", "PACKAGE"}:
            raise ValueError("cleanup_scope must be INSTANCE or PACKAGE")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _required_text(request_id, "request_id"),
                ),
            )
            existing = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
        if existing is not None:
            if (
                str(existing.get("purge_id") or "") != str(purge_id or "")
                or str(existing.get("cleanup_scope") or "") != scope
                or str(existing.get("actor_id") or "")
                != _required_text(actor_id, "actor_id")
                or str(existing.get("actor_role") or "")
                != _required_text(actor_role, "actor_role")
            ):
                raise IdempotencyConflict("purge request was reused with different input")
            return existing
        project = self.get_project(automation_id, for_update=True)
        if project is None:
            raise OrchestrationPersistenceError("automation project is not installed")
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT command.command_id, run.run_id
                FROM agent_commands AS command
                LEFT JOIN agent_runs AS run ON run.command_id=command.command_id
                WHERE command.automation_id=%s
                  AND {_CONTROL_PLANE_UNINSTALL_BLOCK_PREDICATE}
                FOR UPDATE
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            run_blocked = _rows(cursor)
            cursor.execute(
                """
                SELECT lease_id, outcome
                FROM automation_project_generation_leases
                WHERE automation_id=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            lease_blocked = _rows(cursor)
            if run_blocked or lease_blocked:
                raise AutomationPluginPurgeBlocked(
                    "non-terminal or outcome-unknown execution blocks hard uninstall"
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS reference_count FROM automation_projects
                WHERE plugin_id=%s AND plugin_version=%s AND automation_id<>%s
                """,
                (
                    project["plugin_id"],
                    project["plugin_version"],
                    project["automation_id"],
                ),
            )
            references = _row_dict(cursor, cursor.fetchone()) or {}
            delete_shared = scope == "PACKAGE" and int(
                references.get("reference_count") or 0
            ) == 0
            version = self.get_version(
                str(project["plugin_id"]),
                str(project["plugin_version"]),
                for_update=True,
            )
            if version is None:
                raise OrchestrationPersistenceError("automation plugin version disappeared")
            instance_snapshot = {
                "automation_id": str(project["automation_id"]),
                "display_name": str(project.get("display_name") or ""),
                "plugin_id": str(project["plugin_id"]),
                "plugin_version": str(project["plugin_version"]),
                "state": str(project.get("state") or ""),
                "enabled": bool(project.get("enabled")),
                "record_version": int(project.get("record_version") or 0),
                "target_generation": int(project.get("target_generation") or 0),
                "committed_generation": (
                    int(project["committed_generation"])
                    if project.get("committed_generation") is not None
                    else None
                ),
            }
            cursor.execute(
                """
                INSERT INTO automation_plugin_purge_journal (
                    purge_id, automation_id, plugin_id, request_id,
                    plugin_version, package_sha256, cleanup_scope,
                    delete_shared_package, phase, cleanup_devices_json,
                    cleanup_devices_sha256, instance_snapshot_json,
                    instance_snapshot_sha256, actor_id, actor_role
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 'PREPARED',
                    %s, %s, %s, %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE purge_id=purge_id
                """,
                (
                    _required_text(purge_id, "purge_id"),
                    project["automation_id"],
                    project["plugin_id"],
                    _required_text(request_id, "request_id"),
                    project["plugin_version"],
                    version["package_sha256"],
                    scope,
                    delete_shared,
                    _json_param([], []),
                    _json_hash([]),
                    _json_param(instance_snapshot, {}),
                    _json_hash(instance_snapshot),
                    _required_text(actor_id, "actor_id"),
                    _required_text(actor_role, "actor_role"),
                ),
            )
            cursor.execute(
                """
                UPDATE automation_projects
                SET enabled=FALSE, state='UNINSTALLING',
                    reconcile_state='DRAINING',
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s
                """,
                (project["automation_id"],),
            )
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='DRAINING', draining_at=COALESCE(draining_at, NOW(6)),
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s
                  AND state IN (
                      'TARGET', 'PREPARING', 'WAITING_COEFFECTS', 'PREPARED',
                      'COMMITTED', 'FAILED'
                  )
                """,
                (project["automation_id"],),
            )
            cursor.execute(
                "UPDATE scheduled_tasks SET enabled=FALSE WHERE automation_id=%s",
                (project["automation_id"],),
            )
            cursor.execute(
                """
                SELECT * FROM automation_project_policies
                WHERE automation_id=%s FOR UPDATE
                """,
                (project["automation_id"],),
            )
            policy = _row_dict(cursor, cursor.fetchone())
            if policy is not None:
                cursor.execute(
                    """
                    INSERT INTO automation_project_policy_events (
                        automation_id, request_id, from_mode, to_mode,
                        contract_hash, contract_snapshot_json, tool_contract_hash,
                        plugin_contract_hash, project_configuration_version,
                        actor_id, actor_role, reason, correlation_id
                    ) VALUES (
                        %s, %s, %s, 'REQUIRE_EACH_RUN', %s, %s, %s, %s,
                        %s, %s, %s, 'PLUGIN_UNINSTALL', %s
                    )
                    """,
                    (
                        project["automation_id"],
                        f"purge:{request_id}",
                        policy.get("mode"),
                        policy.get("contract_hash"),
                        policy.get("contract_snapshot_json"),
                        policy.get("tool_contract_hash"),
                        policy.get("plugin_contract_hash"),
                        policy.get("project_configuration_version"),
                        _required_text(actor_id, "actor_id"),
                        _required_text(actor_role, "actor_role"),
                        _required_text(purge_id, "purge_id"),
                    ),
                )
            cursor.execute(
                """
                UPDATE automation_project_policies
                SET mode='REQUIRE_EACH_RUN', contract_hash=NULL,
                    contract_snapshot_json=NULL, tool_contract_hash=NULL,
                    plugin_contract_hash=NULL, version=version+1, updated_at=NOW(6)
                WHERE automation_id=%s
                """,
                (project["automation_id"],),
            )
            event_metadata = {
                "purge_id": str(purge_id),
                "cleanup_scope": scope,
                "delete_shared_package": delete_shared,
            }
            cursor.execute(
                """
                INSERT INTO automation_project_events (
                    automation_id, request_id, event_type, from_state, to_state,
                    metadata_json, metadata_sha256, actor_id, actor_role
                ) VALUES (%s, %s, 'UNINSTALL_PREPARED', %s, 'UNINSTALLING',
                          %s, %s, %s, %s)
                """,
                (
                    project["automation_id"],
                    request_id,
                    project.get("state"),
                    _json_param(event_metadata, {}),
                    _json_hash(event_metadata),
                    _required_text(actor_id, "actor_id"),
                    _required_text(actor_role, "actor_role"),
                ),
            )
            cursor.execute(
                "SELECT * FROM automation_plugin_purge_journal WHERE purge_id=%s",
                (_required_text(purge_id, "purge_id"),),
            )
            result = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
        if result is None:
            raise OrchestrationPersistenceError("plugin purge journal did not persist")
        return result

    def get_purge_journal(
        self,
        automation_id: str,
        purge_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM automation_plugin_purge_journal
                WHERE automation_id=%s AND purge_id=%s{suffix}
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _required_text(purge_id, "purge_id"),
                ),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )

    def list_execution_blocks(self, automation_id: str) -> list[dict[str, Any]]:
        safe_automation_id = _required_text(automation_id, "automation_id")
        blocks: list[dict[str, Any]] = []
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT command.command_id, run.run_id, run.status,
                       EXISTS (
                           SELECT 1 FROM agent_run_steps AS blocked_step
                           WHERE blocked_step.run_id=run.run_id
                             AND blocked_step.error_code='WRITE_OUTCOME_UNKNOWN'
                       ) AS outcome_unknown
                FROM agent_commands AS command
                LEFT JOIN agent_runs AS run ON run.command_id=command.command_id
                WHERE command.automation_id=%s
                  AND {_CONTROL_PLANE_UNINSTALL_BLOCK_PREDICATE}
                ORDER BY run.run_id
                """,
                (safe_automation_id,),
            )
            for row in _rows(cursor):
                status = str(row.get("status") or "")
                blocks.append(
                    {
                        "kind": (
                            "VERIFYING"
                            if status == "VERIFYING"
                            else (
                                "WRITE_OUTCOME_UNKNOWN"
                                if bool(row.get("outcome_unknown"))
                                else "RUNNING"
                            )
                        ),
                        "run_id": str(row.get("run_id") or ""),
                        "message": "control-plane run blocks plugin uninstall",
                    }
                )
            cursor.execute(
                """
                SELECT lease_id, outcome
                FROM automation_project_generation_leases
                WHERE automation_id=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                ORDER BY lease_id
                """,
                (safe_automation_id,),
            )
            for row in _rows(cursor):
                unknown = str(row.get("outcome") or "") == "WRITE_OUTCOME_UNKNOWN"
                blocks.append(
                    {
                        "kind": (
                            "WRITE_OUTCOME_UNKNOWN"
                            if unknown
                            else (
                                "VERIFYING"
                                if str(row.get("outcome") or "") == "VERIFYING"
                                else "RUNNING"
                            )
                        ),
                        "run_id": str(row.get("lease_id") or ""),
                        "message": "runtime generation lease blocks plugin uninstall",
                    }
                )
            cursor.execute("""SELECT receipt_id FROM automation_write_attempt_receipts
                WHERE automation_id=%s AND outcome IN ('STARTED','WRITE_OUTCOME_UNKNOWN')
                ORDER BY receipt_id""", (safe_automation_id,))
            for row in _rows(cursor):
                blocks.append({"kind": "WRITE_OUTCOME_UNKNOWN", "run_id": str(row["receipt_id"]),
                    "message": "unresolved write receipt blocks plugin uninstall"})
        return blocks

    def reserve_purge_finalize(self, purge_id: str) -> dict[str, Any]:
        """Lock and re-check the exact uninstall boundary before deletion."""

        safe_purge_id = _required_text(purge_id, "purge_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            journal = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
            if journal is None:
                raise OrchestrationPersistenceError("plugin purge journal does not exist")
            phase = str(journal.get("phase") or "")
            if phase == "CONTROL_PLANE_DELETED":
                return journal
            if phase not in {"PREPARED", "FINALIZE_RESERVED"}:
                raise ConcurrentUpdateError("plugin purge is not ready to finalize")

            automation_id = str(journal["automation_id"])
            cursor.execute(
                """
                SELECT * FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError(
                    "automation project disappeared before purge reservation"
                )
            if str(project.get("state") or "") != "UNINSTALLING" or bool(
                project.get("enabled")
            ):
                raise ConcurrentUpdateError(
                    "automation project is not safely revoked for uninstall"
                )
            cursor.execute(
                f"""
                SELECT command.command_id, run.run_id
                FROM agent_commands AS command
                LEFT JOIN agent_runs AS run ON run.command_id=command.command_id
                WHERE command.automation_id=%s
                  AND {_CONTROL_PLANE_UNINSTALL_BLOCK_PREDICATE}
                FOR UPDATE
                """,
                (automation_id,),
            )
            blocked_runs = _rows(cursor)
            cursor.execute(
                """
                SELECT generation, state FROM automation_project_generations
                WHERE automation_id=%s AND state<>'DISPOSED' FOR UPDATE
                """,
                (automation_id,),
            )
            generation_blocks = _rows(cursor)
            cursor.execute(
                """
                SELECT lease_id FROM automation_project_generation_leases
                WHERE automation_id=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (automation_id,),
            )
            lease_blocks = _rows(cursor)
            if blocked_runs or generation_blocks or lease_blocks:
                raise AutomationPluginPurgeBlocked(
                    "execution or runtime generation appeared before purge finalize"
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS reference_count
                FROM automation_projects
                WHERE plugin_id=%s AND plugin_version=%s AND automation_id<>%s
                """,
                (
                    journal["plugin_id"],
                    journal["plugin_version"],
                    automation_id,
                ),
            )
            references = _row_dict(cursor, cursor.fetchone()) or {}
            delete_shared = (
                str(journal.get("cleanup_scope") or "") == "PACKAGE"
                and int(references.get("reference_count") or 0) == 0
            )
            cursor.execute(
                """
                UPDATE automation_plugin_purge_journal
                SET phase='FINALIZE_RESERVED', delete_shared_package=%s,
                    error_code=NULL, error_summary=NULL, updated_at=NOW(6)
                WHERE purge_id=%s
                  AND phase IN ('PREPARED', 'FINALIZE_RESERVED')
                """,
                (delete_shared, safe_purge_id),
            )
            cursor.execute(
                "SELECT * FROM automation_plugin_purge_journal WHERE purge_id=%s",
                (safe_purge_id,),
            )
            reserved = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
        if reserved is None:
            raise OrchestrationPersistenceError("purge reservation disappeared")
        return reserved

    def hard_delete_project_application_state(self, purge_id: str) -> None:
        """Delete one instance and its exact control-plane graph in FK order."""

        safe_purge_id = _required_text(purge_id, "purge_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            journal = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
            if journal is None:
                raise OrchestrationPersistenceError("plugin purge journal does not exist")
            if str(journal.get("phase") or "") == "CONTROL_PLANE_DELETED":
                return
            if str(journal.get("phase") or "") != "FINALIZE_RESERVED":
                raise ConcurrentUpdateError("plugin purge was not reserved for finalize")
            automation_id = str(journal["automation_id"])

            cursor.execute(
                """
                SELECT automation_id FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (automation_id,),
            )
            if cursor.fetchone() is None:
                raise OrchestrationPersistenceError(
                    "automation project disappeared before application-state deletion"
                )
            _delete_disposed_execution_references(cursor, automation_id)

            command_ids = _selected_ids(
                cursor,
                "SELECT command_id FROM agent_commands "
                "WHERE automation_id=%s FOR UPDATE",
                (automation_id,),
                "command_id",
            )
            work_item_ids: list[str] = []
            run_ids: list[str] = []
            step_ids: list[str] = []
            approval_ids: list[str] = []
            if command_ids:
                command_placeholders = _sql_placeholders(command_ids)
                work_item_ids = _selected_ids(
                    cursor,
                    f"SELECT work_item_id FROM work_items "
                    f"WHERE command_id IN ({command_placeholders}) FOR UPDATE",
                    command_ids,
                    "work_item_id",
                )
                run_ids = _selected_ids(
                    cursor,
                    f"SELECT run_id FROM agent_runs "
                    f"WHERE command_id IN ({command_placeholders}) FOR UPDATE",
                    command_ids,
                    "run_id",
                )
            if run_ids:
                run_placeholders = _sql_placeholders(run_ids)
                cursor.execute(
                    f"""
                    SELECT run_id FROM agent_runs
                    WHERE retry_of_run_id IN ({run_placeholders})
                      AND run_id NOT IN ({run_placeholders})
                    FOR UPDATE
                    """,
                    tuple([*run_ids, *run_ids]),
                )
                if _rows(cursor):
                    raise AutomationPluginPurgeBlocked(
                        "another project Run references this project's retry chain"
                    )
                step_ids = _selected_ids(
                    cursor,
                    f"SELECT step_id FROM agent_run_steps "
                    f"WHERE run_id IN ({run_placeholders}) FOR UPDATE",
                    run_ids,
                    "step_id",
                )
                approval_ids = _selected_ids(
                    cursor,
                    f"SELECT approval_id FROM approval_requests "
                    f"WHERE run_id IN ({run_placeholders}) FOR UPDATE",
                    run_ids,
                    "approval_id",
                )

            event_conditions = ["(entity_type='automation_project' AND entity_id=%s)"]
            event_params: list[Any] = [automation_id]
            if work_item_ids:
                event_conditions.append(
                    f"work_item_id IN ({_sql_placeholders(work_item_ids)})"
                )
                event_params.extend(work_item_ids)
            if run_ids:
                event_conditions.append(f"run_id IN ({_sql_placeholders(run_ids)})")
                event_params.extend(run_ids)
            if step_ids:
                event_conditions.append(f"step_id IN ({_sql_placeholders(step_ids)})")
                event_params.extend(step_ids)
            event_ids = _selected_ids(
                cursor,
                "SELECT event_id FROM domain_events WHERE "
                + " OR ".join(event_conditions)
                + " FOR UPDATE",
                event_params,
                "event_id",
            )
            if event_ids:
                event_placeholders = _sql_placeholders(event_ids)
                cursor.execute(
                    f"DELETE FROM event_consumptions "
                    f"WHERE event_id IN ({event_placeholders})",
                    tuple(event_ids),
                )
                cursor.execute(
                    f"DELETE FROM outbox_events WHERE event_id IN ({event_placeholders})",
                    tuple(event_ids),
                )
                cursor.execute(
                    f"DELETE FROM domain_events WHERE event_id IN ({event_placeholders})",
                    tuple(event_ids),
                )
            if approval_ids:
                approval_placeholders = _sql_placeholders(approval_ids)
                cursor.execute(
                    f"DELETE FROM approval_decisions "
                    f"WHERE approval_id IN ({approval_placeholders})",
                    tuple(approval_ids),
                )
                cursor.execute(
                    f"DELETE FROM approval_requests "
                    f"WHERE approval_id IN ({approval_placeholders})",
                    tuple(approval_ids),
                )
            if work_item_ids:
                work_placeholders = _sql_placeholders(work_item_ids)
                cursor.execute(
                    f"DELETE FROM evidence_records "
                    f"WHERE work_item_id IN ({work_placeholders})",
                    tuple(work_item_ids),
                )
            if run_ids or step_ids:
                log_conditions: list[str] = []
                log_params: list[Any] = []
                if run_ids:
                    log_conditions.append(f"run_id IN ({_sql_placeholders(run_ids)})")
                    log_params.extend(run_ids)
                if step_ids:
                    log_conditions.append(f"step_id IN ({_sql_placeholders(step_ids)})")
                    log_params.extend(step_ids)
                cursor.execute(
                    "DELETE FROM tool_logs WHERE " + " OR ".join(log_conditions),
                    tuple(log_params),
                )
            if step_ids:
                step_placeholders = _sql_placeholders(step_ids)
                cursor.execute(
                    f"DELETE FROM agent_run_steps WHERE step_id IN ({step_placeholders})",
                    tuple(step_ids),
                )
            if run_ids:
                run_placeholders = _sql_placeholders(run_ids)
                cursor.execute(
                    f"UPDATE agent_runs SET retry_of_run_id=NULL "
                    f"WHERE run_id IN ({run_placeholders})",
                    tuple(run_ids),
                )
                cursor.execute(
                    f"DELETE FROM agent_runs WHERE run_id IN ({run_placeholders})",
                    tuple(run_ids),
                )
            if work_item_ids:
                work_placeholders = _sql_placeholders(work_item_ids)
                cursor.execute(
                    f"DELETE FROM work_item_entities "
                    f"WHERE work_item_id IN ({work_placeholders})",
                    tuple(work_item_ids),
                )
                cursor.execute(
                    f"DELETE FROM work_items WHERE work_item_id IN ({work_placeholders})",
                    tuple(work_item_ids),
                )
            if command_ids:
                command_placeholders = _sql_placeholders(command_ids)
                cursor.execute(
                    f"DELETE FROM agent_commands "
                    f"WHERE command_id IN ({command_placeholders})",
                    tuple(command_ids),
                )

            cursor.execute(
                "SELECT id FROM scheduled_tasks WHERE automation_id=%s FOR UPDATE",
                (automation_id,),
            )
            task_ids = [str(row["id"]) for row in _rows(cursor)]
            if task_ids:
                task_placeholders = _sql_placeholders(task_ids)
                cursor.execute(
                    f"DELETE FROM scheduled_task_approval_policy_events "
                    f"WHERE task_id IN ({task_placeholders})",
                    tuple(task_ids),
                )
                cursor.execute(
                    f"DELETE FROM scheduled_task_approval_policies "
                    f"WHERE task_id IN ({task_placeholders})",
                    tuple(task_ids),
                )
                cursor.execute(
                    f"DELETE FROM scheduled_tasks WHERE id IN ({task_placeholders})",
                    tuple(task_ids),
                )
            cursor.execute(
                "DELETE FROM automation_project_approval_batches WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_policy_events WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_policies WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_events WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_configs WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                """
                DELETE FROM automation_project_generation_effects
                WHERE automation_id=%s
                """,
                (automation_id,),
            )
            cursor.execute(
                """
                DELETE FROM automation_project_generation_coeffects
                WHERE automation_id=%s
                """,
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_generation_transitions WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_generations WHERE automation_id=%s",
                (automation_id,),
            )
            # The immutable deployment 018 receipt outlives its original plugin.
            # Deleting an item would corrupt the marker shared by all instances.
            cursor.execute(
                "DELETE FROM automation_projects WHERE automation_id=%s",
                (automation_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "automation project deletion changed while locked"
                )
            # Source identity, published details and operator fields outlive code.
            # This runs only after the existing lease/unknown-write purge barrier.
            cursor.execute(
                """UPDATE module_data_sources SET producer_instance_id=NULL,
                    revision=revision+1,status='history_only',updated_at=UTC_TIMESTAMP(6)
                    WHERE producer_instance_id=%s""",
                (automation_id,),
            )
            cursor.execute(
                """
                UPDATE automation_plugin_purge_journal
                SET phase='CONTROL_PLANE_DELETED', updated_at=NOW(6)
                WHERE purge_id=%s AND phase='FINALIZE_RESERVED'
                """,
                (safe_purge_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("purge journal finalize phase changed")

    def complete_purge(self, purge_id: str) -> None:
        """Remove shared version metadata only at zero references, then self-delete."""

        safe_purge_id = _required_text(purge_id, "purge_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            journal = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
            if journal is None:
                return
            if str(journal.get("phase") or "") != "CONTROL_PLANE_DELETED":
                raise ConcurrentUpdateError(
                    "purge cannot complete before control-plane deletion"
                )
            cursor.execute(
                "SELECT automation_id FROM automation_projects WHERE automation_id=%s",
                (journal["automation_id"],),
            )
            if cursor.fetchone() is not None:
                raise AutomationPluginPurgeBlocked(
                    "automation project still exists at purge completion"
                )
            if bool(journal.get("delete_shared_package")):
                cursor.execute(
                    """
                    SELECT COUNT(*) AS reference_count
                    FROM automation_projects
                    WHERE plugin_id=%s AND plugin_version=%s
                    """,
                    (journal["plugin_id"], journal["plugin_version"]),
                )
                references = _row_dict(cursor, cursor.fetchone()) or {}
                if int(references.get("reference_count") or 0):
                    raise AutomationPluginPurgeBlocked(
                        "plugin version gained a new instance before purge completion"
                    )
                cursor.execute(
                    """
                    DELETE FROM automation_plugin_versions
                    WHERE plugin_id=%s AND version=%s
                    """,
                    (journal["plugin_id"], journal["plugin_version"]),
                )
                cursor.execute(
                    """
                    SELECT version FROM automation_plugin_versions
                    WHERE plugin_id=%s ORDER BY installed_at DESC, version DESC LIMIT 1
                    """,
                    (journal["plugin_id"],),
                )
                remaining = _row_dict(cursor, cursor.fetchone())
                if remaining is None:
                    cursor.execute(
                        "DELETE FROM automation_plugin_package_events WHERE plugin_id=%s",
                        (journal["plugin_id"],),
                    )
                    cursor.execute(
                        "DELETE FROM automation_plugin_packages WHERE plugin_id=%s",
                        (journal["plugin_id"],),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE automation_plugin_packages
                        SET latest_version=%s, record_version=record_version+1,
                            updated_at=NOW(6)
                        WHERE plugin_id=%s
                        """,
                        (remaining["version"], journal["plugin_id"]),
                    )
            cursor.execute(
                "DELETE FROM automation_plugin_purge_journal WHERE purge_id=%s",
                (safe_purge_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("purge journal completion changed")

    def mark_purge_failed(
        self,
        purge_id: str,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        """Record a redacted retryable failure without losing the resume phase."""

        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_plugin_purge_journal
                SET error_code=%s, error_summary=%s, updated_at=NOW(6)
                WHERE purge_id=%s AND phase<>'COMMITTED'
                """,
                (
                    _required_text(error_code, "error_code")[:64],
                    _safe_error(error_summary),
                    _required_text(purge_id, "purge_id"),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("purge failure journal does not exist")
