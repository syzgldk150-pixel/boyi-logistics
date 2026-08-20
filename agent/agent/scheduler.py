"""APScheduler entry adapter; every occurrence is submitted as a Command."""

from __future__ import annotations

import copy
import logging
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING, STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from agent.automation_plugins.release_scope import (
    DEFERRED_R7_LEGACY_SCHEDULE_GENERATION,
    DEFERRED_R7_PLUGIN_IDS,
)
from agent.automation_plugins.quarantine import (
    DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID,
    DELIVERY_STATUS_QUARANTINE_GENERATION,
    DELIVERY_STATUS_QUARANTINE_PLUGIN_ID,
    DELIVERY_STATUS_QUARANTINE_STATUS,
)
from agent.orchestration.models import Actor, ActorType
from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES
from shared.automation_project_manifest import get_first_party_automation_project
from shared.finance.sources import enabled_finance_platforms


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


class DeliveryStatusQuarantineIdentityError(RuntimeError):
    """A delivery schedule row is not the one audited unknown-write incident."""


def _deferred_r7_legacy_schedule_task_ids() -> frozenset[str]:
    task_ids: set[str] = set()
    for automation_id in DEFERRED_R7_PLUGIN_IDS:
        definition = get_first_party_automation_project(automation_id)
        if definition is None or definition.tool_name not in DEFERRED_R7_PLUGIN_IDS:
            raise RuntimeError("Deferred R7 release scope has no reviewed migration template")
        task_ids.update(definition.scheduled_task_ids)
    return frozenset(task_ids)


DEFERRED_R7_LEGACY_SCHEDULE_TASK_IDS = _deferred_r7_legacy_schedule_task_ids()


def _delivery_status_quarantine_schedule_task_ids() -> frozenset[str]:
    definition = get_first_party_automation_project(
        DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID
    )
    if (
        definition is None
        or definition.tool_name != DELIVERY_STATUS_QUARANTINE_PLUGIN_ID
    ):
        raise RuntimeError("Delivery quarantine has no reviewed migration template")
    return frozenset(definition.scheduled_task_ids)


DELIVERY_STATUS_QUARANTINE_SCHEDULE_TASK_IDS = (
    _delivery_status_quarantine_schedule_task_ids()
)
_DELIVERY_STATUS_QUARANTINE_TOOL_NAME = (
    f"automation.{DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID}.run"
)


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


def _is_delivery_status_quarantine_schedule(
    *,
    task_id: str,
    tool_name: str,
    automation_id: str,
    automation_generation: int | None,
) -> bool:
    return (
        task_id in DELIVERY_STATUS_QUARANTINE_SCHEDULE_TASK_IDS
        and tool_name == _DELIVERY_STATUS_QUARANTINE_TOOL_NAME
        and automation_id == DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID
        and type(automation_generation) is int
        and automation_generation == DELIVERY_STATUS_QUARANTINE_GENERATION
    )


