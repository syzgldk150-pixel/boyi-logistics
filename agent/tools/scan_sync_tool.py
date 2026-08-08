"""Phase 7 scan sync: refresh scan data, then execute scan_next in batches."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import child_items_from_scan_rows, normalize_scan_rows, replace_scan_codes
from tools.phase7_sync_common import tms_auth_error_result
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
        return [items]
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def _resolve_batch_size(params: dict) -> int:
    return int(
        params.get("batch_size")
        or params.get("scan_next_batch_size")
        or 50
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
    for key in ("session_profile", "account_id", "accountId"):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
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
    if result.get("error"):
        return str(result.get("error"))
    if result.get("ok"):
        return ""
    return str(result.get("message") or "scan_next failed")


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
    _emit_progress("开始获取扫描数据")
    request_body = dict(params.get("request_body") or {})
    request_params = dict(request_body.get("params") or {})
    request_params.setdefault("output_format", "json")
    for key in ("session_profile", "account_id", "accountId"):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
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

    child_items = child_items_from_scan_rows(
        normalized_rows,
        limit=params.get("child_item_limit"),
    )
    skip_bill_codes = _coerce_code_set(params.get("skip_bill_codes") or params.get("skip_codes"))
    if skip_bill_codes:
        before_skip = len(child_items)
        child_items = [item for item in child_items if str(item.get("bill_code") or "").strip() not in skip_bill_codes]
        _emit_progress("已跳过指定扫描单号", skipped=before_skip - len(child_items))
    batch_size = _resolve_batch_size(params)
    batches = _chunk(child_items, batch_size)
    if params.get("max_batches") not in (None, ""):
        batches = batches[: int(params.get("max_batches"))]
    _emit_progress("已整理子单批次", batch_size=batch_size, max_batches=len(batches))

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
        batch_results.append({"batch": index, "items": len(items), "ok": _scan_next_ok(result), "raw": result})
        detail = _scan_next_detail(result)
        if isinstance(detail, list):
            for item in detail:
                message = str(item.get("message") or item.get("error") or "")
                if "已做过签收" in message:
                    skipped_signed_count += 1
                    if item.get("bill_code"):
                        skipped_signed_codes.append(str(item["bill_code"]))
        _emit_progress("scan_next 批次完成", batch=index, ok=_scan_next_ok(result))

    if params.get("trigger_flow"):
        _emit_progress("触发遗留后续流程")
        flow_result = _trigger_scan_flow(params)
        _emit_progress("遗留后续流程返回", ok=flow_result.get("ok"), skipped=flow_result.get("skipped"))
    else:
        flow_result = {"ok": False, "skipped": True, "reason": "disabled"}
        _emit_progress("跳过遗留后续流程")

    _emit_progress("扫描任务结束")
    return {
        "ok": True,
        "fetched": len(rows),
        "normalized": len(normalized_rows),
        "child_items": len(child_items),
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
