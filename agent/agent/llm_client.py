"""Global LLM client with an immutable per-request configuration snapshot."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from openai import AsyncOpenAI

from agent.llm_settings import (
    LLMSettingsRepository,
    PROVIDERS,
    RuntimeLLMConfig,
    environment_runtime_config,
)
from shared.redaction import redact_text


logger = logging.getLogger("agent")


class LLMClient:
    """Use exactly one selected provider/model; never switch on failure."""

    def __init__(self) -> None:
        self._repository: LLMSettingsRepository | None = None
        environment = environment_runtime_config()
        self._descriptor: dict[str, Any] | None = None if environment is None else {
            "provider": environment.provider,
            "model_id": environment.model_id,
            "source": "environment",
            "config_version_id": None,
            "credential_available": True,
        }
        self._database_activation_exists = False
        self._lock = asyncio.Lock()
        self._last_call: float | None = None
        self._last_error: str = ""

    async def bind_repository(self, repository: LLMSettingsRepository) -> None:
        self._repository = repository
        await self.reload_config()

    async def reload_config(self) -> dict[str, Any]:
        """Atomically replace the config used by future requests.

        The environment-managed configuration is an upgrade-only compatibility
        path while no database version is active.  An invalid active database
        version fails explicitly and never falls back to the environment.
        """

        async with self._lock:
            descriptor = None
            activated = False
            if self._repository is not None:
                descriptor, activated = await asyncio.to_thread(
                    self._repository.runtime_descriptor
                )
            if descriptor is None and not activated:
                environment = environment_runtime_config()
                if environment is not None:
                    descriptor = {
                        "provider": environment.provider,
                        "model_id": environment.model_id,
                        "source": "environment",
                        "config_version_id": None,
                    }
            self._descriptor = descriptor
            self._database_activation_exists = activated
            self._last_error = (
                "active provider credential is unavailable"
                if descriptor and descriptor.get("credential_available") is False
                else ""
            )
        return self.public_status()

    def config_snapshot(self) -> RuntimeLLMConfig:
        if self._repository is not None and self._database_activation_exists:
            config = self._repository.active_config()
        else:
            config = environment_runtime_config()
        if config is None:
            raise RuntimeError("no LLM configuration is active")
        return config

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        provider: str | None = None,
        *,
        expected_model: str | None = None,
        expected_config_version_id: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict:
        # Decrypt/read the key only for this request.  The long-lived client
        # stores only the non-secret descriptor.
        config = await asyncio.to_thread(self.config_snapshot)
        if provider and str(provider).strip().lower() != config.provider:
            raise RuntimeError(
                f"requested provider {provider!r} is not the globally active provider"
            )
        if expected_model is not None and str(expected_model) != config.model_id:
            raise RuntimeError("the active model changed before this request started")
        if expected_config_version_id is not None and expected_config_version_id != config.config_version_id:
            raise RuntimeError("the active configuration version changed before this request started")
        try:
            result = await self._call(config, messages, tools, response_format=response_format)
        except Exception as exc:
            self._last_error = redact_text(str(exc) or type(exc).__name__)[:200]
            logger.warning(
                "LLM request failed provider=%s model=%s error=%s",
                config.provider,
                config.model_id,
                self._last_error,
            )
            raise
        self._last_call = time.time()
        self._last_error = ""
        return result

    async def _call(
        self,
        config: RuntimeLLMConfig,
        messages: list[dict],
        tools: Optional[list[dict]],
        *,
        response_format: dict[str, Any] | None,
    ) -> dict:
        provider = PROVIDERS[config.provider]
        client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=provider["base_url"],
            timeout=provider["timeout"],
        )
        kwargs: dict[str, Any] = {
            "model": config.model_id,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format
        try:
            response = await client.chat.completions.create(**kwargs)
        finally:
            await client.close()
        if not response.choices:
            raise RuntimeError("active model returned no choices")
        message = response.choices[0].message
        result: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
            "provider": config.provider,
            "model": config.model_id,
            "config_source": config.source,
            "config_version_id": config.config_version_id,
        }
        if message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        return result

    def public_status(self) -> dict[str, Any]:
        config = self._descriptor
        return {
            "configured": config is not None,
            "provider": config.get("provider") if config else None,
            "model": config.get("model_id") if config else None,
            "source": config.get("source") if config else None,
            "config_version_id": config.get("config_version_id") if config else None,
            "last_call_at": self._last_call,
            "health": "error" if self._last_error else ("ready" if config else "not_configured"),
            "last_error": self._last_error,
        }

    def status(self, provider: str) -> str:
        status = self.public_status()
        if not status["configured"] or status["provider"] != provider:
            return "not_active"
        if status["health"] == "error":
            return f"error | {status['last_error']}"
        return "ready"

    async def close(self) -> None:
        # Clients are request-scoped so API keys only remain in memory for the
        # duration of a request.  There is no long-lived network client to close.
        return None
