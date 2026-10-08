"""Select this run's three-day arrival tasks and verify every submission."""

from datetime import datetime, timedelta, timezone
import re

SERVICE = "plugin.r7_vehicle_checkin_v2.checkin@1"
CONNECTOR = "connector.boyi.r7_vehicle_tasks@1"
SHANGHAI = timezone(timedelta(hours=8))
ERROR_MESSAGES = {
    "BLOCKED_LOGIN": "所选R7账号未登录或登录态已失效，请在账号管理中重新登录。",
    "R7_TASK_CHANGED": "任务状态或计划发车时间已变化，请在R7页面核实。",
    "R7_DRIVER_ARRIVAL_CONFIRMATION_REQUIRED": "任务缺少司机到达记录，请在R7页面核实车辆已到达并完成确认。",
    "R7_EXISTING_PUNCH_CONFIRMATION_REQUIRED": "任务已有人工到达记录，本次未覆盖，请在R7页面核实。",
    "R7_OPERATION_SITE_REQUIRED": "当前账号不能自动确定操作站点，请在R7页面核实站点。",
    "R7_OPERATION_SITE_AMBIGUOUS": "未找到唯一操作站点，请在R7页面核实线路途经站。",
    "R7_OPERATION_SITE_INVALID": "当前登录站点不能执行此任务的到达待卸。",
    "WRITE_OUTCOME_UNKNOWN": "提交结果尚未确认，请先在R7页面核实该任务，避免重复打卡。",
}


def date_range(now=None):
    today = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date()
    return {"start_time": str(today - timedelta(days=2)) + " 00:00:00", "end_time": str(today) + " 23:59:59"}


def run(broker, now=None):
    interval = date_range(now)
    refs, rows, completed = [], [], []
    attempted = False
    pagination_complete = False
    current_task = None
    total = None

    def call(operation, args):
        result = broker(
            "service.invoke",
            action=operation,
            role="__system__",
            arguments={"service": CONNECTOR, "operation": operation, "arguments": args},
        )
        reference = getattr(result, "host_evidence_ref", None)
        if not isinstance(result, dict) or not isinstance(reference, str) or not reference:
            raise ValueError("R7_HOST_EVIDENCE_MISSING")
        refs.append(reference)
        return result

    try:
        for page in range(1, 101):
            result = call("read_page", {**interval, "page": page})
            if total is None:
                total = result["total"]
            if result["total"] != total or result["page"] != page:
                raise ValueError("R7_PAGINATION_CHANGED")
            rows.extend(result["items"])
            if result["complete"]:
                break
        else:
            raise ValueError("R7_PAGINATION_LIMIT")
        if len(rows) != total:
            raise ValueError("R7_PAGINATION_INCOMPLETE")
        if len({r["task_id"] for r in rows}) != len(rows) or len({r["task_number"] for r in rows}) != len(rows):
            raise ValueError("R7_TASK_IDENTITY_AMBIGUOUS")
        pagination_complete = True
        candidates = [r for r in rows if r["status"] == 55]
        if len(candidates) > 800:
            raise ValueError("R7_CANDIDATE_LIMIT")
        for row in candidates:
            attempted = True
            current_task = {"task_id": row["task_id"], "task_number": row["task_number"]}
            result = call("arrive", {**interval, "task_id": row["task_id"], "task_number": row["task_number"]})
            if (
                result["confirmed"] is not True
                or result["task_id"] != row["task_id"]
                or result["task_number"] != row["task_number"]
                or not result["arrival_time"]
            ):
                raise ValueError("WRITE_OUTCOME_UNKNOWN")
            completed.append(dict(result))
        observed = datetime.now(timezone.utc).isoformat()
        outcome = "WRITE_VERIFIED" if completed else "NOT_APPLIED"
        data = {
            "date_range": interval,
            "queried": len(rows),
            "candidates": len(candidates),
            "checked_in": len(completed),
            "results": completed,
            "message": "打卡完成" if completed else "过去3天无车辆到达任务，无需打卡",
            "evidence": {"service": SERVICE, "operation": "run", "outcome": outcome, "observed_at": observed},
        }
        return {
            "status": "SUCCESS",
            "data": data,
            "warnings": [],
            "error": None,
            "meta": {
                "source_system": "r7",
                "observed_at": observed,
                "record_count": len(completed),
                "pagination_complete": True,
                "evidence_refs": refs,
                "write_outcome": outcome,
                "postconditions": {"0": True},
                "postcondition_evidence": {
                    "0": {
                        "condition": "plugin_result_contract_valid",
                        "verified": True,
                        "observed_at": observed,
                        "evidence_ref": refs[-1],
                        "details": {"result_summary": data, "evidence_refs": refs},
                    }
                },
            },
        }
    except Exception as exc:
        code = str(exc)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code):
            code = "R7_RESULT_INVALID"
        return {
            "status": "FAILED",
            "data": {
                "date_range": interval,
                "checked_in": len(completed),
                "results": completed,
                "failed_task": current_task,
            },
            "warnings": [],
            "error": {
                "code": code,
                "message": ERROR_MESSAGES.get(code, "R7打卡未完成，失败原因：" + code) + " 本次不会自动重试。",
                "retryable": False,
            },
            "meta": {
                "source_system": "r7",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "record_count": len(completed),
                "pagination_complete": pagination_complete,
                "evidence_refs": refs,
                "write_outcome": "WRITE_OUTCOME_UNKNOWN" if attempted else "NOT_APPLIED",
            },
        }
