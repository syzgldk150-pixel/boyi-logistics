"""Closed, read-only business gateways exposed to the AI assistant."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable, Mapping

from shared.redaction import redact_text


_PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_LONG_HASH = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_LABELED_ADDRESS = re.compile(
    r"(?:收货|发货|联系|详细)?地址\s*[:：]?\s*[^\s，。；;]{4,80}"
)
_SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "account",
    "storage",
    "path",
    "hash",
    "source_record_id",
    "entity_id",
    "command_id",
    "actor",
)
_STATUS_LABELS = {
    "PENDING": "待处理",
    "OPEN": "待处理",
    "RUNNING": "运行中",
    "WAITING_APPROVAL": "等待审批",
    "NEEDS_CLARIFICATION": "需要补充信息",
    "BLOCKED": "已阻塞",
    "BLOCKED_DATA": "数据阻塞",
    "BLOCKED_LOGIN": "登录失效",
    "FAILED_RETRYABLE": "失败，可重试",
    "FAILED_TERMINAL": "执行失败",
    "PARTIAL": "部分完成",
    "SUCCEEDED": "已完成",
    "COMPLETED": "已完成",
    "RESOLVED": "已解决",
    "CANCELLED": "已取消",
    "IN_TRANSIT": "运输中",
    "SIGNED": "已签收",
    "PENDING_SCAN": "待扫描",
    "SCANNED": "已扫描",
    "NOT_APPLICABLE": "不适用",
    "COMPLETE": "完整",
    "INCOMPLETE": "不完整",
    "UNKNOWN": "未知",
    "LOW": "低",
    "MEDIUM": "中",
    "HIGH": "高",
    "CRITICAL": "严重",
    "EXTREME": "极高",
}


def _text(value: Any, *, limit: int = 500) -> str:
    value = "" if value is None else str(value)
    value = _PHONE.sub("[手机号已隐藏]", redact_text(value))
    value = _EMAIL.sub("[邮箱已隐藏]", value)
    value = _LABELED_ADDRESS.sub("地址：[已隐藏]", value)
    value = _LONG_HASH.sub("[标识已隐藏]", value)
    return value.strip()[:limit]


def _time(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return _text(value, limit=64)


def _status(value: Any) -> str:
    raw = _text(value, limit=64)
    return _STATUS_LABELS.get(raw.upper(), raw or "未知")


def _unavailable(message: str) -> dict[str, Any]:
    return {"可用": False, "说明": message}


def _safe_summary(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "内容层级过深，已省略"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = str(raw_key).strip()
            lowered = key.lower()
            if not key or any(part in lowered for part in _SECRET_KEY_PARTS):
                continue
            result[_text(key, limit=80)] = _safe_summary(nested, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_summary(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return _text(value, limit=1000)


class ReadOnlyHarnessGateway:
    """Build the exact six fixed handlers from composition-root providers."""

    def __init__(
        self,
        *,
        knowledge_search: Callable[[str, int], object],
        waybill_lookup: Callable[[str], object],
        tracking_lookup: Callable[[str], object],
        list_work_items: Callable[[int], object],
        get_run: Callable[[str], object],
        get_evidence: Callable[[str], object],
    ) -> None:
        self._knowledge_search = knowledge_search
        self._waybill_lookup = waybill_lookup
        self._tracking_lookup = tracking_lookup
        self._list_work_items = list_work_items
        self._get_run = get_run
        self._get_evidence = get_evidence

    def handlers(self) -> dict[str, Callable[[Mapping[str, Any]], object]]:
        return {
            "knowledge.search": self.knowledge,
            "waybill.lookup": self.waybill,
            "tracking.lookup": self.tracking,
            "work_items.list_open": self.work_items,
            "runs.get_summary": self.run,
            "artifact.inspect": self.evidence,
        }

    def knowledge(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            rows = self._knowledge_search(str(arguments["query"]), int(arguments["limit"]))
        except Exception:
            return _unavailable("业务知识暂时无法读取，请稍后重试")
        items = []
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, Mapping):
                continue
            items.append(
                {
                    "分类": _text(row.get("category"), limit=80) or "未分类",
                    "内容": _text(row.get("content"), limit=1500),
                }
            )
        return {"可用": True, "找到": bool(items), "结果": items}

    def waybill(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        number = _text(arguments["waybill_number"], limit=191)
        try:
            row = self._waybill_lookup(number)
        except Exception:
            return _unavailable("运单信息暂时无法读取，请稍后重试")
        if not isinstance(row, Mapping):
            return {"可用": True, "找到": False, "运单号": number}
        actual = _text(row.get("waybill_no"), limit=191)
        if actual != number:
            return {"可用": True, "找到": False, "运单号": number}
        return {
            "可用": True,
            "找到": True,
            "运单号": actual,
            "当前状态": _status(row.get("status")),
            "扫描状态": _status(row.get("scan_status")),
            "创建时间": _time(row.get("created_at")),
            "更新时间": _time(row.get("updated_at")),
        }

    def tracking(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        number = _text(arguments["tracking_number"], limit=191)
        try:
            payload = self._tracking_lookup(number)
        except Exception:
            return _unavailable("物流轨迹暂时无法读取，请稍后重试")
        if not isinstance(payload, Mapping) or payload.get("error"):
            return {
                "可用": False,
                "运单号": number,
                "说明": "物流轨迹查询失败，请检查运单号或稍后重试",
            }
        actual = _text(
            payload.get("tracking_number")
            or payload.get("waybill_no")
            or payload.get("bill_code"),
            limit=191,
        )
        if actual and actual != number:
            return {"可用": True, "找到": False, "运单号": number}
        routes = []
        for row in payload.get("route_rows", ()) if isinstance(payload.get("route_rows"), list) else ():
            if not isinstance(row, Mapping):
                continue
            routes.append(
                {
                    "时间": _time(
                        row.get("scan_time")
                        or row.get("time")
                        or row.get("create_time")
                        or row.get("operate_time")
                    ),
                    "网点": _text(row.get("site_name") or row.get("scan_site"), limit=120),
                    "状态": _status(row.get("status") or row.get("status_text")),
                    "说明": _text(
                        row.get("description") or row.get("content") or row.get("message"),
                        limit=300,
                    ),
                }
            )
        progress = payload.get("arrival_progress")
        safe_progress = {}
        if isinstance(progress, Mapping):
            safe_progress = {
                "应到数量": progress.get("expected_quantity"),
                "已到数量": progress.get("arrived_quantity"),
                "未到数量": progress.get("pending_quantity"),
            }
        return {
            "可用": True,
            "找到": bool(routes or actual),
            "运单号": actual or number,
            "当前状态": _status(payload.get("status") or payload.get("status_text")),
            "到货进度": safe_progress,
            "轨迹": routes[:100],
        }

    def work_items(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            rows = self._list_work_items(int(arguments["limit"]))
        except Exception:
            return _unavailable("待处理事项暂时无法读取，请稍后重试")
        items = []
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, Mapping):
                continue
            items.append(
                {
                    "事项编号": _text(row.get("work_item_id"), limit=191),
                    "标题": _text(row.get("title"), limit=300),
                    "状态": _status(row.get("status")),
                    "优先级": _status(row.get("priority")),
                    "类型": _text(row.get("type"), limit=120),
                    "更新时间": _time(row.get("updated_at")),
                    "截止时间": _time(row.get("sla_due_at")),
                }
            )
        return {"可用": True, "数量": len(items), "事项": items}

    def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        run_id = _text(arguments["run_id"], limit=191)
        try:
            row = self._get_run(run_id)
        except Exception:
            return _unavailable("任务运行结果暂时无法读取，请稍后重试")
        if not isinstance(row, Mapping) or _text(row.get("run_id"), limit=191) != run_id:
            return {"可用": True, "找到": False, "运行编号": run_id}
        steps = []
        for step in row.get("steps", ()) if isinstance(row.get("steps"), list) else ():
            if not isinstance(step, Mapping):
                continue
            steps.append(
                {
                    "步骤": _text(step.get("name") or step.get("step_name"), limit=160),
                    "状态": _status(step.get("status")),
                    "说明": _text(step.get("error_summary") or step.get("message"), limit=300),
                }
            )
        return {
            "可用": True,
            "找到": True,
            "运行编号": run_id,
            "状态": _status(row.get("status")),
            "开始时间": _time(row.get("started_at") or row.get("created_at")),
            "完成时间": _time(row.get("finished_at") or row.get("completed_at")),
            "步骤": steps,
        }

    def evidence(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        evidence_id = _text(arguments["artifact_id"], limit=191)
        try:
            row = self._get_evidence(evidence_id)
        except Exception:
            return _unavailable("运行证据暂时无法读取，请稍后重试")
        if not isinstance(row, Mapping) or _text(row.get("evidence_id"), limit=191) != evidence_id:
            return {"可用": True, "找到": False, "证据编号": evidence_id}
        return {
            "可用": True,
            "找到": True,
            "证据编号": evidence_id,
            "来源系统": _text(row.get("source_system"), limit=100),
            "记录类型": _text(row.get("source_record_type"), limit=100),
            "业务类型": _text(row.get("entity_type"), limit=100),
            "发生时间": _time(row.get("occurred_at")),
            "记录时间": _time(row.get("observed_at")),
            "完整性": _status(row.get("completeness_status")),
            "记录数量": row.get("record_count"),
            "摘要": _safe_summary(row.get("summary_json") or row.get("summary") or {}),
        }


__all__ = ["ReadOnlyHarnessGateway"]
