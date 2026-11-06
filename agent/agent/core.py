"""Composition facade for the common chat engine and direct business readers.

Registered readers return data directly. Installed plugins are invoked through
the signed plugin interface.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from typing import Any, Optional

from agent.llm_client import LLMClient
from agent.direct_readers import invoke_registered_reader
from agent.llm_settings import LLMSettingsRepository
from agent.memory import Memory
from agent.orchestration.models import Actor
from agent.tool_registry import ToolRegistry


logger = logging.getLogger("agent")
MAX_TOOL_ROUNDS = 3
UNKNOWN_EXECUTION_REPLY = "没有匹配到可执行脚本，我不知道该执行哪个任务。"


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
        self._feishu_connected = False
        # Closed host readers are direct requests. Plugins use their dedicated
        # signed invocation entrypoints and never the historical Command path.
        self._direct_tool_runners = dict(direct_tool_runners or {})
        self._direct_invocations = None
        self._plugin_conversations = None
        self._today_provider = today_provider or (
            lambda: dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        )
        self._execution_runtime: Any | None = None

    def configure_direct_readers(self, readers: Mapping[str, Callable[[dict], dict]], *, invocations=None) -> None:
        self._direct_tool_runners.update(readers)
        if invocations is not None:
            self._direct_invocations = invocations

    def configure_execution_runtime(self, execution_runtime: Any) -> None:
        """Bind the plugin execution router built by ``main.py``, the sole composition root."""

        self._execution_runtime = execution_runtime

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

    async def cancel_tool(self, tool_name: str, started_at: str = "") -> dict[str, Any]:
        """Old tool-name cancellation is intentionally disabled; cancel the Invocation."""

        del tool_name, started_at
        return {
            "ok": False,
            "error_code": "RUN_ID_REQUIRED",
            "error": "cancel the plugin invocation by its invocation ID",
        }

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
