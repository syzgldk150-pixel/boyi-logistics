"""Tool executor with isolated subprocess execution and live output streaming."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from zoneinfo import ZoneInfo

from agent.execution_boundary import (
    EXECUTION_CAPABILITY_ENV,
    issue_execution_capability,
    revoke_execution_capability,
)
from shared.redaction import redact_sensitive, redact_text
from shared.scheduled_task_contracts import APPROVED_SCHEDULED_TASK_PROFILES

logger = logging.getLogger("tools")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
LOCK_FILE = os.path.join(PROJECT_ROOT, "logs", ".heavy_task.lock")
CANCEL_MESSAGE = "任务已取消"
HEAVY_LOCK_RETRY_SECONDS = 0.5
DEFAULT_HEAVY_QUEUE_TIMEOUT = 900.0
TRUSTED_SCHEDULER_CONTEXT_ENV = "AGENT_TRUSTED_SCHEDULER_CONTEXT"
_TRUSTED_SCHEDULER_CONTEXT_SCHEMA_VERSION = 1
_TRUSTED_SCHEDULER_TOOL = "r7_arrival_checkin"
_R7_SCHEDULED_PROFILE = APPROVED_SCHEDULED_TASK_PROFILES["r7_arrival_checkin"]
_R7_SCHEDULED_TASK_IDS = _R7_SCHEDULED_PROFILE.approved_task_ids
_SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
SUBPROCESS_STRIPPED_MANAGEMENT_ENV = frozenset(
    {
        "AGENT_INTERNAL_API_TOKEN",
        "CONSOLE_AGENT_SIGNING_SECRET",
        "DOCFLOW_SESSION_SECRET",
        "DOCFLOW_AGENT_WEBHOOK_TOKEN",
        "AGENT_WEBHOOK_TOKEN",
        "FEISHU_EVENT_VERIFICATION_TOKEN",
        "FEISHU_VERIFICATION_TOKEN",
        "DOCFLOW_BASIC_AUTH_PASS",
        "DOCFLOW_BASIC_AUTH_USER",
        "DOCFLOW_ADMIN_PASSWORD",
        "DOCFLOW_ADMIN_USERNAME",
    }
)


def _redact_execution_capability(value: object, capability: str) -> str:
    """Redact the current bearer even when a tool prints it without a key."""

    text = redact_text(value)
    token = str(capability or "")
    return text.replace(token, "[REDACTED]") if token else text


def _redact_structured_execution_capability(value: object, capability: str) -> object:
    """Redact a parsed result without first corrupting its JSON representation."""

    if isinstance(value, Mapping):
        sanitized = {
            str(key): _redact_structured_execution_capability(item, capability)
            for key, item in value.items()
        }
        return redact_sensitive(sanitized)
    if isinstance(value, list):
        return [_redact_structured_execution_capability(item, capability) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_structured_execution_capability(item, capability) for item in value)
    if isinstance(value, str):
        token = str(capability or "")
        without_capability = value.replace(token, "[REDACTED]") if token else value
        return redact_text(without_capability)
    return redact_sensitive(value)


def build_trusted_scheduler_context(
    tool_name: str,
    execution_context: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Extract the private R7 scheduler side-channel from trusted command metadata.

    The returned value is deliberately separate from tool arguments.  Invalid,
    incomplete, manual, or non-R7 contexts receive no side-channel at all.
    """

    if tool_name != _TRUSTED_SCHEDULER_TOOL or not isinstance(execution_context, Mapping):
        return None
    actor = execution_context.get("actor")
    if not isinstance(actor, Mapping):
        return None
    task_id = execution_context.get("task_id")
    configuration_version = execution_context.get("configuration_version")
    scheduled_for = execution_context.get("scheduled_for")
    cron_expression = execution_context.get("cron_expression")
    roles = actor.get("roles")
    if (
        execution_context.get("source") != "scheduler"
        or actor.get("actor_type") != "scheduler"
        or type(task_id) is not str
        or task_id not in _R7_SCHEDULED_TASK_IDS
        or actor.get("actor_id") != task_id
        or not isinstance(roles, (list, tuple))
        or tuple(roles) != ("system",)
        or type(configuration_version) is not int
        or configuration_version <= 0
        or type(scheduled_for) is not str
        or type(cron_expression) is not str
    ):
        return None
    occurrence = _parse_aware_datetime(scheduled_for)
    if occurrence is None:
        return None
    local_occurrence = occurrence.astimezone(_SHANGHAI_TIMEZONE)
    if not _matches_r7_schedule_contract(task_id, cron_expression, local_occurrence):
        return None
    return {
        "schema_version": _TRUSTED_SCHEDULER_CONTEXT_SCHEMA_VERSION,
        "source": "scheduler",
        "actor_type": "scheduler",
        "actor_id": task_id,
        "task_id": task_id,
        "configuration_version": configuration_version,
        "scheduled_for": scheduled_for,
        "cron_expression": cron_expression,
    }


