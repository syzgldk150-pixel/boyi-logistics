"""Phase 7 第一批：获取当日寄件数据 -> 写入飞书多维表格。"""

import datetime as dt
import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import is_receipt_like_tracking, sync_console_waybills
from tools.phase7_sync_common import tms_auth_error_result
from tools.tms_tool import call_http_service


DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 50
MAX_FEISHU_LIST_LIMIT = 200
LOCK_FILE = os.path.join(PROJECT_ROOT, "logs", ".send_order_sync.lock")
INDEX_FIELD_NAME = "运单编号"
DATE_FIELD_NAME = "发件日期"
DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")


@contextmanager
def _sync_lock():
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


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


def _to_number(value: Any) -> int | float | None:
    if value in (None, "", "null"):
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(number)


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
        "收货人": row.get("收货人", ""),
        "收货电话": row.get("收货电话", ""),
        "货物名称": row.get("货物名称", ""),
        "包装类型": row.get("包装类型", ""),
        "派送方式": row.get("派送方式", ""),
        "支付类型": row.get("支付类型", ""),
        "回单号": row.get("回单号", ""),
        "备注": row.get("备注", ""),
        "件数": _to_number(row.get("件数")),
        "实际重量": _to_number(row.get("实际重量")),
        "录单金额": _to_number(row.get("录单金额")),
        "体积重量": _to_number(row.get("体积重量")),
        "体积": _to_number(row.get("体积")),
        "结算重量": _to_number(row.get("结算重量")),
        "到付款": _to_number(row.get("到付款")),
    }


def _transform_rows(rows: list[dict], *, target_date: dt.date) -> list[dict]:
    return [{"fields": _record_fields(row, target_date=target_date)} for row in rows]


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
        ("体积", "体积"),
        ("结算重量", "结算重量"),
        ("体积重", "体积重量"),
    ):
        value = _text_from_field_value(row.get(field_name))
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
    for row in rows:
        waybill_no = _normalize_waybill(row.get(INDEX_FIELD_NAME))
        if not waybill_no:
            continue
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
                "goods_name_lines": row.get("货物名称", ""),
                "package_type_lines": row.get("包装类型", ""),
                "quantity_lines": row.get("件数", ""),
                "weight_volume": _weight_volume_text(row),
                "delivery_method": row.get("派送方式", ""),
                "freight_fee": row.get("录单金额", ""),
                "pickup_fee": "",
                "delivery_fee": "",
                "transfer_fee": "",
                "payment_method": row.get("支付类型", ""),
                "insurance_amount": "",
                "cod_amount": row.get("到付款", ""),
                "remark": row.get("备注", ""),
                "scan_status": _scan_status_text(row),
                "status": _console_status_from_sign_status(row.get("签收状态")),
            }
        )
    return records


def _filter_receipt_like_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    valid_rows: list[dict] = []
    skipped_codes: list[str] = []
    for row in rows:
        waybill_no = _normalize_waybill(row.get(INDEX_FIELD_NAME))
        if is_receipt_like_tracking(waybill_no):
            skipped_codes.append(waybill_no)
            continue
        valid_rows.append(row)
    return valid_rows, skipped_codes


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


