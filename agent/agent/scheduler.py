"""APScheduler entry adapter; every occurrence is submitted as a Command."""

from __future__ import annotations

import copy
import logging
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING, STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from agent.automation_plugins.release_scope import (
    DEFERRED_R7_LEGACY_SCHEDULE_GENERATION,
    DEFERRED_R7_PLUGIN_IDS,
)
from agent.orchestration.models import Actor, ActorType
from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES
from shared.automation_project_manifest import get_first_party_automation_project
from shared.finance.sources import enabled_finance_platforms
from shared.redaction import redact_text


logger = logging.getLogger("agent")
_scheduler: AsyncIOScheduler | None = None
_automation_project_invoker: Any | None = None
_include_startup_catchup_for_process = True
FINANCE_MISFIRE_GRACE_SECONDS = 3600
EXTERNAL_WRITE_MISFIRE_GRACE_SECONDS = 60
FINANCE_SCHEDULE_TASK_ID = "finance_bills_0010"
FINANCE_STARTUP_TASK_ID = "finance_startup_catchup"
SCHEDULER_RELEASE_HOLD_ENV = "BOYI_SCHEDULER_RELEASE_HOLD_FILE"
SCHEDULER_RELEASE_HOLD_NAME = "scheduler-release.pause"


class DeferredR7ScheduleIdentityError(RuntimeError):
    """A persisted R7 row no longer matches the reviewed migration identity."""


class ScheduledTaskIdentityConflictError(RuntimeError):
    """Two enabled rows claim the same global scheduler identity."""


def _deferred_r7_legacy_schedule_task_ids() -> frozenset[str]:
    task_ids: set[str] = set()
    for automation_id in DEFERRED_R7_PLUGIN_IDS:
        definition = get_first_party_automation_project(automation_id)
        if definition is None or definition.tool_name not in DEFERRED_R7_PLUGIN_IDS:
            raise RuntimeError("Deferred R7 release scope has no reviewed migration template")
        task_ids.update(definition.scheduled_task_ids)
    return frozenset(task_ids)


DEFERRED_R7_LEGACY_SCHEDULE_TASK_IDS = _deferred_r7_legacy_schedule_task_ids()


def _enabled_finance_platform_filter() -> dict[str, str]:
    """Narrow scheduled calls when exactly one production platform is live."""

    platforms = enabled_finance_platforms()
    if not platforms:
        raise RuntimeError("没有已上线的财务来源，不能调度财务同步")
    if len(platforms) == 1:
        return {"platform": platforms[0]}
    return {}


def _is_deferred_r7_legacy_schedule(
    *,
    task_id: str,
    tool_name: str,
    automation_id: str,
    automation_generation: int | None,
) -> bool:
    """Recognize only the code-reviewed migration-018 R7 legacy identity."""

    if automation_id not in DEFERRED_R7_PLUGIN_IDS:
        return False
    definition = get_first_party_automation_project(automation_id)
    if definition is None or definition.tool_name not in DEFERRED_R7_PLUGIN_IDS:
        return False
    return (
        task_id in definition.scheduled_task_ids
        and tool_name == definition.tool_name
        and type(automation_generation) is int
        and automation_generation == DEFERRED_R7_LEGACY_SCHEDULE_GENERATION
    )


def _deferred_r7_schedule_must_not_register(task: dict[str, Any]) -> bool:
    """Return true for an exact deferred row; reject every related drift."""

    task_id = str(task.get("id") or "")
    normalized_task_id = task_id.strip()
    tool_name = str(task.get("tool_name") or "")
    persisted_project_id = str(task.get("automation_id") or "")
    project_id = persisted_project_id.strip()
    normalized_tool_name = tool_name.strip()
    if (
        normalized_task_id not in DEFERRED_R7_LEGACY_SCHEDULE_TASK_IDS
        and project_id not in DEFERRED_R7_PLUGIN_IDS
        and normalized_tool_name not in DEFERRED_R7_PLUGIN_IDS
    ):
        return False
    if _is_deferred_r7_legacy_schedule(
        task_id=task_id,
        tool_name=tool_name,
        automation_id=persisted_project_id,
        automation_generation=task.get("automation_generation"),
    ):
        return True
    raise DeferredR7ScheduleIdentityError(
        "Deferred R7 scheduled task does not match its reviewed migration identity"
    )


def _latest_scheduled_fire_time(trigger: CronTrigger, now: datetime) -> datetime | None:
    """Return the latest fire time still inside the configured misfire window."""

    cursor = now - timedelta(seconds=FINANCE_MISFIRE_GRACE_SECONDS + 1)
    candidate = trigger.get_next_fire_time(None, cursor)
    latest = None
    for _ in range(10_000):
        if candidate is None or candidate > now:
            break
        latest = candidate
        candidate = trigger.get_next_fire_time(candidate, candidate + timedelta(microseconds=1))
    return latest


