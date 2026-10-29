"""Release write window for scheduled Service V2 plugin writes.

The release must not stop services shortly before or after an enabled plugin
schedule whose committed contract writes externally. The check is read-only
and prints no task IDs, arguments or digests. Shared contract code is loaded
by exact path and the ``shared`` namespace is restored afterwards, so this
helper never depends on ``sys.path`` or the working directory.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent
SCHEDULED_WRITE_TIMEZONE = ZoneInfo("Asia/Shanghai")
SCHEDULED_WRITE_WINDOW_BEFORE_MINUTES = 60
SCHEDULED_WRITE_WINDOW_AFTER_MINUTES = 45
SCHEDULED_WRITE_OPERATION_TYPES = frozenset(
    {"external_write", "financial_write", "destructive"}
)
_QUARTER_HOUR_DAILY_TIMES = tuple(
    f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 15, 30, 45)
)
_PROJECT_TABLES = (
    "automation_projects",
    "automation_project_policies",
    "automation_project_generations",
    "automation_project_generation_leases",
)


class ScheduledWriteWindowError(RuntimeError):
    """A value-free reason the release must wait."""

    def __init__(self, code: str, *, count: int = 1) -> None:
        super().__init__(code)
        self.code = str(code)
        self.count = max(int(count), 1)


def _load_exact_module(
    module_name: str,
    path: Path,
    *,
    submodule_search_locations: Sequence[str] | None = None,
) -> Any:
    spec = importlib.util.spec_from_file_location(
        module_name,
        path,
        submodule_search_locations=submodule_search_locations,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("exact-path module spec is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _is_shared_module(name: str) -> bool:
    return name == "shared" or name.startswith("shared.")


def load_schedule_contract() -> dict[str, Any]:
    """Load the runtime's scheduler binding without leaking ``shared`` modules."""

    shared_dir = REPOSITORY_ROOT / "shared"
    previous = {name: module for name, module in sys.modules.items() if _is_shared_module(name)}
    for name in previous:
        sys.modules.pop(name, None)
    try:
        _load_exact_module(
            "shared",
            shared_dir / "__init__.py",
            submodule_search_locations=[str(shared_dir)],
        )
        # The repository package owns the module initialization order.
        importlib.import_module("shared.automation_plugin_repository")
        runtime = importlib.import_module("shared.automation_plugin_generation_runtime_repository")
        material = importlib.import_module("shared.automation_schedule_material")
        support = importlib.import_module("shared.orchestration_repository_support")
        return {
            "scheduler_contribution_binding": runtime.scheduler_contribution_binding,
            "schedule_generation_shape": material.schedule_generation_shape,
            "persistence_error_class": support.OrchestrationPersistenceError,
        }
    finally:
        for name in [name for name in sys.modules if _is_shared_module(name)]:
            sys.modules.pop(name, None)
        sys.modules.update(previous)


