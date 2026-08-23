"""Fail-closed release validation for migrated first-party automation projects.

Own the post-018 contract without importing Agent runtime packages through ``sys.path``.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent
AUTOMATION_PROJECT_AUTHORIZATION_VERSION = "018"
EXPECTED_TEMPLATE_COUNT = 18
EXPECTED_RELEASE_PROJECT_COUNT = 16
EXPECTED_RELEASE_SCHEDULE_COUNT = 57
EXPECTED_DEFERRED_PROJECT_COUNT = 2
EXPECTED_DEFERRED_SCHEDULE_COUNT = 14
EXPECTED_REVIEWED_SCHEDULE_COUNT = 71
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_QUARTER_HOUR_DAILY_TIMES = tuple(
    f"{hour:02d}:{minute:02d}"
    for hour in range(24)
    for minute in (0, 15, 30, 45)
)
class AutomationProjectReleaseManifestError(RuntimeError):
    """A value-free reason a post-018 release must remain held."""
    def __init__(self, code: str, *, count: int = 1) -> None:
        super().__init__(code)
        self.code = str(code)
        self.count = max(int(count), 1)
def _target_runtime_stage_is_recoverable(row: Mapping[str, Any], committed_generation: Any, target_generation: Any) -> bool:
    target_state = row.get("target_generation_state")
    target_base_generation = row.get("target_base_generation")
    if target_state is None:
        return bool(
            row.get("project_state") in {"INSTALLED", "ENABLED", "DISABLED", "UPGRADING"}
            and row.get("reconcile_state") == "PREPARING"
            and target_base_generation is None and row.get("max_generation") == committed_generation
        )
    return bool(
        row.get("project_state") in {"ENABLED", "UPGRADING"} and target_base_generation == committed_generation
        and row.get("max_generation") == target_generation
        and (target_state, row.get("reconcile_state"))
        in {
            ("TARGET", "PREPARING"), ("PREPARING", "PREPARING"),
            ("WAITING_COEFFECTS", "WAITING_COEFFECTS"),
            ("PREPARED", "READY_TO_COMMIT"),
        }
    )
def is_staged_unknown_write_quarantine(row: Mapping[str, Any]) -> bool:
    """Recognize only the lease-backed interrupted-upgrade safety fence."""
    committed_generation = row.get("committed_generation")
    target_generation = row.get("target_generation")
    return bool(
        type(committed_generation) is int
        and type(target_generation) is int
        and target_generation == committed_generation + 1
        and row.get("policy_project_generation") == target_generation
        and row.get("generation_state") == "BLOCKED"
        and row.get("generation_error_code") == "WRITE_OUTCOME_UNKNOWN"
        and _target_runtime_stage_is_recoverable(row, committed_generation, target_generation)
        and type(row.get("unsafe_non_disposed_other_count")) is int
        and row.get("unsafe_non_disposed_other_count") == 0
        and type(row.get("active_current_lease_count")) is int
        and row.get("active_current_lease_count") == 0
        and type(row.get("unknown_write_count")) is int
        and row.get("unknown_write_count") > 0
    )
def is_staged_recoverable_runtime(row: Mapping[str, Any]) -> bool:
    """Recognize an un-runnable target at a repository-produced recovery point."""

    committed_generation = row.get("committed_generation")
    target_generation = row.get("target_generation")
    max_generation = row.get("max_generation")
    return bool(
        type(committed_generation) is int
        and type(target_generation) is int
        and type(max_generation) is int
        and target_generation == committed_generation + 1
        and row.get("policy_project_generation") == target_generation
        and row.get("generation_state") == "COMMITTED"
        and row.get("generation_error_code") is None
        and _target_runtime_stage_is_recoverable(row, committed_generation, target_generation)
        and type(row.get("unsafe_non_disposed_other_count")) is int
        and row.get("unsafe_non_disposed_other_count") == 0
        and row.get("active_current_lease_count") == 0
        and row.get("unknown_write_count") == 0
    )


_PROJECT_STATE_DIAGNOSTIC_VALUES = frozenset(
    {"INSTALLED", "ENABLED", "DISABLED", "UPGRADING", "UNINSTALLING", "ERROR"}
)
_RECONCILE_STATE_DIAGNOSTIC_VALUES = frozenset({
    "STABLE", "PREPARING", "WAITING_COEFFECTS", "READY_TO_COMMIT",
    "DRAINING", "DISPOSING", "BLOCKED_UNKNOWN_WRITE", "ERROR",
})
_GENERATION_STATE_DIAGNOSTIC_VALUES = frozenset(
    {
        "TARGET",
        "PREPARING",
        "WAITING_COEFFECTS",
        "PREPARED",
        "COMMITTED",
        "DRAINING",
        "DISPOSING",
        "DISPOSED",
        "FAILED",
        "BLOCKED",
    }
)


def _safe_enum_diagnostic(
    value: Any,
    *,
    allowed: frozenset[str],
) -> str:
    if value is None:
        return "ABSENT"
    if type(value) is not str:
        return "INVALID"
    return value if value in allowed else "OTHER"


def _generation_relation_diagnostic(value: Any, committed: Any) -> str:
    if value is None:
        return "ABSENT"
    if type(value) is not int or type(committed) is not int:
        return "INVALID"
    if value > committed:
        return "AHEAD"
    if value == committed:
        return "MATCH"
    return "BEHIND"


def _committed_error_diagnostic(value: Any) -> str:
    if value is None:
        return "NONE"
    if type(value) is not str:
        return "INVALID"
    if value == "WRITE_OUTCOME_UNKNOWN":
        return "WRITE_OUTCOME_UNKNOWN"
    return "OTHER"


def _unknown_write_lease_diagnostic(value: Any) -> str:
    if type(value) is not int or value < 0:
        return "INVALID"
    if value == 0:
        return "ZERO"
    if value == 1:
        return "ONE"
    return "MULTIPLE"


def _next_generation_diagnostic(value: Any, maximum: Any) -> str:
    if value is None or maximum is None:
        return "ABSENT"
    if type(value) is not int or type(maximum) is not int:
        return "INVALID"
    if value == maximum + 1:
        return "EXACT"
    if value <= maximum:
        return "NOT_AHEAD"
    return "SKIPPED"


def _scheduled_write_runtime_validation_issue(
    row: Mapping[str, Any],
    *,
    seen_task_ids: set[str],
    isolated_runtime: bool,
) -> str | None:
    """Return a fixed, value-free category for the first invalid binding."""

    task_id = row.get("task_id")
    automation_id = row.get("automation_id")
    task_generation = row.get("automation_generation")
    committed_generation = row.get("committed_generation")
    if type(task_id) is not str or not task_id:
        return "TASK_ID_INVALID"
    if task_id in seen_task_ids:
        return "TASK_ID_DUPLICATE"
    if type(automation_id) is not str or not automation_id:
        return "AUTOMATION_ID_INVALID"
    if type(task_generation) is not int or task_generation <= 0:
        return "TASK_GENERATION_INVALID"
    if type(committed_generation) is not int:
        return "COMMITTED_GENERATION_INVALID"
    if task_generation != committed_generation:
        return "TASK_GENERATION_MISMATCH"
    if row.get("tool_name") != f"automation.{automation_id}.run":
        return "TOOL_BINDING_INVALID"
    if (
        type(row.get("enabled")) not in {bool, int}
        or row.get("enabled") not in {True, 1}
    ):
        return "TASK_ENABLED_INVALID"
    if (
        type(row.get("project_enabled")) not in {bool, int}
        or row.get("project_enabled") not in {True, 1}
    ):
        return "PROJECT_ENABLED_INVALID"
    if (
        row.get("project_state") != "ENABLED" and not isolated_runtime
    ) or (
        row.get("project_state") == "ENABLED"
        and row.get("reconcile_state") in {"PREPARING", "WAITING_COEFFECTS", "READY_TO_COMMIT"}
        and not isolated_runtime and not is_staged_recoverable_runtime(row)
    ):
        return "PROJECT_STATE_INVALID"
    if row.get("generation_state") not in {"COMMITTED", "BLOCKED"}:
        return "COMMITTED_STATE_INVALID"
    return None


def _project_schedule_runtime_invalid_code(
    row: Mapping[str, Any],
    *,
    issue: str,
) -> str:
    """Build a bounded diagnostic code without projecting database values."""

    committed_generation = row.get("committed_generation")
    return "__".join(
        (
            "PROJECT_SCHEDULE_RUNTIME_INVALID",
            f"CHECK_{issue}",
            "PROJECT_STATE_"
            + _safe_enum_diagnostic(
                row.get("project_state"),
                allowed=_PROJECT_STATE_DIAGNOSTIC_VALUES,
            ),
            "RECONCILE_STATE_"
            + _safe_enum_diagnostic(
                row.get("reconcile_state"),
                allowed=_RECONCILE_STATE_DIAGNOSTIC_VALUES,
            ),
            "TARGET_RELATION_"
            + _generation_relation_diagnostic(
                row.get("target_generation"),
                committed_generation,
            ),
            "POLICY_TARGET_RELATION_"
            + _generation_relation_diagnostic(
                row.get("policy_project_generation"),
                row.get("target_generation"),
            ),
            "MAX_COMMITTED_RELATION_"
            + _generation_relation_diagnostic(
                row.get("max_generation"),
                committed_generation,
            ),
            "TARGET_MAX_NEXT_"
            + _next_generation_diagnostic(
                row.get("target_generation"),
                row.get("max_generation"),
            ),
            "UNSAFE_NON_DISPOSED_OTHERS_"
            + _unknown_write_lease_diagnostic(
                row.get("unsafe_non_disposed_other_count")
            ),
            "ACTIVE_CURRENT_LEASES_"
            + _unknown_write_lease_diagnostic(
                row.get("active_current_lease_count")
            ),
            "COMMITTED_STATE_"
            + _safe_enum_diagnostic(
                row.get("generation_state"),
                allowed=_GENERATION_STATE_DIAGNOSTIC_VALUES,
            ),
            "COMMITTED_ERROR_"
            + _committed_error_diagnostic(row.get("generation_error_code")),
            "TARGET_STATE_"
            + _safe_enum_diagnostic(
                row.get("target_generation_state"),
                allowed=_GENERATION_STATE_DIAGNOSTIC_VALUES,
            ),
            "TARGET_BASE_RELATION_"
            + _generation_relation_diagnostic(
                row.get("target_base_generation"),
                committed_generation,
            ),
            "UNKNOWN_WRITE_LEASES_"
            + _unknown_write_lease_diagnostic(row.get("unknown_write_count")),
        )
    )


def typed_project_scheduled_write_crons(
    cursor: Any,
    *,
    error_class: type[Exception],
    scheduled_write_operation_types: frozenset[str],
) -> tuple[str, ...]:
    """Return active signed project write crons, excluding exact quarantine."""

    cursor.execute(
        """
        SELECT task.id AS task_id,
               task.automation_id,
               task.automation_generation,
               task.tool_name,
               task.cron_expression,
               task.enabled,
               project.committed_generation,
               project.target_generation,
               project.enabled AS project_enabled,
               project.state AS project_state,
               project.reconcile_state,
               policy.mode AS policy_mode,
               policy.project_generation AS policy_project_generation,
               generation.state AS generation_state,
               generation.error_code AS generation_error_code,
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
        FROM scheduled_tasks AS task
        INNER JOIN automation_projects AS project
          ON BINARY project.automation_id=BINARY task.automation_id
        INNER JOIN automation_project_policies AS policy
          ON BINARY policy.automation_id=BINARY project.automation_id
        INNER JOIN automation_project_generations AS generation
          ON BINARY generation.automation_id=BINARY project.automation_id
         AND generation.generation=project.committed_generation
        LEFT JOIN automation_project_generations AS target_generation
          ON BINARY target_generation.automation_id=BINARY project.automation_id
         AND target_generation.generation=project.target_generation
        WHERE task.enabled=TRUE
          AND task.automation_id IS NOT NULL
          AND task.tool_name LIKE 'automation.%.run'
        ORDER BY BINARY task.id
        """
    )
    crons: list[str] = []
    seen_task_ids: set[str] = set()
    for row in cursor.fetchall():
        if not isinstance(row, Mapping):
            raise error_class(
                "PROJECT_SCHEDULE_RUNTIME_INVALID__CHECK_ROW_SHAPE_INVALID"
            )
        task_id = row.get("task_id")
        quarantined_unknown_write = is_staged_unknown_write_quarantine(row)
        isolated_runtime = quarantined_unknown_write or (is_staged_recoverable_runtime(row) and row.get("project_state") != "ENABLED")
        runtime_issue = _scheduled_write_runtime_validation_issue(
            row,
            seen_task_ids=seen_task_ids,
            isolated_runtime=isolated_runtime,
        )
        if runtime_issue is not None:
            raise error_class(
                _project_schedule_runtime_invalid_code(
                    row,
                    issue=runtime_issue,
                )
            )
        seen_task_ids.add(task_id)
        automation_id = row.get("automation_id")
        committed_generation = row.get("committed_generation")
        policy_mode = row.get("policy_mode")
        if policy_mode not in {
            "PROJECT_FULL_AUTO",
            "LEGACY_SCHEDULE_ONLY",
            "REQUIRE_EACH_RUN",
        }:
            raise error_class("PROJECT_SCHEDULE_POLICY_INVALID")
        try:
            snapshot = _decode_json(
                row.get("snapshot_json"),
                code="PROJECT_SCHEDULE_CONTRACT_INVALID",
            )
        except AutomationProjectReleaseManifestError as exc:
            raise error_class("PROJECT_SCHEDULE_CONTRACT_INVALID") from exc
        execution_metadata = snapshot.get("execution_metadata")
        governance_anchor = (
            execution_metadata.get("governance_anchor")
            if isinstance(execution_metadata, Mapping)
            else None
        )
        compiled_invocations = (
            execution_metadata.get("compiled_invocations")
            if isinstance(execution_metadata, Mapping)
            else None
        )
        if (
            snapshot.get("automation_id") != automation_id
            or snapshot.get("generation") != committed_generation
            or not isinstance(governance_anchor, Mapping)
            or not isinstance(compiled_invocations, Mapping)
            or "scheduler" not in compiled_invocations
        ):
            raise error_class("PROJECT_SCHEDULE_CONTRACT_INVALID")
        scheduled_write = (
            policy_mode in {"PROJECT_FULL_AUTO", "LEGACY_SCHEDULE_ONLY"}
            and governance_anchor.get("operation_type")
            in scheduled_write_operation_types
        )
        if scheduled_write:
            cron_expression = row.get("cron_expression")
            if type(cron_expression) is not str or not cron_expression:
                raise error_class("PROJECT_SCHEDULE_CRON_INVALID")
        if isolated_runtime:
            # The committed generation is fenced or a non-ENABLED target is reconciling.
            continue
        if scheduled_write:
            crons.append(cron_expression)
    return tuple(sorted(set(crons)))


def _shared_module_snapshot() -> dict[str, object]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "shared" or name.startswith("shared.")
    }


def _clear_shared_modules() -> None:
    for name in tuple(sys.modules):
        if name == "shared" or name.startswith("shared."):
            sys.modules.pop(name, None)


def _load_exact_module(
    module_name: str,
    path: Path,
    *,
    submodule_search_locations: Sequence[str] | None = None,
    register: bool = True,
) -> Any:
    spec = importlib.util.spec_from_file_location(
        module_name,
        path,
        submodule_search_locations=submodule_search_locations,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("exact-path module spec is unavailable")
    module = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


credential_policy_history = _load_exact_module(
    "_automation_project_policy_history",
    PROJECT_ROOT / "scripts" / "automation_project_policy_history.py",
    register=False,
)
plugin_policy_history = _load_exact_module(
    "_automation_project_plugin_policy_history",
    PROJECT_ROOT / "scripts" / "automation_project_plugin_policy_history.py",
    register=False,
)
def _load_release_contract() -> dict[str, Any]:
    """Load staged code-owned identities without leaking ``shared`` modules."""

    shared_dir = REPOSITORY_ROOT / "shared"
    manifest_path = shared_dir / "automation_project_manifest.py"
    repository_path = shared_dir / "automation_plugin_repository.py"
    policy_repository_path = shared_dir / "automation_project_policy_repository.py"
    release_scope_path = (
        PROJECT_ROOT / "agent" / "automation_plugins" / "release_scope.py"
    )
    if not all(
        path.is_file()
        for path in (
            shared_dir / "__init__.py",
            manifest_path,
            repository_path,
            policy_repository_path,
            release_scope_path,
        )
    ):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_RELEASE_CONTRACT_MISSING"
        )

    previous_shared = _shared_module_snapshot()
    previous_scope = sys.modules.get("_boyi_release_manifest_scope")
    _clear_shared_modules()
    sys.modules.pop("_boyi_release_manifest_scope", None)
    try:
        _load_exact_module(
            "shared",
            shared_dir / "__init__.py",
            submodule_search_locations=[str(shared_dir)],
        )
        manifest = _load_exact_module(
            "shared.automation_project_manifest",
            manifest_path,
        )
        repository = _load_exact_module(
            "shared.automation_plugin_repository",
            repository_path,
        )
        policy_repository = _load_exact_module(
            "shared.automation_project_policy_repository",
            policy_repository_path,
        )
        release_scope = _load_exact_module(
            "_boyi_release_manifest_scope",
            release_scope_path,
        )
        templates = getattr(
            manifest,
            "FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES",
            None,
        )
        release_plugin_ids = getattr(
            release_scope,
            "RUNNABLE_SERVER_FIRST_PARTY_PLUGIN_IDS",
            None,
        )
        deferred_plugin_ids = getattr(
            release_scope,
            "DEFERRED_R7_PLUGIN_IDS",
            None,
        )
        deferred_generation = getattr(
            release_scope,
            "DEFERRED_R7_LEGACY_SCHEDULE_GENERATION",
            None,
        )
        stable_schedule_task_id = getattr(
            repository,
            "_stable_schedule_task_id",
            None,
        )
        validate_generation_row = getattr(
            repository,
            "_validated_generation_row",
            None,
        )
        evidence_function_names = (
            "automation_project_configuration_bootstrap_request_id",
            "automation_project_policy_bootstrap_request_id",
            "automation_project_bootstrap_source_snapshot_sha256",
            "derive_automation_project_bootstrap_source_snapshot",
            "legacy_scheduled_policy_grant_request_id",
            "validate_automation_project_bootstrap_source_snapshot",
            "validate_automation_project_bootstrap_policy_event",
            "validate_automation_project_configuration_evidence",
            "validate_existing_automation_project_bootstrap",
            "validate_initial_automation_project_bootstrap_policy",
            "validate_legacy_scheduled_grant_event",
            "validate_persisted_automation_project_configuration_evidence",
            "validate_plugin_configuration_retirement_event",
        )
        bootstrap_evidence = {
            name: getattr(policy_repository, name, None)
            for name in evidence_function_names
        }
        bootstrap_evidence.update(
            {
                "error_class": getattr(
                    policy_repository,
                    "AutomationProjectBootstrapContractError",
                    None,
                ),
                "actor_id": getattr(
                    policy_repository,
                    "AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ID",
                    None,
                ),
                "actor_role": getattr(
                    policy_repository,
                    "AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ROLE",
                    None,
                ),
                "reason": getattr(
                    policy_repository,
                    "AUTOMATION_PROJECT_BOOTSTRAP_REASON",
                    None,
                ),
                "completed_by": getattr(
                    policy_repository,
                    "AUTOMATION_PROJECT_BOOTSTRAP_COMPLETED_BY",
                    None,
                ),
                "plugin_actor_id": getattr(
                    policy_repository,
                    "PLUGIN_CONFIGURATION_ACTOR_ID",
                    None,
                ),
                "plugin_actor_role": getattr(
                    policy_repository,
                    "PLUGIN_CONFIGURATION_ACTOR_ROLE",
                    None,
                ),
                "plugin_reason": getattr(
                    policy_repository,
                    "PLUGIN_CONFIGURATION_REASON",
                    None,
                ),
            }
        )
        if (
            not isinstance(templates, Mapping)
            or not isinstance(release_plugin_ids, (set, frozenset))
            or not isinstance(deferred_plugin_ids, (set, frozenset))
            or isinstance(deferred_generation, bool)
            or not isinstance(deferred_generation, int)
            or deferred_generation <= 0
            or not callable(stable_schedule_task_id)
            or not callable(validate_generation_row)
            or any(
                not callable(bootstrap_evidence[name])
                for name in evidence_function_names
            )
            or not isinstance(bootstrap_evidence["error_class"], type)
            or any(
                type(bootstrap_evidence[name]) is not str
                or not bootstrap_evidence[name]
                for name in (
                    "actor_id",
                    "actor_role",
                    "reason",
                    "completed_by",
                    "plugin_actor_id",
                    "plugin_actor_role",
                    "plugin_reason",
                )
            )
        ):
            raise RuntimeError("release contract objects are invalid")

        normalized_templates: dict[str, dict[str, Any]] = {}
        task_to_automation: dict[str, str] = {}
        for template_key, template in templates.items():
            automation_id = getattr(template, "automation_id", None)
            tool_name = getattr(template, "tool_name", None)
            task_ids = getattr(template, "scheduled_task_ids", None)
            legacy_arguments = getattr(template, "legacy_arguments", None)
            legacy_account_bindings = getattr(
                template,
                "legacy_account_bindings",
                None,
            )
            if (
                type(template_key) is not str
                or type(automation_id) is not str
                or automation_id != template_key
                or type(tool_name) is not str
                or not isinstance(task_ids, (set, frozenset))
                or not isinstance(legacy_arguments, Mapping)
                or not isinstance(legacy_account_bindings, Mapping)
            ):
                raise RuntimeError("migration template is invalid")
            normalized_task_ids: set[str] = set()
            for task_id in task_ids:
                if type(task_id) is not str or task_id in task_to_automation:
                    raise RuntimeError("reviewed schedule identity is invalid")
                task_to_automation[task_id] = automation_id
                normalized_task_ids.add(task_id)
            normalized_templates[automation_id] = {
                "automation_id": automation_id,
                "tool_name": tool_name,
                "task_ids": frozenset(normalized_task_ids),
                "legacy_arguments": copy.deepcopy(dict(legacy_arguments)),
                "legacy_account_bindings": copy.deepcopy(
                    dict(legacy_account_bindings)
                ),
            }
    except AutomationProjectReleaseManifestError:
        raise
    except Exception as exc:
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_RELEASE_CONTRACT_INVALID"
        ) from exc
    finally:
        _clear_shared_modules()
        sys.modules.update(previous_shared)
        sys.modules.pop("_boyi_release_manifest_scope", None)
        if previous_scope is not None:
            sys.modules["_boyi_release_manifest_scope"] = previous_scope

    release_projects = frozenset(
        automation_id
        for automation_id, template in normalized_templates.items()
        if template["tool_name"] in release_plugin_ids
    )
    deferred_projects = frozenset(
        automation_id
        for automation_id, template in normalized_templates.items()
        if template["tool_name"] in deferred_plugin_ids
    )
    release_tasks = frozenset(
        task_id
        for automation_id in release_projects
        for task_id in normalized_templates[automation_id]["task_ids"]
    )
    deferred_tasks = frozenset(
        task_id
        for automation_id in deferred_projects
        for task_id in normalized_templates[automation_id]["task_ids"]
    )
    all_tasks = release_tasks | deferred_tasks
    contract_shape = (
        len(normalized_templates) == EXPECTED_TEMPLATE_COUNT
        and len(release_projects) == EXPECTED_RELEASE_PROJECT_COUNT
        and len(release_tasks) == EXPECTED_RELEASE_SCHEDULE_COUNT
        and len(deferred_projects) == EXPECTED_DEFERRED_PROJECT_COUNT
        and len(deferred_tasks) == EXPECTED_DEFERRED_SCHEDULE_COUNT
        and len(all_tasks) == EXPECTED_REVIEWED_SCHEDULE_COUNT
        and not (release_projects & deferred_projects)
        and set(normalized_templates) == release_projects | deferred_projects
        and set(task_to_automation) == all_tasks
        and {
            normalized_templates[item]["tool_name"] for item in deferred_projects
        }
        == set(deferred_plugin_ids)
    )
    if not contract_shape:
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_RELEASE_CONTRACT_SET_INVALID"
        )
    return {
        "templates": normalized_templates,
        "task_to_automation": task_to_automation,
        "release_plugin_ids": frozenset(release_plugin_ids),
        "deferred_plugin_ids": frozenset(deferred_plugin_ids),
        "deferred_generation": deferred_generation,
        "stable_schedule_task_id": stable_schedule_task_id,
        "validate_generation_row": validate_generation_row,
        "bootstrap_evidence": bootstrap_evidence,
        "release_projects": release_projects,
        "deferred_projects": deferred_projects,
        "release_tasks": release_tasks,
        "deferred_tasks": deferred_tasks,
        "all_tasks": all_tasks,
    }


def _strict_json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _strict_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def _decode_json(value: Any, *, code: str) -> Any:
    if isinstance(value, (dict, list, bool, int, float)) or value is None:
        return copy.deepcopy(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AutomationProjectReleaseManifestError(code) from exc
    if type(value) is str:
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise AutomationProjectReleaseManifestError(code) from exc
    raise AutomationProjectReleaseManifestError(code)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _positive_int(value: Any, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AutomationProjectReleaseManifestError(code)
    return value


def _boolean(value: Any, *, code: str) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in {0, 1}:
        return bool(value)
    raise AutomationProjectReleaseManifestError(code)


def _valid_sha256(value: Any) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _contains_broker_account_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key or "").strip().lower()
            if (
                normalized in {"account_id", "account_ids"}
                or normalized.endswith(("_account_id", "_account_ids"))
                or _contains_broker_account_field(nested)
            ):
                return True
    elif isinstance(value, list):
        return any(_contains_broker_account_field(item) for item in value)
    return False


def _normalized_schedule_expressions(value: Any) -> tuple[tuple[str, ...], bool]:
    schedule = _decode_json(value, code="PROJECT_SCHEDULE_CONFIG_INVALID")
    if not isinstance(schedule, dict) or set(schedule) != {"kind", "times", "enabled"}:
        raise AutomationProjectReleaseManifestError("PROJECT_SCHEDULE_CONFIG_INVALID")
    kind = schedule.get("kind")
    times = schedule.get("times")
    enabled = schedule.get("enabled")
    if (
        kind not in {"none", "daily_times", "startup"}
        or type(enabled) is not bool
        or not isinstance(times, list)
        or any(type(item) is not str for item in times)
    ):
        raise AutomationProjectReleaseManifestError("PROJECT_SCHEDULE_CONFIG_INVALID")
    if kind == "none":
        if times or enabled:
            raise AutomationProjectReleaseManifestError("PROJECT_SCHEDULE_CONFIG_INVALID")
        return (), False
    if kind == "startup":
        if times:
            raise AutomationProjectReleaseManifestError("PROJECT_SCHEDULE_CONFIG_INVALID")
        return ("@startup",), enabled
    if not times or times != sorted(times) or len(times) != len(set(times)):
        raise AutomationProjectReleaseManifestError("PROJECT_SCHEDULE_CONFIG_INVALID")
    for item in times:
        if (
            len(item) != 5
            or item[2] != ":"
            or not item[:2].isdigit()
            or not item[3:].isdigit()
            or not 0 <= int(item[:2]) <= 23
            or not 0 <= int(item[3:]) <= 59
        ):
            raise AutomationProjectReleaseManifestError("PROJECT_SCHEDULE_CONFIG_INVALID")
    if tuple(times) == _QUARTER_HOUR_DAILY_TIMES:
        return ("*/15 * * * *",), enabled
    return tuple(
        f"{int(item[3:])} {int(item[:2])} * * *" for item in times
    ), enabled


def _rows_by_id(
    rows: Any,
    *,
    id_field: str,
    expected_ids: frozenset[str],
    mismatch_code: str,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, (list, tuple)):
        raise AutomationProjectReleaseManifestError(mismatch_code)
    result: dict[str, Mapping[str, Any]] = {}
    invalid_count = 0
    for row in rows:
        if not isinstance(row, Mapping):
            invalid_count += 1
            continue
        row_id = row.get(id_field)
        if type(row_id) is not str or row_id in result:
            invalid_count += 1
            continue
        result[row_id] = row
    actual_ids = set(result)
    invalid_count += len(expected_ids - actual_ids) + len(actual_ids - expected_ids)
    if invalid_count:
        raise AutomationProjectReleaseManifestError(
            mismatch_code,
            count=invalid_count,
        )
    return result


def _candidate_schedule_query(contract: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    reviewed_ids = tuple(sorted(contract["all_tasks"]))
    reviewed_projects = tuple(
        sorted(contract["release_projects"] | contract["deferred_projects"])
    )
    legacy_tools = tuple(
        sorted(
            {
                contract["templates"][automation_id]["tool_name"]
                for automation_id in reviewed_projects
            }
        )
    )
    typed_tools = tuple(
        sorted(
            f"automation.{automation_id}.run"
            for automation_id in contract["release_projects"]
        )
    )
    id_slots = ", ".join("%s" for _ in reviewed_ids)
    project_slots = ", ".join("%s" for _ in reviewed_projects)
    legacy_slots = ", ".join("%s" for _ in legacy_tools)
    typed_slots = ", ".join("%s" for _ in typed_tools)
    sql = f"""
        SELECT id, automation_id, automation_generation, tool_name, tool_params,
               cron_expression, enabled, configuration_version
        FROM scheduled_tasks
        WHERE id IN ({id_slots})
           OR automation_id IN ({project_slots})
           OR tool_name IN ({legacy_slots})
           OR tool_name IN ({typed_slots})
        ORDER BY id
    """
    return sql, (*reviewed_ids, *reviewed_projects, *legacy_tools, *typed_tools)


def _read_reviewed_schedule_rows(
    cursor: Any,
    contract: Mapping[str, Any],
    *,
    expect_initial_production_manifest: bool,
) -> dict[str, Mapping[str, Any]]:
    query, params = _candidate_schedule_query(contract)
    cursor.execute(query, params)
    rows = cursor.fetchall()
    if expect_initial_production_manifest:
        return _rows_by_id(
            rows,
            id_field="id",
            expected_ids=contract["all_tasks"],
            mismatch_code="AUTOMATION_PROJECT_REVIEWED_TASK_SET_MISMATCH",
        )
    if not isinstance(rows, (list, tuple)):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_REVIEWED_TASK_SET_MISMATCH"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_REVIEWED_TASK_SET_MISMATCH"
            )
        task_id = row.get("id")
        automation_id = row.get("automation_id")
        if type(task_id) is not str or task_id in result:
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_REVIEWED_TASK_SET_MISMATCH"
            )
        if (
            automation_id not in contract["release_projects"]
            and task_id not in contract["deferred_tasks"]
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_REVIEWED_TASK_COLLISION"
            )
        result[task_id] = row
    missing_deferred = contract["deferred_tasks"] - set(result)
    if missing_deferred:
        raise AutomationProjectReleaseManifestError(
            "DEFERRED_R7_TASK_SET_MISMATCH",
            count=len(missing_deferred),
        )
    return result


def _read_reviewed_backups(
    cursor: Any,
    contract: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    expected_ids = contract["all_tasks"]
    cursor.execute(
        """
        SELECT id, tool_name, tool_params, cron_expression, enabled,
               configuration_version
        FROM scheduled_task_automation_identity_backup_018
        ORDER BY id
        """,
    )
    rows = [
        _decoded_row(
            row,
            json_fields=("tool_params",),
            code="AUTOMATION_PROJECT_BACKUP_INVALID",
        )
        for row in cursor.fetchall()
    ]
    return _rows_by_id(
        rows,
        id_field="id",
        expected_ids=expected_ids,
        mismatch_code="AUTOMATION_PROJECT_BACKUP_SET_MISMATCH",
    )


def _read_release_projects(cursor: Any, contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    project_ids = tuple(sorted(contract["release_projects"]))
    placeholders = ", ".join("%s" for _ in project_ids)
    cursor.execute(
        f"""
        SELECT project.automation_id, project.plugin_id, project.enabled,
               project.state AS project_state,
               project.target_generation, project.committed_generation,
               project.reconcile_state,
               config.config_version, config.configured,
               config.config_json, config.config_sha256,
               config.account_bindings_json,
               config.account_bindings_sha256,
               config.resource_bindings_json,
               config.resource_bindings_sha256,
               config.enabled_entrypoints_json,
               config.enabled_entrypoints_sha256,
               config.desired_schedule_json, config.desired_schedule_sha256,
               config.compiled_invocations_json,
               config.compiled_invocations_sha256,
               config.device_id, config.device_binding_sha256,
               generation.generation,
               generation.state AS generation_state,
               generation.error_code AS generation_error_code,
               target_generation.state AS target_generation_state,
               target_generation.base_committed_generation
                   AS target_base_generation,
               policy.project_generation AS policy_project_generation,
               (SELECT CAST( COALESCE(MAX(history.generation), 0) AS UNSIGNED)
                FROM automation_project_generations AS history
                WHERE BINARY history.automation_id = BINARY project.automation_id) AS max_generation,
               (SELECT COUNT(*) FROM automation_project_generations AS history
                WHERE BINARY history.automation_id = BINARY project.automation_id
                  AND history.generation <> project.committed_generation
                  AND history.generation <> project.target_generation AND history.state <> 'DISPOSED'
                  AND NOT (history.generation < project.committed_generation
                    AND history.state = 'BLOCKED' AND history.error_code = 'WRITE_OUTCOME_UNKNOWN'
                    AND EXISTS (SELECT 1 FROM automation_project_generation_leases AS archive_lease
                      WHERE BINARY archive_lease.automation_id = BINARY history.automation_id
                        AND archive_lease.generation = history.generation
                        AND archive_lease.outcome = 'WRITE_OUTCOME_UNKNOWN'))) AS unsafe_non_disposed_other_count,
               (SELECT COUNT(*) FROM automation_project_generation_leases AS lease
                WHERE BINARY lease.automation_id = BINARY project.automation_id
                  AND lease.generation = project.committed_generation
                  AND lease.outcome IN ('RUNNING', 'VERIFYING')) AS active_current_lease_count,
               (SELECT COUNT(*) FROM automation_project_generation_leases AS lease
                WHERE BINARY lease.automation_id = BINARY project.automation_id
                  AND lease.generation = project.committed_generation
                  AND lease.outcome = 'WRITE_OUTCOME_UNKNOWN') AS unknown_write_count,
               generation.schedule_sha256 AS generation_schedule_sha256,
               generation.compiled_invocations_sha256
                   AS generation_invocations_sha256,
               generation.snapshot_json AS generation_snapshot_json,
               generation.snapshot_sha256 AS generation_snapshot_sha256
        FROM automation_projects AS project
        INNER JOIN automation_project_configs AS config
          ON BINARY config.automation_id = BINARY project.automation_id
        INNER JOIN automation_project_policies AS policy
          ON BINARY policy.automation_id = BINARY project.automation_id
        INNER JOIN automation_project_generations AS generation
          ON BINARY generation.automation_id = BINARY project.automation_id
         AND generation.generation = project.committed_generation
        LEFT JOIN automation_project_generations AS target_generation
          ON BINARY target_generation.automation_id = BINARY project.automation_id
         AND target_generation.generation = project.target_generation
        WHERE project.automation_id IN ({placeholders})
        ORDER BY project.automation_id
        """,
        project_ids,
    )
    return _rows_by_id(
        cursor.fetchall(),
        id_field="automation_id",
        expected_ids=contract["release_projects"],
        mismatch_code="AUTOMATION_PROJECT_COMMITTED_SET_MISMATCH",
    )


def _decoded_row(
    row: Mapping[str, Any],
    *,
    json_fields: Sequence[str],
    code: str,
) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise AutomationProjectReleaseManifestError(code)
    result = dict(row)
    for field_name in json_fields:
        if result.get(field_name) is not None:
            result[field_name] = _decode_json(
                result[field_name],
                code=code,
            )
    return result


def _group_evidence_rows(
    rows: Any,
    *,
    allowed_ids: frozenset[str],
    id_field: str,
    json_fields: Sequence[str] = (),
    code: str,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(rows, (list, tuple)):
        raise AutomationProjectReleaseManifestError(code)
    grouped = {row_id: [] for row_id in allowed_ids}
    for raw in rows:
        row = _decoded_row(raw, json_fields=json_fields, code=code)
        row_id = row.get(id_field)
        if type(row_id) is not str or row_id not in grouped:
            raise AutomationProjectReleaseManifestError(code)
        grouped[row_id].append(row)
    return grouped


def _read_bootstrap_artifacts(
    cursor: Any,
    contract: Mapping[str, Any],
) -> tuple[Mapping[str, Any], dict[str, Mapping[str, Any]]]:
    cursor.execute(
        """
        SELECT marker_id, release_sha, project_set_sha256, completed_by
        FROM automation_project_bootstrap_marker_018
        WHERE marker_id=%s
        """,
        (1,),
    )
    marker_rows = cursor.fetchall()
    if (
        not isinstance(marker_rows, (list, tuple))
        or len(marker_rows) != 1
        or not isinstance(marker_rows[0], Mapping)
    ):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_MARKER_INVALID"
        )
    cursor.execute(
        """
        SELECT automation_id, initial_mode, source_set_sha256,
               source_snapshot_json, policy_version
        FROM automation_project_bootstrap_items_018
        ORDER BY BINARY automation_id
        """
    )
    items = [
        _decoded_row(
            row,
            json_fields=("source_snapshot_json",),
            code="AUTOMATION_PROJECT_BOOTSTRAP_ITEM_INVALID",
        )
        for row in cursor.fetchall()
    ]
    return marker_rows[0], _rows_by_id(
        items,
        id_field="automation_id",
        expected_ids=contract["release_projects"],
        mismatch_code="AUTOMATION_PROJECT_BOOTSTRAP_ITEM_SET_MISMATCH",
    )


def _read_project_policy_evidence(
    cursor: Any,
    contract: Mapping[str, Any],
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    project_ids = tuple(sorted(contract["release_projects"]))
    placeholders = ", ".join("%s" for _ in project_ids)
    cursor.execute(
        f"""
        SELECT automation_id, project_generation, mode, contract_hash,
               contract_snapshot_json, tool_contract_hash,
               plugin_contract_hash, project_configuration_version,
               approved_by_actor_id, approved_by_actor_role,
               approved_by_actor_display_name, approved_at, comment, version
        FROM automation_project_policies
        WHERE automation_id IN ({placeholders})
        ORDER BY BINARY automation_id
        """,
        project_ids,
    )
    policies = [
        _decoded_row(
            row,
            json_fields=("contract_snapshot_json",),
            code="AUTOMATION_PROJECT_POLICY_STATE_INVALID",
        )
        for row in cursor.fetchall()
    ]
    policies_by_id = _rows_by_id(
        policies,
        id_field="automation_id",
        expected_ids=contract["release_projects"],
        mismatch_code="AUTOMATION_PROJECT_POLICY_SET_MISMATCH",
    )
    cursor.execute(
        f"""
        SELECT event_id, automation_id, request_id, from_mode, to_mode,
               contract_hash, contract_snapshot_json, tool_contract_hash,
               plugin_contract_hash, project_configuration_version,
               project_generation, actor_id, actor_role,
               actor_display_name, reason, comment, correlation_id
        FROM automation_project_policy_events
        WHERE automation_id IN ({placeholders})
        ORDER BY BINARY automation_id, event_id
        """,
        project_ids,
    )
    policy_events = _group_evidence_rows(
        cursor.fetchall(),
        allowed_ids=contract["release_projects"],
        id_field="automation_id",
        json_fields=("contract_snapshot_json",),
        code="AUTOMATION_PROJECT_POLICY_EVENT_INVALID",
    )
    cursor.execute(
        f"""
        SELECT policy_event.automation_id,
               policy_event.event_id AS policy_event_id,
               policy_event.request_id,
               policy_event.from_mode, policy_event.to_mode,
               policy_event.contract_hash AS policy_contract_hash,
               policy_event.contract_snapshot_json
                   AS policy_contract_snapshot_json,
               policy_event.tool_contract_hash AS policy_tool_contract_hash,
               policy_event.plugin_contract_hash
                   AS policy_plugin_contract_hash,
               policy_event.project_configuration_version
                   AS policy_configuration_version,
               policy_event.project_generation AS policy_project_generation,
               policy_event.actor_id AS policy_actor_id,
               policy_event.actor_role AS policy_actor_role,
               policy_event.actor_display_name AS policy_actor_display_name,
               policy_event.reason AS policy_reason,
               policy_event.comment AS policy_comment,
               policy_event.correlation_id AS policy_correlation_id,
               project_event.event_id AS configuration_event_id,
               project_event.event_type AS configuration_event_type,
               project_event.from_state AS configuration_from_state,
               project_event.to_state AS configuration_to_state,
               project_event.metadata_json AS configuration_metadata_json,
               project_event.metadata_sha256
                   AS configuration_metadata_sha256,
               project_event.actor_id AS configuration_actor_id,
               project_event.actor_role AS configuration_actor_role
        FROM automation_project_policy_events AS policy_event
        INNER JOIN automation_project_events AS project_event
          ON BINARY project_event.automation_id = BINARY policy_event.automation_id
         AND BINARY project_event.request_id = BINARY policy_event.request_id
        WHERE policy_event.automation_id IN ({placeholders})
        ORDER BY BINARY policy_event.automation_id, policy_event.event_id
        """,
        project_ids,
    )
    configuration_evidence = _group_evidence_rows(
        cursor.fetchall(),
        allowed_ids=contract["release_projects"],
        id_field="automation_id",
        json_fields=(
            "policy_contract_snapshot_json",
            "configuration_metadata_json",
        ),
        code="AUTOMATION_PROJECT_CONFIGURATION_EVENT_INVALID",
    )
    return policies_by_id, policy_events, configuration_evidence


def _read_scheduled_policy_evidence(
    cursor: Any,
    contract: Mapping[str, Any],
    *,
    include_current_policies: bool,
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, list[dict[str, Any]]],
]:
    task_ids = tuple(sorted(contract["release_tasks"]))
    placeholders = ", ".join("%s" for _ in task_ids)
    policies: dict[str, Mapping[str, Any]] = {}
    if include_current_policies:
        cursor.execute(
            f"""
            SELECT task_id, mode AS scheduled_policy_mode,
                   version AS scheduled_policy_version,
                   contract_hash AS scheduled_contract_hash,
                   contract_snapshot_json
                       AS scheduled_contract_snapshot_json,
                   tool_contract_hash AS scheduled_tool_contract_hash
            FROM scheduled_task_approval_policies
            WHERE task_id IN ({placeholders})
            ORDER BY BINARY task_id
            """,
            task_ids,
        )
        policy_rows = [
            _decoded_row(
                row,
                json_fields=("scheduled_contract_snapshot_json",),
                code="AUTOMATION_PROJECT_SCHEDULE_POLICY_INVALID",
            )
            for row in cursor.fetchall()
        ]
        policies = _rows_by_id(
            policy_rows,
            id_field="task_id",
            expected_ids=contract["release_tasks"],
            mismatch_code="AUTOMATION_PROJECT_SCHEDULE_POLICY_SET_MISMATCH",
        )
    cursor.execute(
        f"""
        SELECT event_id, task_id, request_id, from_mode, to_mode,
               contract_hash, contract_snapshot_json, tool_contract_hash,
               actor_id, actor_role, actor_display_name, reason, comment,
               correlation_id
        FROM scheduled_task_approval_policy_events
        WHERE task_id IN ({placeholders})
        ORDER BY BINARY task_id, event_id
        """,
        task_ids,
    )
    events = _group_evidence_rows(
        cursor.fetchall(),
        allowed_ids=contract["release_tasks"],
        id_field="task_id",
        json_fields=("contract_snapshot_json",),
        code="AUTOMATION_PROJECT_SCHEDULE_POLICY_EVENT_INVALID",
    )
    return policies, events


def _read_bootstrap_generations(
    cursor: Any,
    contract: Mapping[str, Any],
    source_snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    identities: list[tuple[str, int]] = []
    for automation_id in sorted(contract["release_projects"]):
        source = source_snapshots[automation_id]
        identities.append(
            (
                automation_id,
                _positive_int(
                    source.get("automation_generation"),
                    code="AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_INVALID",
                ),
            )
        )
    predicates = " OR ".join(
        "(BINARY automation_id = BINARY %s AND generation=%s)"
        for _ in identities
    )
    params = tuple(value for identity in identities for value in identity)
    cursor.execute(
        f"""
        SELECT automation_id, generation, plugin_id, plugin_version,
               trust_source, package_sha256,
               state AS generation_state,
               snapshot_json, snapshot_sha256,
               manifest_sha256, project_config_sha256,
               account_bindings_sha256, resource_bindings_sha256,
               device_binding_sha256, schedule_sha256,
               core_registry_sha256, tool_contract_sha256,
               invocation_contracts_sha256,
               compiled_invocations_sha256,
               runtime_descriptor_sha256,
               governance_anchor_sha256, policy_contract_sha256,
               enabled_entrypoints_sha256, committed_at
        FROM automation_project_generations
        WHERE {predicates}
        ORDER BY BINARY automation_id
        """,
        params,
    )
    rows = [
        _decoded_row(
            row,
            json_fields=("snapshot_json",),
            code="AUTOMATION_PROJECT_BOOTSTRAP_GENERATION_INVALID",
        )
        for row in cursor.fetchall()
    ]
    result = _rows_by_id(
        rows,
        id_field="automation_id",
        expected_ids=contract["release_projects"],
        mismatch_code="AUTOMATION_PROJECT_BOOTSTRAP_GENERATION_SET_MISMATCH",
    )
    if any(
        row.get("generation") != dict(identities)[automation_id]
        for automation_id, row in result.items()
    ):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_GENERATION_INVALID"
        )
    return result


def _verify_deferred_projects_absent(cursor: Any, contract: Mapping[str, Any]) -> None:
    project_ids = tuple(sorted(contract["deferred_projects"]))
    placeholders = ", ".join("%s" for _ in project_ids)
    cursor.execute(
        f"""
        SELECT automation_id
        FROM automation_projects
        WHERE automation_id IN ({placeholders})
        ORDER BY automation_id
        """,
        project_ids,
    )
    rows = cursor.fetchall()
    if not isinstance(rows, (list, tuple)) or rows:
        raise AutomationProjectReleaseManifestError(
            "DEFERRED_R7_PROJECT_MATERIALIZED",
            count=len(rows) if isinstance(rows, (list, tuple)) else 1,
        )


def _validate_release_projects_and_tasks(
    contract: Mapping[str, Any],
    *,
    schedules: Mapping[str, Mapping[str, Any]],
    backups: Mapping[str, Mapping[str, Any]],
    projects: Mapping[str, Mapping[str, Any]],
    expect_initial_production_manifest: bool,
) -> None:
    rows_by_project: dict[str, list[Mapping[str, Any]]] = {
        automation_id: [] for automation_id in contract["release_projects"]
    }
    release_rows = [
        row
        for row in schedules.values()
        if row.get("automation_id") in contract["release_projects"]
    ]
    if expect_initial_production_manifest and {
        str(row.get("id") or "") for row in release_rows
    } != contract["release_tasks"]:
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_RELEASE_TASK_SET_MISMATCH"
        )
    for row in release_rows:
        task_id = str(row.get("id") or "")
        automation_id = str(row.get("automation_id") or "")
        if automation_id not in contract["release_projects"]:
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_TASK_IDENTITY_MISMATCH"
            )
        if row.get("automation_id") != automation_id:
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_TASK_IDENTITY_MISMATCH"
            )
        if row.get("tool_name") != f"automation.{automation_id}.run":
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_TASK_TOOL_MISMATCH"
            )
        if task_id in contract["release_tasks"]:
            if contract["task_to_automation"][task_id] != automation_id:
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_TASK_IDENTITY_MISMATCH"
                )
            backup = backups[task_id]
            if row.get("cron_expression") != backup.get("cron_expression") or (
                expect_initial_production_manifest
                and row.get("enabled") != backup.get("enabled")
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_TASK_SCHEDULE_MISMATCH"
                )
        elif task_id != contract["stable_schedule_task_id"](
            automation_id,
            str(row.get("cron_expression") or ""),
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_TASK_IDENTITY_MISMATCH"
            )
        rows_by_project[automation_id].append(row)

    for automation_id in sorted(contract["release_projects"]):
        project = projects[automation_id]
        template = contract["templates"][automation_id]
        stable_runtime = (
            project.get("reconcile_state") == "STABLE"
            and project.get("generation_state") == "COMMITTED"
        )
        unavailable_runtime = (
            not expect_initial_production_manifest
            and project.get("reconcile_state") == "BLOCKED_UNKNOWN_WRITE"
            and project.get("generation_state") == "BLOCKED"
        )
        staged_unknown_write = (
            not expect_initial_production_manifest
            and is_staged_unknown_write_quarantine(project)
        )
        staged_missing_target = (
            not expect_initial_production_manifest
            and is_staged_recoverable_runtime(project)
        )
        isolated_runtime = staged_unknown_write or staged_missing_target
        if (
            project.get("plugin_id") != template["tool_name"]
            or (
                project.get("project_state") != "ENABLED"
                and not isolated_runtime
            )
            or not _boolean(
                project.get("enabled"),
                code="AUTOMATION_PROJECT_STATE_INVALID",
            )
            or not _boolean(
                project.get("configured"),
                code="AUTOMATION_PROJECT_CONFIG_INVALID",
            )
            or not (
                stable_runtime
                or unavailable_runtime
                or staged_unknown_write
                or staged_missing_target
            )
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_STATE_INVALID"
            )
        committed_generation = _positive_int(
            project.get("committed_generation"),
            code="AUTOMATION_PROJECT_GENERATION_INVALID",
        )
        generation = _positive_int(
            project.get("generation"),
            code="AUTOMATION_PROJECT_GENERATION_INVALID",
        )
        config_version = _positive_int(
            project.get("config_version"),
            code="AUTOMATION_PROJECT_CONFIG_VERSION_INVALID",
        )
        target_generation = _positive_int(
            project.get("target_generation"),
            code="AUTOMATION_PROJECT_GENERATION_INVALID",
        )
        if generation != committed_generation or (
            target_generation != generation and not isolated_runtime
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_GENERATION_MISMATCH"
            )

        desired_schedule = _decode_json(
            project.get("desired_schedule_json"),
            code="PROJECT_SCHEDULE_CONFIG_INVALID",
        )
        compiled = _decode_json(
            project.get("compiled_invocations_json"),
            code="PROJECT_COMPILED_INVOCATIONS_INVALID",
        )
        configured_schedule, configured_compiled = desired_schedule, compiled
        generation_snapshot = _decode_json(
            project.get("generation_snapshot_json"),
            code="AUTOMATION_PROJECT_GENERATION_SNAPSHOT_INVALID",
        )
        execution_metadata = (
            generation_snapshot.get("execution_metadata")
            if isinstance(generation_snapshot, dict)
            else None
        )
        if (
            not isinstance(execution_metadata, dict)
            or generation_snapshot.get("automation_id") != automation_id
            or generation_snapshot.get("generation") != generation
            or generation_snapshot.get("plugin_id") != template["tool_name"]
            or (
                not isolated_runtime
                and (
                    execution_metadata.get("project_config_version") != config_version
                    or not _strict_json_equal(execution_metadata.get("schedule"), desired_schedule)
                    or not _strict_json_equal(execution_metadata.get("compiled_invocations"), compiled)
                )
            )
            or project.get("generation_snapshot_sha256")
            != _canonical_sha256(generation_snapshot)
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_GENERATION_SNAPSHOT_INVALID"
            )
        if isolated_runtime:
            config_version = _positive_int(
                execution_metadata.get("project_config_version"),
                code="AUTOMATION_PROJECT_CONFIG_VERSION_INVALID",
            )
            desired_schedule = execution_metadata.get("schedule")
            compiled = execution_metadata.get("compiled_invocations")
        if not isinstance(compiled, dict):
            raise AutomationProjectReleaseManifestError(
                "PROJECT_COMPILED_INVOCATIONS_INVALID"
            )
        scheduler_contract = compiled.get("scheduler")
        scheduler_arguments = (
            scheduler_contract.get("arguments")
            if isinstance(scheduler_contract, dict)
            else None
        )
        project_rows = rows_by_project[automation_id]
        if project_rows and not isinstance(scheduler_arguments, dict):
            raise AutomationProjectReleaseManifestError(
                "PROJECT_SCHEDULER_ARGUMENTS_MISSING"
            )
        if isinstance(scheduler_arguments, dict) and _contains_broker_account_field(
            scheduler_arguments
        ):
            raise AutomationProjectReleaseManifestError(
                "PROJECT_SCHEDULER_ARGUMENTS_CONTAIN_ACCOUNT"
            )
        desired_expressions, desired_enabled = _normalized_schedule_expressions(
            desired_schedule
        )
        actual_expressions = tuple(
            sorted(str(row.get("cron_expression") or "") for row in project_rows)
        )
        if tuple(sorted(desired_expressions)) != actual_expressions:
            raise AutomationProjectReleaseManifestError(
                "PROJECT_SCHEDULE_ROWS_MISMATCH"
            )
        for row in project_rows:
            if (
                _positive_int(
                    row.get("automation_generation"),
                    code="AUTOMATION_PROJECT_TASK_GENERATION_INVALID",
                )
                != committed_generation
                or _positive_int(
                    row.get("configuration_version"),
                    code="AUTOMATION_PROJECT_TASK_CONFIG_VERSION_INVALID",
                )
                != config_version
                or _boolean(
                    row.get("enabled"),
                    code="AUTOMATION_PROJECT_TASK_ENABLED_INVALID",
                )
                != desired_enabled
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_TASK_RUNTIME_MISMATCH"
                )
            arguments = _decode_json(
                row.get("tool_params"),
                code="AUTOMATION_PROJECT_TASK_ARGUMENTS_INVALID",
            )
            if not _strict_json_equal(arguments, scheduler_arguments):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_TASK_ARGUMENTS_MISMATCH"
                )

        schedule_hash = project.get("desired_schedule_sha256")
        invocation_hash = project.get("compiled_invocations_sha256")
        if (
            not _valid_sha256(schedule_hash)
            or not _valid_sha256(invocation_hash)
            or schedule_hash != _canonical_sha256(configured_schedule)
            or invocation_hash != _canonical_sha256(configured_compiled)
            or project.get("generation_schedule_sha256") != _canonical_sha256(desired_schedule)
            or project.get("generation_invocations_sha256") != _canonical_sha256(compiled)
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_CONFIG_HASH_MISMATCH"
            )


def _deferred_code_owned_legacy_arguments(
    contract: Mapping[str, Any],
    automation_id: str,
) -> dict[str, Any]:
    template = contract["templates"][automation_id]
    expected = copy.deepcopy(dict(template["legacy_arguments"]))
    if automation_id != "r7_departure_checkin":
        return expected

    account_bindings = template["legacy_account_bindings"]
    if (
        template["tool_name"] != "r7_departure_checkin"
        or template["task_ids"] != frozenset({"r7_departure_checkin"})
        or set(account_bindings) != {"account_id"}
        or type(account_bindings.get("account_id")) is not str
        or not account_bindings["account_id"].strip()
        or expected.get("account_id") != account_bindings["account_id"]
    ):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_RELEASE_CONTRACT_INVALID"
        )
    for field_name in account_bindings:
        expected.pop(field_name)
    return expected


def _validate_deferred_rows(
    contract: Mapping[str, Any],
    *,
    schedules: Mapping[str, Mapping[str, Any]],
    backups: Mapping[str, Mapping[str, Any]],
) -> None:
    for task_id in sorted(contract["deferred_tasks"]):
        row = schedules[task_id]
        backup = backups[task_id]
        automation_id = contract["task_to_automation"][task_id]
        template = contract["templates"][automation_id]
        if (
            row.get("automation_id") != automation_id
            or row.get("tool_name") != template["tool_name"]
            or row.get("automation_generation")
            != contract["deferred_generation"]
        ):
            raise AutomationProjectReleaseManifestError(
                "DEFERRED_R7_IDENTITY_MISMATCH"
            )
        for field in (
            "tool_name",
            "cron_expression",
            "enabled",
            "configuration_version",
        ):
            if row.get(field) != backup.get(field):
                raise AutomationProjectReleaseManifestError(
                    "DEFERRED_R7_LEGACY_STATE_MISMATCH"
                )
        arguments = _decode_json(
            row.get("tool_params"),
            code="DEFERRED_R7_ARGUMENTS_INVALID",
        )
        backup_arguments = _decode_json(
            backup.get("tool_params"),
            code="DEFERRED_R7_BACKUP_ARGUMENTS_INVALID",
        )
        expected_arguments = _deferred_code_owned_legacy_arguments(
            contract,
            automation_id,
        )
        if (
            not _strict_json_equal(arguments, backup_arguments)
            or not _strict_json_equal(arguments, expected_arguments)
        ):
            raise AutomationProjectReleaseManifestError(
                "DEFERRED_R7_LEGACY_STATE_MISMATCH"
            )


def _validate_bootstrap_generation_source(
    contract: Mapping[str, Any],
    *,
    automation_id: str,
    source: Mapping[str, Any],
    generation_row: Mapping[str, Any],
    backups: Mapping[str, Mapping[str, Any]],
    allow_blocked: bool = False,
) -> None:
    try:
        validated_generation = contract["validate_generation_row"](
            generation_row
        )
    except Exception as exc:
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_GENERATION_INVALID"
        ) from exc
    snapshot = validated_generation.get("snapshot_json")
    execution_metadata = (
        snapshot.get("execution_metadata")
        if isinstance(snapshot, Mapping)
        else None
    )
    template = contract["templates"][automation_id]
    allowed_generation_states = {"COMMITTED", "DRAINING", "DISPOSING", "DISPOSED"}
    if allow_blocked:
        allowed_generation_states.add("BLOCKED")
    if (
        generation_row.get("generation_state") not in allowed_generation_states
        or generation_row.get("committed_at") is None
        or generation_row.get("generation")
        != source.get("automation_generation")
        or generation_row.get("plugin_id") != template["tool_name"]
        or not isinstance(execution_metadata, Mapping)
        or execution_metadata.get("project_config_version")
        != source.get("project_configuration_version")
    ):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_GENERATION_INVALID"
        )
    desired_schedule = execution_metadata.get("schedule")
    compiled = execution_metadata.get("compiled_invocations")
    if not isinstance(compiled, Mapping):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_GENERATION_INVALID"
        )
    desired_expressions, desired_enabled = _normalized_schedule_expressions(
        desired_schedule
    )
    source_tasks = source.get("scheduled_tasks")
    if not isinstance(source_tasks, list):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_INVALID"
        )
    source_by_task = {
        str(row.get("task_id") or ""): row
        for row in source_tasks
        if isinstance(row, Mapping)
    }
    expected_task_ids = template["task_ids"]
    if set(source_by_task) != expected_task_ids or len(source_by_task) != len(
        source_tasks
    ):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_TASK_SET_MISMATCH"
        )
    actual_expressions = tuple(
        sorted(
            str(backups[task_id].get("cron_expression") or "")
            for task_id in expected_task_ids
        )
    )
    if tuple(sorted(desired_expressions)) != actual_expressions:
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_SCHEDULE_MISMATCH"
        )
    scheduler = compiled.get("scheduler")
    scheduler_arguments = (
        scheduler.get("arguments") if isinstance(scheduler, Mapping) else None
    )
    if expected_task_ids and not isinstance(scheduler_arguments, Mapping):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_ARGUMENTS_INVALID"
        )
    if isinstance(scheduler_arguments, Mapping) and _contains_broker_account_field(
        scheduler_arguments
    ):
        raise AutomationProjectReleaseManifestError(
            "PROJECT_SCHEDULER_ARGUMENTS_CONTAIN_ACCOUNT"
        )
    arguments_hash = (
        _canonical_sha256(scheduler_arguments)
        if isinstance(scheduler_arguments, Mapping)
        else None
    )
    for task_id, task_source in source_by_task.items():
        backup = backups[task_id]
        if (
            task_source.get("tool_name") != f"automation.{automation_id}.run"
            or task_source.get("automation_generation")
            != source.get("automation_generation")
            or task_source.get("configuration_version")
            != source.get("project_configuration_version")
            or task_source.get("enabled") is not desired_enabled
            or _boolean(
                backup.get("enabled"),
                code="AUTOMATION_PROJECT_BACKUP_ENABLED_INVALID",
            )
            is not desired_enabled
            or task_source.get("cron_expression_hash")
            != _canonical_sha256(str(backup.get("cron_expression") or ""))
            or task_source.get("arguments_hash") != arguments_hash
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_TASK_INVALID"
            )


def _validate_historical_scheduled_events(
    contract: Mapping[str, Any],
    *,
    source_snapshots: Mapping[str, Mapping[str, Any]],
    backups: Mapping[str, Mapping[str, Any]],
    events: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    evidence = contract["bootstrap_evidence"]
    for automation_id in sorted(contract["release_projects"]):
        source = source_snapshots[automation_id]
        configuration_request_id = str(
            source.get("configuration_request_id") or ""
        )
        for task_source in source.get("scheduled_tasks", []):
            task_id = str(task_source.get("task_id") or "")
            task_events = list(events[task_id])
            event_ids = [row.get("event_id") for row in task_events]
            if (
                any(
                    isinstance(event_id, bool)
                    or not isinstance(event_id, int)
                    or event_id <= 0
                    for event_id in event_ids
                )
                or event_ids != sorted(event_ids)
                or len(event_ids) != len(set(event_ids))
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_SCHEDULE_POLICY_EVENT_INVALID"
                )
            grant_request_id = evidence[
                "legacy_scheduled_policy_grant_request_id"
            ](task_id)
            grant_events = [
                row
                for row in task_events
                if row.get("request_id") == grant_request_id
            ]
            migration_events = [
                row
                for row in task_events
                if row.get("request_id") == configuration_request_id
            ]
            expected_grant = str(
                task_source.get("legacy_grant_request_id") or ""
            )
            expected_retirement = str(
                task_source.get("retirement_request_id") or ""
            )
            if len(grant_events) != int(bool(expected_grant)) or len(
                migration_events
            ) != int(bool(expected_retirement)):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_BOOTSTRAP_SCHEDULE_EVENT_MISMATCH"
                )
            grant_event = grant_events[0] if grant_events else None
            migration_event = migration_events[0] if migration_events else None
            try:
                if grant_event is not None:
                    evidence["validate_legacy_scheduled_grant_event"](
                        grant_event,
                        row=backups[task_id],
                    )
                if migration_event is not None:
                    evidence[
                        "validate_plugin_configuration_retirement_event"
                    ](
                        migration_event,
                        configuration_request_id=configuration_request_id,
                    )
            except evidence["error_class"] as exc:
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_BOOTSTRAP_SCHEDULE_EVENT_INVALID"
                ) from exc
            if grant_event is not None and (
                task_source.get("legacy_grant_contract_hash")
                != grant_event.get("contract_hash")
                or task_source.get("legacy_grant_tool_contract_hash")
                != grant_event.get("tool_contract_hash")
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_BOOTSTRAP_SCHEDULE_EVENT_MISMATCH"
                )
            authorized = bool(task_source.get("legacy_authorized"))
            if authorized and (
                grant_event is None
                or migration_event is None
                or int(grant_event["event_id"])
                >= int(migration_event["event_id"])
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_BOOTSTRAP_SCHEDULE_EVENT_MISMATCH"
                )


def _project_config_evidence_row(project: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(project)
    for field_name in (
        "config_json",
        "account_bindings_json",
        "resource_bindings_json",
        "enabled_entrypoints_json",
        "desired_schedule_json",
        "compiled_invocations_json",
    ):
        result[field_name] = _decode_json(
            result.get(field_name),
            code="AUTOMATION_PROJECT_CONFIGURATION_EVENT_INVALID",
        )
    return result


def _bootstrap_event_for_project(
    contract: Mapping[str, Any],
    *,
    automation_id: str,
    item: Mapping[str, Any],
    policy_events: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], int]:
    evidence = contract["bootstrap_evidence"]
    request_id = evidence["automation_project_policy_bootstrap_request_id"](
        automation_id
    )
    matches = [
        (index, event)
        for index, event in enumerate(policy_events)
        if event.get("request_id") == request_id
    ]
    if len(matches) != 1:
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_POLICY_EVENT_MISSING"
        )
    index, event = matches[0]
    try:
        evidence["validate_automation_project_bootstrap_policy_event"](
            event,
            item=item,
        )
    except evidence["error_class"] as exc:
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_POLICY_EVENT_INVALID"
        ) from exc
    return event, index


def _validate_followup_configuration_event(
    contract: Mapping[str, Any],
    *,
    event: Mapping[str, Any],
    configuration_evidence: Sequence[Mapping[str, Any]],
) -> bool:
    evidence = contract["bootstrap_evidence"]
    request_id = str(event.get("request_id") or "")
    matches = [row for row in configuration_evidence if row.get("request_id") == request_id]
    if len(matches) != 1:
        raise AutomationProjectReleaseManifestError("AUTOMATION_PROJECT_FOLLOWUP_CONFIGURATION_EVENT_INVALID")
    joined = matches[0]
    metadata = joined.get("configuration_metadata_json")
    event_mode = event.get("to_mode")
    actor_allowed = (
        event.get("actor_role") == "super_admin"
        and type(event.get("actor_id")) is str
        and bool(event.get("actor_id"))
    ) or (
        event.get("actor_id") == evidence["plugin_actor_id"]
        and event.get("actor_role") == evidence["plugin_actor_role"]
    )
    durable_rebind = (
        event.get("from_mode") == event_mode
        and event_mode in _DURABLE_POLICY_MODES
        and actor_allowed
    )
    legacy_rebind = (
        event.get("from_mode") in _DURABLE_POLICY_MODES
        and event.get("from_mode") != event_mode
        and event_mode in {"PROJECT_FULL_AUTO", "REQUIRE_EACH_RUN"}
        and (event_mode == "REQUIRE_EACH_RUN" or event.get("project_generation") == 1)
        and actor_allowed
    )
    if (
        not (durable_rebind or legacy_rebind)
        or not _empty_policy_event_contract(event)
        or event.get("actor_display_name") is not None
        or event.get("reason") != evidence["plugin_reason"]
        or event.get("comment") is not None
        or event.get("correlation_id") != request_id
        or not _joined_policy_event_matches(joined, event)
        or type(joined.get("configuration_event_id")) is not int or joined.get("configuration_event_id", 0) <= 0
        or joined.get("configuration_event_type") != "CONFIGURATION_UPDATED"
        or not isinstance(joined.get("configuration_from_state"), str) or not joined.get("configuration_from_state") or joined.get("configuration_from_state") != joined.get("configuration_to_state")
        or joined.get("configuration_actor_id")
        != event.get("actor_id")
        or joined.get("configuration_actor_role")
        != event.get("actor_role")
        or not isinstance(metadata, Mapping)
        or set(metadata) != {
            "request_payload_sha256", "from_project_configuration_version",
            "to_project_configuration_version", "schedule_sha256", "scheduled_task_count",
        }
        or not _valid_sha256(metadata.get("request_payload_sha256"))
        or not _valid_sha256(metadata.get("schedule_sha256"))
        or metadata.get("to_project_configuration_version")
        != event.get("project_configuration_version")
        or metadata.get("from_project_configuration_version")
        != int(event.get("project_configuration_version") or 0) - 1
        or isinstance(metadata.get("scheduled_task_count"), bool)
        or not isinstance(metadata.get("scheduled_task_count"), int)
        or metadata.get("scheduled_task_count") < 0
        or _canonical_sha256(metadata)
        != joined.get("configuration_metadata_sha256")
    ):
        raise AutomationProjectReleaseManifestError("AUTOMATION_PROJECT_FOLLOWUP_CONFIGURATION_EVENT_INVALID")
    return bool(legacy_rebind or event.get("project_generation") == 1)


def _validate_full_auto_event_contract(*, automation_id: str, event: Mapping[str, Any]) -> None:
    snapshot = event.get("contract_snapshot_json")
    if (
        not isinstance(snapshot, Mapping)
        or event.get("contract_hash") != _canonical_sha256(snapshot)
        or snapshot.get("automation_id") != automation_id
        or snapshot.get("automation_generation")
        != event.get("project_generation")
        or snapshot.get("tool_contract_hash")
        != event.get("tool_contract_hash")
        or snapshot.get("plugin_contract_hash")
        != event.get("plugin_contract_hash")
        or not _valid_sha256(event.get("tool_contract_hash"))
        or not _valid_sha256(event.get("plugin_contract_hash"))
    ):
        raise AutomationProjectReleaseManifestError(_FOLLOWUP_POLICY_INVALID)


_POLICY_EVENT_CONTRACT_FIELDS = ("contract_hash", "contract_snapshot_json", "tool_contract_hash", "plugin_contract_hash")
_DURABLE_POLICY_MODES = frozenset(("PROJECT_FULL_AUTO", "REQUIRE_EACH_RUN", "LEGACY_SCHEDULE_ONLY"))
_FOLLOWUP_POLICY_INVALID = "AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID"
_SYSTEM_FULL_AUTO_EVENTS = {
    "MIGRATION_019_FULL_AUTO": ("migration-019-full-auto", "migration-019", "Migration 019", "Existing automation project converted to durable full auto"),
    "AUTOMATION_DEFAULT_FULL_AUTO": ("default-full-auto", "system:migration:automation-full-auto-v1", "Automation full-auto migration", "Defaulted automation project to durable full auto"),
}
_JOINED_POLICY_FIELDS = {
    "event_id": "policy_event_id", "from_mode": "from_mode", "to_mode": "to_mode",
    "contract_hash": "policy_contract_hash", "contract_snapshot_json": "policy_contract_snapshot_json",
    "tool_contract_hash": "policy_tool_contract_hash", "plugin_contract_hash": "policy_plugin_contract_hash",
    "project_configuration_version": "policy_configuration_version", "project_generation": "policy_project_generation",
    "actor_id": "policy_actor_id", "actor_role": "policy_actor_role", "actor_display_name": "policy_actor_display_name",
    "reason": "policy_reason", "comment": "policy_comment", "correlation_id": "policy_correlation_id",
}


def _empty_policy_event_contract(event: Mapping[str, Any]) -> bool:
    return all(event.get(field) is None for field in _POLICY_EVENT_CONTRACT_FIELDS)


def _raise_followup_policy_invalid() -> None:
    raise AutomationProjectReleaseManifestError(_FOLLOWUP_POLICY_INVALID)


def _joined_policy_event_matches(joined: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    return all(event.get(field) == joined.get(alias) for field, alias in _JOINED_POLICY_FIELDS.items())


def _policy_event_binding(event: Mapping[str, Any]) -> tuple[int, int]:
    return (_positive_int(event.get("project_generation"), code=_FOLLOWUP_POLICY_INVALID), _positive_int(event.get("project_configuration_version"), code=_FOLLOWUP_POLICY_INVALID))


def _validate_system_full_auto_event(*, automation_id: str, event: Mapping[str, Any]) -> None:
    spec = _SYSTEM_FULL_AUTO_EVENTS.get(str(event.get("reason") or ""))
    request_prefix, actor_id, display_name, comment = spec or (None, None, None, None)
    if (
        spec is None
        or event.get("request_id") != f"{request_prefix}:{automation_id}"
        or event.get("from_mode") not in {"REQUIRE_EACH_RUN", "LEGACY_SCHEDULE_ONLY"}
        or event.get("to_mode") != "PROJECT_FULL_AUTO"
        or not _empty_policy_event_contract(event)
        or event.get("actor_id") != actor_id
        or event.get("actor_role") != "system"
        or event.get("actor_display_name") != display_name
        or event.get("comment") != comment
        or type(event.get("correlation_id")) is not str
        or not event.get("correlation_id")
    ):
        _raise_followup_policy_invalid()


def _validate_super_admin_policy_event(*, automation_id: str, event: Mapping[str, Any]) -> None:
    if (
        event.get("actor_role") != "super_admin"
        or type(event.get("actor_id")) is not str
        or not event.get("actor_id")
        or event.get("to_mode") not in {"REQUIRE_EACH_RUN", "PROJECT_FULL_AUTO"}
        or type(event.get("correlation_id")) is not str
        or not event.get("correlation_id")
    ):
        _raise_followup_policy_invalid()
    if _empty_policy_event_contract(event):
        return
    if event.get("to_mode") == "PROJECT_FULL_AUTO":
        _validate_full_auto_event_contract(automation_id=automation_id, event=event)
        return
    _raise_followup_policy_invalid()
def _policy_approval_matches(policy: Mapping[str, Any], anchor: Mapping[str, Any] | None) -> bool:
    if anchor is None:
        return all(
            policy.get(field) is None
            for field in ("approved_by_actor_id", "approved_by_actor_role", "approved_by_actor_display_name", "approved_at", "comment")
        )
    return bool(
        policy.get("approved_by_actor_id") == anchor.get("actor_id")
        and policy.get("approved_by_actor_role") == anchor.get("actor_role")
        and policy.get("approved_by_actor_display_name") == anchor.get("actor_display_name")
        and policy.get("comment") == anchor.get("comment")
        and policy.get("approved_at") is not None
    )


def _validate_later_project_policy_chain(
    contract: Mapping[str, Any],
    *,
    automation_id: str,
    item: Mapping[str, Any],
    project: Mapping[str, Any],
    policy: Mapping[str, Any],
    policy_events: Sequence[Mapping[str, Any]],
    configuration_evidence: Sequence[Mapping[str, Any]],
) -> None:
    bootstrap_event, bootstrap_index = _bootstrap_event_for_project(
        contract,
        automation_id=automation_id,
        item=item,
        policy_events=policy_events,
    )
    event_ids = [event.get("event_id") for event in policy_events]
    request_ids = [event.get("request_id") for event in policy_events]
    if (
        any(
            isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or event_id <= 0
            for event_id in event_ids
        )
        or event_ids != sorted(event_ids)
        or len(event_ids) != len(set(event_ids))
        or any(type(request_id) is not str or not request_id for request_id in request_ids)
        or len(request_ids) != len(set(request_ids))
    ):
        raise AutomationProjectReleaseManifestError("AUTOMATION_PROJECT_POLICY_EVENT_INVALID")
    mode = str(item.get("initial_mode") or "")
    subsequent = list(policy_events[bootstrap_index + 1 :])
    previous_mode = mode
    validated_events = [bootstrap_event]
    _bootstrap_generation, maximum_event_configuration = _policy_event_binding(bootstrap_event)
    full_auto_authorized = False
    system_full_auto_reason: str | None = None
    super_admin_seen = False
    approval_anchor: Mapping[str, Any] | None = None
    plugin_downgrade_variants: dict[int, str] = {}
    for event in subsequent:
        if event.get("from_mode") != previous_mode:
            raise AutomationProjectReleaseManifestError("AUTOMATION_PROJECT_POLICY_EVENT_CHAIN_INVALID")
        reason = str(event.get("reason") or "")
        _event_generation, event_configuration = _policy_event_binding(event)
        if event_configuration < maximum_event_configuration:
            _raise_followup_policy_invalid()
        maximum_event_configuration = event_configuration
        try:
            credential_authority = credential_policy_history.validate_credential_policy_history_event(automation_id=automation_id, event=event, previous_event=validated_events[-1])
        except ValueError:
            _raise_followup_policy_invalid()
        if credential_authority is not None:
            required_variant = None
            if reason == credential_policy_history.PLUGIN_POLICY_RESTORE_REASON:
                required_variant = plugin_policy_history.PREPARED_AWARE
            elif (
                reason
                == credential_policy_history.ORIGINAL_PLUGIN_POLICY_RESTORE_REASON
            ):
                required_variant = plugin_policy_history.ORIGINAL
            if required_variant is not None:
                predecessor_id = validated_events[-1].get("event_id")
                if plugin_downgrade_variants.get(predecessor_id) != required_variant:
                    _raise_followup_policy_invalid()
            full_auto_authorized, approval_anchor = credential_authority, event
        elif reason == contract["bootstrap_evidence"]["plugin_reason"]:
            legacy_configuration = _validate_followup_configuration_event(
                contract,
                event=event,
                configuration_evidence=configuration_evidence,
            )
            if legacy_configuration:
                if system_full_auto_reason is not None:
                    _raise_followup_policy_invalid()
                full_auto_authorized = event.get("to_mode") == "PROJECT_FULL_AUTO"
                approval_anchor = None
        elif reason in _SYSTEM_FULL_AUTO_EVENTS:
            if system_full_auto_reason is not None or (reason == "AUTOMATION_DEFAULT_FULL_AUTO" and super_admin_seen):
                _raise_followup_policy_invalid()
            _validate_system_full_auto_event(automation_id=automation_id, event=event)
            full_auto_authorized = True
            system_full_auto_reason = reason
            approval_anchor = event
        elif reason == "PLUGIN_VERSION_CHANGED":
            try:
                prepared_request, legacy_downgrade, metadata_variant = (
                    plugin_policy_history.validate_plugin_version_evidence(
                        event, configuration_evidence
                    )
                )
            except ValueError:
                _raise_followup_policy_invalid()
            if prepared_request:
                prepared_events = [row for row in validated_events if row.get("request_id") == prepared_request]
                if (
                    len(prepared_events) != 1
                    or prepared_events[0].get("reason") != contract["bootstrap_evidence"]["plugin_reason"]
                    or prepared_events[0].get("actor_id") != event.get("actor_id")
                    or prepared_events[0].get("actor_role") != event.get("actor_role")
                    or _policy_event_binding(prepared_events[0]) != _policy_event_binding(event)
                ):
                    _raise_followup_policy_invalid()
            if legacy_downgrade:
                full_auto_authorized = False
                plugin_downgrade_variants[int(event["event_id"])] = metadata_variant
            approval_anchor = event
        elif reason == "SUPER_ADMIN_PROJECT_POLICY_CHANGED":
            super_admin_seen = True
            _validate_super_admin_policy_event(
                automation_id=automation_id,
                event=event,
            )
            full_auto_authorized = event.get("to_mode") == "PROJECT_FULL_AUTO"
            approval_anchor = event
        else:
            raise AutomationProjectReleaseManifestError(_FOLLOWUP_POLICY_INVALID)
        previous_mode = str(event.get("to_mode") or "")
        validated_events.append(event)

    latest = subsequent[-1] if subsequent else bootstrap_event
    if (
        policy.get("mode") != previous_mode
        or policy.get("version")
        != int(item.get("policy_version") or 0) + len(subsequent)
    ):
        raise AutomationProjectReleaseManifestError("AUTOMATION_PROJECT_POLICY_STATE_INVALID")
    if not subsequent and previous_mode == "LEGACY_SCHEDULE_ONLY":
        try:
            contract["bootstrap_evidence"][
                "validate_initial_automation_project_bootstrap_policy"
            ](
                policy,
                item=item,
                bootstrap_event=bootstrap_event,
            )
        except contract["bootstrap_evidence"]["error_class"] as exc:
            raise AutomationProjectReleaseManifestError("AUTOMATION_PROJECT_POLICY_STATE_INVALID") from exc
        return

    isolated_target = is_staged_unknown_write_quarantine(project) or is_staged_recoverable_runtime(project)
    expected_generation = project.get("target_generation") if isolated_target else project.get("generation")
    legacy_contract_binding = (
        previous_mode == "PROJECT_FULL_AUTO"
        and latest.get("reason") == "SUPER_ADMIN_PROJECT_POLICY_CHANGED"
        and not _empty_policy_event_contract(latest)
    )
    legacy_config_generation = (
        previous_mode == "PROJECT_FULL_AUTO"
        and latest.get("reason") == contract["bootstrap_evidence"]["plugin_reason"]
        and latest.get("to_mode") == "PROJECT_FULL_AUTO"
        and latest.get("project_generation") == 1
        and policy.get("project_generation") == 1
    )
    if (
        (
            not legacy_contract_binding
            and (
                (
                    policy.get("project_generation") != expected_generation
                    and not legacy_config_generation
                )
                or policy.get("project_configuration_version") != project.get("config_version")
            )
        )
        or any(policy.get(field) != latest.get(field) for field in _POLICY_EVENT_CONTRACT_FIELDS)
        or not _policy_approval_matches(policy, approval_anchor)
        or (previous_mode == "PROJECT_FULL_AUTO" and not full_auto_authorized)
        or (
            previous_mode == "PROJECT_FULL_AUTO"
            and not _empty_policy_event_contract(latest)
            and latest.get("reason") != "SUPER_ADMIN_PROJECT_POLICY_CHANGED"
        )
    ):
        raise AutomationProjectReleaseManifestError("AUTOMATION_PROJECT_POLICY_STATE_INVALID")


def _validate_bootstrap_marker_summary(summary: Any) -> None:
    if not isinstance(summary, Mapping):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_POLICY_DISTRIBUTION_MISMATCH"
        )
    legacy_count = summary.get("legacy_schedule_only")
    require_count = summary.get("require_each_run")
    if (
        summary.get("project_count") != EXPECTED_RELEASE_PROJECT_COUNT
        or isinstance(legacy_count, bool)
        or not isinstance(legacy_count, int)
        or legacy_count < 0
        or isinstance(require_count, bool)
        or not isinstance(require_count, int)
        or require_count < 0
        or legacy_count + require_count != EXPECTED_RELEASE_PROJECT_COUNT
    ):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_POLICY_DISTRIBUTION_MISMATCH"
        )


def _validate_bootstrap_and_policy_state(
    cursor: Any,
    *,
    contract: Mapping[str, Any],
    schedules: Mapping[str, Mapping[str, Any]],
    backups: Mapping[str, Mapping[str, Any]],
    projects: Mapping[str, Mapping[str, Any]],
    expect_initial_production_manifest: bool,
) -> int:
    evidence = contract["bootstrap_evidence"]
    marker, items_by_id = _read_bootstrap_artifacts(cursor, contract)
    items = [items_by_id[item] for item in sorted(items_by_id)]
    try:
        marker_summary = evidence[
            "validate_existing_automation_project_bootstrap"
        ](
            marker,
            items,
            expected_automation_ids=tuple(sorted(contract["release_projects"])),
        )
    except evidence["error_class"] as exc:
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_MARKER_INVALID"
        ) from exc
    _validate_bootstrap_marker_summary(marker_summary)

    source_snapshots: dict[str, Mapping[str, Any]] = {}
    source_task_ids: set[str] = set()
    source_enabled_count = 0
    for automation_id in sorted(contract["release_projects"]):
        item = items_by_id[automation_id]
        try:
            source = evidence[
                "validate_automation_project_bootstrap_source_snapshot"
            ](item.get("source_snapshot_json"))
        except evidence["error_class"] as exc:
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_INVALID"
            ) from exc
        task_ids = {
            str(task.get("task_id") or "")
            for task in source.get("scheduled_tasks", [])
        }
        if (
            source.get("automation_id") != automation_id
            or task_ids != contract["templates"][automation_id]["task_ids"]
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_TASK_SET_MISMATCH"
            )
        source_task_ids.update(task_ids)
        source_enabled_count += sum(
            task.get("enabled") is True
            for task in source.get("scheduled_tasks", [])
        )
        source_snapshots[automation_id] = source
    if (
        source_task_ids != contract["release_tasks"]
        or source_enabled_count != 55
    ):
        raise AutomationProjectReleaseManifestError(
            "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_TASK_SET_MISMATCH"
        )

    policies, policy_events, configuration_evidence = (
        _read_project_policy_evidence(cursor, contract)
    )
    scheduled_policies, scheduled_events = _read_scheduled_policy_evidence(
        cursor,
        contract,
        include_current_policies=expect_initial_production_manifest,
    )
    bootstrap_generations = _read_bootstrap_generations(
        cursor,
        contract,
        source_snapshots,
    )
    _validate_historical_scheduled_events(
        contract,
        source_snapshots=source_snapshots,
        backups=backups,
        events=scheduled_events,
    )

    release_sha = str(marker_summary.get("release_sha") or "")
    for automation_id in sorted(contract["release_projects"]):
        item = items_by_id[automation_id]
        source = source_snapshots[automation_id]
        project = projects[automation_id]
        generation_row = bootstrap_generations[automation_id]
        _validate_bootstrap_generation_source(
            contract,
            automation_id=automation_id,
            source=source,
            generation_row=generation_row,
            backups=backups,
            allow_blocked=(
                not expect_initial_production_manifest
                and project.get("generation_state") == "BLOCKED"
                and (
                    project.get("reconcile_state") == "BLOCKED_UNKNOWN_WRITE"
                    or is_staged_unknown_write_quarantine(project)
                )
                and project.get("generation") == generation_row.get("generation")
            ),
        )
        try:
            persisted_configuration = evidence[
                "validate_persisted_automation_project_configuration_evidence"
            ](
                source_snapshot=source,
                release_sha=release_sha,
                policy_events=policy_events[automation_id],
                evidence_rows=configuration_evidence[automation_id],
                generation_schedule_sha256=generation_row.get(
                    "schedule_sha256"
                ),
            )
        except evidence["error_class"] as exc:
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_CONFIGURATION_EVENT_INVALID"
            ) from exc
        if (
            persisted_configuration.get("request_id")
            != source.get("configuration_request_id")
            or persisted_configuration.get("metadata_sha256")
            != source.get("configuration_event_metadata_sha256")
        ):
            raise AutomationProjectReleaseManifestError(
                "AUTOMATION_PROJECT_CONFIGURATION_EVENT_MISMATCH"
            )

        bootstrap_event, bootstrap_index = _bootstrap_event_for_project(
            contract,
            automation_id=automation_id,
            item=item,
            policy_events=policy_events[automation_id],
        )
        if expect_initial_production_manifest:
            if (
                project.get("generation") != source.get("automation_generation")
                or project.get("config_version")
                != source.get("project_configuration_version")
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_INITIAL_BOOTSTRAP_STATE_MISMATCH"
                )
            configuration_request_id = str(
                source.get("configuration_request_id") or ""
            )
            configuration_event_indexes = [
                index
                for index, event in enumerate(policy_events[automation_id])
                if event.get("request_id") == configuration_request_id
            ]
            if (
                len(configuration_event_indexes) != 1
                or configuration_event_indexes[0] >= bootstrap_index
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_CONFIGURATION_EVENT_INVALID"
                )
            initial_policy_events = policy_events[automation_id][
                : configuration_event_indexes[0] + 1
            ]
            try:
                full_configuration = evidence[
                    "validate_automation_project_configuration_evidence"
                ](
                    automation_id=automation_id,
                    release_sha=release_sha,
                    config=_project_config_evidence_row(project),
                    automation_generation=source["automation_generation"],
                    project_configuration_version=source[
                        "project_configuration_version"
                    ],
                    scheduled_task_count=len(source["scheduled_tasks"]),
                    policy_events=initial_policy_events,
                    evidence_rows=configuration_evidence[automation_id],
                )
            except evidence["error_class"] as exc:
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_CONFIGURATION_EVENT_INVALID"
                ) from exc
            if (
                full_configuration.get("request_id")
                != source.get("configuration_request_id")
                or full_configuration.get("metadata_sha256")
                != source.get("configuration_event_metadata_sha256")
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_CONFIGURATION_EVENT_MISMATCH"
                )

            current_rows: list[dict[str, Any]] = []
            current_scheduled_events: list[Mapping[str, Any]] = []
            for task_id in sorted(
                contract["templates"][automation_id]["task_ids"]
            ):
                row = dict(schedules[task_id])
                row.update(scheduled_policies[task_id])
                current_rows.append(row)
                current_scheduled_events.extend(scheduled_events[task_id])
            try:
                derived_source, legacy_authorized, _retired_count = evidence[
                    "derive_automation_project_bootstrap_source_snapshot"
                ](
                    automation_id=automation_id,
                    automation_generation=source["automation_generation"],
                    project_configuration_version=source[
                        "project_configuration_version"
                    ],
                    contract_hash=source["contract_hash"],
                    configuration_request_id=source[
                        "configuration_request_id"
                    ],
                    configuration_event_metadata_sha256=source[
                        "configuration_event_metadata_sha256"
                    ],
                    rows=current_rows,
                    legacy_rows=[
                        backups[task_id]
                        for task_id in sorted(
                            contract["templates"][automation_id]["task_ids"]
                        )
                    ],
                    scheduled_events=current_scheduled_events,
                )
                evidence[
                    "validate_initial_automation_project_bootstrap_policy"
                ](
                    policies[automation_id],
                    item=item,
                    bootstrap_event=bootstrap_event,
                )
            except evidence["error_class"] as exc:
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_INITIAL_BOOTSTRAP_STATE_INVALID"
                ) from exc
            expected_mode = (
                "LEGACY_SCHEDULE_ONLY"
                if current_rows and legacy_authorized
                else "REQUIRE_EACH_RUN"
            )
            if (
                not _strict_json_equal(derived_source, source)
                or item.get("initial_mode") != expected_mode
            ):
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_BOOTSTRAP_SOURCE_MISMATCH"
                )
        else:
            _validate_later_project_policy_chain(
                contract,
                automation_id=automation_id,
                item=item,
                project=project,
                policy=policies[automation_id],
                policy_events=policy_events[automation_id],
                configuration_evidence=configuration_evidence[automation_id],
            )
    return len(policies)


def _migration_018_applied(runner: Mapping[str, Any]) -> bool:
    connection = None
    try:
        connection = runner["_connect"]()
        with connection.cursor() as cursor:
            runner["_require_mysql8"](cursor)
            if not runner["_table_exists"](cursor, "schema_migrations"):
                return False
            cursor.execute(
                "SELECT 1 FROM schema_migrations WHERE version=%s LIMIT 1",
                (AUTOMATION_PROJECT_AUTHORIZATION_VERSION,),
            )
            return cursor.fetchone() is not None
    finally:
        if connection is not None:
            connection.close()


def _check_post_018_manifest(
    runner: Mapping[str, Any],
    *,
    expect_initial_production_manifest: bool,
) -> int:
    connection = None
    failure_code: str | None = None
    failure_count = 1
    schedules: dict[str, Mapping[str, Any]] = {}
    enabled_count = 0
    policy_count = 0
    try:
        contract = _load_release_contract()
        connection = runner["_connect"]()
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            runner["_require_mysql8"](cursor)
            required_tables = (
                "scheduled_tasks",
                "scheduled_task_automation_identity_backup_018",
                "automation_projects",
                "automation_project_events",
                "automation_project_configs",
                "automation_project_generations",
                "automation_project_generation_leases",
                "automation_project_policies",
                "automation_project_policy_events",
                "automation_project_bootstrap_items_018",
                "automation_project_bootstrap_marker_018",
                runner["SCHEDULED_TASK_APPROVAL_POLICY_TABLE"],
                runner["SCHEDULED_TASK_APPROVAL_EVENT_TABLE"],
            )
            missing_count = sum(
                not runner["_table_exists"](cursor, table_name)
                for table_name in required_tables
            )
            if missing_count:
                raise AutomationProjectReleaseManifestError(
                    "AUTOMATION_PROJECT_RELEASE_TABLE_MISSING",
                    count=missing_count,
                )
            schedules = _read_reviewed_schedule_rows(
                cursor,
                contract,
                expect_initial_production_manifest=(
                    expect_initial_production_manifest
                ),
            )
            projects = _read_release_projects(cursor, contract)
            _verify_deferred_projects_absent(cursor, contract)
            backups = _read_reviewed_backups(
                cursor,
                contract,
            )
            _validate_release_projects_and_tasks(
                contract,
                schedules=schedules,
                backups=backups,
                projects=projects,
                expect_initial_production_manifest=(
                    expect_initial_production_manifest
                ),
            )
            _validate_deferred_rows(
                contract,
                schedules=schedules,
                backups=backups,
            )
            policy_count = _validate_bootstrap_and_policy_state(
                cursor,
                contract=contract,
                schedules=schedules,
                backups=backups,
                projects=projects,
                expect_initial_production_manifest=(
                    expect_initial_production_manifest
                ),
            )
            enabled_count = sum(
                _boolean(
                    row.get("enabled"),
                    code="AUTOMATION_PROJECT_TASK_ENABLED_INVALID",
                )
                for row in schedules.values()
            )
    except AutomationProjectReleaseManifestError as exc:
        failure_code = exc.code
        failure_count = exc.count
    except Exception:
        failure_code = "AUTOMATION_PROJECT_MANIFEST_RUNTIME_ERROR"
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                failure_code = "AUTOMATION_PROJECT_MANIFEST_RUNTIME_ERROR"
                failure_count = 1
            try:
                connection.close()
            except Exception:
                failure_code = "AUTOMATION_PROJECT_MANIFEST_RUNTIME_ERROR"
                failure_count = 1

    if failure_code is not None:
        print(
            "control_plane_release_manifest=blocked "
            f"reason={failure_code} count={failure_count}",
            file=sys.stderr,
        )
        return 1

    print(
        "control_plane_release_manifest=ok "
        f"reviewed_rows={len(schedules)} enabled_rows={enabled_count} "
        f"policies={policy_count} marker=1 initial="
        f"{int(expect_initial_production_manifest)}"
    )
    return 0


def _check_legacy_manifest(
    runner: Mapping[str, Any],
    *,
    expect_initial_production_manifest: bool,
) -> int:
    """Preserve the exact pre-018 69-row release contract."""

    connection = None
    error_class = runner["ControlPlaneTaskCutoverPreflightError"]
    try:
        expected_ids = runner["_load_control_plane_reviewed_manifest_ids"]()
        registry = runner["_load_control_plane_tool_registry"]()
        approval_contracts = runner["_load_scheduled_task_approval_contract_module"]()
        scheduled_contracts = runner[
            "_load_control_plane_scheduled_task_contract_module"
        ]()
        profiles = scheduled_contracts.APPROVED_SCHEDULED_TASK_PROFILES
        profile_by_task_id = {
            task_id: profile
            for profile in profiles.values()
            for task_id in profile.approved_task_ids
        }
        connection = runner["_connect"]()
        with connection.cursor() as cursor:
            runner["_require_mysql8"](cursor)
            for table_name in (
                "scheduled_tasks",
                runner["SCHEDULED_TASK_APPROVAL_POLICY_TABLE"],
                runner["SCHEDULED_TASK_APPROVAL_EVENT_TABLE"],
            ):
                if not runner["_table_exists"](cursor, table_name):
                    raise error_class("CONTROL_PLANE_MANIFEST_TABLE_MISSING")
            cursor.execute(runner["CONTROL_PLANE_TASK_CANDIDATE_SQL"])
            rows = cursor.fetchall()
            task_ids = {
                str(row.get("id") or "")
                for row in rows
                if isinstance(row, Mapping)
            }
            if task_ids != expected_ids or len(rows) != len(expected_ids):
                raise error_class(
                    "REVIEWED_MANIFEST_TASK_SET_MISMATCH",
                    count=max(
                        len(expected_ids - task_ids) + len(task_ids - expected_ids),
                        1,
                    ),
                )
            summary = runner["validate_control_plane_task_cutover"](
                rows,
                contracts=runner["_load_control_plane_reviewed_task_contracts"](),
                optional_contracts=runner[
                    "_load_control_plane_optional_task_contracts"
                ](),
                clock_contracts=runner["_load_control_plane_clock_contracts"](),
                r7_contracts=runner["_load_control_plane_r7_contracts"](),
                allow_reviewed_disabled=not expect_initial_production_manifest,
            )
            enabled_ids = {
                str(row["id"])
                for row in rows
                if row.get("enabled") in {True, 1}
            }
            expected_reviewed_count = (
                runner["CONTROL_PLANE_REVIEWED_ENABLED_COUNT"]
                if expect_initial_production_manifest
                else runner["CONTROL_PLANE_REVIEWED_MANIFEST_COUNT"]
            )
            initial_state_mismatch = expect_initial_production_manifest and (
                len(enabled_ids) != runner["CONTROL_PLANE_REVIEWED_ENABLED_COUNT"]
                or expected_ids - enabled_ids
                != runner["CONTROL_PLANE_REVIEWED_DISABLED_IDS"]
            )
            if initial_state_mismatch or summary != {
                "reviewed_rows": expected_reviewed_count,
                "canonical_rows": expected_reviewed_count,
                "legacy_rows": 0,
            }:
                raise error_class("REVIEWED_MANIFEST_STATE_MISMATCH")
            cursor.execute(
                f"""
                SELECT 1
                FROM {runner['SCHEDULED_TASK_APPROVAL_EVENT_TABLE']}
                WHERE task_id=%s AND request_id=%s AND actor_id=%s
                  AND actor_role=%s
                  AND reason='control_plane_v1_bootstrap_complete'
                LIMIT 1
                """,
                (
                    runner["CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID"],
                    runner["CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID"],
                    runner["CONTROL_PLANE_MIGRATION_ACTOR_ID"],
                    runner["CONTROL_PLANE_MIGRATION_ACTOR_ROLE"],
                ),
            )
            if cursor.fetchone() is None:
                raise error_class("CONTROL_PLANE_BOOTSTRAP_MARKER_MISSING")
            placeholders = ", ".join("%s" for _ in expected_ids)
            cursor.execute(
                f"""
                SELECT policy.task_id, policy.mode, policy.contract_hash,
                       policy.contract_snapshot_json,
                       policy.tool_contract_hash,
                       policy.approved_by_actor_id,
                       policy.approved_by_actor_role, policy.version,
                       task.tool_name, task.tool_params,
                       task.cron_expression, task.enabled,
                       task.configuration_version,
                       (
                           latest_event.event_id IS NOT NULL
                           AND latest_event.to_mode = policy.mode
                           AND latest_event.actor_id = policy.approved_by_actor_id
                           AND latest_event.actor_role = policy.approved_by_actor_role
                           AND latest_event.contract_hash <=> policy.contract_hash
                       ) AS has_explaining_event,
                       latest_event.reason AS latest_event_reason
                FROM {runner['SCHEDULED_TASK_APPROVAL_POLICY_TABLE']} AS policy
                INNER JOIN scheduled_tasks AS task ON task.id = policy.task_id
                LEFT JOIN {runner['SCHEDULED_TASK_APPROVAL_EVENT_TABLE']} AS latest_event
                  ON latest_event.event_id = (
                      SELECT MAX(candidate.event_id)
                      FROM {runner['SCHEDULED_TASK_APPROVAL_EVENT_TABLE']} AS candidate
                      WHERE candidate.task_id = policy.task_id
                  )
                WHERE policy.task_id IN ({placeholders})
                """,
                tuple(sorted(expected_ids)),
            )
            policies = cursor.fetchall()
            runner["_validate_control_plane_policy_states"](
                policies,
                enabled_ids=enabled_ids,
                registry=registry,
                approval_contracts=approval_contracts,
                profile_by_task_id=profile_by_task_id,
                arguments_for_schema_validation=(
                    scheduled_contracts._arguments_for_schema_validation
                ),
                require_enabled_exact=expect_initial_production_manifest,
            )
    except error_class as exc:
        print(
            "control_plane_release_manifest=blocked "
            f"reason={exc.code} count={exc.count}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "control_plane_release_manifest=blocked "
            "reason=CONTROL_PLANE_MANIFEST_RUNTIME_ERROR count=1",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            connection.close()
    print(
        "control_plane_release_manifest=ok "
        f"reviewed_rows={len(expected_ids)} enabled_rows={len(enabled_ids)} "
        f"policies={len(policies)} marker=1 "
        f"initial={int(expect_initial_production_manifest)}"
    )
    return 0


def check_control_plane_release_manifest(
    runner: Mapping[str, Any],
    *,
    expect_initial_production_manifest: bool = False,
) -> int:
    """Dispatch only applied migration-018 databases to the project contract."""

    try:
        applied = _migration_018_applied(runner)
    except Exception:
        print(
            "control_plane_release_manifest=blocked "
            "reason=CONTROL_PLANE_MANIFEST_RUNTIME_ERROR count=1",
            file=sys.stderr,
        )
        return 1
    if applied:
        return _check_post_018_manifest(
            runner,
            expect_initial_production_manifest=expect_initial_production_manifest,
        )
    return _check_legacy_manifest(
        runner,
        expect_initial_production_manifest=expect_initial_production_manifest,
    )


__all__ = [
    "AutomationProjectReleaseManifestError",
    "check_control_plane_release_manifest",
]