def trusted_scheduler_context() -> Mapping[str, object] | None:
    """Read a validated private scheduler context inside an R7 tool process."""

    raw = os.environ.get(TRUSTED_SCHEDULER_CONTEXT_ENV)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    normalized = _normalize_trusted_scheduler_payload(payload)
    return MappingProxyType(normalized) if normalized is not None else None


def _normalize_trusted_scheduler_payload(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    expected_keys = {
        "schema_version",
        "source",
        "actor_type",
        "actor_id",
        "task_id",
        "configuration_version",
        "scheduled_for",
        "cron_expression",
    }
    if set(value) != expected_keys:
        return None
    task_id = value.get("task_id")
    configuration_version = value.get("configuration_version")
    scheduled_for = value.get("scheduled_for")
    cron_expression = value.get("cron_expression")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != _TRUSTED_SCHEDULER_CONTEXT_SCHEMA_VERSION
        or value.get("source") != "scheduler"
        or value.get("actor_type") != "scheduler"
        or type(task_id) is not str
        or task_id not in _R7_SCHEDULED_TASK_IDS
        or value.get("actor_id") != task_id
        or type(configuration_version) is not int
        or configuration_version <= 0
        or type(scheduled_for) is not str
        or type(cron_expression) is not str
    ):
        return None
    occurrence = _parse_aware_datetime(scheduled_for)
    if occurrence is None:
        return None
    local_occurrence = occurrence.astimezone(_SHANGHAI_TIMEZONE)
    if not _matches_r7_schedule_contract(task_id, cron_expression, local_occurrence):
        return None
    return {key: value[key] for key in sorted(expected_keys)}


def _parse_aware_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _matches_r7_schedule_contract(
    task_id: str,
    cron_expression: str,
    local_occurrence: datetime,
) -> bool:
    if _R7_SCHEDULED_PROFILE.cron_expression is not None:
        if cron_expression != _R7_SCHEDULED_PROFILE.cron_expression:
            return False
    parts = cron_expression.split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        return False
    minute_text, hour_text = parts[:2]
    if not minute_text.isdigit() or not hour_text.isdigit():
        return False
    minute = int(minute_text)
    hour = int(hour_text)
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return False
    return (
        task_id.rsplit("_", 1)[-1] == f"{hour:02d}{minute:02d}"
        and local_occurrence.hour == hour
        and local_occurrence.minute == minute
        and local_occurrence.second == 0
        and local_occurrence.microsecond == 0
    )


