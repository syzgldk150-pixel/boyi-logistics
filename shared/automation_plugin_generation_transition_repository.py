"""Durable activation-transition persistence for automation generations.

This module owns migration 034's journal, scheduler before-image, activation
acknowledgement, reverse-CAS, and fail-closed block operations.  The public
repository continues to expose these methods through its generation mixin.
"""

from __future__ import annotations

import hashlib
import json

from shared import automation_plugin_repository as _repository
from shared.automation_plugin_generation_runtime_repository import (
    migration_owned_scheduler_enabled as _migration_owned_scheduler_enabled,
    scheduler_contribution_binding as _scheduler_contribution_binding,
)

Any = _repository.Any
ConcurrentUpdateError = _repository.ConcurrentUpdateError
Mapping = _repository.Mapping
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
Sequence = _repository.Sequence
_decode_row = _repository._decode_row
_json_hash = _repository._json_hash
_json_param = _repository._json_param
_normalized_project_schedule = _repository._normalized_project_schedule
_optional_positive_int = _repository._optional_positive_int
_positive_int = _repository._positive_int
_required_text = _repository._required_text
_runtime_contract = _repository._runtime_contract
_row_dict = _repository._row_dict
_rows = _repository._rows
_schedule_expressions = _repository._schedule_expressions
_stable_schedule_task_id = _repository._stable_schedule_task_id
_validated_generation_row = _repository._validated_generation_row
uuid = _repository.uuid


