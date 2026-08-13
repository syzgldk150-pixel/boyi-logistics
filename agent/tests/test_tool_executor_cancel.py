import asyncio
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tool_executor import CANCEL_MESSAGE, PROJECT_ROOT, ToolExecutor


class ToolExecutorCancelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.executor = ToolExecutor()
        self.script_paths: list[Path] = []
        self.lock_dir = tempfile.TemporaryDirectory(prefix="tool-lock-", dir=PROJECT_ROOT)
        self.addCleanup(self.lock_dir.cleanup)
        self.lock_patch = patch(
            "agent.tool_executor.LOCK_FILE",
            os.path.join(self.lock_dir.name, ".heavy_task.lock"),
        )
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
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
        self.assertEqual(result["error"], CANCEL_MESSAGE)

        output = self.executor.get_running_output("cancel_demo_tool", started_at=started_at)
        self.assertFalse(output["running"])
        self.assertIn("已请求取消执行", "\n".join(output["lines"]))

    async def test_heavy_tools_wait_for_existing_heavy_task(self):
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
        slow_result = await slow_task

        self.assertTrue(slow_result["success"])
        self.assertTrue(fast_result["success"])
        self.assertEqual(fast_result["data"], {"tool": "fast"})

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
