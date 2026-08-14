"""R7 arrival check-in tool wrapper.

This tool runs the existing R7 browser automation directly. It intentionally
does not call or validate the TMS SMS session broker.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import date, datetime
from typing import Any

import pymysql

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from agent.tms_runtime.scripts import auto_checkin_r7
from agent.tms_runtime.account_manager import resolve_account_params
from agent.tool_executor import trusted_scheduler_context
from shared.redaction import redact_sensitive, redact_text


DEFAULT_SCRIPT_PARAMS: dict[str, Any] = {
    "headless": True,
    "slow_mo_ms": 0,
    "max_login_attempts": 3,
    "status_text": "车辆到达",
    "verify_status_text": "已到达",
    "flow_mode": 1,
    "do_arrive_wait_unload": True,
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

LOG_TABLE_NAME = "r7_arrival_checkin_log"


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
            print(f"[progress] {redact_text(line)}", file=sys.stderr, flush=True)


def _coerce_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


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
                """
                SELECT 1
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (LOG_TABLE_NAME,),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    "Missing r7_arrival_checkin_log; run deployment migrations first"
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


def _latest_success_observation(*, business_date: str) -> dict[str, Any] | None:
    """Read the latest same-day result that already carried exact R7 proof."""

    conn = _connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, detail_json, created_at
                FROM {LOG_TABLE_NAME}
                WHERE business_date=%s AND status='success' AND ok=TRUE
                ORDER BY id DESC
                LIMIT 1
                """,
                (business_date,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not isinstance(row, dict):
        return None
    detail = row.get("detail_json")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return None
    if not isinstance(detail, dict):
        return None
    task_number = str(detail.get("task_number") or "").strip()
    observed_status = str(detail.get("observed_status") or "").strip()
    expected_status = str(detail.get("verify_status_text") or "").strip()
    if not task_number or not observed_status or observed_status != expected_status:
        return None
    return {
        "log_id": int(row.get("id") or 0),
        "task_number": task_number,
        "observed_status": observed_status,
        "verified_at": str(row.get("created_at") or ""),
    }


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _public_safe(value: Any) -> Any:
    """Redact text and remove browser URLs before returning or persisting."""

    if isinstance(value, dict):
        cleaned = {
            str(key): _public_safe(item)
            for key, item in value.items()
            if str(key).strip().lower() != "url"
        }
        return redact_sensitive(cleaned)
    if isinstance(value, (list, tuple)):
        return [_public_safe(item) for item in value]
    return redact_sensitive(value)


def _postcondition_proof(result: dict[str, Any]) -> dict[str, Any] | None:
    """Build proof only from the script's refreshed exact-task observation."""

    if result.get("ok") is not True or result.get("skipped") is True:
        return None
    if str(result.get("stage") or "") != "done":
        return None
    detail = result.get("detail")
    if not isinstance(detail, dict):
        return None
    task_number = str(detail.get("task_number") or "").strip()
    verified_status = str(detail.get("observed_status") or "").strip()
    expected_status = str(detail.get("verify_status_text") or "").strip()
    observed_at = str(result.get("ts") or "").strip()
    if not task_number or not verified_status or verified_status != expected_status:
        return None
    try:
        if datetime.fromisoformat(observed_at.replace("Z", "+00:00")).tzinfo is None:
            return None
    except ValueError:
        return None
    evidence_ref = f"r7-arrival:{task_number}:{observed_at}"
    result["meta"] = {"observed_at": observed_at}
    result["evidence_refs"] = [evidence_ref]
    result["postconditions"] = {"0": True}
    result["postcondition_evidence"] = {
        "0": {
            "condition": "third_party_r7_arrival_state_confirmed",
            "verified": True,
            "observed_at": observed_at,
            "evidence_ref": evidence_ref,
            "details": {
                "external_task_id": task_number,
                "observed_status": verified_status,
            },
        }
    }
    return result