def read_project_schedule_rows(cursor: Any) -> list[Mapping[str, Any]]:
    """Read every V2 project with its committed generation and physical tasks."""

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
        ORDER BY BINARY task.id
        """)
    return list(cursor.fetchall())


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
_GENERATION_STATE_DIAGNOSTIC_VALUES = frozenset({
    "TARGET", "PREPARING", "WAITING_COEFFECTS", "PREPARED", "COMMITTED",
    "DRAINING", "DISPOSING", "DISPOSED", "FAILED", "BLOCKED",
})


def _safe_enum_diagnostic(value: Any, *, allowed: frozenset[str]) -> str:
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


def _count_diagnostic(value: Any) -> str:
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


def _runtime_validation_issue(
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
    if type(row.get("enabled")) not in {bool, int} or row.get("enabled") not in {True, 1}:
        return "TASK_ENABLED_INVALID"
    if type(row.get("project_enabled")) not in {bool, int} or row.get("project_enabled") not in {True, 1}:
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


def _runtime_invalid_code(row: Mapping[str, Any], *, issue: str) -> str:
    """Build a bounded diagnostic code without projecting database values."""

    committed_generation = row.get("committed_generation")
    return "__".join((
        "PROJECT_SCHEDULE_RUNTIME_INVALID",
        f"CHECK_{issue}",
        "PROJECT_STATE_" + _safe_enum_diagnostic(row.get("project_state"), allowed=_PROJECT_STATE_DIAGNOSTIC_VALUES),
        "RECONCILE_STATE_" + _safe_enum_diagnostic(row.get("reconcile_state"), allowed=_RECONCILE_STATE_DIAGNOSTIC_VALUES),
        "TARGET_RELATION_" + _generation_relation_diagnostic(row.get("target_generation"), committed_generation),
        "POLICY_TARGET_RELATION_" + _generation_relation_diagnostic(
            row.get("policy_project_generation"), row.get("target_generation")),
        "MAX_COMMITTED_RELATION_" + _generation_relation_diagnostic(row.get("max_generation"), committed_generation),
        "TARGET_MAX_NEXT_" + _next_generation_diagnostic(row.get("target_generation"), row.get("max_generation")),
        "UNSAFE_NON_DISPOSED_OTHERS_" + _count_diagnostic(row.get("unsafe_non_disposed_other_count")),
        "ACTIVE_CURRENT_LEASES_" + _count_diagnostic(row.get("active_current_lease_count")),
        "COMMITTED_STATE_" + _safe_enum_diagnostic(row.get("generation_state"), allowed=_GENERATION_STATE_DIAGNOSTIC_VALUES),
        "COMMITTED_ERROR_" + _committed_error_diagnostic(row.get("generation_error_code")),
        "TARGET_STATE_" + _safe_enum_diagnostic(
            row.get("target_generation_state"), allowed=_GENERATION_STATE_DIAGNOSTIC_VALUES),
        "TARGET_BASE_RELATION_" + _generation_relation_diagnostic(row.get("target_base_generation"), committed_generation),
        "UNKNOWN_WRITE_LEASES_" + _count_diagnostic(row.get("unknown_write_count")),
    ))


def _decode_json(value: Any, *, code: str) -> Any:
    if isinstance(value, (dict, list, bool, int, float)) or value is None:
        return copy.deepcopy(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScheduledWriteWindowError(code) from exc
    if type(value) is str:
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ScheduledWriteWindowError(code) from exc
    raise ScheduledWriteWindowError(code)


def normalized_schedule_expressions(value: Any) -> tuple[tuple[str, ...], bool]:
    schedule = _decode_json(value, code="PROJECT_SCHEDULE_CONFIG_INVALID")
    if not isinstance(schedule, dict) or set(schedule) != {"kind", "times", "enabled"}:
        raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONFIG_INVALID")
    kind = schedule.get("kind")
    times = schedule.get("times")
    enabled = schedule.get("enabled")
    if (
        kind not in {"none", "daily_times", "startup"}
        or type(enabled) is not bool
        or not isinstance(times, list)
        or any(type(item) is not str for item in times)
    ):
        raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONFIG_INVALID")
    if kind == "none":
        if times or enabled:
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONFIG_INVALID")
        return (), False
    if kind == "startup":
        if times:
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONFIG_INVALID")
        return ("@startup",), enabled
    if not times or times != sorted(times) or len(times) != len(set(times)):
        raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONFIG_INVALID")
    for item in times:
        if (
            len(item) != 5
            or item[2] != ":"
            or not item[:2].isdigit()
            or not item[3:].isdigit()
            or not 0 <= int(item[:2]) <= 23
            or not 0 <= int(item[3:]) <= 59
        ):
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONFIG_INVALID")
    if tuple(times) == _QUARTER_HOUR_DAILY_TIMES:
        return ("*/15 * * * *",), enabled
    return tuple(f"{int(item[3:])} {int(item[:2])} * * *" for item in times), enabled


def _staged_runtime(row: Mapping[str, Any]) -> bool:
    return is_staged_unknown_write_quarantine(row) or is_staged_recoverable_runtime(row)


def _validate_scheduler_contract(snapshot: Mapping[str, Any], contract: Mapping[str, Any], *, cron_expression: Any) -> None:
    """Use the runtime's unique scheduler binding for release windows."""

    enabled_entrypoints = snapshot.get("enabled_entrypoints")
    if not isinstance(enabled_entrypoints, list) or any(type(item) is not str for item in enabled_entrypoints):
        raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONTRACT_INVALID")
    execution_metadata = snapshot["execution_metadata"]
    try:
        entrypoint, enabled = contract["scheduler_contribution_binding"](
            snapshot=snapshot,
            execution_metadata=execution_metadata,
            enabled_entrypoints=enabled_entrypoints,
            schedule_expressions=(cron_expression,),
        )
    except (ValueError, contract["persistence_error_class"]) as exc:
        raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONTRACT_INVALID") from exc
    if not enabled or entrypoint not in execution_metadata["compiled_invocations"]:
        raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONTRACT_INVALID")