def build_tool_subprocess_environment(
    execution_capability: str,
    *,
    trusted_context: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Preserve business runtime settings but remove service-management secrets."""

    environment = dict(os.environ)
    for name in SUBPROCESS_STRIPPED_MANAGEMENT_ENV:
        environment.pop(name, None)
    environment.pop(EXECUTION_CAPABILITY_ENV, None)
    # Never inherit a same-named value from the service manager or an operator
    # shell.  Only the validated per-invocation value below may cross the
    # parent -> tool process boundary.
    environment.pop(TRUSTED_SCHEDULER_CONTEXT_ENV, None)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (WORKSPACE_ROOT, environment.get("PYTHONPATH", "")))
    )
    # Some isolated legacy helpers still call python-dotenv at import time.
    # The parent service has already loaded the approved runtime environment;
    # prevent a tool subprocess from re-reading the project .env and restoring
    # management credentials that were deliberately removed above.
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    environment[EXECUTION_CAPABILITY_ENV] = str(execution_capability)
    normalized_context = _normalize_trusted_scheduler_payload(trusted_context)
    if normalized_context is not None:
        environment[TRUSTED_SCHEDULER_CONTEXT_ENV] = json.dumps(
            normalized_context,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    return environment


def _resolve_python() -> str:
    candidates = [
        os.path.join(PROJECT_ROOT, ".venv", "bin", "python"),
        os.path.join(PROJECT_ROOT, ".venv-linux", "bin", "python"),
        sys.executable,
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return "python3"


class ToolExecutor:
    def __init__(self):
        self._heavy_lock_fd = None
        self._heavy_queue_lock = asyncio.Lock()
        self._queued_tools: set[str] = set()
        self._last_run: dict | None = None
        self._running_outputs: dict[str, dict] = {}

    def get_running_output(self, tool_name: str, offset: int = 0, started_at: str = "") -> dict:
        """Return live output for a running tool."""
        entry = self._running_outputs.get(tool_name)
        if not entry:
            return {"lines": [], "running": False, "offset": 0, "total": 0, "cancel_requested": False}
        entry_started_at = str(entry.get("started_at") or "")
        if started_at and entry_started_at and entry_started_at < started_at:
            return {"lines": [], "running": False, "offset": 0, "total": 0, "cancel_requested": False}
        lines = entry["lines"]
        return {
            "lines": lines[offset:],
            "running": bool(entry.get("running")),
            "offset": offset,
            "total": len(lines),
            "started_at": entry_started_at,
            "cancel_requested": bool(entry.get("cancel_requested")),
        }

    def is_tool_running(self, tool_name: str) -> bool:
        entry = self._running_outputs.get(tool_name)
        return bool(entry and entry.get("running"))

    def running_tool_info(self, tool_name: str) -> dict:
        entry = self._running_outputs.get(tool_name)
        if not entry or not entry.get("running"):
            return {"running": False, "started_at": "", "cancel_requested": False}
        return {
            "running": True,
            "started_at": str(entry.get("started_at") or ""),
            "cancel_requested": bool(entry.get("cancel_requested")),
        }

    def running_tools(self) -> list[str]:
        return sorted(
            name
            for name, entry in self._running_outputs.items()
            if entry and entry.get("running")
        )

    async def cancel_tool(self, tool_name: str, started_at: str = "") -> dict:
        entry = self._running_outputs.get(tool_name)
        if not entry or not entry.get("running"):
            return {"ok": False, "message": "当前没有运行中的任务。", "code": "NOT_RUNNING"}

        entry_started_at = str(entry.get("started_at") or "")
        if started_at and entry_started_at and entry_started_at != started_at:
            return {"ok": False, "message": "当前运行实例已变化，请刷新后重试。", "code": "RUN_MISMATCH"}

        if entry.get("cancel_requested"):
            return {
                "ok": True,
                "message": "已发送取消请求，正在停止脚本。",
                "started_at": entry_started_at,
                "already_requested": True,
            }

        proc = entry.get("proc")
        if proc is None or proc.returncode is not None:
            entry["running"] = False
            return {"ok": False, "message": "任务已结束，无需取消。", "code": "NOT_RUNNING"}

        entry["cancel_requested"] = True
        entry["lines"].append("[control] 已请求取消执行，正在停止子进程…")
        await self._terminate_process(proc, force=False)
        asyncio.create_task(self._ensure_process_stopped(proc))
        return {"ok": True, "message": "已发送取消请求，正在停止脚本。", "started_at": entry_started_at}

    def heavy_lock_held(self) -> bool:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
            finally:
                os.close(fd)
        except OSError:
            return False

    async def _terminate_process(self, proc: asyncio.subprocess.Process, *, force: bool) -> None:
        if proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except ProcessLookupError:
            return

    async def _ensure_process_stopped(self, proc: asyncio.subprocess.Process, *, graceful_timeout: float = 3.0) -> None:
        if proc.returncode is not None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=graceful_timeout)
            return
        except asyncio.TimeoutError:
            pass
        await self._terminate_process(proc, force=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("tool pid=%s did not exit after forced kill", getattr(proc, "pid", "?"))

    def _cancelled_result(self, *, name: str, entry: dict, duration: float) -> dict:
        """Return the process-layer cancellation contract for either exit code."""

        entry["running"] = False
        entry["proc"] = None
        entry["lines"].append("[control] 任务已取消。")
        logger.info("tool=%s | cancelled=true | duration=%ss", name, duration)
        self._last_run = {
            "tool": name,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "success": False,
            "duration_s": duration,
            # Keep the historical spelling for compatibility while exposing
            # the canonical spelling to new orchestration adapters.
            "canceled": True,
            "cancelled": True,
        }
        return {
            "success": False,
            "canceled": True,
            "cancelled": True,
            "error": CANCEL_MESSAGE,
            "error_code": "CANCELLED",
            "retryable": False,
            "duration_s": duration,
        }

    async def execute(
        self,
        tool_config: dict,
        params: dict,
        *,
        trusted_scheduler_context: Mapping[str, object] | None = None,
    ) -> dict:
        name = tool_config["name"]
        heavy = tool_config.get("heavy", False)
        existing = self._running_outputs.get(name)
        if existing and existing.get("running"):
            return {"success": False, "error": "脚本正在执行中，请先等待完成或取消当前任务。", "error_code": "TOOL_ALREADY_RUNNING"}
        if heavy and name in self._queued_tools:
            return {"success": False, "error": "脚本正在排队或执行中，请先等待完成。", "error_code": "TOOL_ALREADY_RUNNING"}
        if heavy:
            self._queued_tools.add(name)
            try:
                async with self._heavy_queue_lock:
                    return await self._execute_now(
                        tool_config,
                        params,
                        trusted_scheduler_context=trusted_scheduler_context,
                    )
            finally:
                self._queued_tools.discard(name)
        return await self._execute_now(
            tool_config,
            params,
            trusted_scheduler_context=trusted_scheduler_context,
        )

    async def _acquire_heavy_lock(self, *, queue_timeout: float) -> int:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_fd
            except BlockingIOError:
                elapsed = time.monotonic() - start
                if queue_timeout > 0 and elapsed >= queue_timeout:
                    os.close(lock_fd)
                    raise TimeoutError("重任务排队等待超时，请稍后再试")
                sleep_for = HEAVY_LOCK_RETRY_SECONDS
                if queue_timeout > 0:
                    sleep_for = min(sleep_for, max(queue_timeout - elapsed, 0.05))
                await asyncio.sleep(sleep_for)
            except Exception:
                os.close(lock_fd)
                raise

    async def _execute_now(
        self,
        tool_config: dict,
        params: dict,
        *,
        trusted_scheduler_context: Mapping[str, object] | None = None,
    ) -> dict:
        """Execute a tool script via subprocess."""
        name = tool_config["name"]
        executor = os.path.join(PROJECT_ROOT, tool_config["executor"])
        timeout = tool_config.get("timeout", 300)
        heavy = tool_config.get("heavy", False)
        queue_timeout = float(tool_config.get("queue_timeout", DEFAULT_HEAVY_QUEUE_TIMEOUT))

        if not os.path.exists(executor):
            return {"success": False, "error": f"执行脚本不存在: {executor}"}

        existing = self._running_outputs.get(name)
        if existing and existing.get("running"):
            return {"success": False, "error": "脚本正在执行中，请先等待完成或取消当前任务。", "error_code": "TOOL_ALREADY_RUNNING"}

        start = time.time()
        safe_params = redact_sensitive(params)
        logger.info(
            "tool=%s | params=%s | heavy=%s",
            name,
            json.dumps(safe_params, ensure_ascii=False)[:500],
            heavy,
        )

        lock_fd = None
        execution_capability = ""
        try:
            if heavy:
                lock_fd = await self._acquire_heavy_lock(queue_timeout=queue_timeout)

            input_json = json.dumps(params, ensure_ascii=False)
            python_executable = _resolve_python()
            started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._running_outputs[name] = {
                "lines": [],
                "running": True,
                "started": time.time(),
                "started_at": started_at,
                "cancel_requested": False,
                "proc": None,
            }
            entry = self._running_outputs[name]
            buf = entry["lines"]
            execution_capability = issue_execution_capability(
                name,
                ttl_seconds=float(timeout) + 60.0,
            )

            proc = await asyncio.create_subprocess_exec(
                python_executable,
                executor,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=PROJECT_ROOT,
                env=build_tool_subprocess_environment(
                    execution_capability,
                    trusted_context=(
                        trusted_scheduler_context
                        if name == _TRUSTED_SCHEDULER_TOOL
                        else None
                    ),
                ),
                start_new_session=True,
            )
            entry["proc"] = proc

            proc.stdin.write(input_json.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []

            def append_output_line(text: str, *, is_stderr: bool) -> None:
                text = _redact_execution_capability(text, execution_capability)
                if is_stderr:
                    if text.startswith("[progress] "):
                        buf.append(text[len("[progress] "):])
                    else:
                        buf.append("[stderr] " + text)
                    return
                buf.append(text)

            async def read_stream(reader, chunk_store: list[bytes], *, is_stderr: bool) -> None:
                pending = ""
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    chunk_store.append(chunk)
                    pending += chunk.decode("utf-8", errors="replace")
                    while True:
                        newline_index = pending.find("\n")
                        if newline_index == -1:
                            if len(pending) > 65536:
                                append_output_line(pending.rstrip("\r"), is_stderr=is_stderr)
                                pending = ""
                            break
                        line = pending[:newline_index].rstrip("\r")
                        pending = pending[newline_index + 1:]
                        append_output_line(line, is_stderr=is_stderr)
                if pending:
                    append_output_line(pending.rstrip("\r"), is_stderr=is_stderr)

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(proc.stdout, stdout_chunks, is_stderr=False),
                        read_stream(proc.stderr, stderr_chunks, is_stderr=True),
                        proc.wait(),
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                await self._terminate_process(proc, force=False)
                await self._ensure_process_stopped(proc)
                entry["running"] = False
                entry["proc"] = None
                duration = round(time.time() - start, 2)
                if entry.get("cancel_requested"):
                    return self._cancelled_result(
                        name=name,
                        entry=entry,
                        duration=duration,
                    )
                logger.error("tool=%s | error=timeout(%ds) | duration=%ss", name, timeout, duration)
                return {"success": False, "error": f"工具执行超时（{timeout}秒）"}

            entry["running"] = False
            entry["proc"] = None
            duration = round(time.time() - start, 2)

            stdout = b"".join(stdout_chunks)
            stderr = b"".join(stderr_chunks)
            exit_code = proc.returncode
            cancel_requested = bool(entry.get("cancel_requested"))

            # A SIGTERM handler may flush a final result and exit zero.  Once
            # cancellation was accepted, that process exit must never be
            # reclassified as business success (or a generic tool failure).
            if cancel_requested:
                return self._cancelled_result(
                    name=name,
                    entry=entry,
                    duration=duration,
                )

            if exit_code != 0:
                err_msg = _redact_execution_capability(
                    stderr.decode("utf-8", errors="replace").strip(),
                    execution_capability,
                )[-500:]
                logger.error("tool=%s | error=exit_code_%d | stderr=%s | duration=%ss", name, exit_code, err_msg, duration)
                if exit_code == 137:
                    return {"success": False, "error": "工具被 OOM Kill，内存不足"}
                return {"success": False, "error": f"工具执行失败(exit {exit_code}): {err_msg}"}

            raw_output = stdout.decode("utf-8", errors="replace").strip()
            try:
                parsed_result = json.loads(raw_output)
            except json.JSONDecodeError:
                result = {
                    "output": _redact_execution_capability(raw_output, execution_capability)
                }
            else:
                result = _redact_structured_execution_capability(
                    parsed_result,
                    execution_capability,
                )

            result_reports_failure = isinstance(result, dict) and (
                str(result.get("status") or "").upper() not in {"", "SUCCESS"}
                or
                result.get("success") is False
                or result.get("ok") is False
                or bool(result.get("error"))
            )
            if result_reports_failure:
                structured_error = result.get("error")
                error_value = (
                    (
                        structured_error.get("message") or structured_error.get("code")
                        if isinstance(structured_error, dict)
                        else structured_error
                    )
                    or result.get("message")
                    or result.get("error_code")
                    or "工具返回失败状态。"
                )
                safe_error_value = redact_sensitive(error_value)
                if isinstance(safe_error_value, (dict, list)):
                    safe_error = json.dumps(safe_error_value, ensure_ascii=False)
                else:
                    safe_error = redact_text(safe_error_value)
                safe_result = redact_sensitive(result)
                error_log_limit = 500 if name == "sync_finance_bills" else 300
                logger.error(
                    "tool=%s | success=false | error=%s | duration=%ss",
                    name,
                    safe_error[:error_log_limit],
                    duration,
                )
                self._last_run = {
                    "tool": name,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "success": False,
                    "duration_s": duration,
                }
                failure = {
                    "success": False,
                    "error": safe_error,
                    "data": safe_result,
                    "duration_s": duration,
                }
                error_code = (
                    structured_error.get("code")
                    if isinstance(structured_error, dict)
                    else result.get("error_code")
                )
                failure["error_code"] = error_code or "TOOL_REPORTED_FAILURE"
                nested_retryable = (
                    structured_error.get("retryable")
                    if isinstance(structured_error, dict)
                    else None
                )
                failure["retryable"] = bool(
                    result.get("retryable") or nested_retryable
                )
                return failure

            logger.info("tool=%s | success=true | duration=%ss", name, duration)
            self._last_run = {
                "tool": name,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "success": True,
                "duration_s": duration,
            }
            return {"success": True, "data": result, "duration_s": duration}

        except Exception as exc:
            duration = round(time.time() - start, 2)
            safe_error = _redact_execution_capability(exc, execution_capability)
            logger.error("tool=%s | error=%s | duration=%ss", name, safe_error[:200], duration)
            return {"success": False, "error": safe_error}

        finally:
            if execution_capability:
                revoke_execution_capability(execution_capability)
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def last_tool_info(self) -> dict | None:
        return self._last_run
