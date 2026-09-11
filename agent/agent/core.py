"""Composition facade for the common chat engine and direct business readers.

Registered readers return data directly. Installed plugins are invoked through
the signed plugin interface; historical Command access is retained only for
explicit work-item inspection and administration.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from typing import Any, Optional

from agent.llm_client import LLMClient
from agent.direct_readers import invoke_registered_reader
from agent.finance_brain import FinanceBrain
from agent.llm_settings import LLMSettingsRepository
from shared.finance import FinanceRepository
from agent.memory import Memory
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    OrchestrationError,
    RunStatus,
)
from agent.tool_registry import ToolRegistry


logger = logging.getLogger("agent")
MAX_TOOL_ROUNDS = 3
UNKNOWN_EXECUTION_REPLY = "没有匹配到可执行脚本，我不知道该执行哪个任务。"
WAITING_STATUSES = {
    RunStatus.WAITING_APPROVAL.value,
    RunStatus.NEEDS_CLARIFICATION.value,
    RunStatus.BLOCKED_LOGIN.value,
    RunStatus.BLOCKED_DATA.value,
    RunStatus.FAILED_RETRYABLE.value,
}


class AgentCore:
    """One chat engine for both channels, with independently invoked readers."""

    def __init__(
        self,
        *,
        direct_tool_runners: Mapping[str, Callable[[dict], dict]] | None = None,
        today_provider: Callable[[], dt.date] | None = None,
    ) -> None:
        self.llm = LLMClient()
        self.registry = ToolRegistry()
        self.memory = Memory()
        self.finance_brain: FinanceBrain | None = None
        self._feishu_connected = False
        # Closed host readers are direct requests. Plugins use their dedicated
        # signed invocation entrypoints and never the historical Command path.
        self._direct_tool_runners = dict(direct_tool_runners or {})
        self._direct_invocations = None
        self._plugin_conversations = None
        self._today_provider = today_provider or (
            lambda: dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        )
        self._command_gateway: Any | None = None
        self._orchestration_repository: Any | None = None
        self._workflow_runner: Any | None = None
        self._execution_runtime: Any | None = None
        self._control_plane_service: Any | None = None

    def configure_direct_readers(self, readers: Mapping[str, Callable[[dict], dict]], *, invocations=None) -> None:
        self._direct_tool_runners.update(readers)
        if invocations is not None:
            self._direct_invocations = invocations

    def configure_orchestration(
        self,
        *,
        command_gateway: Any,
        repository: Any,
        workflow_runner: Any,
        execution_runtime: Any,
        control_plane_service: Any | None = None,
    ) -> None:
        """Bind ports built by ``main.py``, the sole composition root."""

        self._command_gateway = command_gateway
        self._orchestration_repository = repository
        self._workflow_runner = workflow_runner
        self._execution_runtime = execution_runtime
        self._control_plane_service = control_plane_service

    def configure_tool_catalog(self, catalog: Any) -> None:
        """Use the production-scoped catalog for discovery and preflight."""

        self.registry = catalog

    def configure_plugin_conversations(self, service: Any) -> None:
        self._plugin_conversations = service

    async def init(self) -> None:
        try:
            self.memory.init()
            await self.llm.bind_repository(
                LLMSettingsRepository(self.memory.connection_factory)
            )
            self.finance_brain = FinanceBrain(
                FinanceRepository(self.memory.connection_factory),
                self.llm,
            )
        except Exception as exc:
            logger.error("MySQL connection failed; conversation memory is unavailable: %s", exc)

    def reload_runtime_config(self) -> dict[str, Any]:
        self.registry.load()
        return {
            "chat_engine": "unified",
            "tools": self.registry.list_tools(),
        }

    async def reload_llm_config(self) -> dict:
        """Reload the active LLM version for future requests."""

        return await self.llm.reload_config()

    def configure_conversations(self, service) -> None:
        """Bind the single chat engine shared by Console and Feishu."""
        self._conversations = service

    async def handle_message(
        self, message: str, user_id: str = "unknown", conversation_id: Optional[str] = None,
        *, actor: Actor | None = None, source: str = "legacy_api", request_id: str | None = None,
    ) -> dict[str, Any]:
        import asyncio
        from agent.channel_chat import reply_to_console, reply_to_feishu
        service = getattr(self, "_conversations", None)
        if service is None:
            raise RuntimeError("AI 对话服务未初始化")
        if source == "console":
            return await asyncio.to_thread(reply_to_console, service, actor=actor,
                session_id=conversation_id, request_id=request_id, message=message)
        if source == "feishu":
            return await asyncio.to_thread(reply_to_feishu, service, actor=actor,
                chat_id=conversation_id, event_id=request_id, message=message)
        raise ValueError("AI 对话需要已验证的后台或飞书身份")

    async def execute_tool(
        self,
        tool_name: str,
        params: Mapping[str, Any],
        *,
        actor: Actor | None = None,
        source: str = "legacy_api",
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        execution_context: Mapping[str, Any] | None = None,
        llm_selected: bool = False,
        timeout_seconds: float = 1800.0,
        on_submitted: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Invoke a registered short reader; ordinary queries create no queued work."""

        direct_runner = self._direct_tool_runners.get(str(tool_name or ""))
        if direct_runner is not None:
            return await invoke_registered_reader(
                catalog=self.registry, name=tool_name, arguments=params, handler=direct_runner,
                actor=actor, source=source, llm_selected=llm_selected, timeout_seconds=timeout_seconds,
                invocations=self._direct_invocations,
                identity_access=getattr(self, "identity_access", None),
            )
        # Writes and installed plugins have their own explicit, authenticated
        # interface. Do not turn an unregistered ordinary request into a Run.
        return {
            "success": False,
            "error_code": "DIRECT_INTERFACE_REQUIRED",
            "error": "此功能需要通过已注册的插件或业务接口调用",
        }

    def submit_command(self, command: Command):
        """Submit a trusted command without exposing the Gateway implementation."""

        if self._command_gateway is None:
            raise OrchestrationError(
                "CONTROL_PLANE_UNAVAILABLE",
                "Agent control plane is not initialized",
            )
        return self._command_gateway.submit(command)

    def _legacy_result_from_run(self, run: Mapping[str, Any]) -> dict[str, Any]:
        status = str(run.get("status") or "")
        result: dict[str, Any] = {
            "success": status == RunStatus.COMPLETED.value,
            "status": status,
            "command_id": str(run.get("command_id") or ""),
            "work_item_id": str(run.get("work_item_id") or ""),
            "run_id": str(run.get("run_id") or ""),
            "correlation_id": str(run.get("correlation_id") or ""),
        }
        with self._orchestration_repository.unit_of_work() as uow:
            steps = uow.steps.list_for_run(result["run_id"])
            approval = uow.approvals.get_latest_for_run(result["run_id"], for_update=False)
        if steps:
            last_step = steps[-1]
            summary = last_step.get("result_summary_json")
            if isinstance(summary, Mapping):
                result["tool_result"] = dict(summary)
                data = summary.get("data")
                result["data"] = dict(data) if isinstance(data, Mapping) else data
            if last_step.get("error_code"):
                result["error_code"] = str(last_step["error_code"])
            if last_step.get("error_summary"):
                result["error"] = str(last_step["error_summary"])
        if approval:
            result["approval"] = {
                "approval_id": approval.get("approval_id"),
                "plan_hash": approval.get("plan_hash"),
                "status": approval.get("status"),
                "required_role": approval.get("required_role"),
                "expires_at": approval.get("expires_at"),
            }
        if not result["success"]:
            result.setdefault("error_code", str(run.get("error_code") or status or "RUN_NOT_COMPLETED"))
            result.setdefault(
                "error",
                str(run.get("error_summary") or _waiting_message(status)),
            )
        result["next_poll_after_ms"] = 5000 if status in WAITING_STATUSES else 0
        return result

    async def cancel_tool(self, tool_name: str, started_at: str = "") -> dict[str, Any]:
        """Old tool-name cancellation is intentionally disabled; cancel by run ID."""

        del tool_name, started_at
        return {
            "ok": False,
            "error_code": "RUN_ID_REQUIRED",
            "error": "cancel through /internal/v1/runs/{run_id}/cancel",
        }

    async def cancel_feishu_run(self, run_id: str, *, actor_id: str) -> dict[str, Any]:
        """Cancel only a Run originally submitted by the same Feishu actor.

        The caller supplies only the identity observed on the current Feishu
        event.  Source, actor type, roles, and command ownership are read back
        from durable command state and cannot be overridden by the handler.
        """

        normalized_run_id = str(run_id or "").strip()
        normalized_actor_id = str(actor_id or "").strip()
        if not normalized_run_id or not normalized_actor_id:
            return {
                "ok": False,
                "error_code": "INVALID_CANCEL_IDENTITY",
                "error": "run_id and Feishu actor identity are required",
            }
        if self._orchestration_repository is None or self._control_plane_service is None:
            return {
                "ok": False,
                "error_code": "CONTROL_PLANE_UNAVAILABLE",
                "error": "Agent control plane is not initialized",
            }
        run = self._orchestration_repository.get_run(normalized_run_id)
        if not isinstance(run, Mapping):
            return {"ok": False, "error_code": "RUN_NOT_FOUND", "error": "Run was not found"}
        with self._orchestration_repository.unit_of_work() as uow:
            command = uow.commands.get(str(run.get("command_id") or ""), for_update=False)
        if not isinstance(command, Mapping):
            return {
                "ok": False,
                "error_code": "COMMAND_NOT_FOUND",
                "error": "Run command was not found",
            }
        owned = (
            str(command.get("source") or "") == "feishu"
            and str(command.get("actor_type") or "") == ActorType.FEISHU_USER.value
            and str(command.get("actor_id") or "") == normalized_actor_id
        )
        if not owned:
            return {
                "ok": False,
                "error_code": "RUN_CANCEL_FORBIDDEN",
                "error": "Only the original Feishu command actor may cancel this run",
            }
        try:
            result = await self._control_plane_service.cancel_run(
                normalized_run_id,
                actor=Actor(
                    ActorType.FEISHU_USER,
                    normalized_actor_id,
                    roles=(),
                    authenticated_by="feishu_event",
                ),
                comment="Feishu originator requested cancellation",
            )
        except OrchestrationError as exc:
            return {"ok": False, "error_code": exc.code, "error": exc.message}
        return {"ok": True, **dict(result)}

    def is_tool_running(self, tool_name: str) -> bool:
        runtime = self._execution_runtime
        return bool(runtime and runtime.is_tool_running(tool_name))

    def running_tool_info(self, tool_name: str) -> dict[str, Any]:
        runtime = self._execution_runtime
        return runtime.running_tool_info(tool_name) if runtime else {}

    def running_tools(self) -> list[str]:
        runtime = self._execution_runtime
        return list(runtime.running_tools()) if runtime else []

    def feishu_status(self) -> str:
        return "connected" if self._feishu_connected else "disconnected"

    def set_feishu_connected(self, connected: bool) -> None:
        self._feishu_connected = connected

    def llm_status(self, provider: str) -> str:
        return self.llm.status(provider)

    def db_status(self) -> str:
        return self.memory.status()

    def last_tool_info(self) -> dict[str, Any] | None:
        runtime = self._execution_runtime
        return runtime.last_tool_info() if runtime else None

    def heavy_lock_held(self) -> bool:
        runtime = self._execution_runtime
        return bool(runtime and runtime.heavy_lock_held())

    async def close(self) -> None:
        await self.llm.close()

    @staticmethod
    def _entry_idempotency_key(
        actor: Actor,
        source: str,
        command_type: str,
        request_id: str,
    ) -> str:
        if source == "console":
            return f"console:{actor.actor_id}:{command_type}:{request_id}"
        if source == "feishu":
            return f"feishu:{command_type}:{request_id}"
        return f"{source}:{actor.actor_id}:{command_type}:{request_id}"


def _waiting_message(status: str) -> str:
    messages = {
        RunStatus.WAITING_APPROVAL.value: "计划正在等待管理员审批",
        RunStatus.NEEDS_CLARIFICATION.value: "运行需要补充信息",
        RunStatus.BLOCKED_LOGIN.value: "运行正在等待账号登录恢复",
        RunStatus.BLOCKED_DATA.value: "来源数据不完整，运行已阻塞",
        RunStatus.FAILED_RETRYABLE.value: "运行失败但可以安全重试",
    }
    return messages.get(status, "运行未完成")
