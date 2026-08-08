"""Unified waybill tracking dispatcher for console and Feishu."""

from __future__ import annotations

import json
import re
from typing import Any

from agent.tms_runtime.scripts import ronghui_tms_tracking
from agent.tms_runtime.scripts import yunda_waybill_tracking


RONGHUI_RE = re.compile(r"^(RC|R\d|200)", re.IGNORECASE)


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_tracking_number(value: Any) -> str:
    text = _clean_str(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def _resolve_tracking_number(params: dict[str, Any]) -> str:
    for key in (
        "tracking_number",
        "trackingNumber",
        "bill_code",
        "billCode",
        "waybill_no",
        "waybillNo",
        "ship_id",
        "shipId",
    ):
        code = normalize_tracking_number(params.get(key))
        if code:
            return code
    raw_items = params.get("items")
    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, dict):
                code = _resolve_tracking_number(item)
            else:
                code = normalize_tracking_number(item)
            if code:
                return code
    return ""


def detect_tracking_provider(tracking_number: str) -> str:
    code = normalize_tracking_number(tracking_number)
    if not code:
        return ""
    if RONGHUI_RE.match(code):
        return "ronghui"
    if code.startswith("000"):
        return "zhuanxian"
    if re.fullmatch(r"\d+", code):
        return "yunda"
    return ""


def _zhuanxian_response(tracking_number: str) -> dict[str, Any]:
    return {
        "ok": True,
        "type": "zhuanxian",
        "tracking_number": tracking_number,
        "contact": {
            "company": "专线物流",
            "phone": "",
            "hours": "",
            "note": "专线单号不支持在线轨迹查询，请直接联系物流公司客服查询货物状态。",
        },
    }


def query_tracking(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    tracking_number = _resolve_tracking_number(params)
    if not tracking_number:
        return {"ok": False, "error": "缺少运单号"}

    provider = str(params.get("provider") or "").strip().lower()
    if provider not in {"ronghui", "yunda", "zhuanxian"}:
        provider = detect_tracking_provider(tracking_number)

    if provider == "ronghui":
        return ronghui_tms_tracking.run_once({**params, "tracking_number": tracking_number})
    if provider == "yunda":
        return yunda_waybill_tracking.run_once(
            {**params, "tracking_number": tracking_number, "session_profile": params.get("session_profile") or "yunda"}
        )
    if provider == "zhuanxian":
        return _zhuanxian_response(tracking_number)

    return {
        "ok": False,
        "error": "无法识别单号类型，请输入 R/RC/200 开头的融辉单号、纯数字韵达单号或 000 开头的专线单号",
    }


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    return query_tracking(params or {})


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run_once(json.loads(sys.stdin.read() or "{}")), ensure_ascii=False, default=str))
