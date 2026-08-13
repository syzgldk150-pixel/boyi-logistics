"""Compatibility facade for chat and legacy tool callers.

All tool work is accepted by :class:`CommandGateway`.  This module deliberately
does not own or call ``ToolExecutor``; the durable ``WorkflowRunner`` is the only
caller of the governed execution port.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Optional

from agent.direct_tool_router import (
    direct_tool_request_from_text,
    format_tool_reply,
    parse_login_send_code_session,
)
from agent.llm_client import LLMClient
from agent.memory import Memory
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    OrchestrationError,
    RunStatus,
    new_id,
)
from agent.tool_registry import ToolRegistry
from shared.redaction import redact_text


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
    """Chat and legacy API facade backed by the durable control plane."""

    def __init__(
        self,
        *,
        direct_tool_runners: Mapping[str, Callable[[dict], dict]] | None = None,
    ) -> None:
        self.llm = LLMClient()
        self.registry = ToolRegistry()
        self.memory = Memory()
        self._feishu_connected = False
        self._system_prompt = ""
        self._tool_selection_prompt = ""
        self._business_rules = ""
        # Kept only for composition-root compatibility.  Runners are injected
        # into RegisteredToolExecutionAdapter, never invoked from this facade.
        self._direct_tool_runners = dict(direct_tool_runners or {})
        self._command_gateway: Any | None = None
        self._orchestration_repository: Any | None = None
        self._workflow_runner: Any | None = None
        self._execution_runtime: Any | None = None
        self._control_plane_service: Any | None = None

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

    async def init(self) -> None:
        self._load_prompts()
        try:
            self.memory.init()
        except Exception as exc:
            logger.error("MySQL connection failed; conversation memory is unavailable: %s", exc)

    def reload_runtime_config(self) -> dict[str, Any]:
        self._load_prompts()
        self.registry.load()
        return {
            "prompts": ["system.md", "tool_selection.md", "business_rules.md"],
            "tools": self.registry.list_tools(),
        }

    def _load_prompts(self) -> None:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
        for attr, filename in (
            ("_system_prompt", "system.md"),
            ("_tool_selection_prompt", "tool_selection.md"),
            ("_business_rules", "business_rules.md"),
        ):
            path = os.path.join(base, filename)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    setattr(self, attr, handle.read())
                logger.info("Loaded prompt: %s", filename)

    async def handle_message(
        self,
        message: str,
        user_id: str = "unknown",
        conversation_id: Optional[str] = None,
        *,
        actor: Actor | None = None,
        source: str = "legacy_api",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Handle chat while allowing the LLM to select only exposed reads."""

        started = time.monotonic()
        trusted_actor = actor or Actor(ActorType.LEGACY_API, str(user_id or "unknown"))
        browser_request_id = str(request_id or uuid.uuid4())
        try:
            conv_id = self.memory.get_or_create_conversation(user_id, conversation_id)
        except Exception:
            conv_id = conversation_id or "temp"
        try:
            history = self.memory.get_recent_messages(conv_id, limit=10)
        except Exception:
            history = []

        knowledge_context = ""
        try:
            knowledge = self.memory.search_knowledge(message, limit=3)
            if knowledge:
                knowledge_context = "\n\n相关知识：\n" + "\n".join(
                    f"- [{item['category']}] {item['content']}" for item in knowledge
                )
        except Exception:
            pass

        system_content = "\n\n".join(
            part
            for part in (
                self._system_prompt,
                self._tool_selection_prompt,
                self._business_rules,
                knowledge_context,
            )
            if part
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
        messages.extend(history)
        messages.append({"role": "user", "content": message})
        try:
            self.memory.save_message(conv_id, "user", message)
        except Exception:
            pass

        direct_request = direct_tool_request_from_text(message)
        if direct_request:
            tool_name = str(direct_request["tool_name"])
            params = dict(direct_request["params"])
            local_result = direct_request.get("local_result")
            if isinstance(local_result, dict):
                tool_result = local_result
            else:
                tool_result = await self.execute_tool(
                    tool_name,
                    params,
                    actor=trusted_actor,
                    source=source,
                    idempotency_key=self._entry_idempotency_key(
                        trusted_actor,
                        source,
                        "chat",
                        browser_request_id,
                    ),
                )
            final_content = format_tool_reply(tool_name, tool_result)
            self._save_assistant_message(conv_id, final_content)
            return {
                "reply": final_content,
                "conversation_id": conv_id,
                "duration_s": round(time.monotonic() - started, 2),
                "executed_tools": [{"tool_name": tool_name, "params": params, "result": tool_result}],
            }

        if parse_login_send_code_session(message):
            final_content = "1. 大祥账号\n2. 操作场账号\n3. 韵达账号"
            self._save_assistant_message(conv_id, final_content)
            return {
                "reply": final_content,
                "conversation_id": conv_id,
                "duration_s": round(time.monotonic() - started, 2),
            }

        tools = self.registry.get_openai_tools() or None
        final_content = ""
        executed_tool_calls = 0
        executed_tool_results: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for round_index in range(MAX_TOOL_ROUNDS + 1):
            llm_result = await self.llm.chat(messages, tools=tools)
            content = str(llm_result.get("content") or "")
            tool_calls = llm_result.get("tool_calls")
            if not tool_calls:
                if executed_tool_calls == 0:
                    final_content = UNKNOWN_EXECUTION_REPLY
                    logger.warning(
                        "Blocked unverified LLM reply user=%s conversation=%s",
                        user_id,
                        conv_id,
                    )
                else:
                    final_content = "\n\n".join(
                        format_tool_reply(tool_name, result)
                        for tool_name, _arguments, result in executed_tool_results
                    )
                break

            assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
            assistant_message["tool_calls"] = [
                {"id": call["id"], "type": "function", "function": call["function"]}
                for call in tool_calls
            ]
            messages.append(assistant_message)
            for call_index, call in enumerate(tool_calls):
                function = call.get("function") if isinstance(call, Mapping) else None
                func_name = str((function or {}).get("name") or "")
                raw_arguments = (function or {}).get("arguments")
                try:
                    func_args = json.loads(raw_arguments)
                    if not isinstance(func_args, dict):
                        raise ValueError("tool arguments must be an object")
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    tool_result = {
                        "success": False,
                        "error_code": "INVALID_TOOL_ARGUMENTS",
                        "error": redact_text(exc),
                    }
                else:
                    if self.registry.get_capability(func_name) is None:
                        tool_result = {
                            "success": False,
                            "error_code": "UNKNOWN_TOOL",
                            "error": f"未知工具: {func_name}",
                        }
                    else:
                        key = self._entry_idempotency_key(
                            trusted_actor,
                            source,
                            "chat",
                            f"{browser_request_id}:{round_index}:{call_index}",
                        )
                        tool_result = await self.execute_tool(
                            func_name,
                            func_args,
                            actor=trusted_actor,
                            source=source,
                            idempotency_key=key,
                            llm_selected=True,
                        )
                        executed_tool_calls += 1
                        executed_tool_results.append((func_name, func_args, tool_result))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or new_id()),
                        "content": json.dumps(tool_result, ensure_ascii=False)[:5000],
                    }
                )

        if not final_content and executed_tool_results:
            final_content = "\n\n".join(
                format_tool_reply(tool_name, result)
                for tool_name, _arguments, result in executed_tool_results
            )
        self._save_assistant_message(conv_id, final_content)
        return {
            "reply": final_content,
            "conversation_id": conv_id,
            "duration_s": round(time.monotonic() - started, 2),
            "executed_tools": [
                {"tool_name": name, "params": arguments, "result": result}
                for name, arguments, result in executed_tool_results
            ],
        }

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
        """Deprecated synchronous wrapper; it always traverses CommandGateway."""

        if self._command_gateway is None or self._orchestration_repository is None:
            return {
                "success": False,
                "error_code": "CONTROL_PLANE_UNAVAILABLE",
                "error": "Agent control plane is not initialized",
            }
        capability = self.registry.get_capability(str(tool_name or ""))
        if capability is None:
            return {"success": False, "error_code": "UNKNOWN_TOOL", "error": f"未知工具: {tool_name}"}
        if not isinstance(params, Mapping):
            return {
                "success": False,
                "error_code": "INVALID_TOOL_ARGUMENTS",
                "error": "tool arguments must be a JSON object",
            }
        trusted_actor = actor or Actor(ActorType.LEGACY_API, "legacy-api")
        operation_type = str(capability.get("operation_type") or "")
        if not idempotency_key and operation_type not in {"read", "compute"}:
            return {
                "success": False,
                "error_code": "IDEMPOTENCY_KEY_REQUIRED",
                "error": "write commands require a stable idempotency key",
            }
        key = str(idempotency_key or f"legacy:{trusted_actor.actor_id}:{tool_name}:{uuid.uuid4()}")
        command = Command(
            command_type="tool.execute",
            source=str(source or "legacy_api"),
            actor=trusted_actor,
            parameters={
                "tool_name": str(tool_name),
                "arguments": dict(params),
                "account_id": params.get("account_id"),
                "execution_context": dict(execution_context or {}),
                "llm_selected": bool(llm_selected),
            },
            idempotency_key=key,
            correlation_id=str(correlation_id or new_id()),
        )
        try:
            if on_submitted is None:
                run = await self._command_gateway.submit_and_wait(
                    command,
                    timeout_seconds=timeout_seconds,
                )
            else:
                receipt = self._command_gateway.submit(command)
                on_submitted(receipt.to_dict())
                run = await self._command_gateway.wait_for_run(
                    receipt.run_id,
                    timeout_seconds=timeout_seconds,
                )
            return self._legacy_result_from_run(run)
        except OrchestrationError as exc:
            return {
                "success": False,
                "error_code": exc.code,
                "error": exc.message,
                "details": exc.details,
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
            return f"feishu:{request_id}"
        return f"{source}:{actor.actor_id}:{command_type}:{request_id}"

    def _save_assistant_message(self, conversation_id: str, content: str) -> None:
        try:
            self.memory.save_message(conversation_id, "assistant", content)
        except Exception:
            pass


def _waiting_message(status: str) -> str:
    messages = {
        RunStatus.WAITING_APPROVAL.value: "计划正在等待管理员审批",
        RunStatus.NEEDS_CLARIFICATION.value: "运行需要补充信息",
        RunStatus.BLOCKED_LOGIN.value: "运行正在等待账号登录恢复",
        RunStatus.BLOCKED_DATA.value: "来源数据不完整，运行已阻塞",
        RunStatus.FAILED_RETRYABLE.value: "运行失败但可以安全重试",
    }
    return messages.get(status, "运行未完成")