def _already_satisfied_proof(
    result: dict[str, Any],
    *,
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    """Prove the daily-limit no-op from the prior verified same-day row."""

    log_id = int(observation.get("log_id") or 0)
    task_number = str(observation.get("task_number") or "").strip()
    observed_status = str(observation.get("observed_status") or "").strip()
    if log_id < 1 or not task_number or not observed_status:
        return None
    observed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    evidence_ref = f"r7-arrival-log:{log_id}"
    result["ts"] = observed_at
    result["meta"] = {"observed_at": observed_at}
    result["evidence_refs"] = [evidence_ref]
    result["postconditions"] = {"0": True}
    result["postcondition_evidence"] = {
        "0": {
            "condition": "third_party_r7_arrival_state_confirmed",
            "verified": True,
            "observed_at": observed_at,
            "evidence_ref": evidence_ref,
            "details": {
                "external_task_id": task_number,
                "observed_status": observed_status,
                "source_verified_at": str(observation.get("verified_at") or ""),
            },
        }
    }
    return result


def _sanitize_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            if key == "_scheduled_task":
                continue
            if key.lower() in SECRET_PARAM_KEYS:
                sanitized[key] = "***"
            else:
                sanitized[key] = _sanitize_for_log(raw_item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_log(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return redact_sensitive(value)


def _scheduled_task_id() -> str:
    context = trusted_scheduler_context()
    return str(context.get("task_id") or "").strip() if context is not None else ""


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
    detail = _public_safe(detail)
    scheduled_task_id = _scheduled_task_id()
    conn = _connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {LOG_TABLE_NAME}
                    (
                        business_date, task_id, trigger_mode, status, ok, skipped,
                        daily_success_limit, success_count_before, success_count_after,
                        stage, message, detail_json, params_json
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    business_date,
                    scheduled_task_id,
                    "scheduled" if scheduled_task_id else "manual",
                    status,
                    bool(ok),
                    bool(skipped),
                    int(daily_success_limit),
                    int(success_count_before),
                    int(success_count_after),
                    str(result.get("stage") or "")[:64],
                    redact_text(result.get("message") or result.get("error") or "")[:4000],
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
            if lowered in {"username", "password"}:
                script_params[key] = raw_value
            continue
        if lowered in CONTROL_PARAM_KEYS or lowered.startswith("_"):
            continue
        script_params[key] = raw_value
    script_params["status_text"] = auto_checkin_r7.normalize_arrival_status_text(
        str(script_params.get("status_text") or ""),
        do_arrive_wait_unload=_as_bool(script_params.get("do_arrive_wait_unload"), default=True),
        log_change=False,
    )
    return script_params


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否", "不"}


def run_r7_arrival_checkin(params: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_params = resolve_account_params(_coerce_params(params), default_system="r7")
    script_params = _build_script_params(raw_params)
    daily_success_limit = _daily_success_limit(raw_params)
    business_date = _today()
    try:
        _prepare_log_storage()
        success_count_before = _count_successes_today(business_date=business_date)
    except BaseException as exc:
        safe_error = redact_text(f"{type(exc).__name__}: {exc}")
        return {
            "ok": False,
            "stage": "log_unavailable",
            "message": safe_error,
            "detail": {
                "business_date": business_date,
                "daily_success_limit": daily_success_limit,
                "error_type": type(exc).__name__,
                "error": safe_error,
            },
            "error": redact_text(f"R7 到达打卡日志数据库不可用，已停止执行以避免重复打卡：{safe_error}"),
        }

    if success_count_before >= daily_success_limit:
        prior_observation = _latest_success_observation(business_date=business_date)
        skipped_result = {
            "ok": True,
            "skipped": True,
            "stage": "daily_limit_reached",
            "message": (
                f"当天已成功打卡 {success_count_before} 次，"
                f"达到设置次数 {daily_success_limit}，本次跳过"
            ),
            "detail": {
                "business_date": business_date,
                "daily_success_limit": daily_success_limit,
                "success_count_today": success_count_before,
            },
        }
        if prior_observation is None or _already_satisfied_proof(
            skipped_result,
            observation=prior_observation,
        ) is None:
            skipped_result["ok"] = False
            skipped_result["error"] = (
                "R7 daily limit was reached but the prior exact-task proof is unavailable"
            )
        _insert_log(
            business_date=business_date,
            params=raw_params,
            status="skipped",
            ok=bool(skipped_result["ok"]),
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
            result = auto_checkin_r7.run_once(script_params)
        writer.flush()
    except BaseException as exc:
        writer.flush()
        safe_error = redact_text(f"{type(exc).__name__}: {exc}")
        error_result = {
            "ok": False,
            "stage": "tool_wrapper",
            "message": safe_error,
            "detail": {
                "business_date": business_date,
                "daily_success_limit": daily_success_limit,
                "success_count_today": success_count_before,
                "error_type": type(exc).__name__,
                "error": safe_error,
            },
            "error": redact_text(f"R7 到达打卡执行失败：{safe_error}"),
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
            "message": "R7 脚本返回了非 JSON 对象",
            "detail": {
                "business_date": business_date,
                "daily_success_limit": daily_success_limit,
                "success_count_today": success_count_before,
                "result": redact_text(result)[:500],
            },
            "error": "R7 脚本返回了非 JSON 对象",
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

    public_result = _public_safe(dict(result))
    public_result.setdefault("tool", "r7_arrival_checkin")
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
    if ok and bool(script_params["do_arrive_wait_unload"]) and _postcondition_proof(public_result) is None:
        public_result["ok"] = False
        public_result["error"] = (
            "R7 arrival result lacks a refreshed exact-task postcondition observation"
        )
        ok = False
        status = "failure"
        success_count_after = success_count_before
    if public_result.get("ok") is False and not public_result.get("error"):
        public_result["error"] = str(public_result.get("message") or "R7 到达打卡失败").strip()
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

    result = run_r7_arrival_checkin(_coerce_params(params))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