def _validate_schedule_projections(
    rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    contract: Mapping[str, Any],
) -> None:
    """Check current host times against the complete committed task set."""

    grouped: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    for row, snapshot in rows:
        grouped.setdefault(row["automation_id"], []).append((row, snapshot))
    for project_rows in grouped.values():
        project, snapshot = project_rows[0]
        # An interrupted configuration change intentionally retains the prior
        # committed projection while current configuration describes its target.
        if _staged_runtime(project):
            continue
        metadata = snapshot.get("execution_metadata") or {}
        desired = _decode_json(project.get("desired_schedule_json"), code="PROJECT_SCHEDULE_CONFIG_INVALID")
        expressions, _enabled = normalized_schedule_expressions(desired)
        normalized_schedule_expressions(metadata.get("schedule"))
        digest = hashlib.sha256(json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if (project.get("desired_schedule_sha256") != digest
                or contract["schedule_generation_shape"](desired)
                != contract["schedule_generation_shape"](metadata.get("schedule"))
                or project.get("config_version") != metadata.get("project_config_version")):
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONFIG_INVALID")
        tasks = [row for row, _snapshot in project_rows if row.get("task_id") is not None]
        if tuple(sorted(expressions)) != tuple(sorted(str(row.get("cron_expression") or "") for row in tasks)):
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_ROWS_MISMATCH")
        for row in tasks:
            if (row.get("automation_generation") != project.get("committed_generation")
                    or row.get("configuration_version") != project.get("config_version")
                    or row.get("tool_name") != f"automation.{project['automation_id']}.run"):
                raise ScheduledWriteWindowError("AUTOMATION_PROJECT_TASK_RUNTIME_MISMATCH")


def project_scheduled_write_crons(
    cursor: Any,
    *,
    contract_loader: Callable[[], Mapping[str, Any]] = load_schedule_contract,
) -> tuple[str, ...]:
    """Return enabled committed plugin write crons, excluding exact quarantine."""

    rows = read_project_schedule_rows(cursor)
    contract: Mapping[str, Any] | None = None
    crons: list[str] = []
    seen_task_ids: set[str] = set()
    v2_rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_RUNTIME_INVALID__CHECK_ROW_SHAPE_INVALID")
        snapshot = _decode_json(row.get("snapshot_json"), code="PROJECT_SCHEDULE_CONTRACT_INVALID")
        if not isinstance(snapshot, Mapping) or snapshot.get("runtime_model") != "SERVICE_V2":
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONTRACT_INVALID")
        v2_rows.append((row, snapshot))
        # Every project participates in the projection check; only enabled
        # tasks contribute to the release's active write windows.
        if row.get("task_id") is None or row.get("enabled") in (False, 0):
            continue
        isolated_runtime = is_staged_unknown_write_quarantine(row) or (
            is_staged_recoverable_runtime(row) and row.get("project_state") != "ENABLED"
        )
        issue = _runtime_validation_issue(row, seen_task_ids=seen_task_ids, isolated_runtime=isolated_runtime)
        if issue is not None:
            raise ScheduledWriteWindowError(_runtime_invalid_code(row, issue=issue))
        seen_task_ids.add(str(row["task_id"]))
        if row.get("policy_mode") != "PROJECT_FULL_AUTO":
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_POLICY_INVALID")
        execution_metadata = snapshot.get("execution_metadata")
        governance_anchor = execution_metadata.get("governance_anchor") if isinstance(execution_metadata, Mapping) else None
        compiled_invocations = (
            execution_metadata.get("compiled_invocations") if isinstance(execution_metadata, Mapping) else None
        )
        if (
            snapshot.get("automation_id") != row.get("automation_id")
            or snapshot.get("generation") != row.get("committed_generation")
            or not isinstance(governance_anchor, Mapping)
            or not isinstance(compiled_invocations, Mapping)
        ):
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CONTRACT_INVALID")
        if contract is None:
            contract = contract_loader()
        _validate_scheduler_contract(snapshot, contract, cron_expression=row.get("cron_expression"))
        scheduled_write = governance_anchor.get("operation_type") in SCHEDULED_WRITE_OPERATION_TYPES
        cron_expression = row.get("cron_expression")
        if scheduled_write and (type(cron_expression) is not str or not cron_expression):
            raise ScheduledWriteWindowError("PROJECT_SCHEDULE_CRON_INVALID")
        if scheduled_write and not isolated_runtime:
            crons.append(cron_expression)
    if v2_rows:
        _validate_schedule_projections(v2_rows, contract if contract is not None else contract_loader())
    return tuple(sorted(set(crons)))


def daily_schedule_minutes(cron_expression: Any) -> int:
    if type(cron_expression) is not str:
        raise ScheduledWriteWindowError("SCHEDULED_WRITE_CRON_INVALID")
    match = re.fullmatch(r"(?P<minute>\d{1,2}) (?P<hour>\d{1,2}) \* \* \*", cron_expression.strip())
    if match is None:
        raise ScheduledWriteWindowError("SCHEDULED_WRITE_CRON_INVALID")
    minute = int(match.group("minute"))
    hour = int(match.group("hour"))
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ScheduledWriteWindowError("SCHEDULED_WRITE_CRON_INVALID")
    return hour * 60 + minute


def is_within_daily_schedule_window(
    now: datetime,
    cron_expression: str,
    *,
    before_minutes: int,
    after_minutes: int,
) -> bool:
    if before_minutes < 0 or after_minutes < 0:
        raise ValueError("scheduled write window minutes must be non-negative")
    local_now = now.astimezone(SCHEDULED_WRITE_TIMEZONE)
    scheduled_hour, scheduled_minute = divmod(daily_schedule_minutes(cron_expression), 60)
    today = local_now.replace(hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0)
    return any(
        candidate - timedelta(minutes=before_minutes) <= local_now <= candidate + timedelta(minutes=after_minutes)
        for candidate in (today - timedelta(days=1), today, today + timedelta(days=1))
    )


def _table_exists(cursor: Any, table_name: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) AS table_count FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table_name,),
    )
    row = cursor.fetchone()
    value = row.get("table_count") if isinstance(row, Mapping) else (row[0] if row else 0)
    return bool(value)


