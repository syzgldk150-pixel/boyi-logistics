import asyncio
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import OperationType, PlanStep, RiskLevel, RunStatus
from agent.orchestration.result_verifier import ResultVerifier
from agent.tool_executor import (
    CANCEL_MESSAGE,
    PROJECT_ROOT,
    ToolExecutor,
    build_trusted_scheduler_context,
)


class _Catalog:
    def __init__(self, capability):
        self.capability = capability

    def get_capability(self, tool_name):
        return self.capability if tool_name == self.capability["name"] else None


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
        self.assertIsNone(self.executor._running_outputs[tool_name]["proc"])

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

    async def test_unified_retryable_failure_survives_executor_adapter_and_verifier(self):
        failure_script = self._write_temp_script(
            "tool-retryable-failure-",
            """
            import json

            print(json.dumps({
                "status": "FAILED",
                "data": {"attempted": True},
                "meta": {},
                "warnings": [],
                "error": {
                    "code": "TRANSIENT_SOURCE_FAILURE",
                    "message": "source is temporarily unavailable",
                    "retryable": True,
                },
            }))
            """,
        )
        capability = {
            "name": "retryable_failure_tool",
            "version": "1.0.0",
            "executor": os.path.relpath(failure_script, PROJECT_ROOT),
            "timeout": 5,
            "operation_type": "read",
            "evidence": [],
            "postconditions": [],
        }
        step = PlanStep(
            step_key="retryable",
            tool_name=capability["name"],
            tool_version=capability["version"],
            operation_type=OperationType.READ,
            arguments={},
            account_id=None,
            depends_on=(),
            idempotency_key="retryable-step",
            expected_evidence=(),
            postconditions=(),
            risk_level=RiskLevel.LOW,
        )
        adapter = RegisteredToolExecutionAdapter(
            catalog=_Catalog(capability),
            executor=self.executor,
        )

        normalized = await adapter.execute_step(
            step,
            run_id="run-retryable",
            step_id="step-retryable",
            execution_context={"source": "scheduler"},
        )
        outcome = ResultVerifier().verify(step, normalized, capability)

        self.assertEqual("FAILED", normalized["status"])
        self.assertEqual("TRANSIENT_SOURCE_FAILURE", normalized["error"]["code"])
        self.assertTrue(normalized["error"]["retryable"])
        self.assertFalse(outcome.accepted)
        self.assertIs(outcome.run_status, RunStatus.FAILED_RETRYABLE)
        self.assertEqual("TRANSIENT_SOURCE_FAILURE", outcome.code)

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

    async def test_private_scheduler_context_reaches_only_r7_subprocess(self):
        context_script = self._write_temp_script(
            "tool-private-scheduler-context-",
            """
            import json

            from agent.tool_executor import trusted_scheduler_context

            context = trusted_scheduler_context()
            print(json.dumps({
                "has_context": context is not None,
                "task_id": context.get("task_id") if context is not None else None,
            }))
            """,
        )
        trusted_context = build_trusted_scheduler_context(
            "r7_arrival_checkin",
            {
                "source": "scheduler",
                "actor": {
                    "actor_type": "scheduler",
                    "actor_id": "r7_arrival_checkin_1900",
                    "roles": ["system"],
                },
                "task_id": "r7_arrival_checkin_1900",
                "configuration_version": 4,
                "scheduled_for": "2026-08-14T19:00:00+08:00",
                "cron_expression": "0 19 * * *",
            },
        )
        self.assertIsNotNone(trusted_context)
        executor_path = os.path.relpath(context_script, PROJECT_ROOT)

        r7_result = await self.executor.execute(
            {
                "name": "r7_arrival_checkin",
                "executor": executor_path,
                "timeout": 5,
            },
            {},
            trusted_scheduler_context=trusted_context,
        )
        other_result = await self.executor.execute(
            {
                "name": "unrelated_tool",
                "executor": executor_path,
                "timeout": 5,
            },
            {},
            trusted_scheduler_context=trusted_context,
        )

        self.assertTrue(r7_result["success"])
        self.assertEqual(
            r7_result["data"],
            {"has_context": True, "task_id": "r7_arrival_checkin_1900"},
        )
        self.assertTrue(other_result["success"])
        self.assertEqual(
            other_result["data"],
            {"has_context": False, "task_id": None},
        )

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
