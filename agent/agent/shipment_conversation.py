"""Small structured-context adapter for shipment tools in the existing chat."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Mapping

from shared.shipment_metrics import BUSINESS_ZONE, QUERY_KEYS, ShipmentQueryError, resolve_query


SHIPMENT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": sorted(QUERY_KEYS),
    "properties": {
        "station": {"type": "string", "maxLength": 80},
        "period": {"type": "string", "enum": ["today", "yesterday", "previous_day", "range", "inherit"]},
        "start_date": {"type": "string", "maxLength": 10},
        "end_date": {"type": "string", "maxLength": 10},
        "group_by": {"type": "string", "enum": ["total", "date", "destination", "inherit"]},
        "comparison": {"type": "string", "enum": ["none", "previous_day_full", "previous_day_same_time"]},
    },
}


class ShipmentConversation:
    def __init__(self, *, clock=None):
        self._clock = clock or (lambda: datetime.now(BUSINESS_ZONE))
        self.context = None
        self.result = None

    def bind(self, context: Mapping | None):
        self.context = None
        if not context:
            return
        try:
            updated = datetime.fromisoformat(context["requested_at"])
            age = self._clock() - updated
        except (KeyError, TypeError, ValueError):
            return
        if timedelta(0) <= age <= timedelta(minutes=20):
            self.context = copy.deepcopy(dict(context))

    def clock_hint(self):
        return "业务当前时间（Asia/Shanghai）：" + self._clock().astimezone(BUSINESS_ZONE).isoformat()

    def prepare(self, arguments: Mapping, *, message: str) -> dict:
        station = arguments.get("station")
        if station and station not in message and (self.context is None or station != self.context.get("station")):
            raise ShipmentQueryError("SHIPMENT_STATION_REQUIRED", "请在当前问题中明确网点；不能从过期会话或资料中推断。")
        resolved = resolve_query(arguments, now=self._clock(), previous=self.context)
        return {"station": resolved["station"], "period": "range", "start_date": resolved["start_date"],
                "end_date": resolved["end_date"], "group_by": resolved["group_by"], "comparison": resolved["comparison"]}

    def observe(self, result: Mapping):
        self.result = copy.deepcopy(dict(result))
        if result.get("ok") is True:
            self.context = copy.deepcopy(result["query"])
