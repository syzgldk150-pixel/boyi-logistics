"""Host-owned daily schedule times for one committed project generation.

Imported only by ``shared.automation_plugin_repository`` so the public
repository type keeps one facade below the single-file hygiene limit.  Daily
times are the host timer's: saving them rewrites the project's
``scheduled_tasks`` rows in the caller's Unit of Work and never stages a
plugin generation.  Kind and on/off changes stay generation material.
"""

from __future__ import annotations

from shared import automation_plugin_repository as _repository

Any = _repository.Any
ConcurrentUpdateError = _repository.ConcurrentUpdateError
IdempotencyConflict = _repository.IdempotencyConflict
Mapping = _repository.Mapping
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
_decode_row = _repository._decode_row
_json_hash = _repository._json_hash
_json_param = _repository._json_param
_normalized_project_schedule = _repository._normalized_project_schedule
_positive_int = _repository._positive_int
_required_text = _repository._required_text
_row_dict = _repository._row_dict
_rows = _repository._rows
_schedule_expressions = _repository._schedule_expressions
_sha256 = _repository._sha256
_stable_schedule_task_id = _repository._stable_schedule_task_id
plugin_json_digest = _repository.plugin_json_digest


class AutomationProjectScheduleRepositoryMixin:
    def save_project_schedule_times(
        self,
        automation_id: str,
        *,
        schedule: Mapping[str, Any],
        expected_schedule_sha256: str,
        scheduling: Mapping[str, Any],
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_project_configuration_version: int,
    ) -> dict[str, Any]:
        """Save only daily times on the committed generation, or decline.

        Daily times belong to the host timer: the committed generation, its
        configuration version and scheduler arguments stay bound to the
        rewritten ``scheduled_tasks`` rows, so no generation is staged.  Any
        other change (kind, on/off, a generation still switching, drifted
        physical rows) returns ``INELIGIBLE`` without writing anything and the
        caller uses the full configuration path.
        """

        project_id = _required_text(automation_id, "automation_id")
        expected_version = _positive_int(
            expected_project_configuration_version,
            "expected_project_configuration_version",
        )
        normalized_actor = _required_text(actor_id, "actor_id")
        normalized_role = _required_text(actor_role, "actor_role")
        normalized_request = _required_text(request_id, "request_id")
        new_schedule = _normalized_project_schedule(schedule)
        expected_sha256 = _sha256(expected_schedule_sha256, "expected_schedule_sha256")
        request_payload_sha256 = _json_hash(
            {
                "schedule": new_schedule,
                "expected_schedule_sha256": expected_sha256,
                "expected_project_configuration_version": expected_version,
            }
        )

        def ineligible(reason: str) -> dict[str, Any]:
            return {"outcome": "INELIGIBLE", "reason": reason}

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT project.*, version.runtime_model, version.scheduling_sha256
                FROM automation_projects AS project
                INNER JOIN automation_plugin_versions AS version
                  ON version.plugin_id=project.plugin_id
                 AND version.version=project.plugin_version
                WHERE project.automation_id=%s
                FOR UPDATE
                """,
                (project_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project is not installed")

            cursor.execute(
                """
                SELECT * FROM automation_project_schedule_changes
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (project_id, normalized_request),
            )
            prior_change = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("after_schedule_json",),
            )
            if prior_change is not None:
                if (
                    prior_change.get("request_payload_sha256") != request_payload_sha256
                    or prior_change.get("actor_id") != normalized_actor
                    or prior_change.get("actor_role") != normalized_role
                ):
                    raise IdempotencyConflict(
                        "schedule request was reused with different input"
                    )
                return {
                    "outcome": "REPLAYED",
                    "schedule": _normalized_project_schedule(
                        prior_change["after_schedule_json"]
                    ),
                    "project_configuration_version": int(
                        prior_change["project_configuration_version"]
                    ),
                    "automation_generation": int(prior_change["automation_generation"]),
                }
            cursor.execute(
                """
                SELECT event_id FROM automation_project_events
                WHERE automation_id=%s AND request_id=%s
                """,
                (project_id, normalized_request),
            )
            if _row_dict(cursor, cursor.fetchone()) is not None:
                # The full configuration path owns this request's replay.
                return ineligible("FULL_CONFIGURATION_REQUEST")

            if str(project.get("runtime_model") or "") != "SERVICE_V2":
                return ineligible("RUNTIME_MODEL")
            if str(project.get("state") or "") not in {"INSTALLED", "ENABLED", "DISABLED"}:
                return ineligible("PROJECT_STATE")
            committed_generation = project.get("committed_generation")
            if (
                isinstance(committed_generation, bool)
                or not isinstance(committed_generation, int)
                or committed_generation < 1
                or project.get("target_generation") != committed_generation
                or str(project.get("reconcile_state") or "") != "STABLE"
            ):
                return ineligible("GENERATION_NOT_STABLE")
            cursor.execute(
                """
                SELECT state FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (project_id, committed_generation),
            )
            generation = _row_dict(cursor, cursor.fetchone())
            if generation is None or str(generation.get("state") or "") != "COMMITTED":
                return ineligible("GENERATION_NOT_COMMITTED")
            # A journalled switch can still be reversed until its projection is
            # ACTIVE; a reversal restores the pre-switch task rows wholesale.
            cursor.execute(
                """
                SELECT phase FROM automation_project_generation_transitions
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (project_id, committed_generation),
            )
            transition = _row_dict(cursor, cursor.fetchone())
            if transition is not None and str(transition.get("phase") or "") != "ACTIVE":
                return ineligible("PROJECTION_NOT_ACTIVE")
            if _sha256(project.get("scheduling_sha256"), "scheduling_sha256") != plugin_json_digest(
                scheduling
            ):
                raise OrchestrationPersistenceError(
                    "persisted plugin contract does not match configuration witness"
                )

            cursor.execute(
                """
                SELECT * FROM automation_project_configs
                WHERE automation_id=%s FOR UPDATE
                """,
                (project_id,),
            )
            config = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._CONFIG_JSON_FIELDS,
            )
            if config is None or not bool(config.get("configured")):
                return ineligible("NOT_CONFIGURED")
            current_version = int(config.get("config_version") or 0)
            if current_version != expected_version:
                raise ConcurrentUpdateError("automation project config version changed")
            current_schedule = _normalized_project_schedule(
                config.get("desired_schedule_json")
            )
            current_sha256 = _sha256(config.get("desired_schedule_sha256"), "desired_schedule_sha256")
            if current_sha256 != _json_hash(current_schedule):
                raise OrchestrationPersistenceError("persisted project schedule digest does not match its content")
            if current_sha256 != expected_sha256:
                raise ConcurrentUpdateError("automation project schedule changed")
            if (
                current_schedule["kind"] != "daily_times"
                or new_schedule["kind"] != "daily_times"
                or current_schedule["enabled"] != new_schedule["enabled"]
            ):
                return ineligible("SCHEDULE_SHAPE_CHANGED")
            if (
                scheduling.get("supported") is not True
                or not isinstance(scheduling.get("allowed_kinds"), list)
                or "daily_times" not in scheduling["allowed_kinds"]
            ):
                raise OrchestrationPersistenceError(
                    "project schedule exceeds the signed plugin kinds"
                )
            max_daily_times = scheduling.get("max_daily_times")
            if (
                type(max_daily_times) is not int
                or max_daily_times < 1
                or len(new_schedule["times"]) > max_daily_times
            ):
                raise OrchestrationPersistenceError(
                    "project schedule exceeds the signed plugin daily limit"
                )
            if new_schedule == current_schedule:
                return {
                    "outcome": "UNCHANGED",
                    "schedule": current_schedule,
                    "project_configuration_version": current_version,
                    "automation_generation": committed_generation,
                }

            cursor.execute(
                """
                SELECT task.*, policy.task_id AS policy_task_id,
                       policy.mode AS policy_mode
                FROM scheduled_tasks AS task
                LEFT JOIN scheduled_task_approval_policies AS policy
                  ON policy.task_id=task.id
                WHERE task.automation_id=%s
                ORDER BY task.id FOR UPDATE
                """,
                (project_id,),
            )
            existing_tasks = [
                _decode_row(row, ("tool_params",)) or {}
                for row in _rows(cursor)
            ]
            if sorted(
                str(task.get("cron_expression") or "") for task in existing_tasks
            ) != sorted(_schedule_expressions(current_schedule)):
                return ineligible("PHYSICAL_ROWS_DRIFT")
            tool_name = f"automation.{project_id}.run"
            if any(
                task.get("automation_generation") != committed_generation
                or task.get("configuration_version") != current_version
                or str(task.get("tool_name") or "") != tool_name
                or task.get("policy_task_id") is None
                or str(task.get("policy_mode") or "") == "EXACT_SCHEDULE_EXEMPT"
                for task in existing_tasks
            ):
                return ineligible("PHYSICAL_ROWS_DRIFT")
            physical_enabled = {bool(task.get("enabled")) for task in existing_tasks}
            arguments = {_json_hash(task.get("tool_params") or {}) for task in existing_tasks}
            if len(physical_enabled) != 1 or len(arguments) != 1:
                return ineligible("PHYSICAL_ROWS_DRIFT")
            enabled = physical_enabled.pop()
            tool_params = dict(existing_tasks[0].get("tool_params") or {})

            by_cron = {str(task["cron_expression"]): task for task in existing_tasks}
            target_expressions = _schedule_expressions(new_schedule)
            new_tasks = [
                {
                    "id": _stable_schedule_task_id(project_id, expression),
                    "cron_expression": expression,
                }
                for expression in target_expressions
                if expression not in by_cron
            ]
            if new_tasks:
                placeholders = ", ".join(["%s"] * len(new_tasks))
                cursor.execute(
                    f"""
                    SELECT id, automation_id FROM scheduled_tasks
                    WHERE id IN ({placeholders}) FOR UPDATE
                    """,
                    tuple(task["id"] for task in new_tasks),
                )
                if any(
                    str(row.get("automation_id") or "") != project_id
                    for row in _rows(cursor)
                ):
                    raise OrchestrationPersistenceError(
                        "server-derived schedule identity collided with another project"
                    )
                name = f"{project.get('display_name') or project_id} schedule"[:128]
                cursor.executemany(
                    """
                    INSERT INTO scheduled_tasks (
                        id, automation_id, automation_generation, name,
                        tool_name, tool_params, cron_expression, enabled,
                        configuration_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            task["id"],
                            project_id,
                            committed_generation,
                            name,
                            tool_name,
                            _json_param(tool_params, {}),
                            task["cron_expression"],
                            enabled,
                            current_version,
                        )
                        for task in new_tasks
                    ],
                )
                cursor.executemany(
                    """
                    INSERT INTO scheduled_task_approval_policies (task_id, mode)
                    VALUES (%s, %s)
                    """,
                    [(task["id"], "REQUIRE_EACH_RUN") for task in new_tasks],
                )
            stale_ids = sorted(
                str(task["id"])
                for cron, task in by_cron.items()
                if cron not in target_expressions
            )
            if stale_ids:
                placeholders = ", ".join(["%s"] * len(stale_ids))
                # Approval policy rows follow through ON DELETE CASCADE.
                cursor.execute(
                    f"""
                    DELETE FROM scheduled_tasks
                    WHERE automation_id=%s AND id IN ({placeholders})
                    """,
                    (project_id, *stale_ids),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != len(stale_ids):
                    raise ConcurrentUpdateError(
                        "scheduled task rows changed during schedule save"
                    )

            new_schedule_sha256 = _json_hash(new_schedule)
            cursor.execute(
                """
                UPDATE automation_project_configs
                SET desired_schedule_json=%s, desired_schedule_sha256=%s,
                    updated_at=NOW(6)
                WHERE automation_id=%s AND config_version=%s
                  AND desired_schedule_sha256=%s
                """,
                (
                    _json_param(new_schedule, {}),
                    new_schedule_sha256,
                    project_id,
                    current_version,
                    config.get("desired_schedule_sha256"),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("automation project schedule changed")
            cursor.execute(
                """
                INSERT INTO automation_project_schedule_changes (
                    automation_id, request_id, request_payload_sha256,
                    project_configuration_version, automation_generation,
                    before_schedule_json, before_schedule_sha256,
                    after_schedule_json, after_schedule_sha256,
                    actor_id, actor_role
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_id,
                    normalized_request,
                    request_payload_sha256,
                    current_version,
                    committed_generation,
                    _json_param(current_schedule, {}),
                    _json_hash(current_schedule),
                    _json_param(new_schedule, {}),
                    new_schedule_sha256,
                    normalized_actor,
                    normalized_role,
                ),
            )
        return {
            "outcome": "SAVED",
            "schedule": new_schedule,
            "project_configuration_version": current_version,
            "automation_generation": committed_generation,
        }
