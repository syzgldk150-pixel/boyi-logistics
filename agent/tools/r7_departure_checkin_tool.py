"""R7 departure check-in tool wrapper."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import date
from typing import Any

import pymysql

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "agent", "tms_runtime", "scripts")
for path in (PROJECT_ROOT, SCRIPTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import auto_departure_r7  # noqa: E402
from agent.tms_runtime.account_manager import resolve_account_params  # noqa: E402


DEFAULT_SCRIPT_PARAMS: dict[str, Any] = {
    "headless": True,
    "slow_mo_ms": 0,
    "max_login_attempts": 6,
    "status_text": "已调度",
    "verify_status_text": "装车待发",
    "class_name": "邵阳操作场-长沙",
    "departure_time_fixed": "21:30:00",
    "plate_numbers": ["湘AK6980"],
    "do_departure_checkin": True,
    "after_action_delay_ms": 1500,
}

SECRET_PARAM_KEYS = {
    "account",
    "aurora-token",
    "code",
    "mobile",
    "pass",
    "passwd",
    "password",
    "phone",
    "token",
    "user",
    "username",
    "validatecode",
}

CONTROL_PARAM_KEYS = {
    "client_timeout_sec",
    "daily_checkin_count",
    "daily_checkin_limit",
    "daily_success_limit",
    "timeout",
    "timeout_sec",
}

LOG_TABLE_NAME = "r7_departure_checkin_log"


class _ProgressWriter:
    def __init__(self) -> None:
        self._buffer = ""

    def write(self, text: str) -> int:
        raw = str(text or "")
        self._buffer += raw
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line.rstrip("\r"))
        return len(raw)

    def flush(self) -> None:
        if self._buffer:
            self._emit(self._buffer.rstrip("\r"))
            self._buffer = ""

    @staticmethod
    def _emit(line: str) -> None:
        if line.strip():
            print(f"[progress] {line}", file=sys.stderr, flush=True)


def _coerce_params(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _connect_db():
    return pymysql.connect(
        host=os.getenv("AGENT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_DB_PORT", "3306")),
        user=os.getenv("AGENT_DB_USER", "agent"),
        password=os.getenv("AGENT_DB_PASS", ""),
        database=os.getenv("AGENT_DB_NAME", "agent_db"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _prepare_log_storage() -> None:
    conn = _connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {LOG_TABLE_NAME} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    business_date DATE NOT NULL,
                    task_id VARCHAR(128),
                    trigger_mode VARCHAR(32),
                    status VARCHAR(32) NOT NULL,
                    ok BOOLEAN,
                    skipped BOOLEAN DEFAULT FALSE,
                    daily_success_limit INT,
                    success_count_before INT,
                    success_count_after INT,
                    target_plate_numbers JSON,
                    target_departure_time VARCHAR(32),
                    class_name VARCHAR(128),
                    stage VARCHAR(64),
                    message TEXT,
                    detail_json JSON,
                    params_json JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_r7_departure_date_status (business_date, status),
                    INDEX idx_r7_departure_task_date (task_id, business_date),
                    INDEX idx_r7_departure_created_at (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
    finally:
        conn.close()


def _today() -> str:
    return date.today().isoformat()


def _count_successes_today(*, business_date: str) -> int:
    conn = _connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS count_value
                FROM {LOG_TABLE_NAME}
                WHERE business_date=%s AND status='success' AND ok=TRUE
                """,
                (business_date,),
            )
            row = cur.fetchone() or {}
        return int(row.get("count_value") or 0)
    finally:
        conn.close()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _sanitize_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            if key.lower() in SECRET_PARAM_KEYS:
                sanitized[key] = "***"
            else:
                sanitized[key] = _sanitize_for_log(raw_item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_log(item) for item in value]
    return value


def _scheduled_task_id(params: dict[str, Any]) -> str:
    scheduled = params.get("_scheduled_task")
    if isinstance(scheduled, dict):
        return str(scheduled.get("id") or "").strip()
    return ""