def init_scheduler(
    agent_core,
    *,
    automation_project_invoker: Any | None = None,
    include_startup_catchup: bool = True,
) -> AsyncIOScheduler:
    global _automation_project_invoker, _include_startup_catchup_for_process, _scheduler
    _automation_project_invoker = automation_project_invoker
    _include_startup_catchup_for_process = include_startup_catchup
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    try:
        seeded = ensure_control_plane_schedule_tasks(agent_core)
        if seeded:
            logger.info("Seeded control-plane schedule tasks: %s", ", ".join(seeded))
    except Exception as exc:
        logger.warning("Control-plane schedule initialization failed: %s", exc)
    try:
        _load_tasks_from_db(
            agent_core,
            automation_project_invoker=automation_project_invoker,
        )
    except ScheduledTaskIdentityConflictError:
        raise
    except Exception as exc:
        logger.warning("Scheduled task loading failed: %s", exc)
    if _include_startup_catchup_for_process:
        try:
            _add_finance_startup_catchup_job(
                agent_core,
                automation_project_invoker=automation_project_invoker,
            )
        except Exception as exc:
            logger.warning("Finance startup catch-up initialization failed: %s", exc)
    else:
        logger.info("Finance startup catch-up not registered for this held service start")
    return _scheduler


def ensure_finance_schedule_task(agent_core) -> bool:
    """Seed the locked finance template only when the row is absent."""

    return FINANCE_SCHEDULE_TASK_ID in _seed_locked_schedule_tasks(
        agent_core,
        frozenset({FINANCE_SCHEDULE_TASK_ID}),
    )


def ensure_control_plane_schedule_tasks(agent_core) -> tuple[str, ...]:
    """Seed every safe template disabled when absent; preserve existing rows."""

    return seed_phase7_schedule_tasks(agent_core)


def seed_phase7_schedule_tasks(agent_core) -> tuple[str, ...]:
    """Insert all missing safe templates while preserving every existing row."""

    return _seed_locked_schedule_tasks(
        agent_core,
        frozenset(_task_templates_by_id()),
    )


def _task_templates_by_id() -> dict[str, dict[str, Any]]:
    templates: dict[str, dict[str, Any]] = {}
    for task in PHASE7_SCHEDULED_TASK_TEMPLATES:
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            raise RuntimeError("A schedule template has no id")
        if task_id in templates:
            raise RuntimeError(f"Duplicate schedule template id: {task_id}")
        templates[task_id] = task
    return templates


def _seed_locked_schedule_tasks(agent_core, task_ids: frozenset[str]) -> tuple[str, ...]:
    """Seed exactly ``task_ids`` while preserving any persisted override."""

    existing_ids = {
        str(row.get("id") or "").strip()
        for row in agent_core.memory.list_scheduled_tasks()
        if isinstance(row, dict)
    }
    templates = {
        task_id: task
        for task_id, task in _task_templates_by_id().items()
        if task_id in task_ids
    }
    if set(templates) != set(task_ids):
        raise RuntimeError("A locked control-plane schedule template is missing or duplicated")
    seeded: list[str] = []
    for task_id in sorted(task_ids):
        if task_id in existing_ids:
            continue
        agent_core.memory.upsert_scheduled_task(copy.deepcopy(templates[task_id]))
        seeded.append(task_id)
    return tuple(seeded)


def _add_finance_startup_catchup_job(
    agent_core,
    *,
    automation_project_invoker: Any | None = None,
    startup_gate_provider: Any | None = None,
) -> None:
    """Perform the bounded startup gap scan through the same control plane."""

    startup_task = _finance_startup_schedule_task(agent_core)
    if startup_task is None:
        logger.info("Finance startup catch-up skipped because its independent task is disabled")
        return

    provider = (
        startup_gate_provider
        or getattr(agent_core, "finance_startup_gate_provider", None)
        or _agent_finance_startup_gate_provider(agent_core)
    )
    try:
        registration_occurrence = _finance_startup_occurrence(startup_task)
    except (TypeError, ValueError):
        logger.warning(
            "Finance startup catch-up was not registered: "
            "FINANCE_STARTUP_GATE_INVALID_OCCURRENCE"
        )
        return
    if not _finance_startup_gate_allows(
        startup_task,
        occurrence=registration_occurrence,
        provider=provider,
    ):
        logger.warning("Finance startup catch-up was not registered: STARTUP_GATE_BLOCKED")
        return

    async def startup_catchup() -> None:
        # Use one stable logical occurrence per task-contract version and
        # business day. Repeated starts on the same version therefore reuse the
        # same Command/Run instead of starting another arbitrary wall-clock scan.
        scheduled_for = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
            hour=0,
            minute=10,
            second=0,
            microsecond=0,
        )
        try:
            # Registration is only advisory: runtime/lease state can change
            # while APScheduler waits for its DateTrigger.
            if not _finance_startup_gate_allows(
                startup_task,
                occurrence=_finance_startup_occurrence(startup_task, scheduled_for),
                provider=provider,
            ):
                logger.warning("Finance startup catch-up skipped: STARTUP_GATE_BLOCKED")
                return
            result = await _execute_scheduled_tool(
                agent_core,
                task_id=FINANCE_STARTUP_TASK_ID,
                tool_name=str(startup_task.get("tool_name") or ""),
                arguments=copy.deepcopy(startup_task.get("tool_params") or {}),
                scheduled_for=scheduled_for,
                cron_expression="@startup",
                configuration_version=int(startup_task.get("configuration_version") or 1),
                automation_id=startup_task.get("automation_id"),
                automation_generation=startup_task.get("automation_generation"),
                automation_project_invoker=automation_project_invoker,
            )
            if not isinstance(result, dict) or not result.get("success"):
                logger.error("Startup finance catch-up did not complete: %s", _result_error(result))
        except Exception as exc:
            logger.error("Startup finance catch-up failed: %s", str(exc)[:200])

    if _scheduler is None:
        raise RuntimeError("scheduler is not initialized")
    trigger = DateTrigger(
        run_date=datetime.now().astimezone() + timedelta(seconds=15),
        timezone="Asia/Shanghai",
    )
    _scheduler.add_job(
        startup_catchup,
        trigger,
        id="finance_startup_catchup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


def _finance_startup_occurrence(
    task: Mapping[str, Any],
    scheduled_for: datetime | None = None,
) -> str:
    """Stable identity shared by the gate and scheduler idempotency key."""

    current = scheduled_for or datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=10, second=0, microsecond=0
    )
    if current.tzinfo is None:
        raise ValueError("finance startup occurrence must be timezone-aware")
    automation_id = str(task.get("automation_id") or "").strip()
    generation = task.get("automation_generation")
    version = task.get("configuration_version")
    if not automation_id or type(generation) is not int or type(version) is not int:
        raise ValueError("finance startup task has no exact project occurrence")
    return (
        f"scheduler:{FINANCE_STARTUP_TASK_ID}:v{version}:"
        f"{automation_id}:g{generation}:{current.isoformat()}"
    )


