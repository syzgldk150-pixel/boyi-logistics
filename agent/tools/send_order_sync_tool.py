"""Phase 7 第一批：获取当日寄件数据 -> 写入飞书多维表格。"""

import datetime as dt
import os
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from agent.workflow_resource_store import get_workflow_resource
from agent.tms_runtime.scripts.Send_order import _coerce_number, normalize_receipt_required
from tools.phase7_sync_common import (
    bind_explicit_account_id,
    require_explicit_account_id,
)


DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 50
INDEX_FIELD_NAME = "运单编号"
DATE_FIELD_NAME = "发件日期"
DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")


def _parse_date_param(params: dict[str, Any], key: str) -> dt.date | None:
    raw = str(params.get(key) or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ValueError(f"{key} 必须是 YYYY-MM-DD 格式") from exc


def _target_date(params: dict[str, Any]) -> dt.date:
    target_date = _parse_date_param(params, "target_date")
    return target_date or dt.datetime.now(DEFAULT_TZ).date()


def _date_range(params: dict[str, Any]) -> list[dt.date]:
    start_date = _parse_date_param(params, "start_date")
    end_date = _parse_date_param(params, "end_date")
    if start_date is None and end_date is None:
        return [_target_date(params)]
    if start_date is None:
        start_date = end_date
    if end_date is None:
        end_date = start_date
    if start_date is None or end_date is None:
        return [_target_date(params)]
    if start_date > end_date:
        raise ValueError("start_date 不能晚于 end_date")
    return [start_date + dt.timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _emit_progress(message: str, **extra: Any) -> None:
    payload = " | ".join(f"{key}={value}" for key, value in extra.items() if value not in (None, ""))
    text = f"[progress] {message}"
    if payload:
        text = f"{text} | {payload}"
    print(text, file=sys.stderr, flush=True)


def _to_timestamp_ms(value: Any) -> int | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        value_dt = value
    elif isinstance(value, dt.date):
        value_dt = dt.datetime.combine(value, dt.time.min)
    else:
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                value_dt = dt.datetime.strptime(text[:19] if " " in fmt else text[:10], fmt)
                break
            except ValueError:
                value_dt = None
        if value_dt is None:
            return None
    if value_dt.tzinfo is None:
        value_dt = value_dt.replace(tzinfo=DEFAULT_TZ)
    return int(value_dt.timestamp() * 1000)


def _date_to_timestamp_ms(value: dt.date) -> int:
    return int(dt.datetime.combine(value, dt.time.min, DEFAULT_TZ).timestamp() * 1000)


def _date_from_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return dt.datetime.fromtimestamp(timestamp, DEFAULT_TZ).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    if isinstance(value, dt.datetime):
        value_dt = value if value.tzinfo else value.replace(tzinfo=DEFAULT_TZ)
        return value_dt.astimezone(DEFAULT_TZ).date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        for key in ("text", "value", "name", "link"):
            parsed = _date_from_value(value.get(key))
            if parsed:
                return parsed
        return ""
    if isinstance(value, list):
        for item in value:
            parsed = _date_from_value(item)
            if parsed:
                return parsed
        return ""
    text = str(value).strip()
    if text.isdigit():
        return _date_from_value(int(text))
    match = DATE_RE.search(text)
    if not match:
        return ""
    year, month, day = match.groups()
    try:
        return dt.date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _to_number(value: Any) -> int | str | None:
    return _coerce_number(value)


def _number_text(value: Any) -> str:
    number = _to_number(value)
    return "" if number is None else str(number)


def _resolve_bitable_target(params: dict) -> tuple[str, str]:
    base_token = params.get("base_token") or params.get("app_token")
    table_id = params.get("table_id")
    if base_token and table_id:
        return str(base_token), str(table_id)

    resource = get_workflow_resource("phase7.send_order_bitable")
    if not resource or not resource.get("base_token") or not resource.get("table_id"):
        raise ValueError("未找到 phase7.send_order_bitable，请先导入到 MySQL")
    return str(resource["base_token"]), str(resource["table_id"])


def _record_fields(row: dict, *, target_date: dt.date) -> dict:
    return {
        INDEX_FIELD_NAME: row.get(INDEX_FIELD_NAME, ""),
        DATE_FIELD_NAME: _to_timestamp_ms(row.get(DATE_FIELD_NAME)) or _date_to_timestamp_ms(target_date),
        "签收状态": row.get("签收状态", ""),
        "目的网点": row.get("目的网点", ""),
        "收件区/县": row.get("收件区/县", ""),
        "收件地址": row.get("收件地址", ""),
        "寄件人": row.get("寄件人", ""),
        "寄件手机": row.get("寄件手机", ""),
        "寄件地址": row.get("寄件地址", ""),
        "收货人": row.get("收货人", ""),
        "收货电话": row.get("收货电话", ""),
        "货物名称": row.get("货物名称", ""),
        "包装类型": row.get("包装类型", ""),
        "派送方式": row.get("派送方式", ""),
        "支付类型": row.get("支付类型", ""),
        "回单号": row.get("回单号", ""),
        "需要回单": {"1": "是", "0": "否", "": ""}[normalize_receipt_required(row.get("需要回单"))],
        "备注": row.get("备注", ""),
        "件数": _to_number(row.get("件数")),
        "实际重量": _to_number(row.get("实际重量")),
        "计费重量": _to_number(row.get("计费重量")),
        "录单运费": _to_number(row.get("录单运费")),
        "运费": _to_number(row.get("运费")),
        "代收货款": _to_number(row.get("代收货款")),
        "保价金额": _to_number(row.get("保价金额")),
        "体积重量": _to_number(row.get("体积重量")),
        "体积": _to_number(row.get("体积")),
        "结算重量": _to_number(row.get("结算重量")),
        "到付款": _to_number(row.get("到付款")),
    }


def _text_from_field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, list):
        return "".join(_text_from_field_value(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "value", "name", "link"):
            text = _text_from_field_value(value.get(key))
            if text:
                return text
        return "".join(_text_from_field_value(item) for item in value.values()).strip()
    return str(value).strip()


def _normalize_waybill(value: Any) -> str:
    text = _text_from_field_value(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def _weight_volume_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for label, field_name in (
        ("实际重量", "实际重量"),
        ("计费重量", "计费重量"),
        ("体积", "体积"),
        ("结算重量", "结算重量"),
        ("体积重", "体积重量"),
    ):
        value = _number_text(row.get(field_name))
        if value:
            parts.append(f"{label} {value}")
    return " / ".join(parts)


def _console_status_from_sign_status(value: Any) -> str:
    status = _text_from_field_value(value).replace(" ", "")
    if status in {"签收", "已签收"}:
        return "signed"
    return "in_transit"


def _scan_status_text(row: dict[str, Any]) -> str:
    for field_name in (
        "当前扫描状态",
        "最新扫描状态",
        "扫描状态",
        "最新扫描类型",
        "scan_status",
        "current_scan_status",
        "scan_type",
        "SCAN_TYPE",
    ):
        value = _text_from_field_value(row.get(field_name))
        if value:
            return value
    return ""


def _console_waybill_records(rows: list[dict[str, Any]], *, target_date: dt.date) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    identities: set[str] = set()
    for row in rows:
        waybill_no = _normalize_waybill(row.get(INDEX_FIELD_NAME))
        if not waybill_no:
            raise ValueError("Ronghui send-order row has no waybill identity")
        if waybill_no in identities:
            raise ValueError("Ronghui send-order identity is duplicated")
        identities.add(waybill_no)
        records.append(
            {
                "waybill_no": waybill_no,
                "destination_site": row.get("目的网点", ""),
                "open_date": _date_from_value(row.get(DATE_FIELD_NAME)) or target_date.isoformat(),
                "receiver_address": row.get("收件地址", ""),
                "receiver_name": row.get("收货人", ""),
                "receiver_phone": row.get("收货电话", ""),
                "sender_name": row.get("寄件人", ""),
                "sender_phone": row.get("寄件手机", ""),
                "sender_address": row.get("寄件地址", ""),
                "goods_name_lines": row.get("货物名称", ""),
                "package_type_lines": row.get("包装类型", ""),
                "quantity_lines": _number_text(row.get("件数")),
                "weight_volume": _weight_volume_text(row),
                "delivery_method": row.get("派送方式", ""),
                "recorded_cost": _number_text(row.get("录单运费")),
                "freight_fee": _number_text(row.get("运费")),
                "pickup_fee": "",
                "delivery_fee": "",
                "transfer_fee": "",
                "payment_method": row.get("支付类型", ""),
                "insurance_amount": _number_text(row.get("保价金额")),
                "cod_amount": _number_text(row.get("代收货款")),
                "to_pay_amount": _number_text(row.get("到付款")),
                "receipt_required": normalize_receipt_required(row.get("需要回单")),
                "receipt_number": _text_from_field_value(row.get("回单号")),
                "remark": row.get("备注", ""),
                "scan_status": _scan_status_text(row),
                "status": _console_status_from_sign_status(row.get("签收状态")),
            }
        )
    return records


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for value in (
        payload.get("data", {}).get("items") if isinstance(payload.get("data"), dict) else None,
        payload.get("items"),
        payload.get("records"),
    ):
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _build_request_body(params: dict[str, Any], target_date: dt.date) -> dict[str, Any]:
    request_body = params.get("request_body") if isinstance(params.get("request_body"), dict) else {}
    request_params = dict(request_body.get("params") or {})
    request_params = bind_explicit_account_id(
        request_params,
        require_explicit_account_id(params, label="寄件同步"),
        label="寄件请求",
    )
    request_params["target_date"] = target_date.isoformat()
    request_params.setdefault("referer", "https://tms.ronghuiwl.com/widget/home")
    if "page_size" not in request_params and "pageSize" not in request_params:
        request_params["page_size"] = params.get("page_size", DEFAULT_PAGE_SIZE)
    if "max_pages" not in request_params:
        request_params["max_pages"] = params.get("max_pages", DEFAULT_MAX_PAGES)
    for key in ("referer", "extra_filters", "session_profile"):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
    return {
        "params": request_params,
        "timeout_sec": int(request_body.get("timeout_sec", params.get("timeout_sec", 1800)) or 1800),
        "client_timeout_sec": int(request_body.get("client_timeout_sec", params.get("client_timeout_sec", 1860)) or 1860),
    }


def _summarize_date_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "target_date",
        "ok",
        "fetched",
        "updates",
        "creates",
        "written",
        "deleted",
        "dedup_deleted",
        "sql_upserted",
        "sql_deleted_stale",
        "raw_fetched",
        "skipped_receipt_like",
        "dry_run",
        "sql_only",
        "planned",
        "planned_updates",
        "planned_creates",
        "planned_deletes",
        "planned_sql_upserts",
        "planned_sql_deletes",
        "error",
        "error_code",
    )
    return {key: result.get(key) for key in keys if result.get(key) not in (None, "")}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

