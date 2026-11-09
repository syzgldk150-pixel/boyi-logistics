"""Application-root wiring of the customer-service AI evidence providers.

Lives beside ``harness_composition.py`` because the providers use ``tools``,
which ``agent/agent`` may not import. Every provider is read-only.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from agent.ai_tasks import AITaskGateway
from agent.customer_service_ai import CustomerServiceAIService, EvidenceProvider
from agent.direct_tool_router import format_track_waybill_reply
from agent.execution_boundary import execution_capability_scope
from shared.ai_tasks.audit import AITaskAuditRepository
from shared.customer_service_ai import CustomerServiceAIRepository, mask_address
from shared.runtime_repositories import WaybillRepository
from tools.feishu_knowledge import FeishuKnowledgeSource
from tools.phase7_mysql_store import list_scan_times_for_tracking
from tools.track_waybill_tool import run_track_waybill

_WAYBILL_FIELDS = (
    ("open_date", "开单日期"), ("destination_site", "目的站"), ("status", "状态"), ("scan_status", "扫描状态"),
    ("quantity_lines", "件数"), ("weight_volume", "重量体积"), ("delivery_method", "派送方式"),
    ("goods_name_lines", "货物"), ("remark", "备注"),
)
_KNOWLEDGE_WINDOW = 400


def _waybill_text(row: Mapping[str, Any] | None) -> str:
    if not row:
        return ""
    parts = [f"{label} {row[field]}" for field, label in _WAYBILL_FIELDS if str(row.get(field) or "").strip()]
    if str(row.get("receiver_address") or "").strip():
        parts.append(f"收件地址（数字已隐去）{mask_address(row['receiver_address'])}")
    parts.append("收件电话 " + ("有" if str(row.get("receiver_phone") or "").strip() else "无"))
    return "；".join(str(part) for part in parts)


def _excerpt(body: str, query: str) -> str:
    index = body.casefold().find(query.casefold())
    if index < 0:
        return body[: _KNOWLEDGE_WINDOW * 2]
    start = max(0, index - _KNOWLEDGE_WINDOW)
    return ("…" if start else "") + body[start: index + _KNOWLEDGE_WINDOW] + "…"


def build_customer_service_ai(runtime: Any, gateway: AITaskGateway) -> CustomerServiceAIService:
    connection_factory = runtime.memory.connection_factory
    waybills = WaybillRepository(connection_factory)
    knowledge_source = FeishuKnowledgeSource()

    async def local_waybill(problem: Mapping[str, Any]) -> list[dict[str, str]]:
        number = problem.get("waybill_no")
        if not number:
            return []
        try:
            row = await asyncio.to_thread(waybills.get_by_number, number)
        except ValueError:
            return [{"source": "运单库", "text": "运单库中该单号有多条记录，未使用。"}]
        text = _waybill_text(row)
        return [{"source": "运单库", "text": text}] if text else []

    async def scans(problem: Mapping[str, Any]) -> list[dict[str, str]]:
        number = problem.get("waybill_no")
        if not number:
            return []
        result = await asyncio.to_thread(list_scan_times_for_tracking, number)
        rows = result.get("rows") or []
        if not rows:
            return [{"source": "仓库扫描", "text": "近期仓库扫描记录中没有该单号。"}]
        lines = [f"{row['scanned_at'] or row['snapshot_date']} 扫描 {row['raw_code']}（{row['code_type']}）目的地 {row['destination']}"
                 for row in rows[-10:]]
        return [{"source": "仓库扫描", "text": "；".join(lines)}]

    def _track(number: str) -> dict[str, Any]:
        with execution_capability_scope("track_waybill", ttl_seconds=60):
            return run_track_waybill({"tracking_number": number, "timeout_sec": 20, "client_timeout_sec": 22})

    async def tracking(problem: Mapping[str, Any]) -> list[dict[str, str]]:
        number = problem.get("waybill_no")
        if not number:
            return []
        payload = await asyncio.to_thread(_track, number)
        text = format_track_waybill_reply({"success": not payload.get("error"), "data": payload,
                                           "error": payload.get("error")})
        return [{"source": "物流轨迹", "text": text}]

    async def knowledge(problem: Mapping[str, Any]) -> list[dict[str, str]]:
        query = str(problem.get("problem_type") or "").strip()
        if not query:
            return []
        result = await asyncio.to_thread(knowledge_source.search, query[:50], 3)
        if not result.get("ok"):
            return []
        # document_id and its edit time let approved templates notice a later change of the regulation.
        return [{"source": f"{item.get('space') or '知识库'} · {item.get('title')}", "text": _excerpt(str(item.get("body") or ""), query),
                 "url": item.get("url") or "", "document_id": item.get("document_id") or item.get("source_id") or "",
                 "updated_at": str(item.get("updated_at") or "")} for item in result.get("items") or []]

    return CustomerServiceAIService(
        CustomerServiceAIRepository(connection_factory),
        gateway,
        evidence_providers=(
            EvidenceProvider("运单库", local_waybill, 10),
            EvidenceProvider("仓库扫描", scans, 10),
            EvidenceProvider("物流轨迹", tracking, 30),
        ),
        knowledge=EvidenceProvider("飞书知识库", knowledge, 30),
        audit=AITaskAuditRepository(connection_factory),
    )


__all__ = ["build_customer_service_ai"]
