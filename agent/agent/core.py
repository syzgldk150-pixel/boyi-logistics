"""Agent 核心循环：消息 → LLM → 工具 → 结果"""

import asyncio
import json
import time
import logging
from collections.abc import Callable, Mapping
from typing import Any, Optional

from agent.direct_tool_router import (
    direct_tool_request_from_text,
    format_tool_reply,
    parse_login_send_code_session,
)
from agent.llm_client import LLMClient
from agent.tool_registry import ToolRegistry
from agent.tool_executor import ToolExecutor
from agent.memory import Memory
from shared.redaction import redact_sensitive, redact_text

logger = logging.getLogger("agent")

MAX_TOOL_ROUNDS = 3  # 防止死循环
UNKNOWN_EXECUTION_REPLY = "没有匹配到可执行脚本，我不知道该执行哪个任务。"


class AgentCore:
    def __init__(self, *, direct_tool_runners: Mapping[str, Callable[[dict], dict]] | None = None):
        self.llm = LLMClient()
        self.registry = ToolRegistry()
        self.executor = ToolExecutor()
        self.memory = Memory()
        self._feishu_connected = False
        self._system_prompt: str = ""
        self._tool_selection_prompt: str = ""
        self._business_rules: str = ""
        self._direct_tool_runners: dict[str, Callable[[dict], dict]] = dict(direct_tool_runners or {})

    async def init(self):
        """初始化：加载 prompts，连接 MySQL"""
        self._load_prompts()
        try:
            self.memory.init()
        except Exception as e:
            logger.error("MySQL 连接失败: %s — 对话记忆将不可用", e)

    def reload_runtime_config(self) -> dict:
        """重新加载 prompts 和工具注册表"""
        self._load_prompts()
        self.registry.load()
        return {
            "prompts": ["system.md", "tool_selection.md", "business_rules.md"],
            "tools": self.registry.list_tools(),
        }

    def _load_prompts(self):
        """从 prompts/ 目录加载"""
        import os
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")

        for attr, filename in [
            ("_system_prompt", "system.md"),
            ("_tool_selection_prompt", "tool_selection.md"),
            ("_business_rules", "business_rules.md"),
        ]:
            path = os.path.join(base, filename)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    setattr(self, attr, f.read())
                logger.info("加载 prompt: %s", filename)

    async def _execute_tool_config(self, tool_name: str, tool_config: dict, params: dict) -> dict:
        direct_runner = self._direct_tool_runners.get(tool_name)
        if direct_runner is not None:
            start = time.time()
            try:
                payload = await asyncio.to_thread(direct_runner, dict(params or {}))
            except Exception as exc:
                duration = round(time.time() - start, 2)
                safe_error = redact_text(exc)
                logger.error("tool=%s | direct_error=%s | duration=%ss", tool_name, safe_error[:200], duration)
                return {"success": False, "error": safe_error, "duration_s": duration}

            duration = round(time.time() - start, 2)
            if isinstance(payload, dict) and payload.get("error"):
                safe_payload = redact_sensitive(payload)
                failure = {
                    "success": False,
                    "error": safe_payload["error"],
                    "data": safe_payload,
                    "duration_s": duration,
                }
                if payload.get("error_code"):
                    failure["error_code"] = payload.get("error_code")
                logger.error(
                    "tool=%s | success=false | direct_error=%s | duration=%ss",
                    tool_name,
                    redact_text(payload["error"])[:300],
                    duration,
                )
                return failure
            logger.info("tool=%s | success=true | direct=true | duration=%ss", tool_name, duration)
            return {"success": True, "data": payload, "duration_s": duration}

        return await self.executor.execute(tool_config, params)

    async def handle_message(
        self,
        message: str,
        user_id: str = "unknown",
        conversation_id: Optional[str] = None,
    ) -> dict:
        """
        处理用户消息，返回 Agent 回复。
        支持多轮工具调用（最多 MAX_TOOL_ROUNDS 轮）。
        """
        start = time.time()

        # 获取或创建对话
        try:
            conv_id = self.memory.get_or_create_conversation(user_id, conversation_id)
        except Exception:
            conv_id = conversation_id or "temp"

        # 加载对话历史
        try:
            history = self.memory.get_recent_messages(conv_id, limit=10)
        except Exception:
            history = []

        # 搜索知识库
        knowledge_context = ""
        try:
            knowledge = self.memory.search_knowledge(message, limit=3)
            if knowledge:
                knowledge_context = "\n\n相关知识：\n" + "\n".join(
                    f"- [{k['category']}] {k['content']}" for k in knowledge
                )
        except Exception:
            pass

        # 组装系统 prompt
        system_content = self._system_prompt
        if self._tool_selection_prompt:
            system_content += "\n\n" + self._tool_selection_prompt
        if self._business_rules:
            system_content += "\n\n" + self._business_rules
        if knowledge_context:
            system_content += knowledge_context

        # 组装消息
        messages = [{"role": "system", "content": system_content}]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        # 保存用户消息
        try:
            self.memory.save_message(conv_id, "user", message)
        except Exception:
            pass

        direct_request = direct_tool_request_from_text(message)
        if direct_request:
            tool_name = direct_request["tool_name"]
            params = direct_request["params"]
            local_result = direct_request.get("local_result")
            if isinstance(local_result, dict):
                tool_result = local_result
            else:
                tool_result = await self.execute_tool(tool_name, params)
            final_content = format_tool_reply(tool_name, tool_result)
            try:
                self.memory.save_message(conv_id, "assistant", final_content)
            except Exception:
                pass
            duration = round(time.time() - start, 2)
            logger.info("user=%s | intent=direct_tool:%s | duration=%ss | conv=%s", user_id, tool_name, duration, conv_id)
            return {
                "reply": final_content,
                "conversation_id": conv_id,
                "duration_s": duration,
                "executed_tools": [
                    {"tool_name": tool_name, "params": params, "result": tool_result}
                ],
            }

        if parse_login_send_code_session(message):
            final_content = "1. 大祥账号\n2. 操作场账号\n3. 韵达账号"
            try:
                self.memory.save_message(conv_id, "assistant", final_content)
            except Exception:
                pass
            duration = round(time.time() - start, 2)
            logger.info("user=%s | intent=login_choice | duration=%ss | conv=%s", user_id, duration, conv_id)
            return {
                "reply": final_content,
                "conversation_id": conv_id,
                "duration_s": duration,
            }

        # 获取工具 schema
        tools = self.registry.get_openai_tools() or None

        # 多轮工具调用循环
        final_content = ""
        executed_tool_calls = 0
        executed_tool_results: list[tuple[str, dict, dict]] = []
        for round_idx in range(MAX_TOOL_ROUNDS + 1):
            llm_result = await self.llm.chat(messages, tools=tools)

            content = llm_result.get("content", "")
            tool_calls = llm_result.get("tool_calls")

            if not tool_calls:
                # 没有真实工具调用时，不允许 LLM 自由回答或编造执行结果。
                if executed_tool_calls == 0:
                    final_content = UNKNOWN_EXECUTION_REPLY
                    logger.warning(
                        "blocked llm reply without tool call | user=%s | conv=%s | message=%s",
                        user_id,
                        conv_id,
                        message[:120],
                    )
                else:
                    final_content = "\n\n".join(
                        format_tool_reply(tool_name, tool_result)
                        for tool_name, _params, tool_result in executed_tool_results
                    )
                break

            # 有工具调用，执行工具
            logger.info("user=%s | round=%d | tool_calls=%s", user_id, round_idx + 1,
                        [tc["function"]["name"] for tc in tool_calls])

            # 将 assistant 消息（含 tool_calls）加入上下文
            assistant_msg = {"role": "assistant", "content": content}
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function", "function": tc["function"]}
                for tc in tool_calls
            ]
            messages.append(assistant_msg)

            # 执行每个工具
            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    func_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    func_args = {}

                tool_config = self.registry.get_tool(func_name)
                if not tool_config:
                    tool_result = {"success": False, "error": f"未知工具: {func_name}"}
                else:
                    tool_result = await self._execute_tool_config(func_name, tool_config, func_args)
                    executed_tool_calls += 1
                    executed_tool_results.append((func_name, func_args, tool_result))

                # 记录工具日志
                duration_ms = int(tool_result.get("duration_s", 0) * 1000)
                try:
                    msg_id = self.memory.save_message(conv_id, "assistant", content, tool_calls)
                    self.memory.save_tool_log(
                        conv_id, msg_id, func_name, func_args, tool_result,
                        tool_result.get("success", False), duration_ms,
                    )
                except Exception:
                    logger.warning("保存工具日志失败 tool=%s conv=%s", func_name, conv_id, exc_info=True)

                # 将工具结果加入上下文
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result, ensure_ascii=False)[:5000],
                })

        if not final_content and executed_tool_results:
            final_content = "\n\n".join(
                format_tool_reply(tool_name, tool_result)
                for tool_name, _params, tool_result in executed_tool_results
            )

        # 保存最终回复
        try:
            self.memory.save_message(conv_id, "assistant", final_content)
        except Exception:
            pass

        duration = round(time.time() - start, 2)
        logger.info("user=%s | intent=chat | duration=%ss | conv=%s", user_id, duration, conv_id)

        return {
            "reply": final_content,
            "conversation_id": conv_id,
            "duration_s": duration,
            "executed_tools": [
                {"tool_name": tool_name, "params": params, "result": tool_result}
                for tool_name, params, tool_result in executed_tool_results
            ],
        }

    async def execute_tool(self, tool_name: str, params: dict) -> dict:
        """直接执行工具（定时任务/手动触发）"""
        tool_config = self.registry.get_tool(tool_name)
        if not tool_config:
            return {"success": False, "error": f"未知工具: {tool_name}"}
        result = await self._execute_tool_config(tool_name, tool_config, params)
        try:
            duration_ms = int(result.get("duration_s", 0) * 1000)
            self.memory.save_tool_log(
                conversation_id=None,
                message_id=None,
                tool_name=tool_name,
                params=params,
                result=result,
                success=result.get("success", False),
                duration_ms=duration_ms,
            )
        except Exception:
            logger.warning("保存直接执行工具日志失败 tool=%s", tool_name, exc_info=True)
        return result

    async def cancel_tool(self, tool_name: str, started_at: str = "") -> dict:
        """取消正在运行的工具执行。"""
        return await self.executor.cancel_tool(tool_name, started_at=started_at)

    def is_tool_running(self, tool_name: str) -> bool:
        """返回工具脚本是否正在执行。"""
        return self.executor.is_tool_running(tool_name)

    def running_tool_info(self, tool_name: str) -> dict:
        """返回正在执行的工具脚本元数据。"""
        return self.executor.running_tool_info(tool_name)

    def running_tools(self) -> list[str]:
        """返回当前正在执行的工具脚本列表。"""
        return self.executor.running_tools()

    # ── 状态查询方法（供 health endpoint 使用） ─────────
    def feishu_status(self) -> str:
        return "connected" if self._feishu_connected else "disconnected"

    def set_feishu_connected(self, connected: bool):
        self._feishu_connected = connected

    def llm_status(self, provider: str) -> str:
        return self.llm.status(provider)

    def db_status(self) -> str:
        return self.memory.status()

    def last_tool_info(self) -> dict | None:
        return self.executor.last_tool_info()

    def heavy_lock_held(self) -> bool:
        return self.executor.heavy_lock_held()

    async def close(self):
        await self.llm.close()