def _finance_startup_gate_allows(
    task: Mapping[str, Any],
    *,
    occurrence: str,
    provider: Any,
) -> bool:
    """Read only Agent-side state; malformed/unavailable evidence blocks."""

    if provider is None:
        logger.warning("Finance startup catch-up gate unavailable: FINANCE_STARTUP_GATE_UNAVAILABLE")
        return False
    try:
        reader = getattr(provider, "check_finance_startup_occurrence", provider)
        if not callable(reader):
            return False
        result = reader(
            automation_id=str(task.get("automation_id") or "").strip(),
            generation=task.get("automation_generation"),
            configuration_version=task.get("configuration_version"),
            occurrence=occurrence,
            idempotency_key=_finance_startup_command_idempotency(task, occurrence),
        )
    except Exception:
        logger.warning("Finance startup catch-up gate failed: FINANCE_STARTUP_GATE_UNAVAILABLE")
        return False
    required = {
        "runnable", "runtime_status", "scheduler_enabled",
        "unresolved_run", "unresolved_lease", "unresolved_receipt",
    }
    if not isinstance(result, Mapping) or set(result) != required:
        logger.warning("Finance startup catch-up gate failed: FINANCE_STARTUP_GATE_INVALID_EVIDENCE")
        return False
    return (
        result["runnable"] is True
        and result["runtime_status"] == "READY"
        and result["scheduler_enabled"] is True
        and all(result[key] is False for key in (
            "unresolved_run", "unresolved_lease", "unresolved_receipt"
        ))
    )


def _agent_finance_startup_gate_provider(agent_core: Any) -> Any | None:
    """Build the read-only gate from the Agent's already-bound repository.

    The scheduler must not query Console storage.  ``AgentCore`` is configured
    by the composition root before scheduler registration, so this adapter is
    available only in the Agent process.  If binding is absent, callers fail
    closed rather than treating that absence as a ready runtime.
    """

    repository = getattr(agent_core, "_orchestration_repository", None)
    if not callable(getattr(repository, "unit_of_work", None)):
        return None
    from agent.automation_plugins.runtime_repository import (
        MySQLAutomationPluginRuntimeAdapter,
    )

    return MySQLAutomationPluginRuntimeAdapter(repository)


def _finance_startup_command_idempotency(
    task: Mapping[str, Any],
    occurrence: str,
) -> str:
    """Recover the existing stable Command identity from the richer gate key."""

    generation = task.get("automation_generation")
    version = task.get("configuration_version")
    automation_id = str(task.get("automation_id") or "").strip()
    if (
        not automation_id
        or type(generation) is not int
        or type(version) is not int
    ):
        raise ValueError("finance startup task has no exact project occurrence")
    prefix = (
        f"scheduler:{FINANCE_STARTUP_TASK_ID}:v{version}:"
        f"{automation_id}:g{generation}:"
    )
    if not occurrence.startswith(prefix):
        raise ValueError("finance startup occurrence does not match its task binding")
    scheduled_iso = occurrence[len(prefix):]
    if not scheduled_iso:
        raise ValueError("finance startup occurrence has no scheduled instant")
    return f"scheduler:{FINANCE_STARTUP_TASK_ID}:v{version}:{scheduled_iso}"


def _finance_startup_schedule_task(agent_core) -> dict[str, Any] | None:
    """Return the enabled task that independently owns startup gap scanning."""

    for row in agent_core.memory.list_scheduled_tasks():
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "").strip() != FINANCE_STARTUP_TASK_ID:
            continue
        enabled = row.get("enabled")
        if not (enabled is True or type(enabled) is int and enabled == 1):
            return None
        automation_id = str(row.get("automation_id") or "").strip()
        expected_tool = (
            f"automation.{automation_id}.run"
            if automation_id
            else "sync_finance_bills"
        )
        if str(row.get("tool_name") or "") != expected_tool or str(
            row.get("cron_expression") or ""
        ) != "@startup":
            logger.error("Finance startup task has an invalid persisted binding")
            return None
        if automation_id and (
            type(row.get("automation_generation")) is not int
            or int(row["automation_generation"]) <= 0
        ):
            logger.error("Finance startup task has no committed project generation")
            return None
        return row
    return None