def _trigger_mode(params: dict[str, Any]) -> str:
    if params.get("_feishu"):
        return "feishu"
    return "scheduled" if _scheduled_task_id(params) else "manual"


def _daily_success_limit(params: dict[str, Any]) -> int:
    raw = (
        params.get("daily_success_limit")
        if params.get("daily_success_limit") not in (None, "")
        else params.get("daily_checkin_count")
        if params.get("daily_checkin_count") not in (None, "")
        else params.get("daily_checkin_limit")
    )
    if raw in (None, ""):
        raw = 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(value, 20))


def _target_plate_numbers(params: dict[str, Any], result: dict[str, Any] | None = None) -> list[str]:
    detail = result.get("detail") if isinstance(result, dict) else {}
    if isinstance(detail, dict) and detail.get("plate_numbers") not in (None, ""):
        return auto_departure_r7.normalize_plate_numbers(detail.get("plate_numbers"))
    raw = params.get("plate_numbers") if params.get("plate_numbers") not in (None, "") else params.get("plate_number")
    return auto_departure_r7.normalize_plate_numbers(raw)


def _target_departure_time(params: dict[str, Any], result: dict[str, Any] | None = None) -> str:
    detail = result.get("detail") if isinstance(result, dict) else {}
    if isinstance(detail, dict) and detail.get("departure_time"):
        return str(detail.get("departure_time") or "").strip()
    return auto_departure_r7.expected_departure_time(
        params.get("plan_departure_time"),
        fixed_time=str(params.get("departure_time_fixed") or "21:30:00"),
    )


def _target_class_name(params: dict[str, Any], result: dict[str, Any] | None = None) -> str:
    detail = result.get("detail") if isinstance(result, dict) else {}
    if isinstance(detail, dict) and detail.get("class_name"):
        return str(detail.get("class_name") or "").strip()
    return str(params.get("class_name") or DEFAULT_SCRIPT_PARAMS["class_name"]).strip()


def _insert_log(
    *,
    business_date: str,
    params: dict[str, Any],
    status: str,
    ok: bool,
    skipped: bool,
    daily_success_limit: int,
    success_count_before: int,
    success_count_after: int,
    result: dict[str, Any],
) -> None:
    detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
    conn = _connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {LOG_TABLE_NAME}
                    (
                        business_date, task_id, trigger_mode, status, ok, skipped,
                        daily_success_limit, success_count_before, success_count_after,
                        target_plate_numbers, target_departure_time, class_name,
                        stage, message, detail_json, params_json
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    business_date,
                    _scheduled_task_id(params),
                    _trigger_mode(params),
                    status,
                    bool(ok),
                    bool(skipped),
                    int(daily_success_limit),
                    int(success_count_before),
                    int(success_count_after),
                    _json_dumps(_target_plate_numbers(params, result)),
                    _target_departure_time(params, result),
                    _target_class_name(params, result),
                    str(result.get("stage") or "")[:64],
                    str(result.get("message") or result.get("error") or "")[:4000],
                    _json_dumps(detail),
                    _json_dumps(_sanitize_for_log(params)),
                ),
            )
    finally:
        conn.close()


def _build_script_params(params: dict[str, Any]) -> dict[str, Any]:
    script_params = dict(DEFAULT_SCRIPT_PARAMS)
    for raw_key, raw_value in params.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in SECRET_PARAM_KEYS:
            continue
        if lowered in CONTROL_PARAM_KEYS or lowered.startswith("_"):
            continue
        script_params[key] = raw_value
    return script_params