def check_scheduled_write_window(
    connect: Callable[[], Any],
    *,
    before_minutes: int = SCHEDULED_WRITE_WINDOW_BEFORE_MINUTES,
    after_minutes: int = SCHEDULED_WRITE_WINDOW_AFTER_MINUTES,
    now: datetime | None = None,
) -> int:
    """Block release mutation near an enabled scheduled plugin write."""

    connection = None
    crons: tuple[str, ...] = ()
    try:
        if before_minutes < 0 or after_minutes < 0:
            raise ScheduledWriteWindowError("SCHEDULED_WRITE_WINDOW_INVALID")
        connection = connect()
        with connection.cursor() as cursor:
            presence = tuple(_table_exists(cursor, name) for name in _PROJECT_TABLES)
            if any(presence) and not all(presence):
                raise ScheduledWriteWindowError("PROJECT_SCHEDULE_SCHEMA_INCOMPLETE")
            if all(presence):
                crons = project_scheduled_write_crons(cursor)
        checked_at = now or datetime.now(tz=SCHEDULED_WRITE_TIMEZONE)
        blocked_count = sum(
            is_within_daily_schedule_window(
                checked_at, cron, before_minutes=before_minutes, after_minutes=after_minutes,
            )
            for cron in crons
        )
        if blocked_count:
            raise ScheduledWriteWindowError("SCHEDULED_WRITE_WINDOW_ACTIVE", count=blocked_count)
    except ScheduledWriteWindowError as exc:
        print(f"scheduled_write_window=blocked reason={exc.code} count={exc.count}", file=sys.stderr)
        return 1
    except Exception:
        print("scheduled_write_window=blocked reason=SCHEDULED_WRITE_WINDOW_RUNTIME_ERROR count=1", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
    print(f"scheduled_write_window=ok checked_schedules={len(crons)}")
    return 0


__all__ = [
    "SCHEDULED_WRITE_WINDOW_AFTER_MINUTES",
    "SCHEDULED_WRITE_WINDOW_BEFORE_MINUTES",
    "ScheduledWriteWindowError",
    "check_scheduled_write_window",
    "daily_schedule_minutes",
    "is_within_daily_schedule_window",
    "load_schedule_contract",
    "normalized_schedule_expressions",
    "project_scheduled_write_crons",
    "read_project_schedule_rows",
]