def _load_tasks_from_db(
    agent_core,
    *,
    automation_project_invoker: Any | None = None,
    include_startup_tasks: bool | None = None,
) -> None:
    """Validate and register every enabled non-finance schedule task.

    Finance startup catch-up deliberately remains owned by its dedicated gate
    below.  Every other ``@startup`` row is a normal, project-bound scheduled
    occurrence and is registered here when this process is allowed to run
    startup work.
    """
    if include_startup_tasks is None:
        include_startup_tasks = _include_startup_catchup_for_process
    registration_plan = _scheduled_task_registration_plan(
        agent_core,
        include_startup_tasks=include_startup_tasks,
    )
    plan, deferred_task_ids, _finance_startup_expected, invalid_tasks = registration_plan
    if deferred_task_ids:
        logger.warning(
            "Deferred R7 scheduled tasks were not registered: %s",
            ", ".join(sorted(deferred_task_ids)),
        )
    _log_invalid_scheduled_tasks(invalid_tasks)

    for task, is_startup in plan:
        try:
            _register_scheduled_task(
                task,
                is_startup=is_startup,
                agent_core=agent_core,
                automation_project_invoker=automation_project_invoker,
            )
        except Exception as exc:
            _log_invalid_scheduled_tasks([_scheduled_task_issue(task, exc)])


def _task_configuration_version(task: Mapping[str, Any]) -> int:
    """Use the historic daily default, but never accept an ambiguous value."""

    raw_version = task.get("configuration_version")
    if raw_version in (None, ""):
        return 1
    if type(raw_version) is not int or raw_version < 1:
        raise ValueError("Scheduled task has an invalid configuration_version")
    return raw_version


def _scheduled_task_registration_plan(
    agent_core,
    *,
    include_startup_tasks: bool,
) -> tuple[
    list[tuple[dict[str, Any], bool]],
    list[str],
    bool,
    list[dict[str, str]],
]:
    """Validate rows independently while preserving global task identities.

    A malformed row is quarantined from the in-memory scheduler without
    preventing unrelated valid rows from running. Duplicate non-empty task IDs
    remain a global conflict because no row can safely own that scheduler ID.
    """

    tasks = list(agent_core.memory.list_enabled_scheduled_tasks())
    registered_ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        if task_id in registered_ids:
            raise ScheduledTaskIdentityConflictError(
                f"Duplicate enabled scheduled task id: {task_id}"
            )
        registered_ids.add(task_id)

    deferred_task_ids: list[str] = []
    plan: list[tuple[dict[str, Any], bool]] = []
    finance_startup_expected = False
    invalid_tasks: list[dict[str, str]] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            invalid_tasks.append(
                {
                    "task_id": f"row:{index}",
                    "error_code": "VALUEERROR",
                    "error_summary": "Enabled scheduled task is not an object",
                }
            )
            continue
        try:
            is_deferred = _deferred_r7_schedule_must_not_register(task)
            if is_deferred:
                deferred_task_ids.append(str(task.get("id") or ""))
                continue
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                raise ValueError("Enabled scheduled task has no id")
            cron_expression = str(task.get("cron_expression") or "").strip()
            if task_id == FINANCE_STARTUP_TASK_ID:
                # ``@startup`` is owned by the dedicated startup gate below.
                _validate_finance_startup_task(task)
                finance_startup_expected = include_startup_tasks
                continue
            if cron_expression == "@startup":
                _validate_startup_project_task(task)
                if include_startup_tasks:
                    plan.append((task, True))
                continue
            _validate_cron_task(task)
            plan.append((task, False))
        except Exception as exc:
            invalid_tasks.append(_scheduled_task_issue(task, exc))
    return plan, deferred_task_ids, finance_startup_expected, invalid_tasks


def _scheduled_task_issue(
    task: Mapping[str, Any],
    exc: Exception,
) -> dict[str, str]:
    task_id = str(task.get("id") or "").strip() or "<missing>"
    return {
        "task_id": task_id,
        "error_code": type(exc).__name__.upper()[:64],
        "error_summary": redact_text(exc)[:200],
    }


def _log_invalid_scheduled_tasks(issues: list[dict[str, str]]) -> None:
    for issue in issues:
        logger.error(
            "Scheduled task quarantined: %s code=%s reason=%s",
            issue["task_id"],
            issue["error_code"],
            issue["error_summary"],
        )


def _register_scheduled_task(
    task: Mapping[str, Any],
    *,
    is_startup: bool,
    agent_core,
    automation_project_invoker: Any | None,
) -> None:
    if is_startup:
        _add_startup_project_job(
            task,
            agent_core=agent_core,
            automation_project_invoker=automation_project_invoker,
        )
    else:
        _add_job(
            task_id=str(task["id"]),
            cron_expr=str(task["cron_expression"]),
            tool_name=str(task["tool_name"]),
            tool_params=dict(task.get("tool_params") or {}),
            configuration_version=_task_configuration_version(task),
            automation_id=task.get("automation_id"),
            automation_generation=task.get("automation_generation"),
            automation_project_invoker=automation_project_invoker,
            agent_core=agent_core,
        )
    logger.info(
        "Loaded scheduled task: %s (%s) -> %s",
        task.get("name") or task["id"],
        task["cron_expression"],
        task["tool_name"],
    )


