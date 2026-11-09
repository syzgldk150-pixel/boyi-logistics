"""Run one closed AI task with the single globally active model and audit it.

Flow: redact the caller's payload, send it with the task's system prompt,
parse the JSON answer and run the task validator. An answer that fails
validation is sent back once more with the error (``max_retries``); provider
errors are not retried. Every run, successful or not, is written to
``ai_task_runs``; a successful answer is returned only after its audit row
is stored. Nothing here saves business data or calls a tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Any, Mapping

from shared.ai_tasks import AITaskError, AITaskResult, AITaskSpec
from shared.redaction import redact_sensitive, redact_text

logger = logging.getLogger("agent")

_RETRY_PROMPT = "上一次的输出不符合要求：{error}。请修正后重新输出，只返回 JSON。"


class AITaskGateway:
    def __init__(self, llm: Any, audit: Any | None = None, *, clock=time.monotonic) -> None:
        self._llm = llm
        self._audit = audit
        self._clock = clock

    async def run(
        self,
        spec: AITaskSpec,
        payload: Mapping[str, Any],
        *,
        subject_ref: str,
        actor_id: str | None = None,
    ) -> AITaskResult:
        if not isinstance(spec, AITaskSpec):
            raise TypeError("spec must be an AITaskSpec")
        if spec.model_override is not None:
            raise AITaskError("AI_MODEL_OVERRIDE_UNSUPPORTED", "当前只支持使用已启用的全局模型。")
        run_id = str(uuid.uuid4())
        started = self._clock()
        audit: dict[str, Any] = {
            "run_id": run_id, "task_id": spec.task_id, "prompt_version": spec.prompt_version,
            "subject_ref": str(subject_ref), "actor_id": actor_id, "attempts": 0,
        }
        runtime = self._llm.public_status()
        if not runtime.get("configured"):
            await self._finish(audit, started, status="UNAVAILABLE", error_code="AI_UNAVAILABLE")
            raise AITaskError("AI_UNAVAILABLE", "尚未启用 AI 模型，请先在“智能模型”中启用。", run_id=run_id)
        audit.update(provider=runtime.get("provider"), model=runtime.get("model"),
                     config_version_id=runtime.get("config_version_id"))
        user_text = json.dumps(redact_sensitive(dict(payload)), ensure_ascii=False, sort_keys=True, default=str)
        audit["input_sha256"] = hashlib.sha256(user_text.encode("utf-8")).hexdigest()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": spec.system_prompt},
            {"role": "user", "content": user_text},
        ]
        last_error = ""
        for attempt in range(1, spec.max_retries + 2):
            audit["attempts"] = attempt
            try:
                response = await asyncio.wait_for(
                    self._llm.chat(
                        messages,
                        provider=str(runtime["provider"]),
                        expected_model=str(runtime["model"]),
                        expected_config_version_id=runtime.get("config_version_id"),
                        response_format={"type": "json_object"},
                    ),
                    timeout=float(spec.timeout_seconds),
                )
            except Exception as exc:  # noqa: BLE001 - provider errors never reach the page verbatim
                reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else (str(exc) or type(exc).__name__)
                logger.warning("AI task %s failed: %s", spec.task_id, redact_text(reason)[:200])
                await self._finish(audit, started, status="FAILED", error_code="AI_FAILED",
                                   error_message=redact_text(reason))
                raise AITaskError("AI_FAILED", "AI 暂时无法生成结果，请稍后重试或直接人工处理。", run_id=run_id) from exc
            content = str(response.get("content") or "")
            audit["output_text"] = redact_text(content)
            try:
                result = spec.validate(json.loads(content))
            except (ValueError, TypeError) as exc:
                last_error = str(exc) or type(exc).__name__
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": _RETRY_PROMPT.format(error=last_error)}]
                continue
            audit["result"] = result
            await self._finish(audit, started, status="SUCCEEDED", required=True)
            return AITaskResult(run_id=run_id, result=result, provider=str(runtime["provider"]),
                                model=str(runtime["model"]), config_version_id=runtime.get("config_version_id"),
                                attempts=attempt)
        await self._finish(audit, started, status="INVALID", error_code="AI_INVALID", error_message=last_error)
        raise AITaskError("AI_INVALID", "AI 返回的结果不符合要求，未保存。", run_id=run_id, detail=last_error)

    async def _finish(self, audit: dict[str, Any], started: float, *, status: str, required: bool = False,
                      error_code: str | None = None, error_message: str | None = None) -> None:
        audit.update(status=status, error_code=error_code, error_message=error_message,
                     duration_ms=max(0, int((self._clock() - started) * 1000)))
        if self._audit is None:
            return
        try:
            await asyncio.to_thread(self._audit.record_run, dict(audit))
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI task audit write failed: %s", redact_text(str(exc) or type(exc).__name__)[:200])
            if required:
                raise AITaskError("AI_AUDIT_FAILED", "AI 结果未能记录，未返回结果，请稍后重试。",
                                  run_id=audit["run_id"]) from exc


__all__ = ["AITaskGateway"]