def _delivery_status_quarantine_schedule_must_not_register(
    task: dict[str, Any],
    *,
    agent_core: Any,
) -> bool:
    """Skip precisely the audited delivery schedules, never a name match."""

    task_id = str(task.get("id") or "")
    normalized_task_id = task_id.strip()
    tool_name = str(task.get("tool_name") or "")
    normalized_tool_name = tool_name.strip()
    automation_id = str(task.get("automation_id") or "")
    normalized_automation_id = automation_id.strip()
    if (
        normalized_task_id not in DELIVERY_STATUS_QUARANTINE_SCHEDULE_TASK_IDS
        and normalized_tool_name != _DELIVERY_STATUS_QUARANTINE_TOOL_NAME
        and normalized_automation_id != DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID
    ):
        return False
    if not _is_delivery_status_quarantine_schedule(
        task_id=task_id,
        tool_name=tool_name,
        automation_id=automation_id,
        automation_generation=task.get("automation_generation"),
    ):
        raise DeliveryStatusQuarantineIdentityError(
            "Delivery scheduled task does not match the audited quarantine identity"
        )
    registry = getattr(agent_core, "registry", None)
    status_reader = getattr(
        registry,
        "delivery_status_unknown_write_quarantine_status",
        None,
    )
    if not callable(status_reader):
        raise DeliveryStatusQuarantineIdentityError(
            "Delivery quarantine status reader is unavailable"
        )
    status = status_reader()
    if status is None:
        return False
    if status == DELIVERY_STATUS_QUARANTINE_STATUS:
        return True
    raise DeliveryStatusQuarantineIdentityError(
        "Delivery quarantine status is not the audited unknown-write incident"
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
    except (DeferredR7ScheduleIdentityError, DeliveryStatusQuarantineIdentityError):
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
) -> None:
    """Perform the bounded startup gap scan through the same control plane."""

    startup_task = _finance_startup_schedule_task(agent_core)
    if startup_task is None:
        logger.info("Finance startup catch-up skipped because its independent task is disabled")
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
        misfire_grace_time=300,
    )


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
) -> None:
    classified_tasks: list[tuple[dict[str, Any], bool, bool]] = []
    for task in agent_core.memory.list_enabled_scheduled_tasks():
        classified_tasks.append(
            (
                task,
                _deferred_r7_schedule_must_not_register(task),
                _delivery_status_quarantine_schedule_must_not_register(
                    task,
                    agent_core=agent_core,
                ),
            )
        )
    deferred_task_ids = [
        str(task.get("id") or "")
        for task, is_deferred, _is_quarantined in classified_tasks
        if is_deferred
    ]
    quarantined_task_ids = [
        str(task.get("id") or "")
        for task, _is_deferred, is_quarantined in classified_tasks
        if is_quarantined
    ]
    if deferred_task_ids:
        logger.warning(
            "Deferred R7 scheduled tasks were not registered: %s",
            ", ".join(sorted(deferred_task_ids)),
        )
    if quarantined_task_ids:
        logger.warning(
            "Delivery unknown-write quarantine scheduled tasks were not registered: %s",
            ", ".join(sorted(quarantined_task_ids)),
        )

    for task, is_deferred, is_quarantined in classified_tasks:
        if is_deferred or is_quarantined:
            continue
        if str(task.get("id") or "") == FINANCE_STARTUP_TASK_ID:
            # ``@startup`` is a persisted special occurrence, not a cron
            # expression.  Its dedicated DateTrigger is registered below.
            continue
        _add_job(
            task_id=task["id"],
            cron_expr=task["cron_expression"],
            tool_name=task["tool_name"],
            tool_params=task.get("tool_params") or {},
            configuration_version=int(task.get("configuration_version") or 1),
            automation_id=task.get("automation_id"),
            automation_generation=task.get("automation_generation"),
            automation_project_invoker=automation_project_invoker,
            agent_core=agent_core,
        )
        logger.info(
            "Loaded scheduled task: %s (%s) -> %s",
            task["name"],
            task["cron_expression"],
            task["tool_name"],
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
        logger.error("Invalid cron expression for task %s: %s", task_id, cron_expr)
        return
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
        idempotency_key = (
            f"scheduler:{task_id}:v{configuration_version}:{scheduled_iso}"
        )
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
        return str(result)[:1000]
    parts = [str(result.get("error") or "").strip()]
    data = result.get("data")
    if isinstance(data, dict):
        parts.append(str(data.get("error") or "").strip())
    return " | ".join(dict.fromkeys(part for part in parts if part))[:1000]


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
    global _automation_project_invoker
    if automation_project_invoker is not None:
        _automation_project_invoker = automation_project_invoker
    if _scheduler is None:
        return {"initialized": False, "jobs": 0, "job_ids": []}
    for job in list(_scheduler.get_jobs()):
        _scheduler.remove_job(job.id)
    _load_tasks_from_db(
        agent_core,
        automation_project_invoker=_automation_project_invoker,
    )
    if _include_startup_catchup_for_process:
        try:
            _add_finance_startup_catchup_job(
                agent_core,
                automation_project_invoker=_automation_project_invoker,
            )
        except Exception as exc:
            logger.warning("Finance startup catch-up initialization failed: %s", exc)
    jobs = _scheduler.get_jobs()
    return {
        "initialized": True,
        "jobs": len(jobs),
        "job_ids": [job.id for job in jobs],
    }