def _validate_cron_task(task: Mapping[str, Any]) -> None:
    task_id = str(task.get("id") or "").strip()
    cron_expression = str(task.get("cron_expression") or "").strip()
    if len(cron_expression.split()) != 5:
        raise ValueError(f"Invalid cron expression for task {task_id}: {cron_expression}")
    _task_configuration_version(task)
    _validate_project_task_identity(task, startup=False)
    minute, hour, day, month, day_of_week = cron_expression.split()
    # Parse before mutating the live scheduler so invalid ranges fail closed.
    CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone="Asia/Shanghai",
    )


def _validate_startup_project_task(task: Mapping[str, Any]) -> None:
    if str(task.get("cron_expression") or "").strip() != "@startup":
        raise ValueError("Startup project task must use @startup")
    _task_configuration_version(task)
    _validate_project_task_identity(task, startup=True)


def _validate_finance_startup_task(task: Mapping[str, Any]) -> None:
    if str(task.get("cron_expression") or "").strip() != "@startup":
        raise ValueError("Finance startup task must use @startup")
    _task_configuration_version(task)
    automation_id = str(task.get("automation_id") or "").strip()
    if not automation_id:
        raise ValueError("Finance startup task has no explicit project identity")
    _validate_project_task_identity(task, startup=True)


def _validate_project_task_identity(task: Mapping[str, Any], *, startup: bool) -> None:
    automation_id = str(task.get("automation_id") or "").strip()
    tool_name = str(task.get("tool_name") or "").strip()
    if not isinstance(task.get("tool_params") or {}, Mapping):
        raise ValueError("Scheduled task parameters must be an object")
    if not automation_id:
        if startup:
            raise ValueError("@startup task must have an explicit project identity")
        if tool_name.startswith("automation."):
            raise ValueError("Scheduled automation task is missing an explicit project identity")
        return
    if tool_name != f"automation.{automation_id}.run":
        raise ValueError("Scheduled automation tool identity does not match its project")
    generation = task.get("automation_generation")
    if type(generation) is not int or generation <= 0:
        raise ValueError("Scheduled automation project has no committed generation")


def _startup_daily_occurrence() -> datetime:
    """Return one stable, timezone-aware occurrence for this Shanghai day."""

    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _add_startup_project_job(
    task: Mapping[str, Any],
    *,
    agent_core,
    automation_project_invoker: Any | None = None,
) -> None:
    """Register one generic project startup occurrence through CommandGateway."""

    _validate_startup_project_task(task)
    task_id = str(task["id"])
    tool_name = str(task["tool_name"])
    configuration_version = _task_configuration_version(task)
    automation_id = str(task["automation_id"])
    automation_generation = task["automation_generation"]
    tool_params = copy.deepcopy(task.get("tool_params") or {})

    async def startup_job() -> None:
        try:
            result = await _execute_scheduled_tool(
                agent_core,
                task_id=task_id,
                tool_name=tool_name,
                arguments=copy.deepcopy(tool_params),
                scheduled_for=_startup_daily_occurrence(),
                cron_expression="@startup",
                configuration_version=configuration_version,
                automation_id=automation_id,
                automation_generation=automation_generation,
                automation_project_invoker=automation_project_invoker,
            )
            if not isinstance(result, dict) or not result.get("success"):
                logger.error("Startup project task did not complete: %s", _result_error(result))
        except Exception as exc:
            logger.error("Startup project task failed: %s -> %s", task_id, str(exc)[:200])

    if _scheduler is None:
        raise RuntimeError("scheduler is not initialized")
    _scheduler.add_job(
        startup_job,
        DateTrigger(
            run_date=datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=15),
            timezone="Asia/Shanghai",
        ),
        id=task_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