def _list_existing_records_for_date(
    base_token: str,
    table_id: str,
    params: dict[str, Any],
    target_date: dt.date,
) -> tuple[dict[str, str] | None, list[str], dict[str, Any]]:
    # Feishu Bitable record list is effectively capped at 200 rows in this
    # workflow. Requesting 500 can still return only 200 rows, and treating
    # that as the last page causes old records after row 200 to be missed and
    # recreated as duplicates.
    limit = min(int(params.get("list_limit") or MAX_FEISHU_LIST_LIMIT), MAX_FEISHU_LIST_LIMIT)
    max_pages = int(params.get("list_max_pages") or 50)
    existing_by_waybill: dict[str, str] = {}
    all_date_record_ids: list[str] = []
    duplicate_record_ids: list[str] = []
    pages: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    offset = 0
    for _page_index in range(max_pages):
        list_result = feishu_operation(
            "list_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "limit": limit,
                "offset": offset,
                "as": params.get("as", "bot"),
            },
        )
        pages.append(list_result)
        if "error" in list_result:
            return None, [], {"pages": pages, "error": list_result.get("error")}
        items = _extract_items(list_result)
        new_items: list[dict[str, Any]] = []
        for item in items:
            record_id = str(item.get("record_id") or "").strip()
            if record_id and record_id in seen_record_ids:
                continue
            if record_id:
                seen_record_ids.add(record_id)
            new_items.append(item)
        if items and not new_items:
            break
        for item in new_items:
            record_id = str(item.get("record_id") or "").strip()
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            if not record_id or _date_from_value(fields.get(DATE_FIELD_NAME)) != target_date.isoformat():
                continue
            all_date_record_ids.append(record_id)
            waybill_no = _normalize_waybill(fields.get(INDEX_FIELD_NAME))
            if waybill_no and waybill_no not in existing_by_waybill:
                existing_by_waybill[waybill_no] = record_id
            elif waybill_no:
                duplicate_record_ids.append(record_id)
        if len(items) < limit:
            break
        offset += limit
    return existing_by_waybill, all_date_record_ids, {
        "pages": pages,
        "indexed": len(existing_by_waybill),
        "scanned": len(seen_record_ids),
        "list_limit": limit,
        "target_date_record_ids": all_date_record_ids,
        "duplicate_record_ids": duplicate_record_ids,
    }


def _cleanup_duplicate_records_for_date(
    base_token: str,
    table_id: str,
    params: dict[str, Any],
    target_date: dt.date,
) -> dict[str, Any]:
    existing_by_waybill, all_date_record_ids, list_result = _list_existing_records_for_date(
        base_token,
        table_id,
        params,
        target_date,
    )
    if existing_by_waybill is None:
        return {"error": "飞书复扫同日重复寄件记录失败", "list_result": list_result}

    duplicate_record_ids = [
        str(record_id)
        for record_id in list_result.get("duplicate_record_ids", [])
        if str(record_id or "").strip()
    ]
    if not duplicate_record_ids:
        return {
            "ok": True,
            "deleted": 0,
            "duplicates": 0,
            "indexed": len(existing_by_waybill),
            "target_date_records": len(all_date_record_ids),
            "list_result": list_result,
        }

    _emit_progress("开始清理同日重复寄件单号", count=len(duplicate_record_ids), target_date=target_date.isoformat())
    delete_result = feishu_operation(
        "delete_records",
        {
            "base_token": base_token,
            "table_id": table_id,
            "record_ids": duplicate_record_ids,
            "as": params.get("as", "bot"),
        },
    )
    if "error" in delete_result or delete_result.get("errors"):
        return {
            "error": "飞书清理同日重复寄件记录失败",
            "duplicate_record_ids": duplicate_record_ids,
            "delete_result": delete_result,
            "list_result": list_result,
        }
    return {
        "ok": True,
        "deleted": delete_result.get("deleted", 0),
        "duplicates": len(duplicate_record_ids),
        "duplicate_record_ids": duplicate_record_ids,
        "delete_result": delete_result,
        "list_result": list_result,
    }


def _build_records_for_replace(
    rows: list[dict],
    *,
    existing_by_waybill: dict[str, str],
    target_date: dt.date,
) -> tuple[list[dict[str, Any]], int, int, set[str]]:
    records: list[dict[str, Any]] = []
    update_count = 0
    create_count = 0
    kept_record_ids: set[str] = set()
    seen_waybills: set[str] = set()
    for row in rows:
        waybill_no = _normalize_waybill(row.get(INDEX_FIELD_NAME))
        if not waybill_no or waybill_no in seen_waybills:
            continue
        seen_waybills.add(waybill_no)
        record = {"fields": _record_fields(row, target_date=target_date)}
        record_id = existing_by_waybill.get(waybill_no)
        if record_id:
            record["record_id"] = record_id
            kept_record_ids.add(record_id)
            update_count += 1
        else:
            create_count += 1
        records.append(record)
    return records, update_count, create_count, kept_record_ids