def _exact_json_hash(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _transition_token(value: Any) -> str:
    text = _required_text(value, "transition_token")
    try:
        canonical = str(uuid.UUID(text))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("transition_token must be a UUID") from exc
    if canonical != text:
        raise ValueError("transition_token must be a canonical UUID")
    return canonical


def _lock_lease_eligible_activation_transition(
    cursor: Any,
    *,
    automation_id: str,
    generation: int,
) -> None:
    """Require the committed route's durable projection ACK when it exists.

    Generations committed before migration 034 have no journal row and remain
    explicitly lease-compatible. A journalled generation is routable only
    after its process projection has durably acknowledged ``ACTIVE``.
    """

    cursor.execute(
        """
        SELECT phase FROM automation_project_generation_transitions
        WHERE automation_id=%s AND generation=%s FOR UPDATE
        """,
        (automation_id, generation),
    )
    transition = _row_dict(cursor, cursor.fetchone())
    if transition is None:
        return
    if str(transition.get("phase") or "") != "ACTIVE":
        raise ConcurrentUpdateError(
            "runtime generation activation is not accepting leases"
        )


def _assert_transition_target_has_no_generation_leases(
    cursor: Any,
    *,
    automation_id: str,
    generation: int,
) -> None:
    """Reject reverse CAS after any lease has existed for its target.

    The target is PREPARED before this transition and the only supported lease
    entry point accepts COMMITTED generations, so it cannot have a legitimate
    pre-transition lease. Checking every persisted row therefore gives an
    exact boundary without timestamps or guessed outcome semantics.
    """

    cursor.execute(
        """
        SELECT lease_id, outcome FROM automation_project_generation_leases
        WHERE automation_id=%s AND generation=%s
        ORDER BY lease_id FOR UPDATE
        """,
        (automation_id, generation),
    )
    if _rows(cursor):
        raise ConcurrentUpdateError(
            "runtime generation lease history blocks activation rollback"
        )


def _lock_scheduled_task_before_image(
    cursor: Any,
    *,
    automation_id: str,
) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT task.id, task.automation_id, task.automation_generation,
               task.name, task.tool_name, task.tool_params,
               task.cron_expression, task.enabled, task.last_run,
               task.last_status, task.last_duration_ms, task.last_message,
               task.configuration_version, task.created_at, task.updated_at,
               policy.task_id AS policy_task_id,
               policy.mode AS policy_mode,
               policy.contract_hash AS policy_contract_hash,
               policy.contract_snapshot_json AS policy_contract_snapshot_json,
               policy.tool_contract_hash AS policy_tool_contract_hash,
               policy.approved_by_actor_id AS policy_approved_by_actor_id,
               policy.approved_by_actor_role AS policy_approved_by_actor_role,
               policy.approved_by_actor_display_name
                   AS policy_approved_by_actor_display_name,
               policy.approved_at AS policy_approved_at,
               policy.comment AS policy_comment,
               policy.version AS policy_version,
               policy.updated_at AS policy_updated_at
        FROM scheduled_tasks AS task
        LEFT JOIN scheduled_task_approval_policies AS policy
          ON policy.task_id=task.id
        WHERE task.automation_id=%s
        ORDER BY task.id
        FOR UPDATE
        """,
        (automation_id,),
    )
    rows = [
        _decode_row(
            row,
            ("tool_params", "policy_contract_snapshot_json"),
        )
        or {}
        for row in _rows(cursor)
    ]
    if any(item.get("policy_task_id") is None for item in rows):
        raise OrchestrationPersistenceError(
            "scheduled task approval policy disappeared before generation switch"
        )
    return rows


def _apply_scheduled_task_projection(
    cursor: Any,
    *,
    automation_id: str,
    generation: int,
    project: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> None:
    """Replace the physical scheduler projection from one committed snapshot."""

    cursor.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE automation_id=%s ORDER BY id FOR UPDATE
        """,
        (automation_id,),
    )
    existing_tasks = [
        _decode_row(row, ("tool_params",)) or {}
        for row in _rows(cursor)
    ]
    execution_metadata = snapshot.get("execution_metadata")
    if not isinstance(execution_metadata, Mapping):
        raise OrchestrationPersistenceError(
            "runtime generation execution metadata is invalid"
        )
    project_config_version = _positive_int(
        execution_metadata.get("project_config_version"),
        "project_config_version",
    )
    existing_by_cron = {
        str(row.get("cron_expression") or ""): row
        for row in existing_tasks
    }
    if len(existing_by_cron) != len(existing_tasks):
        raise OrchestrationPersistenceError(
            "committed project schedule contains duplicate cron rows"
        )
    desired_schedule = _normalized_project_schedule(
        execution_metadata.get("schedule")
    )
    enabled_entrypoints = snapshot.get("enabled_entrypoints")
    if not isinstance(enabled_entrypoints, list) or any(
        not isinstance(item, str) for item in enabled_entrypoints
    ):
        raise OrchestrationPersistenceError(
            "runtime generation entrypoints are invalid"
        )
    compiled_invocations = execution_metadata.get("compiled_invocations")
    if not isinstance(compiled_invocations, Mapping):
        raise OrchestrationPersistenceError(
            "runtime compiled invocations are invalid"
        )
    expressions = _schedule_expressions(desired_schedule)
    scheduler_entrypoint, scheduler_enabled = _scheduler_contribution_binding(
        snapshot=snapshot,
        execution_metadata=execution_metadata,
        enabled_entrypoints=enabled_entrypoints,
        schedule_expressions=expressions,
    )
    physical_scheduler_enabled = _migration_owned_scheduler_enabled(
        cursor,
        automation_id=automation_id,
        desired_enabled=bool(desired_schedule["enabled"] and scheduler_enabled),
    )
    scheduler_contract = (
        compiled_invocations.get(scheduler_entrypoint, {})
        if scheduler_entrypoint is not None
        else {}
    )
    scheduler_arguments = (
        scheduler_contract.get("arguments")
        if isinstance(scheduler_contract, Mapping)
        else None
    )
    if (
        expressions
        and scheduler_entrypoint is not None
        and not isinstance(scheduler_arguments, Mapping)
    ):
        raise OrchestrationPersistenceError(
            "scheduled runtime arguments are not compiled"
        )
    target_tasks: list[dict[str, Any]] = []
    for expression in expressions:
        existing = existing_by_cron.get(expression)
        task_id = (
            str(existing["id"])
            if existing is not None
            else _stable_schedule_task_id(automation_id, expression)
        )
        target_tasks.append(
            {
                "id": task_id,
                "name": (
                    str(existing.get("name") or "")
                    if existing is not None
                    else f"{project.get('display_name') or automation_id} schedule"
                )[:128],
                "cron_expression": expression,
            }
        )
    if target_tasks:
        placeholders = ", ".join(["%s"] * len(target_tasks))
        cursor.execute(
            f"""
            SELECT id, automation_id FROM scheduled_tasks
            WHERE id IN ({placeholders}) FOR UPDATE
            """,
            tuple(item["id"] for item in target_tasks),
        )
        if any(
            str(item.get("automation_id") or "") != automation_id
            for item in _rows(cursor)
        ):
            raise OrchestrationPersistenceError(
                "server-derived schedule identity collided with another project"
            )
    target_ids = {str(item["id"]) for item in target_tasks}
    for item in target_tasks:
        cursor.execute(
            """
            INSERT INTO scheduled_tasks (
                id, automation_id, automation_generation, name,
                tool_name, tool_params, cron_expression, enabled,
                configuration_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                automation_id=VALUES(automation_id),
                automation_generation=VALUES(automation_generation),
                name=VALUES(name), tool_name=VALUES(tool_name),
                tool_params=VALUES(tool_params),
                cron_expression=VALUES(cron_expression),
                enabled=VALUES(enabled),
                configuration_version=VALUES(configuration_version),
                updated_at=NOW(6)
            """,
            (
                item["id"],
                automation_id,
                generation,
                item["name"],
                f"automation.{automation_id}.run",
                _json_param(scheduler_arguments or {}, {}),
                item["cron_expression"],
                physical_scheduler_enabled,
                project_config_version,
            ),
        )
        cursor.execute(
            """
            INSERT INTO scheduled_task_approval_policies (task_id, mode)
            VALUES (%s, 'REQUIRE_EACH_RUN')
            ON DUPLICATE KEY UPDATE task_id=task_id
            """,
            (item["id"],),
        )
    stale_ids = {
        str(item["id"])
        for item in existing_tasks
        if str(item["id"]) not in target_ids
    }
    if stale_ids:
        placeholders = ", ".join(["%s"] * len(stale_ids))
        cursor.execute(
            f"""
            DELETE FROM scheduled_tasks
            WHERE automation_id=%s AND id IN ({placeholders})
            """,
            tuple([automation_id, *sorted(stale_ids)]),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != len(stale_ids):
            raise ConcurrentUpdateError(
                "scheduled task projection changed during generation switch"
            )


def _persist_transition_task_before_image(
    cursor: Any,
    *,
    transition_token: str,
    before_tasks: Sequence[Mapping[str, Any]],
) -> None:
    for item in before_tasks:
        cursor.execute(
            """
            INSERT INTO automation_project_generation_transition_tasks (
                transition_token, task_id, automation_generation,
                name, tool_name, tool_params, cron_expression, enabled,
                last_run, last_status, last_duration_ms, last_message,
                configuration_version, task_created_at, task_updated_at,
                policy_mode, policy_contract_hash,
                policy_contract_snapshot_json, policy_tool_contract_hash,
                policy_approved_by_actor_id, policy_approved_by_actor_role,
                policy_approved_by_actor_display_name, policy_approved_at,
                policy_comment, policy_version, policy_updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                transition_token,
                item["id"],
                item["automation_generation"],
                item["name"],
                item["tool_name"],
                (
                    json.dumps(
                        item["tool_params"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    if item.get("tool_params") is not None
                    else None
                ),
                item["cron_expression"],
                item["enabled"],
                item.get("last_run"),
                item.get("last_status"),
                item.get("last_duration_ms"),
                item.get("last_message"),
                item["configuration_version"],
                item["created_at"],
                item["updated_at"],
                item["policy_mode"],
                item.get("policy_contract_hash"),
                (
                    json.dumps(
                        item["policy_contract_snapshot_json"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    if item.get("policy_contract_snapshot_json") is not None
                    else None
                ),
                item.get("policy_tool_contract_hash"),
                item.get("policy_approved_by_actor_id"),
                item.get("policy_approved_by_actor_role"),
                item.get("policy_approved_by_actor_display_name"),
                item.get("policy_approved_at"),
                item.get("policy_comment"),
                item["policy_version"],
                item["policy_updated_at"],
            ),
        )


def _restore_transition_task_before_image(
    cursor: Any,
    *,
    automation_id: str,
    transition_token: str,
) -> None:
    current_tasks = _lock_scheduled_task_before_image(
        cursor,
        automation_id=automation_id,
    )
    cursor.execute(
        "DELETE FROM scheduled_tasks WHERE automation_id=%s",
        (automation_id,),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != len(current_tasks):
        raise ConcurrentUpdateError(
            "scheduled task projection changed before generation rollback"
        )
    cursor.execute(
        """
        SELECT * FROM automation_project_generation_transition_tasks
        WHERE transition_token=%s ORDER BY task_id FOR UPDATE
        """,
        (transition_token,),
    )
    before_tasks = [
        _decode_row(
            row,
            ("tool_params", "policy_contract_snapshot_json"),
        )
        or {}
        for row in _rows(cursor)
    ]
    for item in before_tasks:
        cursor.execute(
            """
            INSERT INTO scheduled_tasks (
                id, automation_id, automation_generation, name,
                tool_name, tool_params, cron_expression, enabled,
                last_run, last_status, last_duration_ms, last_message,
                configuration_version, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                item["task_id"],
                automation_id,
                item["automation_generation"],
                item["name"],
                item["tool_name"],
                (
                    json.dumps(
                        item["tool_params"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    if item.get("tool_params") is not None
                    else None
                ),
                item["cron_expression"],
                item["enabled"],
                item.get("last_run"),
                item.get("last_status"),
                item.get("last_duration_ms"),
                item.get("last_message"),
                item["configuration_version"],
                item["task_created_at"],
                item["task_updated_at"],
            ),
        )
        cursor.execute(
            """
            INSERT INTO scheduled_task_approval_policies (
                task_id, mode, contract_hash, contract_snapshot_json,
                tool_contract_hash, approved_by_actor_id,
                approved_by_actor_role, approved_by_actor_display_name,
                approved_at, comment, version, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            """,
            (
                item["task_id"],
                item["policy_mode"],
                item.get("policy_contract_hash"),
                (
                    json.dumps(
                        item["policy_contract_snapshot_json"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    if item.get("policy_contract_snapshot_json") is not None
                    else None
                ),
                item.get("policy_tool_contract_hash"),
                item.get("policy_approved_by_actor_id"),
                item.get("policy_approved_by_actor_role"),
                item.get("policy_approved_by_actor_display_name"),
                item.get("policy_approved_at"),
                item.get("policy_comment"),
                item["policy_version"],
                item["policy_updated_at"],
            ),
        )


def _begin_generation_transition(
    cursor: Any,
    *,
    automation_id: str,
    generation: int,
    base_committed_generation: int | None,
    project: Mapping[str, Any],
    pending_plugin_version: str,
    rollback_plugin_version: str,
    pending_project_state: str,
    pending_reconcile_state: str,
    policy: Mapping[str, Any],
    pending_policy_configuration_version: int,
    before_tasks: Sequence[Mapping[str, Any]],
) -> str:
    cursor.execute(
        """
        SELECT * FROM automation_project_generation_transitions
        WHERE automation_id=%s AND generation=%s FOR UPDATE
        """,
        (automation_id, generation),
    )
    existing = _row_dict(cursor, cursor.fetchone())
    if existing is not None and str(existing.get("phase") or "") != "ROLLED_BACK":
        raise ConcurrentUpdateError(
            "runtime generation already has an unfinished activation transition"
        )

    transition_token = str(uuid.uuid4())
    before_project_record_version = _positive_int(
        project.get("record_version"),
        "project record_version",
    )
    before_policy_generation = _positive_int(
        policy.get("project_generation"),
        "policy project_generation",
    )
    before_policy_configuration_version = _positive_int(
        policy.get("project_configuration_version"),
        "policy project_configuration_version",
    )
    before_policy_version = _positive_int(
        policy.get("version"),
        "policy version",
    )
    values = (
        transition_token,
        base_committed_generation,
        _required_text(project.get("plugin_version"), "project plugin_version"),
        _required_text(pending_plugin_version, "pending plugin_version"),
        rollback_plugin_version,
        bool(project.get("enabled")),
        _required_text(project.get("state"), "project state"),
        _required_text(
            project.get("reconcile_state"),
            "project reconcile_state",
        ),
        pending_project_state,
        pending_reconcile_state,
        before_project_record_version,
        before_project_record_version + 1,
        before_policy_generation,
        before_policy_configuration_version,
        before_policy_version,
        generation,
        pending_policy_configuration_version,
        before_policy_version,
        _exact_json_hash(before_tasks),
    )
    if existing is None:
        cursor.execute(
            """
            INSERT INTO automation_project_generation_transitions (
                automation_id, generation, transition_token,
                base_committed_generation, phase,
                before_project_plugin_version,
                pending_project_plugin_version,
                rollback_project_plugin_version,
                before_project_enabled, before_project_state,
                before_project_reconcile_state, pending_project_state,
                pending_project_reconcile_state,
                before_project_record_version, pending_project_record_version,
                before_policy_generation,
                before_policy_configuration_version, before_policy_version,
                pending_policy_generation,
                pending_policy_configuration_version, pending_policy_version,
                before_tasks_sha256, pending_tasks_sha256
            ) VALUES (
                %s, %s, %s, %s, 'PENDING_PROJECTION',
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, NULL
            )
            """,
            (automation_id, generation, *values),
        )
    else:
        old_token = _required_text(
            existing.get("transition_token"),
            "transition_token",
        )
        cursor.execute(
            """
            DELETE FROM automation_project_generation_transition_tasks
            WHERE transition_token=%s
            """,
            (old_token,),
        )
        cursor.execute(
            """
            UPDATE automation_project_generation_transitions
            SET transition_token=%s, base_committed_generation=%s,
                phase='PENDING_PROJECTION',
                before_project_plugin_version=%s,
                pending_project_plugin_version=%s,
                rollback_project_plugin_version=%s,
                before_project_enabled=%s, before_project_state=%s,
                before_project_reconcile_state=%s, pending_project_state=%s,
                pending_project_reconcile_state=%s,
                before_project_record_version=%s,
                pending_project_record_version=%s,
                rolled_back_project_record_version=NULL,
                before_policy_generation=%s,
                before_policy_configuration_version=%s,
                before_policy_version=%s, pending_policy_generation=%s,
                pending_policy_configuration_version=%s,
                pending_policy_version=%s, before_tasks_sha256=%s,
                pending_tasks_sha256=NULL, activated_at=NULL,
                rolled_back_at=NULL, updated_at=NOW(6)
            WHERE automation_id=%s AND generation=%s
              AND transition_token=%s AND phase='ROLLED_BACK'
            """,
            (*values, automation_id, generation, old_token),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConcurrentUpdateError(
                "runtime activation retry transition changed"
            )
    _persist_transition_task_before_image(
        cursor,
        transition_token=transition_token,
        before_tasks=before_tasks,
    )
    return transition_token


def _finish_pending_transition(
    cursor: Any,
    *,
    automation_id: str,
    generation: int,
    transition_token: str,
) -> None:
    pending_tasks = _lock_scheduled_task_before_image(
        cursor,
        automation_id=automation_id,
    )
    cursor.execute(
        """
        UPDATE automation_project_generation_transitions
        SET pending_tasks_sha256=%s, updated_at=NOW(6)
        WHERE automation_id=%s AND generation=%s
          AND transition_token=%s AND phase='PENDING_PROJECTION'
          AND pending_tasks_sha256 IS NULL
        """,
        (
            _exact_json_hash(pending_tasks),
            automation_id,
            generation,
            transition_token,
        ),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        raise ConcurrentUpdateError(
            "runtime activation transition changed before persistence"
        )


def _validate_installed_target_version(
    repository: Any,
    cursor: Any,
    *,
    snapshot: Mapping[str, Any],
) -> Mapping[str, Any]:
    runtime_model, plugin_api = _runtime_contract(snapshot)
    cursor.execute(
        """
        SELECT * FROM automation_plugin_versions
        WHERE plugin_id=%s AND version=%s FOR UPDATE
        """,
        (snapshot["plugin_id"], snapshot["plugin_version"]),
    )
    version = _decode_row(
        _row_dict(cursor, cursor.fetchone()),
        repository._VERSION_JSON_FIELDS,
    )
    if version is None:
        raise OrchestrationPersistenceError(
            "installed target plugin version disappeared"
        )
    if str(version.get("state") or "INSTALLED") not in {
        "INSTALLED",
        "ACTIVE",
    }:
        raise ConcurrentUpdateError(
            "runtime target plugin version is no longer installed"
        )
    for persisted_field, snapshot_field in (
        ("package_sha256", "package_sha256"),
        ("manifest_sha256", "manifest_sha256"),
        ("trust_source", "trust_source"),
        ("runtime_model", "runtime_model"),
        ("plugin_api", "plugin_api"),
        ("tool_contract_sha256", "tool_contract_sha256"),
        ("invocation_contracts_sha256", "invocation_contracts_sha256"),
    ):
        expected = (
            runtime_model
            if persisted_field == "runtime_model"
            else plugin_api
            if persisted_field == "plugin_api"
            else snapshot[snapshot_field]
        )
        actual = version.get(persisted_field)
        if actual in (None, "") and persisted_field == "runtime_model":
            actual = "ACTION_V1"
        if actual in (None, "") and persisted_field == "plugin_api":
            actual = "1.0.0"
        if str(actual or "") != str(expected):
            raise ConcurrentUpdateError(
                f"runtime target {persisted_field} differs from installed package"
            )
    execution_metadata = snapshot.get("execution_metadata")
    if not isinstance(execution_metadata, Mapping):
        raise OrchestrationPersistenceError(
            "runtime target execution metadata is invalid"
        )
    manifest = version.get("manifest_json")
    if not isinstance(manifest, Mapping):
        raise OrchestrationPersistenceError(
            "installed plugin manifest is invalid"
        )
    action_contract_hash = _json_hash(execution_metadata.get("action_contract"))
    if action_contract_hash != str(
        version.get("tool_contract_sha256") or ""
    ) or action_contract_hash != _json_hash(manifest.get("tool_contract")):
        raise ConcurrentUpdateError(
            "runtime target action contract differs from signed package"
        )
    governance_anchor_hash = _json_hash(manifest.get("governance_anchor"))
    if (
        _json_hash(execution_metadata.get("governance_anchor"))
        != governance_anchor_hash
        or str(snapshot.get("governance_anchor_sha256") or "")
        != governance_anchor_hash
    ):
        raise ConcurrentUpdateError(
            "runtime target governance anchor differs from signed package"
        )
    expected_runtime_descriptor = {
        "runtime": manifest.get("runtime"),
        "runtime_permissions": manifest.get("runtime_permissions"),
        "account_roles": manifest.get("account_roles"),
        "resource_roles": manifest.get("resource_roles"),
        "install_metadata": version.get("install_root_metadata_json"),
    }
    runtime_descriptor_hash = _json_hash(expected_runtime_descriptor)
    if (
        _json_hash(execution_metadata.get("runtime_descriptor"))
        != runtime_descriptor_hash
        or str(snapshot.get("runtime_descriptor_sha256") or "")
        != runtime_descriptor_hash
    ):
        raise ConcurrentUpdateError(
            "runtime target descriptor differs from signed installation"
        )
    return version


class AutomationPluginGenerationTransitionRepositoryMixin:
    def complete_generation_activation_row(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_transition_token: str,
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        safe_transition_token = _transition_token(expected_transition_token)
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError(
                    "activation transition project disappeared"
                )
            cursor.execute(
                """
                SELECT * FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            target = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._GENERATION_JSON_FIELDS,
            )
            if target is not None:
                target = _validated_generation_row(target)
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_transitions
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            transition = _row_dict(cursor, cursor.fetchone())
            if (
                transition is None
                or str(transition.get("transition_token") or "")
                != safe_transition_token
            ):
                raise ConcurrentUpdateError(
                    "runtime activation transition token changed"
                )
            phase = str(transition.get("phase") or "")
            if phase == "ACTIVE":
                return
            if phase != "PENDING_PROJECTION":
                raise ConcurrentUpdateError(
                    "runtime activation transition is not completable"
                )
            base_committed = transition.get("base_committed_generation")
            if base_committed is not None:
                base_committed = int(base_committed)
            if (
                target is None
                or str(target.get("state") or "") != "COMMITTED"
                or target.get("base_committed_generation") != base_committed
                or int(project.get("target_generation") or 0) != safe_generation
                or int(project.get("committed_generation") or 0)
                != safe_generation
                or int(project.get("record_version") or 0)
                != int(transition.get("pending_project_record_version") or 0)
                or str(project.get("plugin_version") or "")
                != str(transition.get("pending_project_plugin_version") or "")
                or str(project.get("state") or "")
                != str(transition.get("pending_project_state") or "")
                or str(project.get("reconcile_state") or "")
                != str(transition.get("pending_project_reconcile_state") or "")
                or bool(project.get("enabled"))
                != bool(transition.get("before_project_enabled"))
            ):
                raise ConcurrentUpdateError(
                    "runtime database route changed before activation ACK"
                )
            snapshot = target.get("snapshot_json")
            if not isinstance(snapshot, Mapping):
                raise OrchestrationPersistenceError(
                    "activation target snapshot is invalid"
                )
            _validate_installed_target_version(
                self,
                cursor,
                snapshot=snapshot,
            )
            cursor.execute(
                """
                SELECT project_generation, project_configuration_version,
                       version
                FROM automation_project_policies
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            policy = _row_dict(cursor, cursor.fetchone())
            if (
                policy is None
                or int(policy.get("project_generation") or 0)
                != int(transition.get("pending_policy_generation") or 0)
                or int(policy.get("project_configuration_version") or 0)
                != int(
                    transition.get("pending_policy_configuration_version") or 0
                )
                or int(policy.get("version") or 0)
                != int(transition.get("pending_policy_version") or 0)
            ):
                raise ConcurrentUpdateError(
                    "runtime policy changed before activation ACK"
                )
            pending_tasks_sha256 = str(
                transition.get("pending_tasks_sha256") or ""
            )
            pending_tasks = _lock_scheduled_task_before_image(
                cursor,
                automation_id=safe_automation_id,
            )
            if (
                len(pending_tasks_sha256) != 64
                or _exact_json_hash(pending_tasks) != pending_tasks_sha256
            ):
                raise ConcurrentUpdateError(
                    "runtime scheduler projection changed before activation ACK"
                )
            cursor.execute(
                """
                UPDATE automation_project_generation_transitions
                SET phase='ACTIVE', activated_at=NOW(6), updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s
                  AND transition_token=%s AND phase='PENDING_PROJECTION'
                """,
                (
                    safe_automation_id,
                    safe_generation,
                    safe_transition_token,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "runtime activation transition changed before ACK"
                )

    def rollback_generation_cas_row(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_base_committed_generation: int | None,
        expected_transition_token: str,
    ) -> dict[str, Any]:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        expected_base = _optional_positive_int(
            expected_base_committed_generation,
            "expected_base_committed_generation",
        )
        safe_transition_token = _transition_token(expected_transition_token)
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError(
                    "activation rollback project disappeared"
                )
            cursor.execute(
                """
                SELECT * FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            target = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._GENERATION_JSON_FIELDS,
            )
            if target is not None:
                target = _validated_generation_row(target)
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_transitions
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            transition = _row_dict(cursor, cursor.fetchone())
            if (
                transition is None
                or str(transition.get("transition_token") or "")
                != safe_transition_token
                or transition.get("base_committed_generation") != expected_base
            ):
                raise ConcurrentUpdateError(
                    "runtime activation rollback transition changed"
                )
            phase = str(transition.get("phase") or "")
            if phase not in {"PENDING_PROJECTION", "ROLLED_BACK"}:
                raise ConcurrentUpdateError(
                    "runtime activation transition is not rollback eligible"
                )
            base_generation: Mapping[str, Any] | None = None
            base_snapshot: Mapping[str, Any] | None = None
            if expected_base is not None:
                cursor.execute(
                    """
                    SELECT * FROM automation_project_generations
                    WHERE automation_id=%s AND generation=%s FOR UPDATE
                    """,
                    (safe_automation_id, expected_base),
                )
                decoded_base = _decode_row(
                    _row_dict(cursor, cursor.fetchone()),
                    self._GENERATION_JSON_FIELDS,
                )
                if decoded_base is not None:
                    base_generation = _validated_generation_row(decoded_base)
                    raw_base_snapshot = base_generation.get("snapshot_json")
                    if isinstance(raw_base_snapshot, Mapping):
                        base_snapshot = raw_base_snapshot
            expected_target_state = (
                "PREPARED" if phase == "ROLLED_BACK" else "COMMITTED"
            )
            expected_base_state = (
                "COMMITTED" if phase == "ROLLED_BACK" else "DRAINING"
            )
            if (
                target is None
                or str(target.get("state") or "") != expected_target_state
                or target.get("base_committed_generation") != expected_base
                or (
                    expected_base is not None
                    and (
                        base_generation is None
                        or base_snapshot is None
                        or str(base_generation.get("state") or "")
                        != expected_base_state
                        or str(base_snapshot.get("plugin_id") or "")
                        != str(target.get("plugin_id") or "")
                        or str(base_snapshot.get("plugin_version") or "")
                        != str(
                            transition.get("rollback_project_plugin_version")
                            or ""
                        )
                    )
                )
            ):
                raise ConcurrentUpdateError(
                    "runtime generation lineage is not rollback recoverable"
                )
            cursor.execute(
                """
                SELECT project_generation, project_configuration_version,
                       version
                FROM automation_project_policies
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            policy = _row_dict(cursor, cursor.fetchone())
            policy_prefix = (
                "before" if phase == "ROLLED_BACK" else "pending"
            )
            if (
                policy is None
                or int(policy.get("project_generation") or 0)
                != int(transition.get(f"{policy_prefix}_policy_generation") or 0)
                or int(policy.get("project_configuration_version") or 0)
                != int(
                    transition.get(
                        f"{policy_prefix}_policy_configuration_version"
                    )
                    or 0
                )
                or int(policy.get("version") or 0)
                != int(transition.get(f"{policy_prefix}_policy_version") or 0)
            ):
                raise ConcurrentUpdateError(
                    "runtime policy changed before activation rollback"
                )
            task_hash_field = (
                "before_tasks_sha256"
                if phase == "ROLLED_BACK"
                else "pending_tasks_sha256"
            )
            expected_tasks_sha256 = str(
                transition.get(task_hash_field) or ""
            )
            current_tasks = _lock_scheduled_task_before_image(
                cursor,
                automation_id=safe_automation_id,
            )
            if (
                len(expected_tasks_sha256) != 64
                or _exact_json_hash(current_tasks) != expected_tasks_sha256
            ):
                raise ConcurrentUpdateError(
                    "runtime scheduler projection changed before activation rollback"
                )
            project_record_field = (
                "rolled_back_project_record_version"
                if phase == "ROLLED_BACK"
                else "pending_project_record_version"
            )
            expected_project_record_version = int(
                transition.get(project_record_field) or 0
            )
            expected_committed = (
                expected_base if phase == "ROLLED_BACK" else safe_generation
            )
            expected_plugin_version = str(
                transition.get(
                    "rollback_project_plugin_version"
                    if phase == "ROLLED_BACK"
                    else "pending_project_plugin_version"
                )
                or ""
            )
            expected_project_state = str(
                transition.get(
                    "before_project_state"
                    if phase == "ROLLED_BACK"
                    else "pending_project_state"
                )
                or ""
            )
            expected_reconcile_state = str(
                transition.get(
                    "before_project_reconcile_state"
                    if phase == "ROLLED_BACK"
                    else "pending_project_reconcile_state"
                )
                or ""
            )
            current_project_committed = project.get("committed_generation")
            if current_project_committed is not None:
                current_project_committed = int(current_project_committed)
            if (
                int(project.get("target_generation") or 0) != safe_generation
                or current_project_committed != expected_committed
                or int(project.get("record_version") or 0)
                != expected_project_record_version
                or str(project.get("plugin_version") or "")
                != expected_plugin_version
                or str(project.get("state") or "") != expected_project_state
                or str(project.get("reconcile_state") or "")
                != expected_reconcile_state
                or bool(project.get("enabled"))
                != bool(transition.get("before_project_enabled"))
            ):
                raise ConcurrentUpdateError(
                    "runtime database route changed before activation rollback"
                )
            if phase == "ROLLED_BACK":
                row = self.get_project_runtime_row(
                    safe_automation_id,
                    for_update=True,
                )
                if row is None:
                    raise OrchestrationPersistenceError(
                        "rolled back project runtime disappeared"
                    )
                return row
            _assert_transition_target_has_no_generation_leases(
                cursor,
                automation_id=safe_automation_id,
                generation=safe_generation,
            )
            _restore_transition_task_before_image(
                cursor,
                automation_id=safe_automation_id,
                transition_token=safe_transition_token,
            )
            restored_tasks = _lock_scheduled_task_before_image(
                cursor,
                automation_id=safe_automation_id,
            )
            if _exact_json_hash(restored_tasks) != str(
                transition.get("before_tasks_sha256") or ""
            ):
                raise OrchestrationPersistenceError(
                    "scheduled task rollback before-image is inconsistent"
                )
            if expected_base is not None:
                cursor.execute(
                    """
                    UPDATE automation_project_generations
                    SET state='COMMITTED', draining_at=NULL,
                        error_code=NULL, error_summary=NULL,
                        record_version=record_version+1, updated_at=NOW(6)
                    WHERE automation_id=%s AND generation=%s
                      AND state='DRAINING'
                    """,
                    (safe_automation_id, expected_base),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError(
                        "previous runtime generation changed during rollback"
                    )
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='PREPARED', committed_at=NULL, draining_at=NULL,
                    error_code=NULL, error_summary=NULL,
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s
                  AND state='COMMITTED'
                  AND base_committed_generation <=> %s
                """,
                (safe_automation_id, safe_generation, expected_base),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "target runtime generation changed during rollback"
                )
            cursor.execute(
                """
                UPDATE automation_project_policies
                SET project_generation=%s,
                    project_configuration_version=%s,
                    updated_at=NOW(6)
                WHERE automation_id=%s AND project_generation=%s
                  AND project_configuration_version=%s AND version=%s
                """,
                (
                    int(transition["before_policy_generation"]),
                    int(transition["before_policy_configuration_version"]),
                    safe_automation_id,
                    int(transition["pending_policy_generation"]),
                    int(transition["pending_policy_configuration_version"]),
                    int(transition["pending_policy_version"]),
                ),
            )
            cursor.execute(
                """
                SELECT project_generation, project_configuration_version,
                       version
                FROM automation_project_policies
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            restored_policy = _row_dict(cursor, cursor.fetchone())
            if (
                restored_policy is None
                or int(restored_policy.get("project_generation") or 0)
                != int(transition["before_policy_generation"])
                or int(
                    restored_policy.get("project_configuration_version") or 0
                )
                != int(transition["before_policy_configuration_version"])
                or int(restored_policy.get("version") or 0)
                != int(transition["before_policy_version"])
            ):
                raise ConcurrentUpdateError(
                    "runtime policy changed during activation rollback"
                )
            rolled_back_record_version = (
                int(transition["pending_project_record_version"]) + 1
            )
            cursor.execute(
                """
                UPDATE automation_projects
                SET plugin_version=%s, committed_generation=%s,
                    state=%s, reconcile_state=%s,
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND target_generation=%s
                  AND committed_generation=%s AND plugin_version=%s
                  AND state=%s AND reconcile_state=%s AND enabled=%s
                  AND record_version=%s
                """,
                (
                    str(transition["rollback_project_plugin_version"]),
                    expected_base,
                    str(transition["before_project_state"]),
                    str(transition["before_project_reconcile_state"]),
                    safe_automation_id,
                    safe_generation,
                    safe_generation,
                    str(transition["pending_project_plugin_version"]),
                    str(transition["pending_project_state"]),
                    str(transition["pending_project_reconcile_state"]),
                    bool(transition["before_project_enabled"]),
                    int(transition["pending_project_record_version"]),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "project runtime route rollback CAS failed"
                )
            cursor.execute(
                """
                UPDATE automation_project_generation_transitions
                SET phase='ROLLED_BACK',
                    rolled_back_project_record_version=%s,
                    rolled_back_at=NOW(6), updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s
                  AND transition_token=%s AND phase='PENDING_PROJECTION'
                """,
                (
                    rolled_back_record_version,
                    safe_automation_id,
                    safe_generation,
                    safe_transition_token,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "runtime activation transition changed during rollback"
                )
        row = self.get_project_runtime_row(safe_automation_id, for_update=True)
        if row is None:
            raise OrchestrationPersistenceError(
                "project runtime disappeared after activation rollback"
            )
        return row

    def block_generation_activation_row(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_transition_token: str,
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        safe_transition_token = _transition_token(expected_transition_token)
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT transition_token, phase
                FROM automation_project_generation_transitions
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            transition = _row_dict(cursor, cursor.fetchone())
            if (
                transition is None
                or str(transition.get("transition_token") or "")
                != safe_transition_token
            ):
                raise ConcurrentUpdateError(
                    "runtime activation block transition token changed"
                )
            phase = str(transition.get("phase") or "")
            if phase == "BLOCKED":
                return
            if phase != "PENDING_PROJECTION":
                raise ConcurrentUpdateError(
                    "runtime activation transition is not blockable"
                )
            cursor.execute(
                """
                UPDATE automation_project_generation_transitions
                SET phase='BLOCKED', updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s
                  AND transition_token=%s AND phase='PENDING_PROJECTION'
                """,
                (
                    safe_automation_id,
                    safe_generation,
                    safe_transition_token,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "runtime activation transition changed before block"
                )
