"""One capability vocabulary for Console, Feishu and conversational tools."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from shared.plugin_management import management_for


PERMISSIONS = {
    "business.query": ("业务查询", "寄件运单、物流轨迹、回单和业务知识查询"),
    "waybill.write": ("运单录入", "博益开单、OCR 和各平台原页录单"),
    "customer_service": ("客户服务", "查询、登记和处理问题件"),
    "dispatch": ("货拉拉调度", "线路规划与询价"),
    "line_haul": ("专线分流", "查询和维护专线信息"),
    "plugins.execute": ("执行插件", "手动或对话触发已安装插件、确认候选并查看执行结果"),
    "finance.read": ("财务数据", "查看财务单据、汇总和分析"),
    "finance.manage": ("财务采集与维护", "执行财务采集插件并处理财务单据；需同时勾选财务数据"),
    "accounts.manage": ("业务账号", "维护各平台业务账号及登录状态"),
    "ai.chat": ("AI 对话", "使用后台和飞书的自然语言对话；查询和执行仍受对应权限限制"),
}
SUPER_ADMIN = "super_admin"


def validate_permissions(values) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or v not in PERMISSIONS for v in values):
        raise ValueError("包含未知权限")
    result = tuple(sorted(set(values)))
    if "finance.manage" in result and "finance.read" not in result:
        raise ValueError("财务采集与维护需要同时勾选财务数据")
    return result


@dataclass(frozen=True)
class IdentityAccess:
    active: bool = False
    super_admin: bool = False
    role_id: str | None = None
    role_name: str = "未分配身份"
    permissions: tuple[str, ...] = ()

    def allows(self, permission: str) -> bool:
        return self.active and (self.super_admin or permission in self.permissions)

    def public(self) -> dict:
        return {"access_active": self.active, "access_super_admin": self.super_admin,
                "access_role_id": self.role_id, "access_role_name": self.role_name,
                "access_permissions": list(PERMISSIONS) if self.active and self.super_admin else list(self.permissions)}


def access_from_row(row: Mapping | None) -> IdentityAccess:
    import json
    if not row or not row.get("is_active"):
        return IdentityAccess()
    if row.get("control_plane_role") == SUPER_ADMIN:
        return IdentityAccess(active=True, super_admin=True, role_name="超级管理员")
    if not row.get("access_role_id") or not row.get("role_active"):
        return IdentityAccess(role_id=row.get("access_role_id"), role_name=row.get("role_name") or "未分配身份")
    raw = row.get("permissions_json")
    permissions = validate_permissions(json.loads(raw) if isinstance(raw, str) else raw)
    return IdentityAccess(True, False, str(row["access_role_id"]), str(row["role_name"]), permissions)


def tool_permission(name: str) -> str:
    """Closed known readers; unknown tools require the built-in super admin."""
    return {
        "query_waybill": "business.query", "query_tracking": "business.query",
        "query_knowledge": "business.query", "track_waybill": "business.query",
        "search_tracking": "business.query", "query_receipts": "business.query",
        "receipt_feishu_detail_query": "business.query", "get_price": "dispatch",
        "query_finance_summary": "finance.read", "query_business_finance_summary": "finance.read",
        "query_business_finance": "finance.read",
        "query_automation_operations": "finance.read",
        "knowledge.search": "business.query", "waybill.lookup": "business.query", "tracking.lookup": "business.query",
        "finance.summary": "finance.read",
        "query_run_result": SUPER_ADMIN, "query_evidence_summary": SUPER_ADMIN,
    }.get(name, SUPER_ADMIN)


def plugin_permission(plugin_id: str, *, management=None, entrypoint: str = "") -> str:
    if entrypoint == "module_slots":
        return "waybill.write"
    module = management_for(plugin_id, management)["module"]
    if module == "finance":
        return "finance.manage"
    if module == "customer_service":
        return "customer_service"
    return "plugins.execute"
