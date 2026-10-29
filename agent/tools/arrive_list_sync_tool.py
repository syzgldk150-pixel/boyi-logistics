"""Phase 7 arrive-list sync: fetch TMS dispatch forecast base rows."""

from __future__ import annotations

import sys
from datetime import date
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import (
    is_receipt_like_tracking,
    normalize_waybill_record,
)
from tools.daily_sign_values import business_now
from tools.phase7_sync_common import (
    bind_explicit_account_id,
    build_range_from_template,
    parse_a1_range,
    raise_tms_auth_error_if_present,
    require_explicit_account_id,
    resolve_sheet_target,
)
from tools.tms_tool import call_http_service


def _required_account_id(params: dict[str, Any]) -> str:
    return require_explicit_account_id(params, label="到货清单同步")


def _emit_progress(message: str, **extra: Any) -> None:
    payload = " | ".join(f"{key}={value}" for key, value in extra.items() if value not in (None, ""))
    text = f"[progress] {message}"
    if payload:
        text = f"{text} | {payload}"
    print(text, file=sys.stderr, flush=True)


def _extract_rows(tms_result: dict) -> list[Any] | None:
    rows = tms_result.get("data") if isinstance(tms_result, dict) else None
    if isinstance(rows, list):
        return rows
    if isinstance(tms_result, list):
        return tms_result
    return None


def _is_valid_main_waybill(code: str) -> bool:
    return bool(code) and not is_receipt_like_tracking(code)


def _build_dispatch_request(params: dict) -> dict:
    request_body = params.get("request_body", {"params": {}, "timeout_sec": 600})
    request_params = dict(request_body.get("params") or {})
    account_id = _required_account_id(params)
    request_params = bind_explicit_account_id(request_params, account_id, label="到货清单 request_body")
    if params.get("target_date") and "target_date" not in request_params:
        request_params["target_date"] = params["target_date"]
    for key in (
        "login_site_code",
        "loginSiteCode",
        "LOGIN_SITE_CODE",
        "page_size",
        "pageSize",
        "max_pages",
        "maxPages",
        "session_profile",
    ):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
    return {
        "params": request_params,
        "timeout_sec": int(request_body.get("timeout_sec", 900) or 900),
        "client_timeout_sec": int(request_body.get("client_timeout_sec", 960) or 960),
    }


def _target_date(params: dict) -> date:
    raw = str(params.get("target_date") or "").strip()
    if raw:
        return date.fromisoformat(raw)
    return business_now().date()


def _normalize_dispatch_records(rows: list[Any]) -> tuple[list[dict[str, Any]], list[str], int]:
    records_by_tracking: dict[str, dict[str, Any]] = {}
    skipped_receipt_codes: set[str] = set()
    invalid_rows = 0
    for row in rows:
        record = normalize_waybill_record(row)
        if not record:
            invalid_rows += 1
            continue
        tracking_number = str(record.get("tracking_number") or "").strip()
        if not _is_valid_main_waybill(tracking_number):
            if tracking_number:
                skipped_receipt_codes.add(tracking_number)
            continue
        previous = records_by_tracking.get(tracking_number)
        if previous is not None and previous != record:
            raise ValueError(f"派件预报存在重复冲突运单号: {tracking_number}")
        records_by_tracking[tracking_number] = record
    return list(records_by_tracking.values()), sorted(skipped_receipt_codes), invalid_rows


def fetch_arrive_list_records(
    params: dict,
    *,
    http_call: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch and normalize the target day's authoritative arrive-list rows."""
    _emit_progress("开始拉取派件预报")
    dispatch_result = (http_call or call_http_service)("/fetch_dispatch", _build_dispatch_request(params))
    raise_tms_auth_error_if_present(dispatch_result)
    rows = _extract_rows(dispatch_result)
    if rows is None:
        raise ValueError("fetch_dispatch 返回格式异常")
    _emit_progress("派件预报拉取完成", rows=len(rows))

    records, skipped_receipt_codes, invalid_rows = _normalize_dispatch_records(rows)
    if invalid_rows:
        raise ValueError(f"派件预报存在 {invalid_rows} 条缺少主单号或结构异常的记录，停止提交")
    result = {
        "ok": True,
        "source": "fetch_dispatch",
        "fetched": len(rows),
        "bill_codes": len(records),
        "skipped_receipt_like": len(skipped_receipt_codes),
        "invalid_rows": invalid_rows,
    }
    _emit_progress(
        "派件预报整理完成",
        tracking_number=len(records),
        skipped_receipt_like=len(skipped_receipt_codes),
        invalid_rows=invalid_rows,
    )
    return records, result


def _load_sheet_resource(resource_key: str) -> dict:
    resource = get_workflow_resource(resource_key)
    if not resource:
        raise ValueError(f"未找到资源 {resource_key}，请先导入到 MySQL")
    return resource


def _build_title(params: dict | None = None) -> list[str]:
    params = params or {}

    return [
        f"{_target_date(params):%m.%d}运单编号",
        "货物名称",
        "包装类型",
        "派送方式",
        "件数",
        "回单号",
        "实际重量",
        "体积",
        "备注",
        "目的站点",
        "收件人",
        "收件电话",
        "收件地址",
        "结算重量",
        "体积重",
        "运费",
        "支付类型",
        "到付款",
    ]


def _summarize_feishu_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return result
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    summary = {
        "ok": bool(result.get("ok")),
        "rows": result.get("rows"),
        "skipped": result.get("skipped"),
        "updatedCells": data.get("updatedCells"),
        "updatedRows": data.get("updatedRows"),
        "updatedColumns": data.get("updatedColumns"),
        "updatedRange": data.get("updatedRange"),
    }
    if result.get("error"):
        summary["error"] = result.get("error")
    return {key: value for key, value in summary.items() if value is not None}


def _write_sheet_resource(resource_key: str, rows: list[list[Any]], params: dict) -> dict:
    resource = _load_sheet_resource(resource_key)
    spreadsheet_token, template_range = resolve_sheet_target(
        {
            "spreadsheet_token": params.get("spreadsheet_token"),
            "range": params.get("range"),
        },
        resource_key,
    )
    clear_range = resource.get("clear_range")
    title_range = resource.get("title_range")

    clear_result = None
    if clear_range:
        shape = parse_a1_range(str(clear_range))
        blank_values = [["" for _ in range(shape["col_count"])] for _ in range(shape["row_count"])]
        clear_result = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": clear_range,
                "values": blank_values,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in clear_result:
            return {"error": "清空电子表格失败", "feishu_result": _summarize_feishu_result(clear_result)}

    write_result: dict[str, Any] = {"ok": True, "rows": 0, "skipped": True}
    if rows:
        write_range = build_range_from_template(
            template_range,
            len(rows),
            max(len(row) for row in rows),
        )
        write_result = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": write_range,
                "values": rows,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in write_result:
            return {
                "error": "写入电子表格失败",
                "clear_result": _summarize_feishu_result(clear_result),
                "feishu_result": _summarize_feishu_result(write_result),
            }

    title_result = None
    if title_range:
        title_result = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": title_range,
                "values": [_build_title(params)],
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in title_result:
            return {
                "error": "写入标题失败",
                "clear_result": _summarize_feishu_result(clear_result),
                "write_result": _summarize_feishu_result(write_result),
                "feishu_result": _summarize_feishu_result(title_result),
            }

    return {
        "ok": True,
        "rows": len(rows),
        "clear_result": _summarize_feishu_result(clear_result),
        "write_result": _summarize_feishu_result(write_result),
        "title_result": _summarize_feishu_result(title_result),
    }

