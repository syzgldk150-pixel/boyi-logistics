"""Tool executor with isolated subprocess execution and live output streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
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
CANCEL_MESSAGE = "任务已取消"
TRUSTED_SCHEDULER_CONTEXT_ENV = "AGENT_TRUSTED_SCHEDULER_CONTEXT"
_TRUSTED_SCHEDULER_CONTEXT_SCHEMA_VERSION = 1
_TRUSTED_SCHEDULER_TOOL = "r7_arrival_checkin"
MAX_COMPLETED_EXECUTION_HISTORY = 64
_R7_SCHEDULED_PROFILE = APPROVED_SCHEDULED_TASK_PROFILES["r7_arrival_checkin"]
_R7_SCHEDULED_TASK_IDS = _R7_SCHEDULED_PROFILE.approved_task_ids
_SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
_EXPLICIT_FAILURE_RESULT_STATUSES = frozenset(
    {
        "AUTH_PENDING_CODE",
        "AUTH_REQUIRED",
        "BLOCKED",
        "CANCELED",
        "CANCELLED",
        "ERROR",
        "FAIL",
        "FAILED",
        "FAILURE",
        "LOGIN_REQUIRED",
        "NEEDS_CLARIFICATION",
        "PARTIAL",
        "PARTIAL_FAILED",
        "PARTIAL_FAILURE",
        "SESSION_EXPIRED",
    }
)
_EXPLICIT_FAILURE_RESULT_STATUS_PREFIXES = (
    "BLOCKED_",
    "ERROR_",
    "FAILED_",
    "FAILURE_",
)
_UNIFIED_RESULT_FIELDS = frozenset({"status", "data", "meta", "warnings", "error"})
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


def _result_reports_failure(result: object) -> bool:
    """Classify execution envelopes separately from tool business statuses.

    Unified results own the ``status`` field and only ``SUCCESS`` is accepted.
    Legacy/business payloads may use statuses such as ``no_data`` or ``signed``;
    those are accepted only when the tool also returns an explicit boolean
    success marker.  Explicit failure evidence always wins over contradictory
    success markers.
    """

    if not isinstance(result, Mapping):
        return False
    if result.get("success") is False or result.get("ok") is False or bool(result.get("error")):
        return True

    status = str(result.get("status") or "").strip().upper()
    if status in _EXPLICIT_FAILURE_RESULT_STATUSES or any(
        status.startswith(prefix)
        for prefix in _EXPLICIT_FAILURE_RESULT_STATUS_PREFIXES
    ):
        return True

    if _UNIFIED_RESULT_FIELDS.issubset(result):
        return status != "SUCCESS"

    explicit_success = result.get("success") is True or result.get("ok") is True
    return bool(status and status != "SUCCESS" and not explicit_success)


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
        self._last_run: dict | None = None
        self._running_outputs: dict[str, dict] = {}

    @staticmethod
    def _execution_key(execution_identity: Mapping[str, object] | None) -> str:
        if execution_identity is None:
            return f"legacy:{uuid.uuid4()}"
        run_id = str(execution_identity.get("run_id") or "").strip()
        step_id = str(execution_identity.get("step_id") or "").strip()
        if not run_id or not step_id:
            raise ValueError("execution_identity requires non-empty run_id and step_id")
        return f"run:{run_id}:step:{step_id}"

    def _matching_entries(
        self,
        tool_name: str,
        *,
        started_at: str = "",
        live_only: bool = False,
    ) -> list[dict]:
        matches = [
            entry
            for entry in self._running_outputs.values()
            if entry.get("tool_name") == tool_name
            and (not live_only or entry.get("running"))
            and (not started_at or entry.get("started_at") == started_at)
        ]
        return sorted(
            matches,
            key=lambda entry: float(entry.get("started") or 0.0),
            reverse=True,
        )

    def _prune_completed_entries(self) -> None:
        completed = sorted(
            (
                (execution_id, entry)
                for execution_id, entry in self._running_outputs.items()
                if not entry.get("running")
            ),
            key=lambda item: float(item[1].get("started") or 0.0),
            reverse=True,
        )
        for execution_id, _entry in completed[MAX_COMPLETED_EXECUTION_HISTORY:]:
            self._running_outputs.pop(execution_id, None)

    def _entry_for_status(self, tool_name: str, *, started_at: str = "") -> tuple[dict | None, int]:
        live = self._matching_entries(
            tool_name,
            started_at=started_at,
            live_only=True,
        )
        if live:
            return live[0], len(live)
        history = self._matching_entries(tool_name, started_at=started_at)
        return (history[0], len(history)) if history else (None, 0)

    def get_running_output(self, tool_name: str, offset: int = 0, started_at: str = "") -> dict:
        """Return live output for a running tool."""
        entry, matches = self._entry_for_status(tool_name, started_at=started_at)
        if not entry:
            return {"lines": [], "running": False, "offset": 0, "total": 0, "cancel_requested": False}
        if matches > 1 and entry.get("running"):
            return {
                "lines": [],
                "running": True,
                "offset": 0,
                "total": 0,
                "cancel_requested": False,
                "ambiguous": True,
                "code": "AMBIGUOUS_TOOL_EXECUTION",
                "instances": matches,
            }
        entry_started_at = str(entry.get("started_at") or "")
        lines = entry["lines"]
        return {
            "lines": lines[offset:],
            "running": bool(entry.get("running")),
            "offset": offset,
            "total": len(lines),
            "started_at": entry_started_at,
            "cancel_requested": bool(entry.get("cancel_requested")),
            "execution_id": str(entry.get("execution_id") or ""),
        }

    def is_tool_running(self, tool_name: str) -> bool:
        return bool(self._matching_entries(tool_name, live_only=True))

    def running_tool_info(self, tool_name: str) -> dict:
        matches = self._matching_entries(tool_name, live_only=True)
        if not matches:
            return {"running": False, "started_at": "", "cancel_requested": False}
        if len(matches) != 1:
            return {
                "running": True,
                "started_at": "",
                "cancel_requested": False,
                "ambiguous": True,
                "code": "AMBIGUOUS_TOOL_EXECUTION",
                "instances": len(matches),
            }
        entry = matches[0]
        return {
            "running": True,
            "started_at": str(entry.get("started_at") or ""),
            "cancel_requested": bool(entry.get("cancel_requested")),
            "execution_id": str(entry.get("execution_id") or ""),
        }

    def running_tools(self) -> list[str]:
        return sorted({
            str(entry.get("tool_name") or "")
            for entry in self._running_outputs.values()
            if entry and entry.get("running") and entry.get("tool_name")
        })

    async def cancel_tool(self, tool_name: str, started_at: str = "") -> dict:
        matches = self._matching_entries(
            tool_name,
            started_at=started_at,
            live_only=True,
        )
        if not matches:
            return {"ok": False, "message": "当前没有运行中的任务。", "code": "NOT_RUNNING"}
        if len(matches) != 1:
            return {
                "ok": False,
                "message": "存在多个同名运行实例，请按 Run 和 Step 精确取消。",
                "code": "AMBIGUOUS_TOOL_EXECUTION",
            }
        return await self._cancel_entry(matches[0])

    async def _cancel_entry(self, entry: dict) -> dict:
        """Cancel a previously resolved exact execution entry."""

        entry_started_at = str(entry.get("started_at") or "")

        if entry.get("cancel_requested"):
            return {
                "ok": True,
                "message": "已发送取消请求，正在停止脚本。",
                "started_at": entry_started_at,
                "already_requested": True,
            }

        proc = entry.get("proc")
        if proc is not None and proc.returncode is not None:
            entry["running"] = False
            return {"ok": False, "message": "任务已结束，无需取消。", "code": "NOT_RUNNING"}

        entry["cancel_requested"] = True
        entry["lines"].append("[control] 已请求取消执行，正在停止子进程…")
        if proc is None:
            return {
                "ok": True,
                "message": "已发送取消请求，正在停止脚本。",
                "started_at": entry_started_at,
            }
        await self._terminate_process(proc, force=False)
        asyncio.create_task(self._ensure_process_stopped(proc))
        return {"ok": True, "message": "已发送取消请求，正在停止脚本。", "started_at": entry_started_at}

    async def cancel_bound_run(
        self,
        *,
        tool_name: str,
        run_id: str,
        step_id: str,
    ) -> dict:
        """Cancel exactly one subprocess owned by a durable Run step."""

        try:
            execution_key = self._execution_key(
                {"run_id": run_id, "step_id": step_id}
            )
        except ValueError:
            return {
                "ok": False,
                "message": "Run 和 Step 身份不完整。",
                "code": "INVALID_EXECUTION_IDENTITY",
            }
        entry = self._running_outputs.get(execution_key)
        if (
            entry is None
            or entry.get("tool_name") != tool_name
            or not entry.get("running")
        ):
            return {
                "ok": False,
                "message": "指定的 Run Step 当前没有运行。",
                "code": "NOT_RUNNING",
            }
        return await self._cancel_entry(entry)

    def heavy_lock_held(self) -> bool:
        """Compatibility status: report active heavy work without serializing it."""

        return any(
            entry.get("running") and entry.get("heavy")
            for entry in self._running_outputs.values()
        )

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
        execution_identity: Mapping[str, object] | None = None,
    ) -> dict:
        try:
            execution_key = self._execution_key(execution_identity)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "error_code": "INVALID_EXECUTION_IDENTITY",
            }
        existing = self._running_outputs.get(execution_key)
        if existing and existing.get("running"):
            return {
                "success": False,
                "error": "指定的 Run Step 已经在执行。",
                "error_code": "EXECUTION_ALREADY_RUNNING",
            }
        self._prune_completed_entries()
        return await self._execute_now(
            tool_config,
            params,
            trusted_scheduler_context=trusted_scheduler_context,
            execution_key=execution_key,
        )

    async def _execute_now(
        self,
        tool_config: dict,
        params: dict,
        *,
        trusted_scheduler_context: Mapping[str, object] | None = None,
        execution_key: str,
    ) -> dict:
        """Execute a tool script via subprocess."""
        name = tool_config["name"]
        executor = os.path.join(PROJECT_ROOT, tool_config["executor"])
        timeout = tool_config.get("timeout", 300)
        heavy = tool_config.get("heavy", False)

        if not os.path.exists(executor):
            return {"success": False, "error": f"执行脚本不存在: {executor}"}

        start = time.time()
        safe_params = redact_sensitive(params)
        logger.info(
            "tool=%s | params=%s | heavy=%s",
            name,
            json.dumps(safe_params, ensure_ascii=False)[:500],
            heavy,
        )

        entry: dict | None = None
        execution_capability = ""
        try:
            input_json = json.dumps(params, ensure_ascii=False)
            python_executable = _resolve_python()
            started_at = datetime.now().isoformat(timespec="microseconds")
            self._running_outputs[execution_key] = {
                "execution_id": execution_key,
                "tool_name": name,
                "lines": [],
                "running": True,
                "started": time.time(),
                "started_at": started_at,
                "cancel_requested": False,
                "proc": None,
                "heavy": bool(heavy),
            }
            entry = self._running_outputs[execution_key]
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

            if entry.get("cancel_requested"):
                if proc.stdin is not None:
                    proc.stdin.close()
                await self._terminate_process(proc, force=False)
                await self._ensure_process_stopped(proc)
                duration = round(time.time() - start, 2)
                return self._cancelled_result(
                    name=name,
                    entry=entry,
                    duration=duration,
                )

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

            result_reports_failure = _result_reports_failure(result)
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
            if entry is not None and self._running_outputs.get(execution_key) is entry:
                entry["running"] = False
                entry["proc"] = None
            safe_error = _redact_execution_capability(exc, execution_capability)
            logger.error("tool=%s | error=%s | duration=%ss", name, safe_error[:200], duration)
            return {"success": False, "error": safe_error}

        finally:
            if execution_capability:
                revoke_execution_capability(execution_capability)
            if entry is not None:
                self._prune_completed_entries()

    def last_tool_info(self) -> dict | None:
        return self._last_run
