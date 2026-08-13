"""Narrow read-only Feishu lookup for one receipt waybill number."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from tools.feishu_cli_tool import search_bitable_records


RECEIPT_FEISHU_BASE_TOKEN = "Fcm8b2H7wayK1UsYLjlcFmWhnMh"
RECEIPT_FEISHU_TABLE_ID = "tblX96gGAuBfJrtW"
RECEIPT_FEISHU_VIEW_ID = "veweDmbdIS"
RECEIPT_FEISHU_WAYBILL_FIELD = "运单编号"
RECEIPT_FEISHU_FIELD_NAMES = (
    "收货人",
    "收件人",
    "收货客户",
    "收件地址",
    "收货地址",
    "地址",
    "货物名称",
    "品名",
    "托寄物",
    "包装类型",
    "包装",
    "包装方式",
    "件数",
    "数量",
    "实际重量",
    "体积",
    RECEIPT_FEISHU_WAYBILL_FIELD,
    "运单号",
    "运单号码",
)


SearchRecords = Callable[[str, str, dict[str, Any]], dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "title"):
            text = _text(value.get(key))
            if text:
                return text
        return " / ".join(filter(None, (_text(item) for item in value.values())))
    if isinstance(value, (list, tuple)):
        return " / ".join(filter(None, (_text(item) for item in value)))
    return re.sub(r"\s+", " ", str(value)).strip()


def _meta(*, record_count: int, pagination_complete: bool) -> dict[str, Any]:
    return {
        "source_system": "feishu",
        "source": "feishu_bitable",
        "account_id": "internal_projection",
        "observed_at": _now(),
        "record_count": record_count,
        "pagination_complete": pagination_complete,
        "evidence_refs": [],
    }


def _failed(code: str, message: str, *, pagination_complete: bool = False) -> dict[str, Any]:
    return {
        "status": "FAILED",
        "data": {},
        "meta": _meta(record_count=0, pagination_complete=pagination_complete),
        "warnings": [],
        "error": {"code": code, "message": message, "retryable": False},
    }


def query_receipt_feishu_detail(
    arguments: Mapping[str, Any],
    *,
    search_records: SearchRecords = search_bitable_records,
) -> dict[str, Any]:
    """Return zero or one exact record; ambiguity and incomplete paging fail."""

    if set(arguments) != {"waybill_no"}:
        return _failed(
            "INVALID_ARGUMENTS",
            "Only waybill_no is accepted by the receipt Feishu detail query.",
        )
    waybill_no = _text(arguments.get("waybill_no"))
    if not waybill_no or len(waybill_no) > 100 or any(ord(char) < 32 for char in waybill_no):
        return _failed("INVALID_WAYBILL_NO", "waybill_no is required and must be at most 100 characters.")

    response = search_records(
        RECEIPT_FEISHU_BASE_TOKEN,
        RECEIPT_FEISHU_TABLE_ID,
        {
            "view_id": RECEIPT_FEISHU_VIEW_ID,
            "field_names": list(RECEIPT_FEISHU_FIELD_NAMES),
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {
                        "field_name": RECEIPT_FEISHU_WAYBILL_FIELD,
                        "operator": "is",
                        "value": [waybill_no],
                    }
                ],
            },
            "page_size": 2,
        },
    )
    if not isinstance(response, Mapping):
        return _failed("FEISHU_CONTRACT_ERROR", "Feishu returned a non-object response.")
    if response.get("error"):
        return _failed("FEISHU_QUERY_FAILED", _text(response.get("error")) or "Feishu query failed.")
    data = response.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("has_more"), bool):
        return _failed(
            "PAGINATION_PROOF_MISSING",
            "Feishu did not return an authoritative has_more pagination flag.",
        )
    if data.get("has_more"):
        return _failed(
            "AMBIGUOUS_WAYBILL",
            "More than one page matched the exact waybill number.",
        )
    items = data.get("items")
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        return _failed(
            "FEISHU_CONTRACT_ERROR",
            "Feishu did not return an item array.",
            pagination_complete=True,
        )
    if len(items) > 1:
        return _failed(
            "AMBIGUOUS_WAYBILL",
            "Multiple Feishu records matched the exact waybill number.",
            pagination_complete=True,
        )

    normalized_items: list[dict[str, Any]] = []
    evidence_refs: list[str] = []
    for item in items:
        record_id = _text(item.get("record_id"))
        fields = item.get("fields")
        if not record_id or not isinstance(fields, Mapping):
            return _failed(
                "FEISHU_CONTRACT_ERROR",
                "The matched Feishu record is missing record_id or fields.",
                pagination_complete=True,
            )
        actual_waybill = _text(fields.get(RECEIPT_FEISHU_WAYBILL_FIELD))
        if actual_waybill != waybill_no:
            return _failed(
                "WAYBILL_MISMATCH",
                "The Feishu record does not exactly match the requested waybill number.",
                pagination_complete=True,
            )
        safe_fields = {
            name: _text(fields.get(name))
            for name in RECEIPT_FEISHU_FIELD_NAMES
            if _text(fields.get(name))
        }
        normalized_items.append({"record_id": record_id, "fields": safe_fields})
        digest = hashlib.sha256(f"{waybill_no}:{record_id}".encode("utf-8")).hexdigest()
        evidence_refs.append(f"feishu-receipt-detail:{digest}")

    meta = _meta(record_count=len(normalized_items), pagination_complete=True)
    meta["evidence_refs"] = evidence_refs
    return {
        "status": "SUCCESS",
        "data": {"items": normalized_items, "waybill_no": waybill_no},
        "meta": meta,
        "warnings": [],
        "error": None,
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        result = _failed("INVALID_JSON", "Tool input must be one JSON object.")
    else:
        result = query_receipt_feishu_detail(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
