"""定时任务调度器：APScheduler，替代 N8N cron"""

import copy
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES
from shared.finance.sources import enabled_finance_platforms

logger = logging.getLogger("agent")

_scheduler: AsyncIOScheduler | None = None
FINANCE_MISFIRE_GRACE_SECONDS = 3600
FINANCE_SCHEDULE_TASK_ID = "finance_bills_0010"


def _enabled_finance_platform_filter() -> dict[str, str]:
    """Narrow scheduled calls when exactly one production platform is live."""

    platforms = enabled_finance_platforms()
    if not platforms:
        raise RuntimeError("没有已上线的财务来源，不能调度财务同步")
    if len(platforms) == 1:
        return {"platform": platforms[0]}
    return {}


def _latest_scheduled_fire_time(trigger: CronTrigger, now: datetime) -> datetime | None:
    """Return the latest CronTrigger fire time still inside the misfire window."""

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
    """初始化调度器，从 MySQL 加载任务定义"""
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    try:
        if ensure_finance_schedule_task(agent_core):
            logger.info("已补种财务定时任务: %s", FINANCE_SCHEDULE_TASK_ID)
    except Exception as e:
        logger.warning("财务定时任务初始化失败: %s", e)

    # 从 MySQL 加载已保存的定时任务
    try:
        _load_tasks_from_db(agent_core)
    except Exception as e:
        logger.warning("加载定时任务失败（数据库可能还没有任务）: %s", e)

    _add_finance_startup_catchup_job(agent_core)

    return _scheduler


def ensure_finance_schedule_task(agent_core) -> bool:
    """Insert the required finance schedule only when it is absent.

    Existing rows are preserved verbatim so an administrator can temporarily
    disable the job without the next service restart silently overwriting that
    operational choice.
    """

    existing_ids = {
        str(row.get("id") or "").strip()
        for row in agent_core.memory.list_scheduled_tasks()
        if isinstance(row, dict)
    }
    if FINANCE_SCHEDULE_TASK_ID in existing_ids:
        return False

    templates = [
        task
        for task in PHASE7_SCHEDULED_TASK_TEMPLATES
        if str(task.get("id") or "") == FINANCE_SCHEDULE_TASK_ID
    ]
    if len(templates) != 1:
        raise RuntimeError("财务定时任务模板缺失或重复")
    agent_core.memory.upsert_scheduled_task(copy.deepcopy(templates[0]))
    return True


def _add_finance_startup_catchup_job(agent_core) -> None:
    """Run one gap-only finance catch-up shortly after every service start."""

    async def startup_catchup() -> None:
        logger.info("服务启动财务缺口扫描触发")
        try:
            result = await agent_core.execute_tool(
                "sync_finance_bills",
                {
                    "mode": "sync",
                    "rescan_days": 7,
                    "_startup_catchup": True,
                    **_enabled_finance_platform_filter(),
                },
            )
            if not isinstance(result, dict) or not result.get("success"):
                logger.error(
                    "服务启动财务缺口扫描失败: %s",
                    str((result or {}).get("error") if isinstance(result, dict) else result)[:200],
                )
        except Exception as exc:
            logger.error("服务启动财务缺口扫描异常: %s", str(exc)[:200])

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


def _load_tasks_from_db(agent_core):
    """从 scheduled_tasks 表加载任务"""
    for task in agent_core.memory.list_enabled_scheduled_tasks():
        _add_job(
            task_id=task["id"],
            cron_expr=task["cron_expression"],
            tool_name=task["tool_name"],
            tool_params=task.get("tool_params") or {},
            agent_core=agent_core,
        )
        logger.info("加载定时任务: %s (%s) → %s", task["name"], task["cron_expression"], task["tool_name"])


def _add_job(task_id: str, cron_expr: str, tool_name: str, tool_params: dict, agent_core):
    """添加定时任务"""
    parts = cron_expr.split()
    if len(parts) == 5:
        minute, hour, day, month, day_of_week = parts
    else:
        logger.error("无效的 cron 表达式: %s", cron_expr)
        return

    trigger = CronTrigger(
        minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
        timezone="Asia/Shanghai",
    )

    async def job_func(tn=tool_name, tp=tool_params, tid=task_id, cron=cron_expr):
        logger.info("定时任务触发: %s → %s", tid, tn)
        try:
            scheduled_params = copy.deepcopy(tp or {})
            scheduled_metadata = {
                "id": tid,
                "cron_expression": cron,
            }
            if tn == "sync_finance_bills":
                for key, value in _enabled_finance_platform_filter().items():
                    scheduled_params.setdefault(key, value)
                now = datetime.now(trigger.timezone)
                scheduled_for = _latest_scheduled_fire_time(trigger, now)
                if scheduled_for is None:
                    raise RuntimeError("无法从财务任务 CronTrigger 确定本次计划触发时间")
                target_date = (scheduled_for.date() - timedelta(days=1)).isoformat()
                scheduled_metadata.update(
                    {
                        "scheduled_for": scheduled_for.isoformat(),
                        "target_date": target_date,
                    }
                )
                scheduled_params["target_date"] = target_date
            scheduled_params["_scheduled_task"] = scheduled_metadata
            result = await agent_core.execute_tool(tn, scheduled_params)
            status = "success" if isinstance(result, dict) and result.get("success") else "error"
            if status != "success":
                logger.error("定时任务失败: %s → %s", tid, str((result or {}).get("error") or result)[:200])
            _update_task_status(agent_core, tid, status, result)
        except Exception as e:
            logger.error("定时任务失败: %s → %s", tid, str(e)[:200])
            _update_task_status(agent_core, tid, "error", {"error": str(e)})

    job_options = {}
    if tool_name == "sync_finance_bills":
        job_options = {
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": FINANCE_MISFIRE_GRACE_SECONDS,
        }
    _scheduler.add_job(
        job_func,
        trigger,
        id=task_id,
        replace_existing=True,
        **job_options,
    )


def _update_task_status(agent_core, task_id: str, status: str, result):
    """更新任务执行状态"""
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
                message_parts: list[str] = []
                error_text = str(result.get("error") or "").strip()
                if error_text:
                    message_parts.append(error_text)
                data = result.get("data")
                if isinstance(data, dict):
                    data_error = str(data.get("error") or "").strip()
                    if data_error and data_error not in message_parts:
                        message_parts.append(data_error)
                if message_parts:
                    last_message = " | ".join(message_parts)[:1000]

        agent_core.memory.update_scheduled_task_runtime(
            task_id,
            last_status=status,
            last_duration_ms=duration_ms,
            last_message=last_message,
        )
    except Exception as e:
        logger.error("更新任务状态失败: %s", e)


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def reload_scheduler(agent_core) -> dict:
    """热重载调度器中的任务定义。"""
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
