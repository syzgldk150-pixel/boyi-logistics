"""Runtime-row and scheduler-gate helpers for plugin generation persistence.

This module deliberately keeps the public repository API on
``AutomationPluginGenerationRepositoryMixin``.  The mixin delegates here so
the split does not alter its MRO, transaction ownership, or call contracts.
"""

from __future__ import annotations

from shared import automation_plugin_repository as _repository

Any = _repository.Any
ConcurrentUpdateError = _repository.ConcurrentUpdateError
Mapping = _repository.Mapping
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
Sequence = _repository.Sequence
_decode_row = _repository._decode_row
_normalized_project_schedule = _repository._normalized_project_schedule
_optional_positive_int = _repository._optional_positive_int
_positive_int = _repository._positive_int
_required_text = _repository._required_text
_row_dict = _repository._row_dict
_rows = _repository._rows
_runtime_contract = _repository._runtime_contract
_schedule_expressions = _repository._schedule_expressions
_validated_generation_row = _repository._validated_generation_row


def scheduler_contribution_binding(
    *,
    snapshot: Mapping[str, Any],
    execution_metadata: Mapping[str, Any],
    enabled_entrypoints: Sequence[str],
    schedule_expressions: Sequence[str],
) -> tuple[str | None, bool]:
    """Resolve one v1/v2 scheduler contribution without guessing."""

    runtime_model, _ = _runtime_contract(snapshot)
    if runtime_model == "ACTION_V1":
        return "scheduler", "scheduler" in enabled_entrypoints
    contributions = execution_metadata.get("contributions")
    scheduler_items = (
        contributions.get("scheduler")
        if isinstance(contributions, Mapping)
        else None
    )
    if not isinstance(scheduler_items, list) or any(
        not isinstance(item, Mapping)
        or not str(item.get("id") or "")
        or not isinstance(item.get("schedule"), Mapping)
        for item in scheduler_items
    ):
        raise OrchestrationPersistenceError(
            "service-v2 scheduler contributions are invalid"
        )
    declared_ids = [str(item.get("id") or "") for item in scheduler_items]
    if len(declared_ids) != len(set(declared_ids)):
        raise OrchestrationPersistenceError(
            "service-v2 scheduler contribution identities are duplicated"
        )
    enabled_ids = sorted(set(enabled_entrypoints) & set(declared_ids))
    if len(enabled_ids) > 1:
        raise OrchestrationPersistenceError(
            "project schedule has multiple enabled scheduler contributions"
        )
    enabled = len(enabled_ids) == 1
    if enabled:
        entrypoint = enabled_ids[0]
    elif len(declared_ids) == 1:
        entrypoint = declared_ids[0]
    else:
        entrypoint = None
    if schedule_expressions and entrypoint is None:
        raise OrchestrationPersistenceError(
            "project schedule contribution cannot be determined"
        )
    if entrypoint is not None:
        declaration = next(
            item
            for item in scheduler_items
            if str(item.get("id") or "") == entrypoint
        )
        default_schedule = declaration["schedule"]
        if (
            schedule_expressions
            and str(default_schedule.get("timezone") or "") != "Asia/Shanghai"
        ):
            raise OrchestrationPersistenceError(
                "service-v2 scheduler timezone is unavailable"
            )
    return entrypoint, enabled


def migration_owned_scheduler_enabled(
    cursor: Any,
    *,
    automation_id: str,
    desired_enabled: bool,
) -> bool:
    """Return whether the physical scheduler belongs to this project now."""

    cursor.execute(
        """
        SELECT source_automation_id, target_automation_id, state
        FROM automation_plugin_migration_pairs
        WHERE (source_automation_id=%s OR target_automation_id=%s)
          AND state<>'COMPLETED'
        ORDER BY created_at, migration_pair_id
        LIMIT 2
        """,
        (automation_id, automation_id),
    )
    rows = _rows(cursor)
    if len(rows) > 1:
        raise OrchestrationPersistenceError(
            "automation project has multiple unfinished migration pairs"
        )
    if not rows:
        return desired_enabled
    pair = rows[0]
    state = str(pair.get("state") or "")
    source_id = str(pair.get("source_automation_id") or "")
    target_id = str(pair.get("target_automation_id") or "")
    if automation_id == source_id:
        return bool(
            desired_enabled and state in {"PREPARING", "TESTING", "READY", "ROLLED_BACK"}
        )
    if automation_id == target_id:
        return bool(desired_enabled and state == "CUTOVER")
    raise OrchestrationPersistenceError("migration pair owner is invalid")


