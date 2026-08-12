"""Synchronize and upload current split/undelivered Ronghui problem items."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from tools.feishu_cli_tool import feishu_operation
from tools.daily_sign_rules import is_before_problem_cutoff
from tools.daily_sign_store import upsert_problem_events
from tools.phase7_mysql_store import (
    list_split_pending_problem_items,
    replace_split_pending_problem_items,
    update_split_pending_combined_results,
)
from tools.phase7_sync_common import get_required_resource, tms_auth_error_result
from tools.split_pending_snapshot import (
    EXPECTED_SPREADSHEET_TOKEN,
    classify_sheet_values,
    clean_text as _clean_text,
    sync_target_sheet,
)
from tools.tms_tool import call_http_service


SOURCE_RESOURCE_KEY = "phase7.split_pending_source_sheet"
EXPECTED_SOURCE_SHEET_ID = "8fc516"
DEFAULT_ACCOUNT_ID = "ronghui_default"
UPLOAD_TIMEOUT_SEC = 7200


def _bool_param(params: dict[str, Any], key: str, default: bool = False) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _sheet_values(payload: Any) -> list[list[Any]]:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        value_range = payload.get("valueRange") if isinstance(payload.get("valueRange"), dict) else {}
        data_value_range = data.get("valueRange") if isinstance(data.get("valueRange"), dict) else {}
        candidates.extend(
            [
                data_value_range.get("values"),
                value_range.get("values"),
                data.get("values"),
                payload.get("values"),
            ]
        )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [row if isinstance(row, list) else [] for row in candidate]
    return []


def _sheet_ref(resource: dict[str, Any], expected_sheet_id: str, *, resource_key: str) -> tuple[str, str, str]:
    spreadsheet_token = _clean_text(resource.get("spreadsheet_token"))
    value_range = _clean_text(resource.get("range"))
    sheet_id = _clean_text(resource.get("sheet_id"))
    if not sheet_id and "!" in value_range:
        sheet_id = value_range.split("!", 1)[0]
    if not spreadsheet_token or not value_range or not sheet_id:
        raise ValueError(f"{resource_key} 缺少 spreadsheet_token、sheet_id 或 range")
    if spreadsheet_token != EXPECTED_SPREADSHEET_TOKEN:
        raise ValueError(f"{resource_key} 未绑定指定的每日到货文档")
    if sheet_id != expected_sheet_id:
        raise ValueError(f"{resource_key} 绑定了错误的 sheet_id: {sheet_id}")
    return spreadsheet_token, sheet_id, value_range


def _candidate_preview(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "bill_code": item["bill_code"],
            "problem_type": item["problem_type"],
            "expected_quantity": item["expected_quantity"],
            "arrived_quantity": item["arrived_quantity"],
            "pending_quantity": item["pending_quantity"],
            "problem_cause": item["problem_cause"],
            "status": item["candidate_status"],
            "complaint_status": item["complaint_status"],
            "problem_item_status": item["problem_item_status"],
        }
        for item in candidates
    ]


def _type_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "少货/分批": sum(1 for item in candidates if item.get("problem_type") == "少货/分批"),
        "有发未到": sum(1 for item in candidates if item.get("problem_type") == "有发未到"),
    }


def _stateful_candidates(
    candidates: list[dict[str, Any]],
    stored_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    stored_by_code = {
        _clean_text(record.get("tracking_number")): record
        for record in stored_records
        if _clean_text(record.get("tracking_number"))
    }
    eligible: list[dict[str, Any]] = []
    hidden_completed = 0
    for candidate in candidates:
        previous = stored_by_code.get(candidate["bill_code"])
        same_type = bool(previous and _clean_text(previous.get("problem_type")) == candidate["problem_type"])
        problem_status = _clean_text(previous.get("upload_status")) if same_type else "pending"
        if problem_status not in {"pending", "failed", "success"}:
            problem_status = "pending"
        if candidate["problem_type"] == "少货/分批":
            complaint_status = _clean_text(previous.get("complaint_status")) if same_type else "pending"
            if complaint_status not in {"pending", "failed", "success", "duplicate"}:
                complaint_status = "pending"
            complete = complaint_status in {"success", "duplicate"} and problem_status == "success"
        else:
            complaint_status = "not_applicable"
            complete = problem_status == "success"
        if complete:
            hidden_completed += 1
            continue
        if complaint_status == "failed":
            candidate_status = "差错失败"
        elif problem_status == "failed":
            candidate_status = "问题件失败"
        elif candidate["problem_type"] == "少货/分批" and problem_status == "success":
            candidate_status = "待补差错"
        else:
            candidate_status = "未执行"
        eligible.append(
            {
                **candidate,
                "candidate_status": candidate_status,
                "complaint_status": complaint_status,
                "problem_item_status": problem_status,
                "run_complaint": (
                    candidate["problem_type"] == "少货/分批"
                    and complaint_status not in {"success", "duplicate"}
                ),
                "run_problem_item": problem_status != "success",
            }
        )
    return eligible, hidden_completed


def _preview_fingerprint(candidates: list[dict[str, Any]]) -> str:
    material = [
        {
            "bill_code": item["bill_code"],
            "problem_type": item["problem_type"],
            "expected_quantity": item["expected_quantity"],
            "arrived_quantity": item["arrived_quantity"],
            "pending_quantity": item["pending_quantity"],
            "complaint_status": item["complaint_status"],
            "problem_item_status": item["problem_item_status"],
        }
        for item in candidates
    ]
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_source_values() -> tuple[list[list[Any]], dict[str, Any]]:
    resource = get_required_resource(SOURCE_RESOURCE_KEY)
    spreadsheet_token, sheet_id, value_range = _sheet_ref(
        resource,
        EXPECTED_SOURCE_SHEET_ID,
        resource_key=SOURCE_RESOURCE_KEY,
    )
    result = feishu_operation(
        "read_sheet",
        {
            "spreadsheet_token": spreadsheet_token,
            "sheet_id": sheet_id,
            "range": value_range,
            "value_render_option": "FormattedValue",
            "as": "bot",
        },
    )
    if not isinstance(result, dict) or result.get("error"):
        detail = str(result.get("error") if isinstance(result, dict) else result)[:300]
        raise RuntimeError(f"读取每日到货表失败: {detail}")
    return _sheet_values(result), {"sheet_id": sheet_id, "resource_key": SOURCE_RESOURCE_KEY}


def _sync_target_sheet(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return sync_target_sheet(candidates)


def _upload_to_tms(candidates: list[dict[str, Any]], account_id: str) -> dict[str, Any]:
    items = [
        {
            key: item[key]
            for key in (
                "bill_code",
                "problem_type",
                "problem_owner_type",
                "problem_cause",
                "expected_quantity",
                "arrived_quantity",
                "pending_quantity",
                "complaint_status",
                "problem_item_status",
            )
        }
        for item in candidates
    ]
    result = call_http_service(
        "/split_pending_problem_upload",
        {
            "params": {
                "account_id": account_id,
                "items": items,
                "dry_run": False,
                "update_postpone_days": False,
                "upload_screenshot": False,
            },
            "timeout_sec": UPLOAD_TIMEOUT_SEC,
            "client_timeout_sec": UPLOAD_TIMEOUT_SEC + 30,
        },
    )
    if auth_error := tms_auth_error_result(result):
        return auth_error
    if not isinstance(result, dict):
        return {"error": f"split_pending_problem_upload 返回异常: {result}"}
    if result.get("error"):
        return {"error": str(result.get("error")), "raw": result}
    payload = result.get("data") if isinstance(result.get("data"), dict) else result
    if not isinstance(payload, dict):
        return {"error": f"split_pending_problem_upload 返回格式异常: {result}"}
    return payload


def _validate_upload_results(
    candidates: list[dict[str, Any]], upload_result: dict[str, Any]
) -> list[dict[str, Any]]:
    raw_results = upload_result.get("results")
    if not isinstance(raw_results, list):
        raise RuntimeError("融辉上传结果缺少逐票 results")
    expected_codes = [item["bill_code"] for item in candidates]
    expected_set = set(expected_codes)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_results, start=1):
        if not isinstance(raw, dict):
            raise RuntimeError(f"融辉上传结果第 {index} 项不是对象")
        bill_code = _clean_text(raw.get("bill_code"))
        if not bill_code:
            raise RuntimeError(f"融辉上传结果第 {index} 项缺少 bill_code")
        if bill_code in seen:
            raise RuntimeError(f"融辉上传结果包含重复运单号: {bill_code}")
        if bill_code not in expected_set:
            raise RuntimeError(f"融辉上传结果包含未知运单号: {bill_code}")
        if not isinstance(raw.get("complete"), bool):
            raise RuntimeError(f"{bill_code} 的融辉上传结果缺少布尔 complete 状态")
        seen.add(bill_code)
        normalized.append(raw)
    if seen != expected_set:
        missing = [code for code in expected_codes if code not in seen]
        raise RuntimeError(f"融辉上传结果缺少运单: {', '.join(missing)}")
    return normalized


def run_split_pending_problem_upload(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    dry_run = _bool_param(params, "dry_run", True)
    account_id = _clean_text(params.get("account_id") or DEFAULT_ACCOUNT_ID)
    selected_bill_codes: list[str] = []
    requested_fingerprint = _clean_text(params.get("preview_fingerprint"))
    if not dry_run:
        raw_selected = params.get("selected_bill_codes")
        if not isinstance(raw_selected, list) or not raw_selected:
            return {
                "ok": False,
                "stage": "selection_required",
                "error": "正式执行必须提供非空 selected_bill_codes",
                "message": "请先发送“分批”并选择运单",
            }
        seen_selected: set[str] = set()
        for raw_code in raw_selected:
            code = _clean_text(raw_code)
            if not code:
                return {
                    "ok": False,
                    "stage": "selection_required",
                    "error": "selected_bill_codes 包含空运单号",
                    "message": "请重新发送“分批”并选择运单",
                }
            if code in seen_selected:
                return {
                    "ok": False,
                    "stage": "selection_required",
                    "error": f"selected_bill_codes 包含重复运单号: {code}",
                    "message": "请重新发送“分批”并选择运单",
                }
            seen_selected.add(code)
            selected_bill_codes.append(code)
        if not re.fullmatch(r"[0-9a-f]{64}", requested_fingerprint):
            return {
                "ok": False,
                "stage": "selection_required",
                "error": "正式执行缺少有效 preview_fingerprint",
                "message": "请重新发送“分批”并选择运单",
            }

    try:
        values, source = _read_source_values()
        snapshot_candidates, source_rows = classify_sheet_values(values)
        stored_records = list_split_pending_problem_items()
        candidates, hidden_completed = _stateful_candidates(snapshot_candidates, stored_records)
    except Exception as exc:
        return {
            "ok": False,
            "stage": "validation_failed",
            "error": str(exc),
            "message": str(exc),
        }

    fingerprint = _preview_fingerprint(candidates)
    preview = _candidate_preview(candidates)
    counts = _type_counts(candidates)
    common = {
        "candidate_count": len(candidates),
        "snapshot_count": len(snapshot_candidates),
        "candidates": preview,
        "type_counts": counts,
        "source_rows": source_rows,
        "complete_count": source_rows - len(snapshot_candidates),
        "hidden_completed_count": hidden_completed,
        "preview_fingerprint": fingerprint,
        "quantity_summary": {
            "expected_min": min((item["expected_quantity"] for item in candidates), default=None),
            "expected_max": max((item["expected_quantity"] for item in candidates), default=None),
            "arrived_min": min((item["arrived_quantity"] for item in candidates), default=None),
            "arrived_max": max((item["arrived_quantity"] for item in candidates), default=None),
            "pending_min": min((item["pending_quantity"] for item in candidates), default=None),
            "pending_max": max((item["pending_quantity"] for item in candidates), default=None),
        },
        "source": source,
        "account_id": account_id,
    }
    if dry_run:
        return {
            "ok": True,
            "stage": "dry_run",
            "message": f"演练：可执行 {len(candidates)} 单，未写表、数据库或融辉",
            **common,
            "database_rows": 0,
            "target_sheet_rows": 0,
            "saved_bills": 0,
            "failed_bills": 0,
            "failed_bill_codes": [],
            "results": [],
        }

    if requested_fingerprint != fingerprint:
        return {
            "ok": False,
            "stage": "preview_expired",
            "error": "预览后来源或执行状态已变化，本次选择未执行",
            "message": "列表已变化，请重新发送“分批”",
            **common,
        }
    by_code = {item["bill_code"]: item for item in candidates}
    unavailable = [code for code in selected_bill_codes if code not in by_code]
    if unavailable:
        return {
            "ok": False,
            "stage": "preview_expired",
            "error": f"所选运单已不可执行: {', '.join(unavailable)}",
            "message": "列表已变化，请重新发送“分批”",
            **common,
        }
    selected_candidates = [by_code[code] for code in selected_bill_codes]

    try:
        database_result = replace_split_pending_problem_items(snapshot_candidates)
    except Exception as exc:
        return {
            "ok": False,
            "stage": "database_failed",
            "error": f"刷新未齐问题件数据库失败: {str(exc)[:500]}",
            "message": str(exc)[:500],
            "selected_bill_codes": selected_bill_codes,
            **common,
        }
    try:
        target_sheet_result = _sync_target_sheet(snapshot_candidates)
    except Exception as exc:
        return {
            "ok": False,
            "stage": "sheet_failed",
            "error": str(exc),
            "message": str(exc),
            "database_result": database_result,
            "selected_bill_codes": selected_bill_codes,
            **common,
        }

    upload_result = _upload_to_tms(selected_candidates, account_id)
    if upload_result.get("error"):
        return {
            **upload_result,
            "ok": False,
            "stage": str(upload_result.get("stage") or "upload_failed"),
            "database_result": database_result,
            "target_sheet_result": target_sheet_result,
            "selected_bill_codes": selected_bill_codes,
            **common,
        }

    try:
        results = _validate_upload_results(selected_candidates, upload_result)
    except Exception as exc:
        detail = str(exc)[:500]
        return {
            "ok": False,
            "stage": "upload_result_invalid",
            "error": f"融辉已执行，但逐票结果不可用: {detail}",
            "message": detail,
            "database_result": database_result,
            "target_sheet_result": target_sheet_result,
            "upload_result": upload_result,
            "selected_bill_codes": selected_bill_codes,
            **common,
        }
    saved_bills = sum(1 for result in results if result["complete"])
    failed_bill_codes = [result["bill_code"] for result in results if not result["complete"]]
    try:
        database_upload_result = update_split_pending_combined_results(results)
    except Exception as exc:
        return {
            "ok": False,
            "stage": "database_result_update_failed",
            "error": f"融辉已执行，但数据库状态回写失败: {str(exc)[:500]}",
            "message": str(exc)[:500],
            "database_result": database_result,
            "target_sheet_result": target_sheet_result,
            "upload_result": upload_result,
            "selected_bill_codes": selected_bill_codes,
            **common,
        }
    event_rows: list[dict[str, Any]] = []
    for result in results:
        if not result.get("complete"):
            continue
        problem_item = result.get("problem_item") if isinstance(result.get("problem_item"), dict) else {}
        external_id = _clean_text(problem_item.get("external_id") or problem_item.get("guid"))
        registered_at = _clean_text(problem_item.get("registered_at"))
        if not external_id or not registered_at:
            return {
                "ok": False,
                "stage": "event_store_failed",
                "error": f"{result.get('bill_code')} 已执行，但TMS结果缺少问题件唯一ID或登记时间",
                "database_result": database_result,
                "database_upload_result": database_upload_result,
                "target_sheet_result": target_sheet_result,
                "selected_bill_codes": selected_bill_codes,
                **common,
            }
        event_rows.append(
            {
                "source": "split_pending_script",
                "external_id": external_id,
                "tracking_number": _clean_text(result.get("bill_code")),
                "problem_type": _clean_text(result.get("problem_type")),
                "registered_at": registered_at,
                "registered_site": _clean_text(problem_item.get("registered_site")),
                "upload_complete": True,
                "before_cutoff": is_before_problem_cutoff(registered_at),
                "postpones_sign": False,
                "payload": result,
            }
        )
    try:
        problem_event_result = upsert_problem_events(event_rows)
    except Exception as exc:
        return {
            "ok": False,
            "stage": "event_store_failed",
            "error": f"融辉已执行，但共享问题件事件写入失败: {str(exc)[:500]}",
            "database_result": database_result,
            "database_upload_result": database_upload_result,
            "target_sheet_result": target_sheet_result,
            "selected_bill_codes": selected_bill_codes,
            **common,
        }
    return {
        "ok": not failed_bill_codes,
        "stage": "done" if not failed_bill_codes else "partial_failed",
        "message": str(upload_result.get("message") or "分批执行完成"),
        **common,
        "selected_count": len(selected_bill_codes),
        "selected_bill_codes": selected_bill_codes,
        "database_result": database_result,
        "database_upload_result": database_upload_result,
        "problem_event_result": problem_event_result,
        "target_sheet_result": target_sheet_result,
        "database_rows": int(database_result.get("current") or 0),
        "target_sheet_rows": int(target_sheet_result.get("rows") or 0),
        "saved_bills": saved_bills,
        "failed_bills": len(failed_bill_codes),
        "failed_bill_codes": failed_bill_codes,
        "results": results,
    }


def main() -> None:
    raw = sys.stdin.read()
    params = json.loads(raw) if raw.strip() else {}
    result = run_split_pending_problem_upload(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
