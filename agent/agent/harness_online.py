"""Bounded online model loop for the read-only AI assistant.

This module talks only to the already active :class:`LLMClient` and to a
closed :class:`HarnessToolCatalog`.  It never launches scripts, opens files,
or falls back to an offline model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, Mapping, Sequence

from agent.harness.catalog import HarnessToolCatalog, ToolDescriptor
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage, strict_json
from agent.harness.sidecar import SidecarResult
from agent.llm_client import LLMClient
from shared.redaction import is_sensitive_key, redact_text


_MAX_TOOL_CALLS = 8
_MAX_TIMEOUT_SECONDS = 30
_CHINESE_TEXT = re.compile(r"[\u3400-\u9fff]")
_PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_LONG_HASH = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_LABELED_ADDRESS = re.compile(
    r"(?:收货|发货|联系|详细)?地址\s*[:：]?\s*[^\s，。；;]{4,80}"
)
_LABELED_ACCOUNT = re.compile(r"(?:业务)?账号(?:标识)?\s*[:：]?\s*[^\s，。；;]{2,80}")
_PRIVATE_MODEL_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "account",
    "path",
    "storage",
    "hash",
    "raw",
    "internal",
)
_SYSTEM_PROMPT = """你是博益物流的 AI 助手。请始终使用简明中文回答。
你只能进行自然对话，或调用系统提供的只读查询。不得建议或声称已经执行审批、回单、财务、配置修改等写操作。
查询运单或物流轨迹必须使用用户提供的完整编号；缺少编号时先询问，不能猜测候选。
查询运行结果或证据也必须使用用户提供的精确编号；无法唯一确定时先询问。
工具返回无数据、不可用或超时时，要如实说明并给出下一步建议，不能编造结果。
不要输出内部工具名、数据库字段名、存储位置、内部路径、密钥、令牌、哈希或原始错误详情。
一次回答可以组合多个只读查询，但应只调用完成问题所需的最少工具。"""

_FIXED_MODEL_NAMES = {
    "knowledge.search": "query_knowledge",
    "waybill.lookup": "query_waybill",
    "tracking.lookup": "query_tracking",
    "work_items.list_open": "query_open_items",
    "runs.get_summary": "query_run_result",
    "artifact.inspect": "query_evidence",
}


def _error(message: str, code: str) -> HarnessError:
    return HarnessError(message, code=code)


def _minimize_text(value: Any) -> str:
    text = redact_text(value)
    text = _PHONE.sub("[手机号已隐藏]", text)
    text = _EMAIL.sub("[邮箱已隐藏]", text)
    text = _LABELED_ADDRESS.sub("地址：[已隐藏]", text)
    text = _LABELED_ACCOUNT.sub("账号：[已隐藏]", text)
    return _LONG_HASH.sub("[标识已隐藏]", text)


def _minimize_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "内容层级过深，已省略"
    if isinstance(value, Mapping):
        minimized: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = str(raw_key).strip()
            lowered = key.lower()
            if (
                not key
                or is_sensitive_key(key)
                or any(part in lowered for part in _PRIVATE_MODEL_KEY_PARTS)
            ):
                continue
            minimized[_minimize_text(key)[:80]] = _minimize_value(
                nested,
                depth=depth + 1,
            )
        return minimized
    if isinstance(value, (list, tuple)):
        return [_minimize_value(item, depth=depth + 1) for item in value[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _minimize_text(value)[:2000]


def _is_chinese_descriptor(descriptor: ToolDescriptor) -> bool:
    return bool(
        _CHINESE_TEXT.search(descriptor.title)
        and _CHINESE_TEXT.search(descriptor.description)
    )


def visible_descriptors(catalog: HarnessToolCatalog) -> tuple[ToolDescriptor, ...]:
    """Return only localized tools that passed catalog governance."""

    return tuple(
        descriptor
        for descriptor in catalog.descriptors()
        if _is_chinese_descriptor(descriptor)
    )


def _model_tool_name(descriptor: ToolDescriptor) -> str:
    fixed = _FIXED_MODEL_NAMES.get(descriptor.tool_id)
    if fixed:
        return fixed
    digest = hashlib.sha256(descriptor.tool_id.encode("utf-8")).hexdigest()[:24]
    return f"extension_{digest}"


def _model_tools(
    descriptors: Sequence[ToolDescriptor],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    tools: list[dict[str, Any]] = []
    name_to_id: dict[str, str] = {}
    for descriptor in descriptors:
        name = _model_tool_name(descriptor)
        if name in name_to_id:
            raise _error("AI 助手工具目录冲突", "HARNESS_TOOL_COLLISION")
        name_to_id[name] = descriptor.tool_id
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": descriptor.description,
                    "parameters": strict_json(
                        descriptor.input_schema,
                        field_name="AI assistant tool schema",
                    ),
                },
            }
        )
    return tools, name_to_id


def _run_chat(
    llm: LLMClient,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout_seconds: float,
) -> dict[str, Any]:
    async def _call() -> dict[str, Any]:
        return await asyncio.wait_for(
            llm.chat(messages, tools=tools),
            timeout=max(0.1, timeout_seconds),
        )

    try:
        return asyncio.run(_call())
    except asyncio.TimeoutError as exc:
        raise _error("智能模型响应超时，请稍后重试", "HARNESS_TIMEOUT") from exc
    except HarnessError:
        raise
    except RuntimeError as exc:
        status = llm.public_status()
        if not status.get("configured"):
            raise _error(
                "尚未启用智能模型，请先在智能模型页面完成配置",
                "HARNESS_MODEL_NOT_CONFIGURED",
            ) from exc
        raise _error(
            "智能模型暂时无法连接，请稍后重试",
            "HARNESS_MODEL_UNAVAILABLE",
        ) from exc
    except Exception as exc:
        raise _error(
            "智能模型暂时无法连接，请稍后重试",
            "HARNESS_MODEL_UNAVAILABLE",
        ) from exc


class OnlineHarnessSidecar:
    """Run one bounded active-model conversation with closed read-only tools."""

    def __init__(self, *, catalog: HarnessToolCatalog, llm: LLMClient) -> None:
        if not isinstance(catalog, HarnessToolCatalog):
            raise TypeError("catalog must be HarnessToolCatalog")
        if not isinstance(llm, LLMClient):
            raise TypeError("llm must be LLMClient")
        self._catalog = catalog
        self._llm = llm

    def run(
        self,
        *,
        messages: Sequence[HarnessMessage],
        timeout_seconds: int = _MAX_TIMEOUT_SECONDS,
    ) -> SidecarResult:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise _error("AI 助手超时设置无效", "HARNESS_PROTOCOL_INVALID")
        if not messages or not all(isinstance(item, HarnessMessage) for item in messages):
            raise _error("AI 助手会话内容无效", "HARNESS_PROTOCOL_INVALID")
        if not self._llm.public_status().get("configured"):
            raise _error(
                "尚未启用智能模型，请先在智能模型页面完成配置",
                "HARNESS_MODEL_NOT_CONFIGURED",
            )

        descriptors = visible_descriptors(self._catalog)
        model_tools, name_to_id = _model_tools(descriptors)
        transcript: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        transcript.extend(
            {"role": item.role, "content": _minimize_text(item.content)}
            for item in messages
        )
        started = time.monotonic()
        calls = 0

        while True:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise _error("智能模型响应超时，请稍后重试", "HARNESS_TIMEOUT")
            response = _run_chat(self._llm, transcript, model_tools, remaining)
            if not isinstance(response, Mapping):
                raise _error("智能模型返回内容无法读取", "HARNESS_PROTOCOL_INVALID")
            raw_calls = response.get("tool_calls")
            if not raw_calls:
                content = _minimize_text(response.get("content") or "").strip()
                if not content:
                    raise _error("智能模型没有返回可读内容", "HARNESS_PROTOCOL_INVALID")
                return SidecarResult(content=content, tool_calls=calls)
            if not isinstance(raw_calls, list) or not raw_calls:
                raise _error("智能模型工具请求无法读取", "HARNESS_PROTOCOL_INVALID")
            if calls + len(raw_calls) > _MAX_TOOL_CALLS:
                raise _error("本次查询调用次数过多，请缩小问题范围", "HARNESS_LIMIT_EXCEEDED")

            assistant_calls: list[dict[str, Any]] = []
            resolved_calls: list[tuple[str, str, dict[str, Any]]] = []
            for raw_call in raw_calls:
                if not isinstance(raw_call, Mapping):
                    raise _error("智能模型工具请求无法读取", "HARNESS_PROTOCOL_INVALID")
                call_id = str(raw_call.get("id") or "").strip()
                function = raw_call.get("function")
                if not call_id or not isinstance(function, Mapping):
                    raise _error("智能模型工具请求无法读取", "HARNESS_PROTOCOL_INVALID")
                name = str(function.get("name") or "").strip()
                tool_id = name_to_id.get(name)
                if tool_id is None:
                    raise _error("智能模型请求了未开放的查询", "HARNESS_TOOL_NOT_FOUND")
                raw_arguments = function.get("arguments")
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except (TypeError, ValueError) as exc:
                    raise _error("智能模型查询参数无法读取", "HARNESS_ARGUMENT_INVALID") from exc
                if not isinstance(arguments, dict):
                    raise _error("智能模型查询参数无效", "HARNESS_ARGUMENT_INVALID")
                safe_arguments = strict_json(arguments, field_name="AI assistant tool arguments")
                assistant_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                safe_arguments,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                )
                resolved_calls.append((call_id, tool_id, safe_arguments))

            transcript.append(
                {
                    "role": "assistant",
                    "content": str(response.get("content") or ""),
                    "tool_calls": assistant_calls,
                }
            )
            for call_id, tool_id, arguments in resolved_calls:
                result = self._catalog.invoke(tool_id=tool_id, arguments=arguments)
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(
                            _minimize_value(result),
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                )
                calls += 1


__all__ = ["OnlineHarnessSidecar", "visible_descriptors"]