def _add_job(
    task_id: str,
    cron_expr: str,
    tool_name: str,
    tool_params: dict,
    agent_core,
    *,
    configuration_version: int = 1,
    automation_id: str | None = None,
    automation_generation: int | None = None,
    automation_project_invoker: Any | None = None,
) -> None:
    parts = str(cron_expr or "").split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression for task {task_id}: {cron_expr}")
    minute, hour, day, month, day_of_week = parts
    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone="Asia/Shanghai",
    )

    async def job_func(
        tn: str = tool_name,
        tp: dict = tool_params,
        tid: str = task_id,
        cron: str = cron_expr,
        config_version: int = configuration_version,
        project_id: str | None = automation_id,
        project_generation: int | None = automation_generation,
    ) -> None:
        logger.info("Scheduled task fired: %s -> %s", tid, tn)
        try:
            scheduled_for = _latest_scheduled_fire_time(trigger, datetime.now(trigger.timezone))
            if scheduled_for is None:
                raise RuntimeError("Unable to determine a stable scheduled fire time")
            arguments = copy.deepcopy(tp or {})
            if not project_id and tn == "sync_finance_bills":
                for key, value in _enabled_finance_platform_filter().items():
                    arguments.setdefault(key, value)
                arguments["target_date"] = (
                    scheduled_for.date() - timedelta(days=1)
                ).isoformat()
            result = await _execute_scheduled_tool(
                agent_core,
                task_id=tid,
                tool_name=tn,
                arguments=arguments,
                scheduled_for=scheduled_for,
                cron_expression=cron,
                configuration_version=config_version,
                automation_id=project_id,
                automation_generation=project_generation,
                automation_project_invoker=automation_project_invoker,
            )
            status = "success" if isinstance(result, dict) and result.get("success") else "error"
            if status != "success":
                logger.error("Scheduled task did not complete: %s -> %s", tid, _result_error(result))
            _update_task_status(agent_core, tid, status, result)
        except Exception as exc:
            logger.error("Scheduled task failed: %s -> %s", tid, str(exc)[:200])
            _update_task_status(agent_core, tid, "error", {"error": str(exc)})

    options: dict[str, Any] = {}
    registry = getattr(agent_core, "registry", None)
    capability = (
        registry.get_capability(tool_name)
        if registry is not None and hasattr(registry, "get_capability")
        else None
    )
    operation_type = (
        str(capability.get("operation_type") or "")
        if isinstance(capability, dict)
        else ""
    )
    if automation_id or operation_type == "external_write":
        # External writes are never replayed concurrently.  A missed in-memory
        # occurrence gets only a short grace window; durable Command
        # idempotency still protects duplicate submissions for the same exact
        # scheduled timestamp.
        options = {
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": EXTERNAL_WRITE_MISFIRE_GRACE_SECONDS,
        }
    if tool_name == "sync_finance_bills" or automation_id in {
        "finance_bills",
        "finance_startup_catchup",
    }:
        options = {
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": FINANCE_MISFIRE_GRACE_SECONDS,
        }
    if _scheduler is None:
        raise RuntimeError("scheduler is not initialized")
    _scheduler.add_job(job_func, trigger, id=task_id, replace_existing=True, **options)


async def _execute_scheduled_tool(
    agent_core,
    *,
    task_id: str,
    tool_name: str,
    arguments: dict,
    scheduled_for: datetime,
    cron_expression: str,
    configuration_version: int | None = None,
    automation_id: str | None = None,
    automation_generation: int | None = None,
    automation_project_invoker: Any | None = None,
):
    """Submit one deterministic scheduler occurrence."""

    if scheduled_for.tzinfo is None:
        raise ValueError("scheduled_for must be timezone-aware")
    scheduled_iso = scheduled_for.isoformat()
    execution_context = {
        "task_id": task_id,
        "scheduled_for": scheduled_iso,
        "cron_expression": cron_expression,
    }
    if configuration_version is not None:
        execution_context["configuration_version"] = configuration_version
    idempotency_key = f"scheduler:{task_id}:{scheduled_iso}"
    if cron_expression == "@startup":
        if type(configuration_version) is not int or configuration_version < 1:
            raise ValueError("@startup tasks require a positive configuration_version")
        # A startup occurrence is stable for one task contract version.  Older
        # production builds used the same daily timestamp without a contract
        # version; retaining that key after the task contract changes would
        # collide with an immutable legacy Command instead of submitting the
        # newly governed occurrence.
        startup_identity = f"v{configuration_version}"
        project_id = str(automation_id or "").strip()
        if project_id and project_id != FINANCE_STARTUP_TASK_ID:
            if type(automation_generation) is not int or automation_generation <= 0:
                raise RuntimeError(
                    "Scheduled automation project has no committed generation"
                )
            startup_identity += f":g{automation_generation}"
        idempotency_key = f"scheduler:{task_id}:{startup_identity}:{scheduled_iso}"
    project_id = str(automation_id or "").strip()
    if project_id:
        expected_tool = f"automation.{project_id}.run"
        if tool_name != expected_tool:
            raise RuntimeError(
                "Scheduled automation tool identity does not match its project"
            )
        if type(automation_generation) is not int or automation_generation <= 0:
            raise RuntimeError(
                "Scheduled automation project has no committed generation"
            )
        invoker = automation_project_invoker or _automation_project_invoker
        if invoker is None or not hasattr(invoker, "invoke_trusted_and_wait"):
            raise RuntimeError("Scheduled automation project invoker is unavailable")
        return await invoker.invoke_trusted_and_wait(
            project_id,
            entrypoint="scheduler",
            request_id=idempotency_key,
            actor=Actor(
                ActorType.SCHEDULER,
                task_id,
                roles=("system",),
                authenticated_by="apscheduler",
            ),
            trusted_context=execution_context,
            idempotency_key=idempotency_key,
            expected_automation_generation=automation_generation,
            expected_project_configuration_version=configuration_version,
        )
    if str(tool_name or "").startswith("automation."):
        raise RuntimeError(
            "Scheduled automation command is missing an explicit project identity"
        )
    return await agent_core.execute_tool(
        tool_name,
        arguments,
        actor=Actor(ActorType.SCHEDULER, task_id, roles=("system",)),
        source="scheduler",
        idempotency_key=idempotency_key,
        execution_context=execution_context,
    )