def run_r7_departure_checkin(params: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_params = resolve_account_params(_coerce_params(params))
    script_params = _build_script_params(raw_params)
    daily_success_limit = _daily_success_limit(raw_params)
    business_date = _today()
    try:
        _prepare_log_storage()
        success_count_before = _count_successes_today(business_date=business_date)
    except BaseException as exc:
        return {
            "ok": False,
            "stage": "log_unavailable",
            "message": f"{type(exc).__name__}: {exc}",
            "detail": {
                "business_date": business_date,
                "daily_success_limit": daily_success_limit,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            "error": f"R7 发车打卡日志数据库不可用，已停止执行以避免重复打卡：{type(exc).__name__}: {exc}",
        }

    if success_count_before >= daily_success_limit:
        skipped_result = {
            "ok": True,
            "skipped": True,
            "stage": "daily_limit_reached",
            "message": (
                f"当天已成功发车打卡 {success_count_before} 次，"
                f"达到设置次数 {daily_success_limit}，本次跳过"
            ),
            "detail": {
                "business_date": business_date,
                "daily_success_limit": daily_success_limit,
                "success_count_today": success_count_before,
                "plate_numbers": _target_plate_numbers(script_params),
                "departure_time": _target_departure_time(script_params),
                "class_name": _target_class_name(script_params),
            },
        }
        _insert_log(
            business_date=business_date,
            params=raw_params,
            status="skipped",
            ok=True,
            skipped=True,
            daily_success_limit=daily_success_limit,
            success_count_before=success_count_before,
            success_count_after=success_count_before,
            result=skipped_result,
        )
        return skipped_result

    writer = _ProgressWriter()
    try:
        with contextlib.redirect_stdout(writer):
            result = auto_departure_r7.run_once(script_params)
        writer.flush()
    except BaseException as exc:
        writer.flush()
        error_result = {
            "ok": False,
            "stage": "tool_wrapper",
            "message": f"{type(exc).__name__}: {exc}",
            "detail": {
                "business_date": business_date,
                "daily_success_limit": daily_success_limit,
                "success_count_today": success_count_before,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            "error": f"R7 发车打卡执行失败：{type(exc).__name__}: {exc}",
        }
        _insert_log(
            business_date=business_date,
            params=raw_params,
            status="failure",
            ok=False,
            skipped=False,
            daily_success_limit=daily_success_limit,
            success_count_before=success_count_before,
            success_count_after=success_count_before,
            result=error_result,
        )
        return error_result

    if not isinstance(result, dict):
        error_result = {
            "ok": False,
            "stage": "tool_wrapper",
            "message": "R7 发车脚本返回了非 JSON 对象",
            "detail": {
                "business_date": business_date,
                "daily_success_limit": daily_success_limit,
                "success_count_today": success_count_before,
                "result": str(result)[:500],
            },
            "error": "R7 发车脚本返回了非 JSON 对象",
        }
        _insert_log(
            business_date=business_date,
            params=raw_params,
            status="failure",
            ok=False,
            skipped=False,
            daily_success_limit=daily_success_limit,
            success_count_before=success_count_before,
            success_count_after=success_count_before,
            result=error_result,
        )
        return error_result

    public_result = dict(result)
    public_result.setdefault("tool", "r7_departure_checkin")
    ok = bool(public_result.get("ok"))
    skipped = bool(public_result.get("skipped"))
    status = "success" if ok and not skipped else "skipped" if ok and skipped else "failure"
    success_count_after = success_count_before + 1 if status == "success" else success_count_before
    detail = public_result.get("detail")
    if not isinstance(detail, dict):
        detail = {}
    detail.update(
        {
            "business_date": business_date,
            "daily_success_limit": daily_success_limit,
            "success_count_before": success_count_before,
            "success_count_today": success_count_after,
        }
    )
    public_result["detail"] = detail
    if public_result.get("ok") is False and not public_result.get("error"):
        public_result["error"] = str(public_result.get("message") or "R7 发车打卡失败").strip()
    _insert_log(
        business_date=business_date,
        params=raw_params,
        status=status,
        ok=ok,
        skipped=skipped,
        daily_success_limit=daily_success_limit,
        success_count_before=success_count_before,
        success_count_after=success_count_after,
        result=public_result,
    )
    return public_result


def main() -> int:
    try:
        params = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"参数 JSON 解析失败：{exc.msg}"}, ensure_ascii=False))
        return 1

    result = run_r7_departure_checkin(_coerce_params(params))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
