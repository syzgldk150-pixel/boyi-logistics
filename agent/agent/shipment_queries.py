"""Bounded shipment query service; the host owns source and account selection."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Mapping
from uuid import uuid4

from shared.shipment_metrics import (
    BUSINESS_ZONE, ShipmentDay, ShipmentQueryError, aggregate, compare, interval, resolve_query,
)


class ShipmentQueryService:
    def __init__(self, *, read_day: Callable[[str, object], ShipmentDay], clock=None):
        self._read_day = read_day
        self._clock = clock or (lambda: datetime.now(BUSINESS_ZONE))

    def __call__(self, arguments: Mapping) -> dict:
        try:
            now = self._clock()
            query = resolve_query(arguments, now=now)
            start, end = interval(query)
            days = []
            current = start.date()
            while current <= (end - timedelta(microseconds=1)).astimezone(BUSINESS_ZONE).date():
                day = self._read_day(query["station"], current)
                if not isinstance(day, ShipmentDay):
                    raise ShipmentQueryError("SHIPMENT_SOURCE_INVALID", "发货来源没有返回可核验的数据。")
                if day.captured_at.tzinfo is None or day.captured_at > self._clock() + timedelta(seconds=3):
                    raise ShipmentQueryError("SHIPMENT_CLOCK_INVALID", "来源更新时间异常，不能报告总量。")
                # Closed days may also be edited upstream; no indefinite final-day cache.
                if self._clock() - day.captured_at > timedelta(minutes=5):
                    raise ShipmentQueryError("SHIPMENT_SOURCE_STALE", "发货数据已过期，请刷新来源后再查询。")
                days.append(day)
                current += timedelta(days=1)
            result = {"ok": True, "query_id": str(uuid4()), "query": query,
                      "current": aggregate(days, start=start, end=end, group_by=query["group_by"])}
            if query["comparison"] != "none":
                prior_start = start - timedelta(days=1)
                prior_end = start if query["comparison"] == "previous_day_full" else end - timedelta(days=1)
                prior_day = self._read_day(query["station"], prior_start.date())
                if (not isinstance(prior_day, ShipmentDay) or prior_day.captured_at.tzinfo is None
                        or self._clock() - prior_day.captured_at > timedelta(minutes=5)
                        or prior_day.captured_at > self._clock() + timedelta(seconds=3)):
                    raise ShipmentQueryError("SHIPMENT_SOURCE_STALE", "比较日期的数据未取得有效更新。")
                if (prior_day.source, prior_day.station_id) != (days[0].source, days[0].station_id):
                    raise ShipmentQueryError("SHIPMENT_SCOPE_MISMATCH", "比较数据的网点或来源不一致。")
                result["previous"] = aggregate([prior_day], start=prior_start, end=prior_end, group_by=query["group_by"])
                result["comparison"] = compare(result["current"], result["previous"])
            return result
        except ShipmentQueryError as exc:
            return {"ok": False, "status": "unavailable", "code": exc.code, "message": str(exc)}


def unavailable_shipment_source(_station, _day):
    """Until native unit, validity and exact site mapping have been verified.

    Existing waybills.weight_volume is a display string and is deliberately not
    used as a substitute for the typed chargeable-weight contract.
    """
    raise ShipmentQueryError("SHIPMENT_SOURCE_CONTRACT_UNVERIFIED",
        "真实寄件数据尚未接通，仍需核实账户与网点映射并完成完整范围读取，暂不能报告真实吨位。")