def _update_task_status(agent_core, task_id: str, status: str, result: Any) -> None:
    try:
        duration_ms = None
        last_message = None
        if isinstance(result, dict):
            duration_s = result.get("duration_s")
            if duration_s not in (None, ""):
                try:
                    duration_ms = int(float(duration_s) * 1000)
                except (TypeError, ValueError):
                    duration_ms = None
            if not result.get("success"):
                last_message = _result_error(result) or None
        agent_core.memory.update_scheduled_task_runtime(
            task_id,
            last_status=status,
            last_duration_ms=duration_ms,
            last_message=last_message,
        )
    except Exception as exc:
        logger.error("Failed to update scheduled task status: %s", exc)


def _result_error(result: Any) -> str:
    if not isinstance(result, dict):
        return redact_text(result)[:1000]
    parts = [
        str(result.get("error_code") or "").strip(),
        str(result.get("error_summary") or "").strip(),
        str(result.get("error") or "").strip(),
    ]
    error = result.get("error")
    if isinstance(error, dict):
        parts.extend(
            str(error.get(key) or "").strip()
            for key in ("code", "summary", "message")
        )
    data = result.get("data")
    if isinstance(data, dict):
        parts.extend(
            str(data.get(key) or "").strip()
            for key in ("error_code", "error_summary", "error")
        )
    return redact_text(
        " | ".join(dict.fromkeys(part for part in parts if part))
    )[:1000]


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def scheduler_release_hold_path() -> Path:
    """Return the fixed release hold path without reading deployment secrets."""

    configured = str(os.getenv(SCHEDULER_RELEASE_HOLD_ENV, "") or "").strip()
    path = (
        Path(configured)
        if configured
        else Path.home() / ".boyi-deploy" / SCHEDULER_RELEASE_HOLD_NAME
    )
    if not path.is_absolute():
        raise RuntimeError("Scheduler release hold path must be absolute")
    return path


