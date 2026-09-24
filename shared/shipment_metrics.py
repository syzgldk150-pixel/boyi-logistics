"""Deterministic opening-date / chargeable-weight shipment query contract.

Provider adapters supply typed, complete day snapshots. No display-weight text,
model output, estimated weight or partial page can enter the calculation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


BUSINESS_ZONE = ZoneInfo("Asia/Shanghai")
METRIC_VERSION = "opened-chargeable-v1"
QUERY_KEYS = frozenset({"station", "period", "start_date", "end_date", "group_by", "comparison"})


class ShipmentQueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def fail(code: str, message: str):
    raise ShipmentQueryError(code, message)


def weight_kg(value: object, unit: str) -> Decimal:
    if unit != "kg":
        fail("SHIPMENT_WEIGHT_UNIT_UNVERIFIED", "计费重量单位尚未核实，不能换算吨位。")
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        fail("SHIPMENT_WEIGHT_MISSING", "存在缺少计费重量的运单，不能报告完整吨位。")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        fail("SHIPMENT_WEIGHT_INVALID", "计费重量不是有效数值。")
    if not result.is_finite() or result < 0:
        fail("SHIPMENT_WEIGHT_INVALID", "计费重量必须为有限非负数。")
    return result


def number(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class ShipmentRecord:
    record_id: str
    station_id: str
    opened_at: datetime
    chargeable_kg: Decimal
    destination: str
    cancelled: bool
    is_receipt: bool
    is_child: bool


@dataclass(frozen=True)
class ShipmentDay:
    source: str
    station_id: str
    station_name: str
    business_date: date
    captured_at: datetime
    complete: bool
    source_total: int
    records: tuple[ShipmentRecord, ...]
    source_revision: str


def resolve_query(arguments: Mapping, *, now: datetime, previous: Mapping | None = None) -> dict:
    if set(arguments) != QUERY_KEYS:
        fail("SHIPMENT_ARGUMENT_INVALID", "查询参数不在允许范围内。")
    if now.tzinfo is None:
        raise ValueError("aware query clock required")
    now = now.astimezone(BUSINESS_ZONE)
    previous = previous or {}
    station = arguments["station"]
    if station in (None, ""):
        station = previous.get("station")
    if not isinstance(station, str) or not station.strip() or len(station) > 80:
        fail("SHIPMENT_STATION_REQUIRED", "请说明要查询哪个网点；会话过期后需要重新指定。")
    period = arguments["period"]
    if period in {"today", "yesterday"}:
        start = end = now.date() - timedelta(days=period == "yesterday")
    elif period == "previous_day":
        if not previous.get("start_date") or previous.get("start_date") != previous.get("end_date"):
            fail("SHIPMENT_CONTEXT_REQUIRED", "请说明要比较的日期。")
        start = end = date.fromisoformat(previous["start_date"]) - timedelta(days=1)
    elif period == "inherit":
        if not previous.get("start_date"):
            fail("SHIPMENT_CONTEXT_REQUIRED", "查询上下文已失效，请重新说明日期。")
        start, end = date.fromisoformat(previous["start_date"]), date.fromisoformat(previous["end_date"])
    elif period == "range":
        try:
            start = date.fromisoformat(arguments["start_date"])
            end = date.fromisoformat(arguments["end_date"])
        except (TypeError, ValueError):
            fail("SHIPMENT_DATE_INVALID", "请提供明确的起止日期。")
    else:
        fail("SHIPMENT_DATE_INVALID", "不支持该日期条件。")
    if period != "range" and (arguments["start_date"] not in (None, "") or arguments["end_date"] not in (None, "")):
        fail("SHIPMENT_DATE_INVALID", "相对日期与显式日期不能混用。")
    if start > end or (end-start).days >= 31 or end > now.date():
        fail("SHIPMENT_DATE_INVALID", "日期须为过去或今天，范围不能超过一个月。")
    group = arguments["group_by"]
    if group == "inherit":
        group = previous.get("group_by")
    if group not in {"total", "date", "destination"}:
        fail("SHIPMENT_GROUP_INVALID", "请指定汇总、按日或按目的地分组。")
    comparison = arguments["comparison"]
    if comparison not in {"none", "previous_day_full", "previous_day_same_time"}:
        fail("SHIPMENT_COMPARISON_INVALID", "不支持该比较方式。")
    if comparison != "none" and start != end:
        fail("SHIPMENT_COMPARISON_INVALID", "昨日比较只支持单日查询。")
    return {"station": station.strip(), "start_date": start.isoformat(), "end_date": end.isoformat(),
            "group_by": group, "comparison": comparison, "metric_version": METRIC_VERSION,
            "timezone": str(BUSINESS_ZONE), "requested_at": now.isoformat()}


def aggregate(days: Sequence[ShipmentDay], *, start: datetime, end: datetime, group_by: str) -> dict:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("invalid half-open interval")
    expected_days = set()
    current = start.astimezone(BUSINESS_ZONE).date()
    while current <= (end - timedelta(microseconds=1)).astimezone(BUSINESS_ZONE).date():
        expected_days.add(current)
        current += timedelta(days=1)
    if not days or len(days) != len(expected_days) or {item.business_date for item in days} != expected_days:
        fail("SHIPMENT_COVERAGE_INCOMPLETE", "日期数据未完整覆盖，不能报告总吨位。")
    if len({(item.source, item.station_id) for item in days}) != 1:
        fail("SHIPMENT_SCOPE_MISMATCH", "来源或网点范围发生变化。")
    seen, accepted, excluded = set(), [], {"cancelled": 0, "receipt": 0, "child": 0, "outside_interval": 0}
    for day in days:
        if (not day.complete or day.source_total != len(day.records) or day.captured_at.tzinfo is None
                or not day.source_revision):
            fail("SHIPMENT_COVERAGE_INCOMPLETE", "来源分页或数据版本未完整核实。")
        for row in day.records:
            if not row.record_id or row.record_id in seen:
                fail("SHIPMENT_DUPLICATE", "来源存在重复运单，不能报告完整总量。")
            seen.add(row.record_id)
            if row.station_id != day.station_id or row.opened_at.tzinfo is None or row.opened_at.astimezone(BUSINESS_ZONE).date() != day.business_date:
                fail("SHIPMENT_SCOPE_MISMATCH", "运单网点或开单日期与查询范围不符。")
            if any(type(v) is not bool for v in (row.cancelled, row.is_receipt, row.is_child)):
                fail("SHIPMENT_STATUS_UNVERIFIED", "运单有效状态未核实。")
            reason = ("cancelled" if row.cancelled else "receipt" if row.is_receipt else
                      "child" if row.is_child else "outside_interval" if not start <= row.opened_at < end else None)
            if reason:
                excluded[reason] += 1
                continue
            value = weight_kg(row.chargeable_kg, "kg")
            if group_by == "destination" and not row.destination:
                fail("SHIPMENT_DESTINATION_MISSING", "缺少目的地，不能完整分组。")
            accepted.append((row, value))
    total = sum((weight for _, weight in accepted), Decimal(0))
    groups = {}
    for row, weight in accepted:
        key = row.opened_at.astimezone(BUSINESS_ZONE).date().isoformat() if group_by == "date" else row.destination if group_by == "destination" else "合计"
        bucket = groups.setdefault(key, {"tickets": 0, "kg": Decimal(0)})
        bucket["tickets"] += 1
        bucket["kg"] += weight
    if group_by == "date":
        for day in expected_days:
            groups.setdefault(day.isoformat(), {"tickets": 0, "kg": Decimal(0)})
    group_total = sum((item["kg"] for item in groups.values()), Decimal(0))
    if group_total != total or len(seen) != len(accepted) + sum(excluded.values()):
        fail("SHIPMENT_VALIDATION_FAILED", "分组或行数校验失败。")
    tonnes = total / Decimal(1000)
    if tonnes * Decimal(1000) != total:
        fail("SHIPMENT_VALIDATION_FAILED", "重量反算校验失败。")
    weights = [weight for _, weight in accepted]
    return {"status": "complete" if accepted else "zero", "weight_kg": number(total), "tonnes": number(tonnes),
            "tickets": len(accepted), "source_rows": len(seen), "excluded": excluded,
            "groups": [{"label": key, "tickets": item["tickets"], "tonnes": number(item["kg"] / Decimal(1000))}
                       for key, item in sorted(groups.items())],
            "validation": {"group_total_matches": True, "row_count_matches": True, "reverse_matches": True,
                           "min_kg": number(min(weights)) if weights else None, "max_kg": number(max(weights)) if weights else None},
            "start": start.isoformat(), "end_exclusive": end.isoformat(), "source": days[0].source,
            "station": days[0].station_name, "updated_at": min(item.captured_at for item in days).isoformat(),
            "versions": [{"date": item.business_date.isoformat(), "revision": item.source_revision} for item in days]}


def interval(query: Mapping) -> tuple[datetime, datetime]:
    start = datetime.combine(date.fromisoformat(query["start_date"]), time.min, BUSINESS_ZONE)
    end = datetime.combine(date.fromisoformat(query["end_date"]) + timedelta(days=1), time.min, BUSINESS_ZONE)
    return start, min(end, datetime.fromisoformat(query["requested_at"]))


def compare(current: Mapping, previous: Mapping) -> dict:
    delta = Decimal(current["tonnes"]) - Decimal(previous["tonnes"])
    denominator = Decimal(previous["tonnes"])
    return {"difference_tonnes": number(delta),
            "change_percent": number((delta / denominator * 100).quantize(Decimal("0.01"))) if denominator else None,
            "zero_denominator": not bool(denominator)}


def format_shipment_result(result: Mapping) -> str:
    if result.get("ok") is not True:
        return str(result.get("message") or "发货数据暂不可用。")
    current, query = result["current"], result["query"]
    lines = [f"{current['station']}，{query['start_date']} 至 {query['end_date']}：计费重量 {current['tonnes']} 吨，{current['tickets']} 票。",
             f"按开单日期统计；作废单、回单及子单不重复计入。截止 {current['end_exclusive']}（不含），数据更新于 {current['updated_at']}。"]
    if query["group_by"] != "total":
        lines.extend(f"{item['label']}：{item['tonnes']} 吨，{item['tickets']} 票。" for item in current["groups"])
    if "previous" in result:
        previous, comparison = result["previous"], result["comparison"]
        label = "前一日全天" if query["comparison"] == "previous_day_full" else "前一日相同截止时间"
        lines.append(f"{label}：{previous['tonnes']} 吨；差额 {comparison['difference_tonnes']} 吨。")
        lines.append("前一日为零，不计算变化百分比。" if comparison["zero_denominator"] else f"变化 {comparison['change_percent']}%。")
    lines.append(f"口径：{query['metric_version']}；来源：{current['source']}；查询编号：{result['query_id']}。")
    return "\n".join(lines)