def _build_request_body(params: dict[str, Any], target_date: dt.date) -> dict[str, Any]:
    request_body = params.get("request_body") if isinstance(params.get("request_body"), dict) else {}
    request_params = dict(request_body.get("params") or {})
    request_params["target_date"] = target_date.isoformat()
    request_params.setdefault("referer", "https://tms.ronghuiwl.com/widget/home")
    if "page_size" not in request_params and "pageSize" not in request_params:
        request_params["page_size"] = params.get("page_size", DEFAULT_PAGE_SIZE)
    if "max_pages" not in request_params:
        request_params["max_pages"] = params.get("max_pages", DEFAULT_MAX_PAGES)
    for key in ("referer", "extra_filters", "session_profile", "account_id", "accountId"):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
    return {
        "params": request_params,
        "timeout_sec": int(request_body.get("timeout_sec", params.get("timeout_sec", 1800)) or 1800),
        "client_timeout_sec": int(request_body.get("client_timeout_sec", params.get("client_timeout_sec", 1860)) or 1860),
    }


def _run_send_order_sync_for_date(params: dict[str, Any], target_date: dt.date) -> dict[str, Any]:
    _emit_progress("开始拉取融辉寄件数据", target_date=target_date.isoformat())
    tms_result = call_http_service("/send_order", _build_request_body(params, target_date))
    if auth_error := tms_auth_error_result(tms_result):
        return auth_error
    rows = tms_result.get("data") if isinstance(tms_result, dict) else None
    if not isinstance(rows, list):
        return {"error": "send_order 返回格式异常", "raw": tms_result}
    raw_fetched = len(rows)
    rows, skipped_receipt_codes = _filter_receipt_like_rows([row for row in rows if isinstance(row, dict)])
    _emit_progress(
        "融辉寄件数据拉取完成",
        rows=len(rows),
        raw_fetched=raw_fetched,
        skipped_receipt_like=len(skipped_receipt_codes),
        target_date=target_date.isoformat(),
    )

    if params.get("sql_only"):
        console_records = _console_waybill_records(rows, target_date=target_date)
        if params.get("dry_run"):
            return {
                "ok": True,
                "dry_run": True,
                "sql_only": True,
                "target_date": target_date.isoformat(),
                "fetched": len(rows),
                "raw_fetched": raw_fetched,
                "skipped_receipt_like": len(skipped_receipt_codes),
                "planned": 0,
                "planned_updates": 0,
                "planned_creates": 0,
                "planned_deletes": 0,
                "planned_sql_upserts": len(console_records),
                "planned_sql_deletes": len(console_records),
            }
        sql_result: dict[str, Any] = {"ok": True, "skipped": True, "upserted": 0, "deleted_stale": 0}
        if params.get("sync_sql") is not False:
            try:
                sql_result = sync_console_waybills(
                    console_records,
                    source="ronghui",
                    target_date=target_date,
                    replace_date=True,
                )
            except Exception as exc:
                return {
                    "error": "SQL 写入融辉寄件运单失败",
                    "sql_only": True,
                    "fetched": len(rows),
                    "sql_error": str(exc),
                }
        _emit_progress("融辉寄件数据 SQL 回填完成", sql_upserted=sql_result.get("upserted", 0), sql_deleted_stale=sql_result.get("deleted_stale", 0))
        return {
            "ok": True,
            "sql_only": True,
            "target_date": target_date.isoformat(),
            "fetched": len(rows),
            "raw_fetched": raw_fetched,
            "skipped_receipt_like": len(skipped_receipt_codes),
            "updates": 0,
            "creates": 0,
            "written": 0,
            "deleted": 0,
            "sql_upserted": sql_result.get("upserted", 0),
            "sql_updates": sql_result.get("updates", 0),
            "sql_creates": sql_result.get("creates", 0),
            "sql_deleted_stale": sql_result.get("deleted_stale", 0),
            "sql_result": sql_result,
        }

    base_token, table_id = _resolve_bitable_target(params)
    existing_by_waybill, all_date_record_ids, list_result = _list_existing_records_for_date(
        base_token,
        table_id,
        params,
        target_date,
    )
    if existing_by_waybill is None:
        return {"error": "飞书读取同日寄件旧记录失败", "fetched": len(rows), "list_result": list_result}
    records, update_count, create_count, kept_record_ids = _build_records_for_replace(
        rows,
        existing_by_waybill={},
        target_date=target_date,
    )
    stale_record_ids = list(all_date_record_ids)

    if params.get("dry_run"):
        console_records = _console_waybill_records(rows, target_date=target_date)
        return {
            "ok": True,
            "dry_run": True,
            "target_date": target_date.isoformat(),
            "fetched": len(rows),
            "raw_fetched": raw_fetched,
            "skipped_receipt_like": len(skipped_receipt_codes),
            "planned": len(records),
            "planned_updates": update_count,
            "planned_creates": create_count,
            "planned_deletes": len(stale_record_ids),
            "planned_sql_upserts": len(console_records),
            "planned_sql_deletes": len(stale_record_ids),
            "list_result": list_result,
        }

    delete_result: dict[str, Any] = {"ok": True, "requested": 0, "deleted": 0, "results": []}
    if stale_record_ids:
        delete_result = feishu_operation(
            "delete_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "record_ids": stale_record_ids,
                "as": params.get("as", "bot"),
            },
        )
        if "error" in delete_result or delete_result.get("errors"):
            return {
                "error": "delete target-date send order records failed",
                "fetched": len(rows),
                "updates": update_count,
                "creates": create_count,
                "stale_record_ids": stale_record_ids,
                "delete_result": delete_result,
            }
    write_result: dict[str, Any] = {"ok": True, "requested": 0, "written": 0, "results": []}
    if records:
        _emit_progress("开始写入融辉寄件数据", rows=len(records), updates=update_count, creates=create_count)
        write_result = feishu_operation(
            "write_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "records": records,
                "as": params.get("as", "bot"),
            },
        )
        if "error" in write_result or write_result.get("errors"):
            return {
                "error": "飞书写入失败",
                "fetched": len(rows),
                "updates": update_count,
                "creates": create_count,
                "deleted": delete_result.get("deleted", 0),
                "delete_result": delete_result,
                "feishu_result": write_result,
            }

    duplicate_cleanup_result = _cleanup_duplicate_records_for_date(base_token, table_id, params, target_date)
    if "error" in duplicate_cleanup_result:
        return {
            "error": duplicate_cleanup_result["error"],
            "fetched": len(rows),
            "updates": update_count,
            "creates": create_count,
            "written": write_result.get("written", 0),
            "deleted": delete_result.get("deleted", 0),
            "duplicate_cleanup_result": duplicate_cleanup_result,
        }
    sql_result: dict[str, Any] = {"ok": True, "skipped": True, "upserted": 0, "deleted_stale": 0}
    if params.get("sync_sql") is not False:
        console_records = _console_waybill_records(rows, target_date=target_date)
        try:
            sql_result = sync_console_waybills(
                console_records,
                source="ronghui",
                target_date=target_date,
                replace_date=True,
            )
        except Exception as exc:
            return {
                "error": "SQL 写入融辉寄件运单失败",
                "fetched": len(rows),
                "updates": update_count,
                "creates": create_count,
                "written": write_result.get("written", 0),
                "deleted": delete_result.get("deleted", 0),
                "sql_error": str(exc),
                "write_result": write_result,
                "delete_result": delete_result,
            }
    _emit_progress(
        "融辉寄件数据同步完成",
        written=write_result.get("written", 0),
        deleted=delete_result.get("deleted", 0),
        dedup_deleted=duplicate_cleanup_result.get("deleted", 0),
        sql_upserted=sql_result.get("upserted", 0),
    )
    return {
        "ok": True,
        "target_date": target_date.isoformat(),
        "fetched": len(rows),
        "raw_fetched": raw_fetched,
        "skipped_receipt_like": len(skipped_receipt_codes),
        "updates": update_count,
        "creates": create_count,
        "written": write_result.get("written"),
        "deleted": delete_result.get("deleted", 0),
        "dedup_deleted": duplicate_cleanup_result.get("deleted", 0),
        "sql_upserted": sql_result.get("upserted", 0),
        "sql_updates": sql_result.get("updates", 0),
        "sql_creates": sql_result.get("creates", 0),
        "sql_deleted_stale": sql_result.get("deleted_stale", 0),
        "list_result": list_result,
        "write_result": write_result,
        "delete_result": delete_result,
        "duplicate_cleanup_result": duplicate_cleanup_result,
        "sql_result": sql_result,
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


def _run_date_range(params: dict[str, Any], dates: list[dt.date]) -> dict[str, Any]:
    _emit_progress(
        "开始范围同步融辉寄件数据",
        start_date=dates[0].isoformat(),
        end_date=dates[-1].isoformat(),
        days=len(dates),
    )
    results: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for target_date in dates:
        result = _run_send_order_sync_for_date(params, target_date)
        results.append(result)
        summaries.append(_summarize_date_result(result))
        if result.get("error") or not result.get("ok"):
            return {
                "error": f"融辉寄件数据范围同步失败，失败日期：{target_date.isoformat()}",
                "failed_date": target_date.isoformat(),
                "start_date": dates[0].isoformat(),
                "end_date": dates[-1].isoformat(),
                "completed_days": len(summaries) - 1,
                "per_date": summaries,
                "raw_error": result,
            }
    payload = {
        "ok": True,
        "start_date": dates[0].isoformat(),
        "end_date": dates[-1].isoformat(),
        "days": len(dates),
        "fetched": sum(_safe_int(result.get("fetched")) for result in results),
        "raw_fetched": sum(_safe_int(result.get("raw_fetched")) for result in results),
        "skipped_receipt_like": sum(_safe_int(result.get("skipped_receipt_like")) for result in results),
        "updates": sum(_safe_int(result.get("updates")) for result in results),
        "creates": sum(_safe_int(result.get("creates")) for result in results),
        "written": sum(_safe_int(result.get("written")) for result in results),
        "deleted": sum(_safe_int(result.get("deleted")) for result in results),
        "sql_upserted": sum(_safe_int(result.get("sql_upserted")) for result in results),
        "sql_deleted_stale": sum(_safe_int(result.get("sql_deleted_stale")) for result in results),
        "per_date": summaries,
    }
    if params.get("dry_run"):
        payload.update(
            {
                "dry_run": True,
                "planned": sum(_safe_int(result.get("planned")) for result in results),
                "planned_updates": sum(_safe_int(result.get("planned_updates")) for result in results),
                "planned_creates": sum(_safe_int(result.get("planned_creates")) for result in results),
                "planned_deletes": sum(_safe_int(result.get("planned_deletes")) for result in results),
                "planned_sql_upserts": sum(_safe_int(result.get("planned_sql_upserts")) for result in results),
                "planned_sql_deletes": sum(_safe_int(result.get("planned_sql_deletes")) for result in results),
            }
        )
    return payload


def run_send_order_sync(params: dict) -> dict:
    params = params or {}
    try:
        dates = _date_range(params)
    except ValueError as exc:
        return {"error": str(exc)}
    with _sync_lock():
        if len(dates) == 1:
            return _run_send_order_sync_for_date(params, dates[0])
        return _run_date_range(params, dates)


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_send_order_sync(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
