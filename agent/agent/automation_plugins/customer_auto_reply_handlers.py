"""Reviewed Host primitives for authorized automatic customer-service replies (AI phase 4).

The plugin only picks *which* problems to try. Everything that decides whether
a reply may be sent, and what it says, is re-derived here from Host state:

* ``candidates`` (read): open problems that never had an automatic reply.
* ``draft`` (read; stores only advice and its audit): AI advice for one problem, plus whether it qualifies
  (template authorized for automatic replies, reply follows it, confidence).
* ``reply`` (external write): takes only a ``suggestion_id``. The Host reloads
  the stored advice and re-checks authorization, resolves the problem's single
  bound account (which must belong to this invocation's account collection),
  reads the platform first and skips if a reply already exists, claims the
  one-per-problem record, sends, and reads back. A reply that cannot be read
  back ends as ``WRITE_OUTCOME_UNKNOWN`` and is never retried.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.connector_namespaces import CUSTOMER_AUTO_REPLY_TOOL

ROLE = "customer_service_source"
CANDIDATES_KEY = ("projection.invoke", "customer_auto_reply.candidates")
DRAFT_KEY = ("projection.invoke", "customer_auto_reply.draft")
REPLY_KEY = ("browser.invoke", "customer_auto_reply.reply")
MARKED_WRITE_ACTION_KEYS = frozenset({REPLY_KEY})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REPLY_KEYS = {"reply_text", "reversion", "deal_result", "reply_content"}


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _require_context(context: CoreBrokerInvocationContext) -> None:
    if context.tool_name != CUSTOMER_AUTO_REPLY_TOOL or context.role != ROLE:
        raise _error("broker action is not valid for this signed tool role", "BROKER_CONTEXT_INVALID")


def _strict(arguments: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping) or set(arguments) - allowed:
        raise _error("primitive arguments are not closed", "BROKER_ARGUMENT_INVALID")
    return dict(arguments)


def _normalize(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _reply_values(value: Any, found: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _REPLY_KEYS and isinstance(item, str) and item.strip():
                found.append(item)
            else:
                _reply_values(item, found)
    elif isinstance(value, list):
        for item in value:
            _reply_values(item, found)


def existing_replies(detail: Any) -> list[str]:
    """Reply texts a platform detail already carries (placeholders such as “暂无回复” excluded)."""
    found: list[str] = []
    _reply_values(detail, found)
    return [item for item in found if _normalize(item) not in {"", "0", "-", "无", "暂无", "暂无回复", "无回复", "未回复"}]


@dataclass(frozen=True)
class CustomerAutoReplyPorts:
    describe_account: Callable[[str], Mapping[str, Any]]
    customer_action: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    service: Callable[[], Any]
    """Returns the bound ``CustomerServiceAIService`` or ``None`` until the Agent finished starting."""


class _Handlers:
    def __init__(self, ports: CustomerAutoReplyPorts, *, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("customer auto-reply secret must contain at least 32 bytes")
        self._ports = ports
        self._secret = bytes(secret)

    def _evidence(self, context: CoreBrokerInvocationContext, purpose: str, payload: Mapping[str, Any]) -> str:
        material = json.dumps({"automation_id": context.automation_id, "purpose": purpose, "payload": dict(payload)},
                              sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        return f"broker-evidence:customer-auto-reply:{purpose}:v1:" + hmac.new(self._secret, material, hashlib.sha256).hexdigest()

    def _service(self) -> Any:
        service = self._ports.service()
        if service is None:
            raise _error("customer-service AI is not available", "BROKER_ACTION_UNAVAILABLE")
        return service

    def candidates(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]):
        _require_context(context)
        values = _strict(arguments, {"max_items"})
        limit = values.get("max_items", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise _error("max_items is invalid", "BROKER_ARGUMENT_INVALID")
        service = self._service()

        async def run() -> Mapping[str, Any]:
            items = await service.auto_candidates(limit=limit)
            public = [{key: str(item[key]) for key in ("source_id", "external_id", "source_direction", "platform", "waybill_no")}
                      for item in items]
            return {"items": public, "evidence_ref": self._evidence(context, "candidates", {"count": len(public)})}
        return run()

    def draft(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]):
        _require_context(context)
        values = _strict(arguments, {"source_id", "external_id", "source_direction"})
        service = self._service()

        async def run() -> Mapping[str, Any]:
            try:
                result = await service.auto_draft(str(values.get("source_id") or ""), str(values.get("external_id") or ""),
                                                  str(values.get("source_direction") or ""))
            except Exception as exc:  # advice failure is a per-item outcome, not a broken run
                code = str(getattr(exc, "code", "") or "CS_AI_FAILED")
                return {"eligible": False, "reason": code[:64], "suggestion_id": "",
                        "evidence_ref": self._evidence(context, "draft", {"failed": code[:64]})}
            public = {"eligible": bool(result["eligible"]), "reason": str(result["reason"])[:200],
                      "suggestion_id": str(result["suggestion_id"]), "scenario_id": str(result.get("scenario_id") or ""),
                      "confidence": str(result.get("confidence") or "")}
            return {**public, "evidence_ref": self._evidence(context, "draft", public)}
        return run()

    def reply(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]):
        _require_context(context)
        values = _strict(arguments, {"suggestion_id"})
        suggestion_id = str(values.get("suggestion_id") or "")
        if not _UUID.fullmatch(suggestion_id):
            raise _error("suggestion_id is invalid", "BROKER_ARGUMENT_INVALID")
        service = self._service()
        ports = self._ports

        def platform_call(plan: Mapping[str, Any], descriptor: Mapping[str, Any], action: str) -> Mapping[str, Any]:
            key = plan["key"]
            request: dict[str, Any] = {
                "platform": plan["platform"], "account_id": plan["account_id"], "account_label": plan["account_id"],
                "session_profile": str(descriptor.get("session_profile") or ""), "action": action,
                "direction": key[2],
                "item": {"platform": plan["platform"], "external_id": key[1], "source_direction": key[2],
                         "waybill_no": plan["waybill_no"], "status": plan["status"]},
            }
            if action == "reply":
                request["payload"] = {"reply_text": plan["reply_text"], "prob_status": "已处理",
                                      "old_prob_status": plan["status"]}
            raw = ports.customer_action(request)
            if not isinstance(raw, Mapping) or raw.get("ok") is not True:
                code = str(raw.get("error_code") or "BROKER_SOURCE_FAILED") if isinstance(raw, Mapping) else "BROKER_SOURCE_INVALID"
                raise _error(f"customer {action} failed", code[:64])
            return raw

        async def run() -> Mapping[str, Any]:
            try:
                plan = await service.auto_reply_plan(suggestion_id)
            except Exception as exc:
                raise _error("automatic reply is not authorized", str(getattr(exc, "code", "") or "AUTO_NOT_ALLOWED")[:64]) from exc
            if plan["account_id"] not in context.account_ids:
                raise _error("problem account is outside this invocation's bound accounts", "BROKER_ACCOUNT_MISMATCH")
            descriptor = await asyncio.to_thread(ports.describe_account, plan["account_id"])
            if (not isinstance(descriptor, Mapping) or str(descriptor.get("account_id") or "") != plan["account_id"]
                    or str(descriptor.get("system") or "").lower() != plan["platform"]):
                raise _error("account binding changed", "BROKER_ACCOUNT_MISMATCH")
            before = await asyncio.to_thread(platform_call, plan, descriptor, "detail")
            if existing_replies(before):
                # Someone already answered on the platform: record it and write nothing.
                await service.auto_reply_begin(plan, status="skipped")
                proof = {"suggestion_id": suggestion_id, "skipped": "already_replied"}
                return {"sent": False, "confirmed": False, "skipped": "already_replied",
                        "evidence_ref": self._evidence(context, "reply", proof)}
            await service.auto_reply_begin(plan)  # one automatic reply per problem, claimed before sending
            if context.mark_write_started is not None:
                context.mark_write_started()
            try:
                await asyncio.to_thread(platform_call, plan, descriptor, "reply")
                after = await asyncio.to_thread(platform_call, plan, descriptor, "detail")
            except Exception as exc:
                await service.auto_reply_finish(plan, "unknown")
                raise _error("automatic reply outcome is unknown", "WRITE_OUTCOME_UNKNOWN") from exc
            expected = _normalize(plan["reply_text"])
            if not any(expected in _normalize(item) for item in existing_replies(after)):
                await service.auto_reply_finish(plan, "unknown")
                raise _error("automatic reply was not found on the platform", "WRITE_OUTCOME_UNKNOWN")
            await service.auto_reply_finish(plan, "confirmed")
            proof = {"suggestion_id": suggestion_id, "reply_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest()}
            return {"sent": True, "confirmed": True, "skipped": "", "evidence_ref": self._evidence(context, "reply", proof)}
        return run()

    def handler_map(self) -> dict[tuple[str, str], Any]:
        return {CANDIDATES_KEY: self.candidates, DRAFT_KEY: self.draft, REPLY_KEY: self.reply}


def build_customer_auto_reply_handler_map(ports: CustomerAutoReplyPorts, *, cursor_secret: bytes) -> dict[tuple[str, str], Any]:
    return _Handlers(ports, secret=cursor_secret).handler_map()


__all__ = [
    "CANDIDATES_KEY", "CustomerAutoReplyPorts", "DRAFT_KEY", "MARKED_WRITE_ACTION_KEYS", "REPLY_KEY",
    "build_customer_auto_reply_handler_map", "existing_replies",
]
