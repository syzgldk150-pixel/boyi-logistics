import asyncio
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.orchestration.models import OperationType, PlanStep, RiskLevel, RunStatus
from agent.orchestration.result_verifier import ResultVerifier
from agent.tool_executor import CANCEL_MESSAGE, MAX_COMPLETED_EXECUTION_HISTORY, PROJECT_ROOT, ToolExecutor


class _Catalog:
    def __init__(self, capability):
        self.capability = capability

    def get_capability(self, tool_name):
        return self.capability if tool_name == self.capability["name"] else None


class ToolExecutorCancelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.executor = ToolExecutor()
        self.script_paths: list[Path] = []
        self.script_path = self._write_temp_script(
            "tool-cancel-",
            """
            import sys
            import time

            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")

            print("[progress] started", flush=True)
            while True:
                time.sleep(0.2)
            """,
        )
        self.executor_relpath = os.path.relpath(self.script_path, PROJECT_ROOT)

    def _write_temp_script(self, prefix: str, content: str) -> Path:
        fd, script_path = tempfile.mkstemp(prefix=prefix, suffix=".py", dir=PROJECT_ROOT)
        os.close(fd)
        path = Path(script_path)
        path.write_text(
            textwrap.dedent(content).strip()
            + "\n",
            encoding="utf-8",
        )
        self.script_paths.append(path)
        return path

    async def asyncTearDown(self):
        for script_path in self.script_paths:
            try:
                script_path.unlink()
            except FileNotFoundError:
                pass

    async def test_cancel_tool_stops_running_process(self):
        task = asyncio.create_task(
            self.executor.execute(
                {
                    "name": "cancel_demo_tool",
                    "executor": self.executor_relpath,
                    "timeout": 30,
                },
                {},
            )
        )

        started_at = ""
        for _ in range(50):
            output = self.executor.get_running_output("cancel_demo_tool")
            if output.get("running"):
                started_at = str(output.get("started_at") or "")
                break
            await asyncio.sleep(0.1)
        self.assertTrue(started_at)

        cancel_result = await self.executor.cancel_tool("cancel_demo_tool", started_at=started_at)
        self.assertTrue(cancel_result["ok"])
        self.assertEqual(cancel_result["started_at"], started_at)

        result = await asyncio.wait_for(task, timeout=5)
        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertTrue(result["cancelled"])
        self.assertEqual("CANCELLED", result["error_code"])
        self.assertEqual(result["error"], CANCEL_MESSAGE)

        output = self.executor.get_running_output("cancel_demo_tool", started_at=started_at)
        self.assertFalse(output["running"])
        self.assertIn("已请求取消执行", "\n".join(output["lines"]))

    async def test_cancel_request_wins_when_sigterm_handler_exits_zero(self):
        graceful_script = self._write_temp_script(
            "tool-cancel-zero-",
            """
            import json
            import signal
            import sys
            import time

            def stop(_signum, _frame):
                print(json.dumps({"status": "SUCCESS", "data": {"finished": True}}), flush=True)
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, stop)
            print("[progress] started", flush=True)
            while True:
                time.sleep(0.2)
            """,
        )
        tool_name = "cancel_zero_exit_tool"
        task = asyncio.create_task(
            self.executor.execute(
                {
                    "name": tool_name,
                    "executor": os.path.relpath(graceful_script, PROJECT_ROOT),
                    "timeout": 30,
                },
                {},
            )
        )

        started_at = ""
        for _ in range(50):
            output = self.executor.get_running_output(tool_name)
            if output.get("running") and "[progress] started" in output.get("lines", []):
                started_at = str(output.get("started_at") or "")
                break
            await asyncio.sleep(0.1)
        self.assertTrue(started_at)

        cancel_result = await self.executor.cancel_tool(tool_name, started_at=started_at)
        self.assertTrue(cancel_result["ok"])
        result = await asyncio.wait_for(task, timeout=5)

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertTrue(result["cancelled"])
        self.assertEqual("CANCELLED", result["error_code"])

    async def test_cancel_is_accepted_while_subprocess_is_spawning(self):
        tool_name = "cancel_during_spawn_tool"
        spawn_entered = asyncio.Event()
        allow_spawn = asyncio.Event()
        create_subprocess_exec = asyncio.create_subprocess_exec

        async def delayed_spawn(*args, **kwargs):
            spawn_entered.set()
            await allow_spawn.wait()
            return await create_subprocess_exec(*args, **kwargs)

        with patch("agent.tool_executor.asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            task = asyncio.create_task(
                self.executor.execute(
                    {
                        "name": tool_name,
                        "executor": self.executor_relpath,
                        "timeout": 30,
                    },
                    {},
                )
            )
            await asyncio.wait_for(spawn_entered.wait(), timeout=2)

            output = self.executor.get_running_output(tool_name)
            self.assertTrue(output["running"])
            started_at = str(output.get("started_at") or "")
            self.assertTrue(started_at)

            cancel_result = await self.executor.cancel_tool(tool_name, started_at=started_at)
            self.assertTrue(cancel_result["ok"])
            self.assertTrue(
                self.executor.get_running_output(tool_name)["cancel_requested"]
            )

            allow_spawn.set()
            result = await asyncio.wait_for(task, timeout=5)

        self.assertFalse(result["success"])
        self.assertTrue(result["cancelled"])
        self.assertEqual("CANCELLED", result["error_code"])
        self.assertFalse(self.executor.is_tool_running(tool_name))

    async def test_subprocess_spawn_failure_clears_running_state(self):
        tool_name = "spawn_failure_tool"

        async def fail_spawn(*_args, **_kwargs):
            raise OSError("controlled spawn failure")

        with patch("agent.tool_executor.asyncio.create_subprocess_exec", side_effect=fail_spawn):
            result = await self.executor.execute(
                {
                    "name": tool_name,
                    "executor": self.executor_relpath,
                    "timeout": 30,
                },
                {},
            )

        self.assertFalse(result["success"])
        self.assertIn("controlled spawn failure", result["error"])
        self.assertFalse(self.executor.is_tool_running(tool_name))
        self.assertTrue(self.executor._running_outputs)
        self.assertTrue(
            all(entry["proc"] is None for entry in self.executor._running_outputs.values())
        )

    async def test_heavy_tools_do_not_share_a_global_execution_lock(self):
        slow_script = self._write_temp_script(
            "tool-heavy-slow-",
            """
            import json
            import time

            time.sleep(0.4)
            print(json.dumps({"tool": "slow"}))
            """,
        )
        fast_script = self._write_temp_script(
            "tool-heavy-fast-",
            """
            import json

            print(json.dumps({"tool": "fast"}))
            """,
        )

        slow_task = asyncio.create_task(
            self.executor.execute(
                {
                    "name": "slow_heavy_tool",
                    "executor": os.path.relpath(slow_script, PROJECT_ROOT),
                    "timeout": 5,
                    "heavy": True,
                },
                {},
            )
        )
        for _ in range(50):
            if self.executor.get_running_output("slow_heavy_tool").get("running"):
                break
            await asyncio.sleep(0.02)
        else:
            self.fail("slow heavy tool did not start")

        fast_result = await self.executor.execute(
            {
                "name": "fast_heavy_tool",
                "executor": os.path.relpath(fast_script, PROJECT_ROOT),
                "timeout": 5,
                "heavy": True,
            },
            {},
        )
        self.assertFalse(slow_task.done())
        slow_result = await slow_task

        self.assertTrue(slow_result["success"])
        self.assertTrue(fast_result["success"])
        self.assertEqual(fast_result["data"], {"tool": "fast"})

    async def test_completed_execution_history_is_bounded_without_pruning_live_runs(self):
        for index in range(MAX_COMPLETED_EXECUTION_HISTORY + 5):
            self.executor._running_outputs[f"complete-{index}"] = {
                "running": False,
                "started": float(index),
            }
        self.executor._running_outputs["live"] = {
            "running": True,
            "started": -1.0,
        }

        self.executor._prune_completed_entries()

        completed = [
            entry
            for entry in self.executor._running_outputs.values()
            if not entry.get("running")
        ]
        self.assertEqual(MAX_COMPLETED_EXECUTION_HISTORY, len(completed))
        self.assertIn("live", self.executor._running_outputs)

    async def test_unified_tool_failure_preserves_auth_required_classification(self):
        failure_script = self._write_temp_script(
            "tool-unified-failure-",
            """
            import json

            print(json.dumps({
                "status": "FAILED",
                "data": {},
                "meta": {},
                "warnings": [],
                "error": {
                    "code": "AUTH_REQUIRED",
                    "message": "session unavailable",
                    "retryable": True,
                },
            }))
            """,
        )

        result = await self.executor.execute(
            {
                "name": "unified_failure_tool",
                "executor": os.path.relpath(failure_script, PROJECT_ROOT),
                "timeout": 5,
            },
            {},
        )

        self.assertFalse(result["success"])
        self.assertEqual("AUTH_REQUIRED", result["error_code"])
        self.assertTrue(result["retryable"])
        self.assertEqual("FAILED", result["data"]["status"])

    async def test_explicit_business_no_data_status_is_success(self):
        no_data_script = self._write_temp_script(
            "tool-business-no-data-",
            """
            import json

            print(json.dumps({
                "ok": True,
                "success": True,
                "status": "no_data",
                "runs": [],
            }))
            """,
        )

        result = await self.executor.execute(
            {
                "name": "business_no_data_tool",
                "executor": os.path.relpath(no_data_script, PROJECT_ROOT),
                "timeout": 5,
            },
            {},
        )

        self.assertTrue(result["success"])
        self.assertEqual("no_data", result["data"]["status"])
        self.assertTrue(self.executor.last_tool_info()["success"])

    async def test_failure_status_wins_over_contradictory_success_marker(self):
        for status in (
            "FAILED",
            "ERROR",
            "FAILURE",
            "BLOCKED",
            "BLOCKED_DATA",
            "PARTIAL",
            "PARTIAL_FAILED",
            "PARTIAL_FAILURE",
        ):
            with self.subTest(status=status):
                failure_script = self._write_temp_script(
                    "tool-contradictory-status-",
                    f"""
                    import json

                    print(json.dumps({{
                        "ok": True,
                        "success": True,
                        "status": {status!r},
                    }}))
                    """,
                )

                result = await self.executor.execute(
                    {
                        "name": f"contradictory_{status.lower()}_tool",
                        "executor": os.path.relpath(failure_script, PROJECT_ROOT),
                        "timeout": 5,
                    },
                    {},
                )

                self.assertFalse(result["success"])
                self.assertEqual("TOOL_REPORTED_FAILURE", result["error_code"])

    async def test_unknown_business_status_requires_explicit_success(self):
        unknown_script = self._write_temp_script(
            "tool-unknown-status-",
            """
            import json

            print(json.dumps({"status": "signed"}))
            """,
        )

        result = await self.executor.execute(
            {
                "name": "unknown_status_tool",
                "executor": os.path.relpath(unknown_script, PROJECT_ROOT),
                "timeout": 5,
            },
            {},
        )

        self.assertFalse(result["success"])
        self.assertEqual("TOOL_REPORTED_FAILURE", result["error_code"])

    async def test_explicit_false_result_is_failure_without_error_text(self):
        failure_script = self._write_temp_script(
            "tool-explicit-failure-",
            """
            import json

            print(json.dumps({"success": False, "status": "failed"}))
            """,
        )

        result = await self.executor.execute(
            {
                "name": "explicit_failure_tool",
                "executor": os.path.relpath(failure_script, PROJECT_ROOT),
                "timeout": 5,
            },
            {},
        )

        self.assertFalse(result["success"])
        self.assertEqual("工具返回失败状态。", result["error"])
        self.assertFalse(result["data"]["success"])
        self.assertFalse(self.executor.last_tool_info()["success"])

    async def test_explicit_ok_false_result_is_failure_and_is_redacted(self):
        failure_script = self._write_temp_script(
            "tool-redacted-failure-",
            """
            import json

            print(json.dumps({
                "ok": False,
                "message": "password=dummy-value",
                "details": {"token": "dummy-token"},
            }))
            """,
        )

        result = await self.executor.execute(
            {
                "name": "redacted_failure_tool",
                "executor": os.path.relpath(failure_script, PROJECT_ROOT),
                "timeout": 5,
            },
            {},
        )

        serialized = str(result)
        self.assertFalse(result["success"])
        self.assertNotIn("dummy-value", serialized)
        self.assertNotIn("dummy-token", serialized)
        self.assertEqual("[REDACTED]", result["data"]["details"]["token"])


if __name__ == "__main__":
    unittest.main()
