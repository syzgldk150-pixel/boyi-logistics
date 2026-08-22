"""Phase 7 scan sync: refresh scan data, then execute scan_next in batches."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import child_items_from_scan_rows, normalize_scan_rows, replace_scan_codes
from tools.phase7_sync_common import (
    bind_explicit_account_id,
    normalize_explicit_account_params,
    tms_auth_error_result,
)
from tools.tms_tool import call_http_service


def _emit_progress(message: str, **extra: Any) -> None:
    payload = " | ".join(f"{key}={value}" for key, value in extra.items() if value not in (None, ""))
    text = f"[progress] {message}"
    if payload:
        text = f"{text} | {payload}"
    print(text, file=sys.stderr, flush=True)


def _extract_rows(tms_result: dict) -> list[dict] | None:
    rows = tms_result.get("data") if isinstance(tms_result, dict) else None
    if isinstance(rows, list):
        return rows
    if isinstance(tms_result, list):
        return tms_result
    return None


def _chunk(items: list[dict[str, str]], batch_size: int) -> list[list[dict[str, str]]]:
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def _resolve_batch_size(params: dict) -> int:
    return int(
        params.get("batch_size")
        or params.get("scan_next_batch_size")
        or 50
    )


def _resolve_get_scan_request_params(params: dict, request_body: dict) -> dict[str, Any]:
    request_params = dict(request_body.get("params") or {})
    request_params.setdefault("output_format", "json")

    target_date = str(params.get("target_date") or "").strip()
    if target_date:
        conflicting_fields = [
            key
            for key in ("date", "start", "end")
            if request_params.get(key) not in (None, "")
        ]
        if conflicting_fields:
            joined_fields = ", ".join(conflicting_fields)
            raise ValueError(
                "target_date 不能与 request_body.params 中的日期参数同时设置："
                f"{joined_fields}"
            )
        try:
            parsed_date = dt.date.fromisoformat(target_date)
        except ValueError as exc:
            raise ValueError("target_date 必须是 YYYY-MM-DD 格式") from exc
        request_params["date"] = parsed_date.strftime("%Y/%m/%d")

    return bind_explicit_account_id(
        request_params,
        str(params["account_id"]),
        label="扫描同步 get_scan 请求",
    )


def _coerce_code_set(value: Any) -> set[str]:
    if value in (None, ""):
        return set()
    if isinstance(value, str):
        normalized = value.replace(",", "\n").replace("，", "\n").replace(" ", "\n")
        return {item.strip() for item in normalized.splitlines() if item.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return {str(value).strip()} if str(value).strip() else set()


def _scan_next_payload(items: list[dict[str, str]], params: dict) -> dict:
    request_body = dict(params.get("scan_next_request_body") or {})
    request_params = dict(request_body.get("params") or {})
    request_params["items"] = items
    request_params = bind_explicit_account_id(
        request_params,
        str(params["account_id"]),
        label="扫描同步 scan_next 请求",
    )
    return {
        "params": request_params,
        "timeout_sec": int(request_body.get("timeout_sec") or params.get("scan_next_timeout_sec") or 3600),
        "client_timeout_sec": int(
            request_body.get("client_timeout_sec") or params.get("scan_next_client_timeout_sec") or 3660
        ),
    }


def _scan_next_ok(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    return bool(result.get("ok"))


def _scan_next_error(result: Any) -> str:
    if not isinstance(result, dict):
        return "invalid_result"
    for key in ("error", "message", "reason"):
        value = result.get(key)
        if value:
            return str(value).strip()
    for nested_key in ("data", "detail", "raw"):
        nested = result.get(nested_key)
        if isinstance(nested, dict):
            nested_error = _scan_next_error(nested)
            if nested_error not in ("", "scan_next failed"):
                return nested_error
    return "" if result.get("ok") else "scan_next failed"


def _scan_next_detail(result: dict[str, Any]) -> Any:
    if not isinstance(result, dict):
        return {}
    return result.get("detail") or result.get("data") or {}


def _trigger_scan_flow(params: dict) -> dict:
    resource = get_workflow_resource("phase7.scan_flow_webhook")
    if not resource or not resource.get("url"):
        return {"ok": False, "skipped": True, "reason": "missing_resource"}
    return feishu_operation(
        "trigger_webhook",
        {
            "url": resource["url"],
            "payload": params.get("flow_payload"),
            "timeout": params.get("flow_timeout", 20),
            "dry_run": bool(params.get("dry_run", False)),
        },
    )


def run_scan_sync(params: dict) -> dict:
    params = normalize_explicit_account_params(dict(params), label="扫描同步")
    _emit_progress("开始获取扫描数据")
    request_body = dict(params.get("request_body") or {})
    request_params = _resolve_get_scan_request_params(params, request_body)
    get_scan_result = call_http_service(
        "/get_scan",
        {
            "params": request_params,
            "timeout_sec": int(request_body.get("timeout_sec") or 600),
        },
    )
    if auth_error := tms_auth_error_result(get_scan_result):
        return auth_error
    rows = _extract_rows(get_scan_result)
    if rows is None:
        return {"error": "get_scan 返回格式异常", "raw": get_scan_result}
    _emit_progress("扫描数据已拉取", rows=len(rows))

    normalized_rows = normalize_scan_rows(rows)
    if bool(params.get("dry_run", False)):
        replace_result = {"ok": True, "replaced": len(normalized_rows), "skipped": True}
    else:
        replace_result = replace_scan_codes(normalized_rows)
    _emit_progress("扫描索引已刷新", replaced=replace_result.get("replaced"))
    if not replace_result.get("ok"):
        index_error = _scan_next_error(replace_result)
        return {
            "ok": False,
            "error_code": "SCAN_INDEX_REFRESH_FAILED",
            "error": f"扫描索引刷新失败：{index_error}",
            "fetched": len(rows),
            "normalized": len(normalized_rows),
            "scan_index_result": replace_result,
        }

    candidate_items = child_items_from_scan_rows(normalized_rows)
    skip_bill_codes = _coerce_code_set(params.get("skip_bill_codes") or params.get("skip_codes"))
    if skip_bill_codes:
        before_skip = len(candidate_items)
        candidate_items = [
            item
            for item in candidate_items
            if str(item.get("bill_code") or "").strip() not in skip_bill_codes
        ]
        _emit_progress("已跳过指定扫描单号", skipped=before_skip - len(candidate_items))
    child_items = list(candidate_items)
    if params.get("child_item_limit") not in (None, ""):
        child_item_limit = int(params.get("child_item_limit"))
        if child_item_limit <= 0:
            raise ValueError("child_item_limit 必须大于 0")
        child_items = child_items[:child_item_limit]
    batch_size = _resolve_batch_size(params)
    batches = _chunk(child_items, batch_size)
    total_batches = len(batches)
    if params.get("max_batches") not in (None, ""):
        max_batches = int(params.get("max_batches"))
        if max_batches <= 0:
            raise ValueError("max_batches 必须大于 0")
        batches = batches[:max_batches]
    scheduled_items = sum(len(batch) for batch in batches)
    omitted_items = len(candidate_items) - scheduled_items
    _emit_progress(
        "已整理子单批次",
        batch_size=batch_size,
        scheduled_batches=len(batches),
        total_batches=total_batches,
        omitted_items=omitted_items,
    )

    if bool(params.get("dry_run", False)):
        _emit_progress("演练模式，不执行 scan_next")
        return {
            "ok": True,
            "dry_run": True,
            "fetched": len(rows),
            "normalized": len(normalized_rows),
            "candidate_items": len(candidate_items),
            "child_items": len(child_items),
            "scheduled_items": scheduled_items,
            "omitted_items": omitted_items,
            "truncated": omitted_items > 0,
            "batches": len(batches),
            "batch_results": [],
            "scan_index_result": replace_result,
            "flow_result": {"ok": False, "skipped": True, "reason": "dry_run"},
            "skipped_signed_count": 0,
            "skipped_signed_codes": [],
        }

    batch_results = []
    skipped_signed_count = 0
    skipped_signed_codes: list[str] = []
    for index, items in enumerate(batches, start=1):
        if not items:
            continue
        _emit_progress("即将执行 scan_next", batch=index, items=len(items))
        result = call_http_service("/scan_next", _scan_next_payload(items, params))
        if auth_error := tms_auth_error_result(result):
            return auth_error
        batch_ok = _scan_next_ok(result)
        batch_results.append({"batch": index, "items": len(items), "ok": batch_ok, "raw": result})
        detail = _scan_next_detail(result)
        if isinstance(detail, list):
            for item in detail:
                message = str(item.get("message") or item.get("error") or "")
                if "已做过签收" in message:
                    skipped_signed_count += 1
                    if item.get("bill_code"):
                        skipped_signed_codes.append(str(item["bill_code"]))
        _emit_progress("scan_next 批次完成", batch=index, ok=batch_ok)
        if not batch_ok:
            error = _scan_next_error(result)
            _emit_progress("scan_next 批次失败，停止后续执行", batch=index, error=error)
            return {
                "ok": False,
                "error_code": "SCAN_NEXT_BATCH_FAILED",
                "error": f"scan_next 第 {index} 批失败：{error}",
                "failed_batch": index,
                "fetched": len(rows),
                "normalized": len(normalized_rows),
                "candidate_items": len(candidate_items),
                "child_items": len(child_items),
                "scheduled_items": scheduled_items,
                "omitted_items": omitted_items,
                "truncated": omitted_items > 0,
                "batches": len(batch_results),
                "batch_results": batch_results,
                "scan_index_result": replace_result,
                "flow_result": {"ok": False, "skipped": True, "reason": "batch_failed"},
                "skipped_signed_count": skipped_signed_count,
                "skipped_signed_codes": skipped_signed_codes,
            }

    if params.get("trigger_flow"):
        _emit_progress("触发遗留后续流程")
        flow_result = _trigger_scan_flow(params)
        _emit_progress("遗留后续流程返回", ok=flow_result.get("ok"), skipped=flow_result.get("skipped"))
        if not flow_result.get("ok"):
            flow_error = _scan_next_error(flow_result)
            return {
                "ok": False,
                "error_code": "SCAN_FOLLOWUP_FAILED",
                "error": f"扫描已完成，但后续流程失败：{flow_error}",
                "fetched": len(rows),
                "normalized": len(normalized_rows),
                "candidate_items": len(candidate_items),
                "child_items": len(child_items),
                "scheduled_items": scheduled_items,
                "omitted_items": omitted_items,
                "truncated": omitted_items > 0,
                "batches": len(batch_results),
                "batch_results": batch_results,
                "scan_index_result": replace_result,
                "flow_result": flow_result,
                "skipped_signed_count": skipped_signed_count,
                "skipped_signed_codes": skipped_signed_codes,
            }
    else:
        flow_result = {"ok": False, "skipped": True, "reason": "disabled"}
        _emit_progress("跳过遗留后续流程")

    _emit_progress("扫描任务结束")
    return {
        "ok": True,
        "fetched": len(rows),
        "normalized": len(normalized_rows),
        "candidate_items": len(candidate_items),
        "child_items": len(child_items),
        "scheduled_items": scheduled_items,
        "omitted_items": omitted_items,
        "truncated": omitted_items > 0,
        "batches": len(batch_results),
        "batch_results": batch_results,
        "scan_index_result": replace_result,
        "flow_result": flow_result,
        "skipped_signed_count": skipped_signed_count,
        "skipped_signed_codes": skipped_signed_codes,
    }


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_scan_sync(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
