"""Two permission sets per identity: Console modules and agent capabilities.

* Console (the back-office pages): each module is ``view`` or ``operate``.
  Read-only modules only have ``view``.
* Agent (Feishu, WeChat and the Console AI assistant): a write capability is
  ``direct`` or ``confirm`` (the person confirms in chat before it runs); a
  private capability (finance data) is ``private`` (one-to-one chats only)
  or ``direct`` (group chats too); other reads are always ``direct``.

Checks pass one token. ``module:view`` and ``module:operate`` are Console
tokens; a bare capability token (``business.query``, ``plugins.execute`` ...)
is understood by both channels and means "may use it" — in the Console it is
translated to the matching module level (``BARE_TO_CONSOLE``).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Mapping

from shared.plugin_management import management_for

# key: (label, description, operable)
CONSOLE_MODULES = {
    "business.query": ("业务查询", "寄件运单、物流轨迹和回单查询", False),
    "waybill.write": ("运单录入", "可见：打开录单与 OCR 页面；可操作：开单、修改、绑定平台运单", True),
    "customer_service": ("客户服务", "可见：查看问题件；可操作：回复、登记、审核回单", True),
    "receipt.share": ("客户共享", "可见：查看共享链接；可操作：维护规则，生成、停用、重置链接", True),
    "dispatch": ("货拉拉调度", "线路规划与询价", False),
    "line_haul": ("专线分流", "可见：查询专线；可操作：维护专线信息", True),
    "warehouse.monitor": ("监控查询", "按单号查看卸车录像与仓库位置", False),
    "plugins.execute": ("自动化", "可见：查看插件与执行记录；可操作：手动执行、确认候选", True),
    "finance": ("财务", "可见：查看单据、汇总和分析；可操作：制单、采集与处理财务单据", True),
    "accounts.manage": ("业务账号", "可见：查看各平台账号状态；可操作：维护账号与登录", True),
    "ai_assistant": ("AI 助手", "打开后台 AI 助手页面；能问什么、能执行什么按“机器人”权限", False),
}
CONSOLE_LEVELS = ("view", "operate")

# key: (label, description, kind) — kind "read", "write" or "private"
AGENT_CAPABILITIES = {
    "ai.chat": ("AI 对话", "用自然语言提问；能查询和执行什么仍按下面各项", "read"),
    "business.query": ("业务查询", "寄件运单、物流轨迹、回单和业务知识查询", "read"),
    "warehouse.monitor": ("监控查询", "按单号查询卸车录像与仓库位置", "read"),
    "finance.read": ("财务数据", "查询财务汇总和经营情况；“仅私聊”时群聊里不回答", "private"),
    "plugins.execute": ("执行插件", "触发已安装插件并确认候选", "write"),
    "customer_service": ("客户服务", "执行客服类插件", "write"),
    "finance.manage": ("财务采集", "执行财务采集插件；需同时允许财务数据", "write"),
    "accounts.manage": ("业务账号登录", "发送和提交各平台登录验证码（本身即逐步确认）", "read"),
}
AGENT_LEVELS = {"read": ("direct",), "write": ("direct", "confirm"), "private": ("private", "direct")}
SUPER_ADMIN = "super_admin"
CONSOLE = "console"
AGENT = "agent"

# A bare capability token checked in the Console means this module level.
BARE_TO_CONSOLE = {
    "business.query": "business.query:view",
    "warehouse.monitor": "warehouse.monitor:view",
    "finance.read": "finance:view",
    "finance.manage": "finance:operate",
    "plugins.execute": "plugins.execute:operate",
    "customer_service": "customer_service:operate",
    "waybill.write": "waybill.write:operate",
    "accounts.manage": "accounts.manage:operate",
    "receipt.share": "receipt.share:operate",
    "line_haul": "line_haul:operate",
    "dispatch": "dispatch:view",
    "ai.chat": "ai_assistant:view",
}


def validate_console_permissions(values) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise ValueError("后台权限格式无效")
    result = {}
    for key, level in values.items():
        if key not in CONSOLE_MODULES or level not in CONSOLE_LEVELS:
            raise ValueError("包含未知的后台权限")
        if level == "operate" and not CONSOLE_MODULES[key][2]:
            raise ValueError(f"“{CONSOLE_MODULES[key][0]}”只有可见，没有可操作")
        result[key] = level
    return dict(sorted(result.items()))


def validate_agent_permissions(values) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise ValueError("机器人权限格式无效")
    result = {}
    for key, level in values.items():
        if key not in AGENT_CAPABILITIES or level not in {"direct", "confirm", "private"}:
            raise ValueError("包含未知的机器人权限")
        label, _, kind = AGENT_CAPABILITIES[key]
        if level not in AGENT_LEVELS[kind]:
            raise ValueError(f"“{label}”只是查询，无需确认" if level == "confirm" else f"“{label}”没有“仅私聊”选项")
        result[key] = level
    if "finance.manage" in result and "finance.read" not in result:
        raise ValueError("财务采集需要同时允许财务数据")
    return dict(sorted(result.items()))


# Chat the current request came from: "private" (one-to-one Feishu, WeChat,
# the Console AI assistant) or "group". Unknown counts as a group, so a
# missing chat type never discloses private-only data.
_CHAT_SCOPE: ContextVar[str] = ContextVar("identity_chat_scope", default="")


@contextmanager
def chat_scope(scope: str) -> Iterator[None]:
    if scope not in {"private", "group"}:
        raise ValueError("unknown chat scope")
    token = _CHAT_SCOPE.set(scope)
    try:
        yield
    finally:
        _CHAT_SCOPE.reset(token)


def in_private_chat() -> bool:
    return _CHAT_SCOPE.get() == "private"


def console_tokens(console: Mapping[str, str]) -> tuple[str, ...]:
    """Membership tokens for menus and route checks."""
    tokens = []
    for key, level in console.items():
        tokens.append(f"{key}:view")
        if level == "operate":
            tokens.append(f"{key}:operate")
    return tuple(sorted(tokens))


@dataclass(frozen=True)
class IdentityAccess:
    active: bool = False
    super_admin: bool = False
    role_id: str | None = None
    role_name: str = "未分配身份"
    person_id: str | None = None
    console: Mapping[str, str] = field(default_factory=dict)
    agent: Mapping[str, str] = field(default_factory=dict)
    channel: str = CONSOLE

    def for_channel(self, channel: str) -> "IdentityAccess":
        if channel not in {CONSOLE, AGENT}:
            raise ValueError("unknown identity channel")
        return replace(self, channel=channel)

    @property
    def permissions(self) -> tuple[str, ...]:
        """Tokens granted in the current channel."""
        if self.channel == AGENT:
            return tuple(sorted(self.agent))
        return console_tokens(self.console)

    def allows(self, permission: str, *, read: bool = False) -> bool:
        """``read`` only needs the Console ``view`` level of the same module."""
        if not self.active:
            return False
        if self.channel == AGENT:
            level = self.agent_level(permission)
            if level is None:
                return self.super_admin  # super-admin-only tokens outside the catalog
            return level != "private" or in_private_chat()
        if self.super_admin:
            return True
        token = BARE_TO_CONSOLE.get(permission, permission)
        module, _, level = token.partition(":")
        granted = self.console.get(module)
        return granted is not None and (read or level == "view" or (level == "operate" and granted == "operate"))

    def agent_level(self, capability: str) -> str | None:
        """Agent level of a capability, ``None`` when not allowed.

        A super admin may do everything, but private data still stays out of
        group chats: other members would read it too.
        """
        if not self.active:
            return None
        if self.super_admin:
            kind = AGENT_CAPABILITIES.get(capability, ("", "", ""))[2]
            return "private" if kind == "private" else "direct" if kind else None
        return self.agent.get(capability)

    def blocked_in_group(self, capability: str) -> bool:
        """Allowed in a one-to-one chat, refused here only because this is a group."""
        return self.channel == AGENT and self.agent_level(capability) == "private" and not in_private_chat()

    def public(self) -> dict:
        everything = self.active and self.super_admin
        return {"access_active": self.active, "access_super_admin": self.super_admin,
                "access_role_id": self.role_id, "access_role_name": self.role_name,
                "access_permissions": list(console_tokens({k: ("operate" if v[2] else "view") for k, v in CONSOLE_MODULES.items()}))
                if everything else list(console_tokens(self.console)),
                "access_agent_permissions": {key: "private" if item[2] == "private" else "direct" for key, item in AGENT_CAPABILITIES.items()}
                if everything else dict(self.agent)}


def _json(raw: Any):
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


def access_from_row(row: Mapping | None, *, channel: str = CONSOLE) -> IdentityAccess:
    person_id = str(row["person_id"]) if row and row.get("person_id") else None
    if not row or not row.get("is_active"):
        return IdentityAccess(person_id=person_id, channel=channel)
    if row.get("control_plane_role") == SUPER_ADMIN:
        return IdentityAccess(active=True, super_admin=True, role_name="超级管理员", person_id=person_id, channel=channel)
    if not row.get("access_role_id") or not row.get("role_active"):
        return IdentityAccess(role_id=row.get("access_role_id"), role_name=row.get("role_name") or "未分配身份",
                              person_id=person_id, channel=channel)
    return IdentityAccess(True, False, str(row["access_role_id"]), str(row["role_name"]), person_id,
                          validate_console_permissions(_json(row["console_permissions_json"])),
                          validate_agent_permissions(_json(row["agent_permissions_json"])), channel)


def tool_permission(name: str) -> str:
    """Closed known readers; unknown tools require the built-in super admin."""
    return {
        "query_waybill": "business.query", "query_tracking": "business.query",
        "query_knowledge": "business.query", "track_waybill": "business.query",
        "search_tracking": "business.query", "query_receipts": "business.query",
        "query_finance_summary": "finance.read", "query_business_finance_summary": "finance.read",
        "query_business_finance": "finance.read",
        "query_automation_operations": "finance.read",
        "knowledge.search": "business.query", "shipment.query": "business.query", "waybill.lookup": "business.query", "tracking.lookup": "business.query",
        "finance.summary": "finance.read",
        "query_run_result": SUPER_ADMIN, "query_evidence_summary": SUPER_ADMIN,
    }.get(name, SUPER_ADMIN)


def plugin_permission(plugin_id: str, *, management=None, entrypoint: str = "") -> str:
    if entrypoint == "module_slots":
        return "waybill.write"
    module = management_for(management)["module"]
    if module == "finance":
        return "finance.manage"
    if module == "customer_service":
        return "customer_service"
    return "plugins.execute"
