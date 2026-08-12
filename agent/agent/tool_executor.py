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
from datetime import datetime

from shared.redaction import redact_sensitive, redact_text

logger = logging.getLogger("tools")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
LOCK_FILE = os.path.join(PROJECT_ROOT, "logs", ".heavy_task.lock")
CANCEL_MESSAGE = "任务已取消"
HEAVY_LOCK_RETRY_SECONDS = 0.5
DEFAULT_HEAVY_QUEUE_TIMEOUT = 900.0


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

    async def execute(self, tool_config: dict, params: dict) -> dict:
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
                    return await self._execute_now(tool_config, params)
            finally:
                self._queued_tools.discard(name)
        return await self._execute_now(tool_config, params)

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

    async def _execute_now(self, tool_config: dict, params: dict) -> dict:
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

            proc = await asyncio.create_subprocess_exec(
                python_executable,
                executor,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=PROJECT_ROOT,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        filter(None, (WORKSPACE_ROOT, os.environ.get("PYTHONPATH", "")))
                    ),
                },
                start_new_session=True,
            )
            entry["proc"] = proc

            proc.stdin.write(input_json.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []

            def append_output_line(text: str, *, is_stderr: bool) -> None:
                text = redact_text(text)
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
                logger.error("tool=%s | error=timeout(%ds) | duration=%ss", name, timeout, duration)
                return {"success": False, "error": f"工具执行超时（{timeout}秒）"}

            entry["running"] = False
            entry["proc"] = None
            duration = round(time.time() - start, 2)

            stdout = b"".join(stdout_chunks)
            stderr = b"".join(stderr_chunks)
            exit_code = proc.returncode
            cancel_requested = bool(entry.get("cancel_requested"))

            if exit_code != 0:
                if cancel_requested:
                    entry["lines"].append("[control] 任务已取消。")
                    logger.info("tool=%s | cancelled=true | duration=%ss", name, duration)
                    self._last_run = {
                        "tool": name,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "success": False,
                        "duration_s": duration,
                        "canceled": True,
                    }
                    return {"success": False, "canceled": True, "error": CANCEL_MESSAGE, "duration_s": duration}

                err_msg = redact_text(stderr.decode("utf-8", errors="replace").strip())[-500:]
                logger.error("tool=%s | error=exit_code_%d | stderr=%s | duration=%ss", name, exit_code, err_msg, duration)
                if exit_code == 137:
                    return {"success": False, "error": "工具被 OOM Kill，内存不足"}
                return {"success": False, "error": f"工具执行失败(exit {exit_code}): {err_msg}"}

            raw_output = stdout.decode("utf-8", errors="replace").strip()
            try:
                result = json.loads(raw_output)
            except json.JSONDecodeError:
                result = {"output": raw_output}

            if isinstance(result, dict) and result.get("error"):
                safe_error = redact_text(result["error"])
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
                failure = {"success": False, "error": safe_error, "data": result, "duration_s": duration}
                if result.get("error_code"):
                    failure["error_code"] = result.get("error_code")
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
            safe_error = redact_text(exc)
            logger.error("tool=%s | error=%s | duration=%ss", name, safe_error[:200], duration)
            return {"success": False, "error": safe_error}

        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def last_tool_info(self) -> dict | None:
        return self._last_run