def _scheduler_release_hold_value() -> str | None:
    path = scheduler_release_hold_path()
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("Scheduler release hold cannot be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128:
        raise RuntimeError("Scheduler release hold is not a bounded regular file")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Scheduler release hold cannot be read") from exc


def scheduler_release_hold_requested() -> bool:
    """Fail closed when the deployment-owned hold marker is present."""

    return _scheduler_release_hold_value() is not None


def scheduler_runtime_status() -> dict[str, Any]:
    scheduler = _scheduler
    state = STATE_STOPPED if scheduler is None else scheduler.state
    state_name = {
        STATE_STOPPED: "stopped",
        STATE_RUNNING: "running",
        STATE_PAUSED: "paused",
    }.get(state, "unknown")
    return {
        "state": state_name,
        "release_hold": scheduler_release_hold_requested(),
        "jobs": len(scheduler.get_jobs()) if scheduler is not None else 0,
    }


def _validated_release_hold(expected_release_sha: str) -> tuple[str, str | None]:
    release_sha = str(expected_release_sha or "").strip().lower()
    if len(release_sha) != 40 or any(char not in "0123456789abcdef" for char in release_sha):
        raise RuntimeError("A canonical release SHA is required to activate the scheduler")
    hold_value = _scheduler_release_hold_value()
    if hold_value is not None and hold_value != release_sha:
        raise RuntimeError("Scheduler release hold does not match the active release")
    return release_sha, hold_value


def begin_scheduler_release_activation(expected_release_sha: str) -> dict[str, Any]:
    """Resume the validated scheduler without consuming its crash-safe marker.

    Marker deletion is deliberately a separate final step.  The caller must
    first confirm both APScheduler and WorkflowRunner are runnable, otherwise
    a process failure between unlink and resume could bypass the release gate
    on the next service start.
    """

    _release_sha, hold_value = _validated_release_hold(expected_release_sha)
    scheduler = _scheduler
    if scheduler is None or scheduler.state == STATE_STOPPED:
        raise RuntimeError("Scheduler is not available for release activation")
    if hold_value is None and scheduler.state == STATE_PAUSED:
        raise RuntimeError("Scheduler is paused without the active release hold")

    if scheduler.state == STATE_PAUSED:
        scheduler.resume()
    if scheduler.state != STATE_RUNNING:
        raise RuntimeError("Scheduler did not enter the running state")
    return scheduler_runtime_status()


def pause_scheduler_for_release() -> dict[str, Any]:
    """Best-effort re-hold used when activation fails before marker commit."""

    scheduler = _scheduler
    if scheduler is None or scheduler.state == STATE_STOPPED:
        raise RuntimeError("Scheduler is not available for release hold")
    if _scheduler_release_hold_value() is None:
        raise RuntimeError("Scheduler release hold is missing")
    if scheduler.state == STATE_RUNNING:
        scheduler.pause()
    if scheduler.state != STATE_PAUSED:
        raise RuntimeError("Scheduler did not enter the paused state")
    return scheduler_runtime_status()


def consume_scheduler_release_hold(expected_release_sha: str) -> dict[str, Any]:
    """Remove the matching marker only after every runtime reports runnable."""

    _release_sha, hold_value = _validated_release_hold(expected_release_sha)
    scheduler = _scheduler
    if scheduler is None or scheduler.state != STATE_RUNNING:
        raise RuntimeError("Scheduler must be running before release hold commit")
    if hold_value is None:
        return scheduler_runtime_status()
    try:
        scheduler_release_hold_path().unlink()
    except OSError as exc:
        raise RuntimeError("Scheduler release hold could not be consumed") from exc
    return scheduler_runtime_status()


def reload_scheduler(
    agent_core,
    *,
    automation_project_invoker: Any | None = None,
) -> dict[str, Any]:
    """Atomically replace the live registration plan from persisted schedules.

    The read-only plan validation happens before touching the active scheduler.
    If APScheduler rejects any new registration, every prior job is rebuilt with
    its original trigger, options and next run time.
    """
    global _automation_project_invoker
    if automation_project_invoker is not None:
        _automation_project_invoker = automation_project_invoker
    if _scheduler is None:
        return {"initialized": False, "jobs": 0, "job_ids": []}

    # Validate all regular rows first. The finance startup path intentionally
    # retains its historic gate-and-log behavior and is registered separately.
    registration_plan = _scheduled_task_registration_plan(
        agent_core,
        include_startup_tasks=_include_startup_catchup_for_process,
    )
    plan, deferred_task_ids, _finance_startup_expected, invalid_tasks = registration_plan
    previous_jobs = [_snapshot_scheduler_job(job) for job in _scheduler.get_jobs()]
    desired_job_ids: set[str] = set()
    registration_failures: list[dict[str, str]] = []
    try:
        for task, is_startup in plan:
            task_id = str(task["id"])
            # APScheduler does not apply ``replace_existing`` to a pending job
            # until the scheduler starts. Remove the old id explicitly so a
            # stopped scheduler cannot accumulate duplicate pending jobs.
            existing_job = _scheduler.get_job(task_id)
            if existing_job is not None:
                _scheduler.remove_job(existing_job.id)
            try:
                _register_scheduled_task(
                    task,
                    is_startup=is_startup,
                    agent_core=agent_core,
                    automation_project_invoker=_automation_project_invoker,
                )
            except Exception as exc:
                failed_job = _scheduler.get_job(task_id)
                if failed_job is not None:
                    _scheduler.remove_job(task_id)
                registration_failures.append(_scheduled_task_issue(task, exc))
                continue
            desired_job_ids.add(task_id)

        if _include_startup_catchup_for_process:
            # The special finance job must be removed first so an unavailable
            # gate cannot leave behind an obsolete startup action.
            existing_finance_job = _scheduler.get_job(FINANCE_STARTUP_TASK_ID)
            if existing_finance_job is not None:
                _scheduler.remove_job(FINANCE_STARTUP_TASK_ID)
            _add_finance_startup_catchup_job(
                agent_core,
                automation_project_invoker=_automation_project_invoker,
            )
            if _scheduler.get_job(FINANCE_STARTUP_TASK_ID) is not None:
                desired_job_ids.add(FINANCE_STARTUP_TASK_ID)

        for job in list(_scheduler.get_jobs()):
            if job.id not in desired_job_ids:
                _scheduler.remove_job(job.id)
    except Exception:
        _restore_scheduler_jobs(previous_jobs)
        raise

    if deferred_task_ids:
        logger.warning(
            "Deferred R7 scheduled tasks were not registered: %s",
            ", ".join(sorted(deferred_task_ids)),
        )
    all_invalid_tasks = [*invalid_tasks, *registration_failures]
    _log_invalid_scheduled_tasks(all_invalid_tasks)
    jobs = _scheduler.get_jobs()
    return {
        "initialized": True,
        "jobs": len(jobs),
        "job_ids": [job.id for job in jobs],
        "invalid_tasks": all_invalid_tasks,
        "deferred_task_ids": deferred_task_ids,
    }


def _snapshot_scheduler_job(job: Any) -> dict[str, Any]:
    """Capture every APScheduler property required for an exact rollback."""

    return {
        "id": job.id,
        "func": job.func,
        "trigger": job.trigger,
        "args": tuple(job.args),
        "kwargs": dict(job.kwargs),
        "name": job.name,
        "executor": job.executor,
        "misfire_grace_time": job.misfire_grace_time,
        "coalesce": job.coalesce,
        "max_instances": job.max_instances,
        # Pending jobs have not yet had APScheduler compute a next fire time.
        # Preserve that distinction instead of converting it into a paused job.
        "pending": job.pending,
        "next_run_time": getattr(job, "next_run_time", None),
    }


def _restore_scheduler_jobs(jobs: list[dict[str, Any]]) -> None:
    """Restore the pre-reload scheduler state after a failed registration."""

    if _scheduler is None:
        raise RuntimeError("scheduler is not initialized")
    for job in list(_scheduler.get_jobs()):
        _scheduler.remove_job(job.id)
    for job in jobs:
        restore_options = {
            "args": job["args"],
            "kwargs": job["kwargs"],
            "id": job["id"],
            "name": job["name"],
            "executor": job["executor"],
            "misfire_grace_time": job["misfire_grace_time"],
            "coalesce": job["coalesce"],
            "max_instances": job["max_instances"],
            "replace_existing": True,
        }
        if not job["pending"]:
            restore_options["next_run_time"] = job["next_run_time"]
        _scheduler.add_job(
            job["func"],
            job["trigger"],
            **restore_options,
        )
