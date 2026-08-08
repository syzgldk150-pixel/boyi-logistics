"""LLM 客户端：DeepSeek-V3 主力 + GLM-4-Flash 备用，OpenAI 兼容格式"""

import os
import time
import logging
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger("agent")

# 供应商配置
PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
        "timeout": 30,
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "env_key": "GLM_API_KEY",
        "timeout": 30,
    },
}


class LLMClient:
    def __init__(self):
        self._clients: dict[str, AsyncOpenAI] = {}
        self._last_call: dict[str, float] = {}
        self._last_error: dict[str, str] = {}

        for name, cfg in PROVIDERS.items():
            api_key = os.getenv(cfg["env_key"], "")
            if api_key:
                self._clients[name] = AsyncOpenAI(
                    api_key=api_key,
                    base_url=cfg["base_url"],
                    timeout=cfg["timeout"],
                )
                logger.info("LLM 供应商 %s 已配置", name)
            else:
                logger.warning("LLM 供应商 %s 未配置 API Key (%s)", name, cfg["env_key"])

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        provider: str = "deepseek",
    ) -> dict:
        """
        调用 LLM，返回 OpenAI 格式的 response message。
        主用 deepseek，失败自动降级到 glm。
        """
        # 尝试顺序：主供应商 → 备用
        order = [provider] + [p for p in PROVIDERS if p != provider]

        for name in order:
            if name not in self._clients:
                continue
            try:
                result = await self._call(name, messages, tools)
                self._last_call[name] = time.time()
                self._last_error.pop(name, None)
                return result
            except Exception as e:
                self._last_error[name] = str(e)
                logger.warning(
                    "LLM %s 调用失败: %s，尝试降级", name, str(e)[:200]
                )
                continue

        raise RuntimeError("所有 LLM 供应商均不可用")

    async def _call(
        self,
        provider: str,
        messages: list[dict],
        tools: Optional[list[dict]],
    ) -> dict:
        cfg = PROVIDERS[provider]
        kwargs = {
            "model": cfg["model"],
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        client = self._clients[provider]
        response = await client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        # 统一输出格式
        result = {
            "role": "assistant",
            "content": msg.content or "",
            "provider": provider,
            "model": cfg["model"],
        }

        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        return result

    def status(self, provider: str) -> str:
        if provider not in self._clients:
            return "not_configured"
        last = self._last_call.get(provider)
        err = self._last_error.get(provider)
        if err:
            return f"error | {err[:80]}"
        if last:
            ago = int(time.time() - last)
            return f"ok | last_call: {ago}s ago"
        return "standby"

    async def close(self):
        for client in self._clients.values():
            await client.close()
