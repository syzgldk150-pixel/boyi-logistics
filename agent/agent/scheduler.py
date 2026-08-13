"""APScheduler entry adapter; every occurrence is submitted as a Command."""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from agent.orchestration.models import Actor, ActorType
from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES
from shared.finance.sources import enabled_finance_platforms


logger = logging.getLogger("agent")
_scheduler: AsyncIOScheduler | None = None
FINANCE_MISFIRE_GRACE_SECONDS = 3600
FINANCE_SCHEDULE_TASK_ID = "finance_bills_0010"
CUSTOMER_PROBLEM_SHADOW_TASK_ID = "customer_problems_shadow"
LOCKED_CONTROL_PLANE_TASK_IDS = frozenset(
    {FINANCE_SCHEDULE_TASK_ID, CUSTOMER_PROBLEM_SHADOW_TASK_ID}
)


def _enabled_finance_platform_filter() -> dict[str, str]:
    """Narrow scheduled calls when exactly one production platform is live."""

    platforms = enabled_finance_platforms()
    if not platforms:
        raise RuntimeError("没有已上线的财务来源，不能调度财务同步")
    if len(platforms) == 1:
        return {"platform": platforms[0]}
    return {}


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


def init_scheduler(agent_core) -> AsyncIOScheduler:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    try:
        seeded = ensure_control_plane_schedule_tasks(agent_core)
        if seeded:
            logger.info("Seeded control-plane schedule tasks: %s", ", ".join(seeded))
    except Exception as exc:
        logger.warning("Control-plane schedule initialization failed: %s", exc)
    try:
        _load_tasks_from_db(agent_core)
    except Exception as exc:
        logger.warning("Scheduled task loading failed: %s", exc)
    _add_finance_startup_catchup_job(agent_core)
    return _scheduler


def ensure_finance_schedule_task(agent_core) -> bool:
    """Seed the locked finance template only when the row is absent."""

    return FINANCE_SCHEDULE_TASK_ID in _seed_locked_schedule_tasks(
        agent_core,
        frozenset({FINANCE_SCHEDULE_TASK_ID}),
    )


def ensure_control_plane_schedule_tasks(agent_core) -> tuple[str, ...]:
    """Idempotently seed new locked tasks without overwriting admin choices."""

    return _seed_locked_schedule_tasks(agent_core, LOCKED_CONTROL_PLANE_TASK_IDS)


def _seed_locked_schedule_tasks(agent_core, task_ids: frozenset[str]) -> tuple[str, ...]:
    """Seed exactly ``task_ids`` while preserving any persisted override."""

    existing_ids = {
        str(row.get("id") or "").strip()
        for row in agent_core.memory.list_scheduled_tasks()
        if isinstance(row, dict)
    }
    templates = {
        str(task.get("id") or ""): task
        for task in PHASE7_SCHEDULED_TASK_TEMPLATES
        if str(task.get("id") or "") in task_ids
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


def _add_finance_startup_catchup_job(agent_core) -> None:
    """Perform the bounded startup gap scan through the same control plane."""

    async def startup_catchup() -> None:
        # Use one stable logical occurrence per business day.  Repeated service
        # restarts therefore reuse the same Command/Run instead of starting a
        # second finance scan for an arbitrary wall-clock timestamp.
        scheduled_for = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
            hour=0,
            minute=10,
            second=0,
            microsecond=0,
        )
        try:
            result = await _execute_scheduled_tool(
                agent_core,
                task_id="finance_startup_catchup",
                tool_name="sync_finance_bills",
                arguments={
                    "mode": "sync",
                    "rescan_days": 7,
                    "_startup_catchup": True,
                    **_enabled_finance_platform_filter(),
                },
                scheduled_for=scheduled_for,
                cron_expression="@startup",
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


def _load_tasks_from_db(agent_core) -> None:
    for task in agent_core.memory.list_enabled_scheduled_tasks():
        _add_job(
            task_id=task["id"],
            cron_expr=task["cron_expression"],
            tool_name=task["tool_name"],
            tool_params=task.get("tool_params") or {},
            agent_core=agent_core,
        )
        logger.info(
            "Loaded scheduled task: %s (%s) -> %s",
            task["name"],
            task["cron_expression"],
            task["tool_name"],
        )


def _add_job(task_id: str, cron_expr: str, tool_name: str, tool_params: dict, agent_core) -> None:
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
    ) -> None:
        logger.info("Scheduled task fired: %s -> %s", tid, tn)
        try:
            scheduled_for = _latest_scheduled_fire_time(trigger, datetime.now(trigger.timezone))
            if scheduled_for is None:
                raise RuntimeError("Unable to determine a stable scheduled fire time")
            arguments = copy.deepcopy(tp or {})
            if tn == "sync_finance_bills":
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
            )
            status = "success" if isinstance(result, dict) and result.get("success") else "error"
            if status != "success":
                logger.error("Scheduled task did not complete: %s -> %s", tid, _result_error(result))
            _update_task_status(agent_core, tid, status, result)
        except Exception as exc:
            logger.error("Scheduled task failed: %s -> %s", tid, str(exc)[:200])
            _update_task_status(agent_core, tid, "error", {"error": str(exc)})

    options: dict[str, Any] = {}
    if tool_name == "sync_finance_bills":
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
):
    """Submit one deterministic scheduler occurrence."""

    if scheduled_for.tzinfo is None:
        raise ValueError("scheduled_for must be timezone-aware")
    scheduled_iso = scheduled_for.isoformat()
    return await agent_core.execute_tool(
        tool_name,
        arguments,
        actor=Actor(ActorType.SCHEDULER, task_id, roles=("system",)),
        source="scheduler",
        idempotency_key=f"scheduler:{task_id}:{scheduled_iso}",
        execution_context={
            "task_id": task_id,
            "scheduled_for": scheduled_iso,
            "cron_expression": cron_expression,
        },
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


def reload_scheduler(agent_core) -> dict[str, Any]:
    if _scheduler is None:
        return {"initialized": False, "jobs": 0, "job_ids": []}
    for job in list(_scheduler.get_jobs()):
        _scheduler.remove_job(job.id)
    _load_tasks_from_db(agent_core)
    _add_finance_startup_catchup_job(agent_core)
    jobs = _scheduler.get_jobs()
    return {
        "initialized": True,
        "jobs": len(jobs),
        "job_ids": [job.id for job in jobs],
    }
