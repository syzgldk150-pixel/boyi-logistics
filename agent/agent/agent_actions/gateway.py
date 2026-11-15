"""The single checkpoint every model-chosen action passes (agent layer P1).

Five checks in order — identity, capability, object (arguments), data, audit —
end in exactly one of: EXECUTE, CONFIRM (the person confirms first), REVIEW
(a super admin approves first; P4), CLARIFY (ask the person back), REJECT.

The gateway never runs anything. Channels run EXECUTE decisions through their
existing paths (which re-check permissions and still apply the identity's
"需确认" level), and run CONFIRM decisions only after the same person confirms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from agent.agent_actions.grounding import GroundingError, ground_inputs
from agent.agent_actions.spec import ActionSpec

# A message that asks about something rather than asking for it to be done.
# A writing action chosen for such a message is never run without a confirm.
_QUESTION_LIKE = re.compile(
    r"[?？]|吗|呢|什么|怎么|为什么|为何|如何|多少|几票|几件|几吨|哪些|哪个|是否|有没有|能不能|可不可以|规则|依据|"
    r"原因|失败|不要|不用|别(?:给我|帮我)?(?:扫|执行|运行|上传|同步)|先看|看看|查询|查一下|查看|查下|^(?:请|帮我)?查|"
    r"扫描量|吨位|口径"
)


def looks_like_question(message: str) -> bool:
    return bool(_QUESTION_LIKE.search(str(message or "")))


@dataclass(frozen=True)
class GatewayDecision:
    decision: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    question: str = ""
    reason: str = ""
    error_code: str | None = None

    @property
    def runs(self) -> bool:
        return self.decision in {"EXECUTE", "CONFIRM"}


class ActionGateway:
    def __init__(self, *, identity_access: Any = None) -> None:
        self._identity_access = identity_access

    def decide(self, spec: ActionSpec, *, actor: Any, arguments: Mapping[str, Any] | None,
               message: str) -> GatewayDecision:
        # 1 identity and 2 capability: read live on every decision.
        if self._identity_access is not None:
            access = self._identity_access.for_actor(actor)
            if not access.active:
                return GatewayDecision("REJECT", reason="当前身份已停用", error_code="IDENTITY_INACTIVE")
            if not access.allows(spec.capability):
                if getattr(access, "blocked_in_group", lambda _: False)(spec.capability):
                    return GatewayDecision("REJECT", reason="这项内容只在私聊里回答", error_code="PRIVATE_CHAT_ONLY")
                return GatewayDecision("REJECT", reason=f"当前身份没有“{spec.title}”的权限", error_code="PERMISSION_DENIED")
        # 3 object: every argument tied to the person's own words.
        supplied = dict(arguments or {})
        if spec.input_schema is None:
            if supplied:
                return GatewayDecision("REJECT", reason="这个操作不接受 AI 填写的条件", error_code="ARGUMENTS_FORBIDDEN")
            grounded: dict[str, Any] = {}
        else:
            try:
                grounded = ground_inputs(spec.input_schema, supplied, message)
            except GroundingError as exc:
                return GatewayDecision("CLARIFY", question=exc.question, error_code=exc.code)
        # 4 data: internal colleagues only; the row-scope slot stays unused (decision 1).
        # 5 audit happens in the caller before any EXECUTE/CONFIRM write is run.
        if spec.writes and not spec.preview and (grounded or looks_like_question(message)):
            # Model-filled write, or a write chosen for a question: always confirm.
            return GatewayDecision("CONFIRM", arguments=grounded,
                                   reason="参数由 AI 从你的话里提取" if grounded else "你的话更像是在提问")
        return GatewayDecision("EXECUTE", arguments=grounded)


__all__ = ["ActionGateway", "GatewayDecision", "looks_like_question"]