def set_project_dependency_scheduler_gate(
    repository: Any,
    automation_id: str,
    *,
    dependency_ready: bool,
) -> dict[str, Any]:
    """Gate physical task rows while retaining the committed desired intent."""

    project_id = _required_text(automation_id, "automation_id")
    if type(dependency_ready) is not bool:
        raise ValueError("dependency_ready must be bool")
    with repository.cursor() as cursor:
        if not dependency_ready:
            cursor.execute(
                "UPDATE scheduled_tasks SET enabled=FALSE WHERE automation_id=%s",
                (project_id,),
            )
            return {
                "automation_id": project_id,
                "scheduler_enabled": False,
                "task_count": int(getattr(cursor, "rowcount", 0) or 0),
            }
        cursor.execute(
            """
            SELECT automation_id, enabled, target_generation,
                   committed_generation, reconcile_state
            FROM automation_projects WHERE automation_id=%s FOR UPDATE
            """,
            (project_id,),
        )
        project = _row_dict(cursor, cursor.fetchone())
        if project is None:
            raise OrchestrationPersistenceError("dependency gate project does not exist")
        cursor.execute(
            """
            SELECT configured, config_sha256, compiled_invocations_sha256,
                   enabled_entrypoints_json, desired_schedule_json,
                   compiled_invocations_json
            FROM automation_project_configs WHERE automation_id=%s FOR UPDATE
            """,
            (project_id,),
        )
        config = _decode_row(
            _row_dict(cursor, cursor.fetchone()), repository._CONFIG_JSON_FIELDS
        )
        committed = _optional_positive_int(
            project.get("committed_generation"), "committed_generation"
        )
        physical_enabled = False
        if (
            bool(project.get("enabled"))
            and committed is not None
            and project.get("target_generation") == committed
            and str(project.get("reconcile_state") or "") == "STABLE"
            and isinstance(config, Mapping)
            and bool(config.get("configured"))
        ):
            cursor.execute(
                """
                SELECT generation, state, project_config_sha256,
                       compiled_invocations_sha256, snapshot_json
                FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (project_id, committed),
            )
            generation = _decode_row(
                _row_dict(cursor, cursor.fetchone()), repository._GENERATION_JSON_FIELDS
            )
            if (
                isinstance(generation, Mapping)
                and str(generation.get("state") or "") == "COMMITTED"
                and generation.get("project_config_sha256") == config.get("config_sha256")
                and generation.get("compiled_invocations_sha256")
                == config.get("compiled_invocations_sha256")
            ):
                snapshot = generation.get("snapshot_json")
                schedule = config.get("desired_schedule_json")
                compiled = config.get("compiled_invocations_json")
                enabled = config.get("enabled_entrypoints_json")
                if (
                    isinstance(snapshot, Mapping)
                    and isinstance(schedule, Mapping)
                    and isinstance(compiled, Mapping)
                    and isinstance(enabled, list)
                    and set(map(str, enabled)) == set(compiled)
                ):
                    normalized = _normalized_project_schedule(schedule)
                    expressions = _schedule_expressions(normalized)
                    entrypoint, scheduler_enabled = scheduler_contribution_binding(
                        snapshot=snapshot,
                        execution_metadata=(
                            snapshot.get("execution_metadata")
                            if isinstance(snapshot.get("execution_metadata"), Mapping)
                            else {}
                        ),
                        enabled_entrypoints=[str(item) for item in enabled],
                        schedule_expressions=expressions,
                    )
                    physical_enabled = bool(
                        entrypoint is not None
                        and normalized["enabled"]
                        and scheduler_enabled
                        and expressions
                        and migration_owned_scheduler_enabled(
                            cursor,
                            automation_id=project_id,
                            desired_enabled=True,
                        )
                    )
        cursor.execute(
            "UPDATE scheduled_tasks SET enabled=%s WHERE automation_id=%s",
            (physical_enabled, project_id),
        )
        return {
            "automation_id": project_id,
            "scheduler_enabled": physical_enabled,
            "task_count": int(getattr(cursor, "rowcount", 0) or 0),
        }


def get_project_runtime_row(
    repository: Any,
    automation_id: str,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    with repository.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT automation_id, target_generation, committed_generation,
                   reconcile_state, record_version
            FROM automation_projects WHERE automation_id=%s{suffix}
            """,
            (_required_text(automation_id, "automation_id"),),
        )
        return _row_dict(cursor, cursor.fetchone())


def list_project_runtime_rows(repository: Any) -> list[dict[str, Any]]:
    """Return only the runtime pointer projection used by reconciliation."""

    with repository.cursor() as cursor:
        cursor.execute(
            """
            SELECT automation_id, target_generation, committed_generation,
                   reconcile_state, record_version
            FROM automation_projects
            ORDER BY automation_id
            """
        )
        return _rows(cursor)


def get_generation_row(
    repository: Any,
    automation_id: str,
    generation: int,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    safe_automation_id = _required_text(automation_id, "automation_id")
    safe_generation = _positive_int(generation, "generation")
    with repository.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT * FROM automation_project_generations
            WHERE automation_id=%s AND generation=%s{suffix}
            """,
            (safe_automation_id, safe_generation),
        )
        row = _decode_row(
            _row_dict(cursor, cursor.fetchone()), repository._GENERATION_JSON_FIELDS
        )
        if row is None:
            return None
        row = _validated_generation_row(row)
        cursor.execute(
            f"""
            SELECT * FROM automation_project_generation_coeffects
            WHERE automation_id=%s AND generation=%s
            ORDER BY coeffect_kind, coeffect_key{suffix}
            """,
            (safe_automation_id, safe_generation),
        )
        row["coeffects"] = [
            _decode_row(item, repository._COEFFECT_JSON_FIELDS) or {}
            for item in _rows(cursor)
        ]
        cursor.execute(
            f"""
            SELECT * FROM automation_project_generation_effects
            WHERE automation_id=%s AND generation=%s
            ORDER BY effect_sequence, effect_id{suffix}
            """,
            (safe_automation_id, safe_generation),
        )
        row["effects"] = [
            _decode_row(item, repository._EFFECT_JSON_FIELDS) or {}
            for item in _rows(cursor)
        ]
        return row


def list_generation_rows(
    repository: Any,
    automation_id: str,
) -> list[dict[str, Any]]:
    """Return every persisted generation with its coeffect/effect journals."""

    safe_automation_id = _required_text(automation_id, "automation_id")
    with repository.cursor() as cursor:
        cursor.execute(
            """
            SELECT generation FROM automation_project_generations
            WHERE automation_id=%s
            ORDER BY generation
            """,
            (safe_automation_id,),
        )
        generations = [int(row["generation"]) for row in _rows(cursor)]
    result: list[dict[str, Any]] = []
    for generation in generations:
        row = get_generation_row(repository, safe_automation_id, generation)
        if row is None:
            raise ConcurrentUpdateError(
                "runtime generation changed during reconciliation listing"
            )
        result.append(row)
    return result
