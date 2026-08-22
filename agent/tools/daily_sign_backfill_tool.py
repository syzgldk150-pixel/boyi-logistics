"""Backfill historical arrival snapshots and preview the rebuilt daily-sign ledger."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.workflow_resource_store import get_workflow_resource
from tools.daily_sign_rules import TARGET_STATION, build_ledger_row, business_now, clean_text, parse_datetime
from tools.daily_sign_store import save_arrival_stat_snapshot, snapshot_fingerprint, upsert_ledger_rows
from tools.daily_sign_sync_tool import (
    _extract_rows,
    _daily_sign_candidate_codes,
    _r13_by_code,
    _sync_manual_problem_events,
    _sync_r13_sign_conflicts,
    _sync_sign_events,
    build_daily_sign_request_body,
)
from tools.feishu_cli_tool import (
    _spreadsheet_sheet_info,
    _spreadsheet_sheet_ref_map,
    feishu_operation,
)
from tools.phase7_sync_common import get_required_resource
from tools.phase7_sync_common import tms_auth_error_result
from tools.tms_tool import call_http_service

ARCHIVE_TITLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _sheet_values(payload: Any) -> list[list[Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nested = data.get("valueRange") if isinstance(data.get("valueRange"), dict) else {}
    direct = payload.get("valueRange") if isinstance(payload.get("valueRange"), dict) else {}
    for candidate in (nested.get("values"), direct.get("values"), data.get("values"), payload.get("values")):
        if isinstance(candidate, list):
            return [row if isinstance(row, list) else [] for row in candidate]
    return []


def _archive_records(values: list[list[Any]]) -> list[dict[str, Any]]:
    if not values:
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_number, row in enumerate(values[1:], start=2):
        if not row or not _clean(row[0]):
            continue
        if len(row) < 19:
            raise ValueError(f"归档第 {row_number} 行不足19列")
        code = _clean(row[0])
        if code in seen:
            raise ValueError(f"归档存在重复运单号: {code}")
        seen.add(code)
        records.append(
            {
                "tracking_number": code,
                "goods_name": _clean(row[1]),
                "package_type": _clean(row[2]),
                "delivery_method": _clean(row[3]),
                "expected_quantity": _int(row[4]),
                "destination_station": _clean(row[9]),
                "recipient_address": _clean(row[12]),
                "arrived_quantity": _int(row[18]),
            }
        )
    return records


def _seed_row_from_sheet(row: list[Any]) -> dict[str, Any]:
    code = _clean(row[0])
    is_nine_columns = len(row) >= 9
    return {
        "tracking_number": code,
        "r13_plan_sign_at": _clean(row[1]) if len(row) > 1 else None,
        "r13_sign_status": None,
        "r13_sign_at": None,
        "first_seen_r13_at": None,
        "last_seen_r13_at": None,
        "r13_current": False,
        "first_arrival_date": None,
        "completion_date": None,
        "expected_quantity": _int(row[5] if is_nine_columns else row[4]),
        "arrived_quantity": _int(row[8]) if is_nine_columns else None,
        "arrival_status": "unknown",
        "system_sign_due_at": _clean(row[2]) if is_nine_columns else None,
        "tms_signed": False,
        "tms_signed_at": None,
        "goods_name": _clean(row[3] if is_nine_columns else row[2]),
        "package_type": _clean(row[4] if is_nine_columns else row[3]),
        "delivery_method": _clean(row[7] if is_nine_columns else row[6]),
        "recipient_address": _clean(row[6] if is_nine_columns else row[5]),
        "data_quality_flags": ["backfilled_current_sign_sheet", "arrival_date_unknown"],
        "calculation_trace": {"reason": "historical_source_missing"},
    }


def _merge_r13_seed(
    seed: dict[str, Any] | None,
    r13_row: dict[str, Any],
    *,
    observed_at: datetime,
) -> dict[str, Any]:
    seed = dict(seed or {})

    def prefer(*values: Any) -> Any:
        return next((value for value in values if value not in (None, "")), None)

    flags = list(seed.get("data_quality_flags") or [])
    if "backfilled_r13_history" not in flags:
        flags.append("backfilled_r13_history")
    return {
        "tracking_number": clean_text(r13_row.get("billNumberMain")),
        "r13_plan_sign_at": prefer(r13_row.get("planSignTime"), seed.get("r13_plan_sign_at")),
        "r13_sign_status": prefer(r13_row.get("isSigns"), seed.get("r13_sign_status")),
        "r13_sign_at": prefer(r13_row.get("signTime"), seed.get("r13_sign_at")),
        "first_seen_r13_at": prefer(seed.get("first_seen_r13_at"), observed_at),
        "last_seen_r13_at": observed_at,
        "r13_current": True,
        "first_arrival_date": seed.get("first_arrival_date"),
        "completion_date": seed.get("completion_date"),
        "expected_quantity": prefer(r13_row.get("pcs"), seed.get("expected_quantity")),
        "arrived_quantity": seed.get("arrived_quantity"),
        "arrival_status": seed.get("arrival_status") or "unknown",
        "system_sign_due_at": seed.get("system_sign_due_at"),
        "tms_signed": False,
        "tms_signed_at": None,
        "goods_name": prefer(r13_row.get("goodsName"), seed.get("goods_name")),
        "package_type": prefer(r13_row.get("packTypeDesc"), seed.get("package_type")),
        "delivery_method": prefer(r13_row.get("dispatchMode"), seed.get("delivery_method")),
        "recipient_address": prefer(r13_row.get("dispAddress"), seed.get("recipient_address")),
        "data_quality_flags": flags,
        "calculation_trace": seed.get("calculation_trace") or {"reason": "historical_source_missing"},
    }


def _read_r13_history(params: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    request_params = dict(params)
    request_body = dict(params.get("request_body") or {})
    if not any(request_body.get(key) or params.get(key) for key in ("start", "end", "days")):
        request_body["days"] = max(int(params.get("r13_backfill_days") or 3650), 1)
    request_params["request_body"] = request_body
    request = build_daily_sign_request_body(request_params)
    response = call_http_service("/get_qianshou", request)
    if auth_error := tms_auth_error_result(response):
        return None, auth_error
    if isinstance(response, dict) and response.get("error"):
        return None, {"error": f"get_qianshou 历史回填失败: {response.get('error')}"}
    rows = _extract_rows(response)
    if rows is None:
        return None, {"error": "get_qianshou 历史回填返回格式异常"}
    try:
        unique = _r13_by_code(rows)
    except ValueError as exc:
        return None, {"error": str(exc)}
    return list(unique.values()), {
        "ok": True,
        "rows": len(unique),
        "fingerprint": snapshot_fingerprint(list(unique.values())),
    }


def run_daily_sign_backfill(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    apply = bool(params.get("apply", False))
    resource = get_required_resource("phase7.stats_archive_sheet")
    spreadsheet_token = _clean(params.get("spreadsheet_token") or resource.get("spreadsheet_token"))
    if not spreadsheet_token:
        return {"error": "统计归档资源缺少 spreadsheet_token"}
    refs = _spreadsheet_sheet_ref_map(spreadsheet_token)
    archive_titles = sorted(title for title in refs if ARCHIVE_TITLE_RE.fullmatch(title))
    snapshots: list[dict[str, Any]] = []
    arrivals_by_code: dict[str, list[dict[str, Any]]] = {}
    target_station_codes: set[str] = set()
    total_rows = 0
    for title in archive_titles:
        sheet_id = _clean(refs.get(title))
        info = _spreadsheet_sheet_info(spreadsheet_token, sheet_id) or {}
        row_count = max(_int(info.get("row_count")) or 199, 1)
        result = feishu_operation(
            "read_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": f"{sheet_id}!A1:S{row_count}",
                "as": params.get("as", "bot"),
            },
        )
        if result.get("error"):
            return {"error": f"读取到货归档 {title} 失败: {result.get('error')}", "snapshots": snapshots}
        records = _archive_records(_sheet_values(result))
        for record in records:
            item = {**record, "business_date": title}
            code = clean_text(item.get("tracking_number"))
            arrivals_by_code.setdefault(code, []).append(item)
            if (
                clean_text(item.get("destination_station")) == TARGET_STATION
                and (_int(item.get("arrived_quantity")) or 0) > 0
            ):
                target_station_codes.add(code)
        fingerprint = snapshot_fingerprint(records)
        save_result = save_arrival_stat_snapshot(date.fromisoformat(title), records, dry_run=not apply)
        snapshots.append(
            {
                "business_date": title,
                "rows": len(records),
                "fingerprint": fingerprint,
                "save_result": save_result,
            }
        )
        total_rows += len(records)

    current_sign_resource = get_workflow_resource("phase7.daily_sign_sheet") or {}
    current_sign_rows = 0
    current_sign_fingerprint = ""
    seeded_ledger_rows = 0
    seed_by_code: dict[str, dict[str, Any]] = {}
    token = _clean(current_sign_resource.get("spreadsheet_token"))
    value_range = _clean(current_sign_resource.get("read_range") or current_sign_resource.get("clear_range") or current_sign_resource.get("range"))
    if token and value_range:
        current = feishu_operation(
            "read_sheet",
            {"spreadsheet_token": token, "range": value_range, "as": params.get("as", "bot")},
        )
        if current.get("error"):
            return {"error": f"读取当前应签明细失败: {current.get('error')}", "snapshots": snapshots}
        sign_values = _sheet_values(current)
        sign_material = [row for row in sign_values if row and _clean(row[0]) and _clean(row[0]) != "运单编号"]
        current_sign_rows = len(sign_material)
        current_sign_fingerprint = snapshot_fingerprint(
            [{"row": row} for row in sign_material]
        )
        for row in sign_material:
            seed = _seed_row_from_sheet(row)
            seed_by_code[seed["tracking_number"]] = seed

    r13_rows: list[dict[str, Any]] = []
    r13_result: dict[str, Any] = {"ok": True, "skipped": True, "rows": 0}
    if not params.get("skip_r13_history"):
        fetched_r13, r13_result = _read_r13_history(params)
        if fetched_r13 is None:
            return {"error": r13_result.get("error"), "snapshots": snapshots}
        r13_rows = fetched_r13
        observed_at = business_now()
        for code, r13_row in _r13_by_code(r13_rows).items():
            seed_by_code[code] = _merge_r13_seed(
                seed_by_code.get(code),
                r13_row,
                observed_at=observed_at,
            )

    r13_by_code = _r13_by_code(r13_rows)
    candidate_codes, excluded_child_codes = _daily_sign_candidate_codes(
        r13_by_code,
        seed_by_code,
        target_station_codes,
    )
    now = business_now()
    backfill_days = max(int(params.get("event_backfill_days") or params.get("r13_backfill_days") or 3650), 1)
    event_start = now - timedelta(days=backfill_days)
    event_params = {
        **params,
        "dry_run": not apply,
        "problem_start_date": event_start.strftime("%Y-%m-%d"),
        "problem_end_date": now.strftime("%Y-%m-%d"),
        "sign_start": event_start.strftime("%Y/%m/%d 00:00:00"),
        "sign_end": now.strftime("%Y/%m/%d %H:%M:%S"),
    }
    problem_events, problem_result = _sync_manual_problem_events(event_params)
    if problem_events is None:
        return {"error": f"历史问题件回填失败: {problem_result.get('error')}", "snapshots": snapshots}
    sign_events, sign_result = _sync_sign_events(event_params, candidate_codes)
    if sign_events is None:
        return {"error": f"历史签收扫描回填失败: {sign_result.get('error')}", "snapshots": snapshots}

    signs_by_code: dict[str, dict[str, Any]] = {}
    for event in sign_events:
        code = clean_text(event.get("tracking_number"))
        previous = signs_by_code.get(code)
        if previous is None or (
            parse_datetime(event.get("scanned_at")) or datetime.min
        ) > (parse_datetime(previous.get("scanned_at")) or datetime.min):
            signs_by_code[code] = event
    exact_events, exact_result = _sync_r13_sign_conflicts(
        event_params,
        r13_by_code,
        {"signs": signs_by_code},
    )
    if not exact_result.get("complete"):
        return {"error": "R13签收冲突轨迹核验不完整", "exact_result": exact_result, "snapshots": snapshots}
    for event in exact_events:
        signs_by_code[clean_text(event.get("tracking_number"))] = event

    problems_by_code: dict[str, list[dict[str, Any]]] = {}
    for event in problem_events:
        problems_by_code.setdefault(clean_text(event.get("tracking_number")), []).append(event)
    ledger_rows = [
        build_ledger_row(
            code,
            r13_row=r13_by_code.get(code),
            previous_row=seed_by_code.get(code),
            arrival_history=arrivals_by_code.get(code, []),
            problem_events=problems_by_code.get(code, []),
            sign_event=signs_by_code.get(code),
            observed_at=now,
        )
        for code in sorted(candidate_codes)
    ]
    if apply and ledger_rows:
        upsert_ledger_rows(ledger_rows)
        seeded_ledger_rows = len(ledger_rows)

    previous_open_codes = set(seed_by_code)
    rebuilt_open_codes = {
        clean_text(row.get("tracking_number")) for row in ledger_rows if not row.get("tms_signed")
    }
    state_counts = Counter(clean_text(row.get("arrival_status")) or "unknown" for row in ledger_rows)
    quality_counts = Counter(
        clean_text(flag)
        for row in ledger_rows
        for flag in (row.get("data_quality_flags") or [])
        if clean_text(flag)
    )

    return {
        "ok": True,
        "mode": "apply" if apply else "shadow",
        "archive_sheets": len(archive_titles),
        "archive_rows": total_rows,
        "current_sign_rows": current_sign_rows,
        "current_sign_fingerprint": current_sign_fingerprint,
        "r13_history_rows": len(r13_rows),
        "r13_history_result": r13_result,
        "problem_history_result": problem_result,
        "sign_history_result": sign_result,
        "exact_sign_result": exact_result,
        "candidate_seed_rows": len(candidate_codes),
        "excluded_child_candidate_rows": len(excluded_child_codes),
        "excluded_child_candidate_codes": sorted(excluded_child_codes),
        "seeded_ledger_rows": seeded_ledger_rows,
        "rebuilt_open_rows": len(rebuilt_open_codes),
        "closed_by_tms_rows": sum(1 for row in ledger_rows if row.get("tms_signed")),
        "added_open_rows": len(rebuilt_open_codes - previous_open_codes),
        "removed_open_rows_with_tms_sign": len(previous_open_codes - rebuilt_open_codes),
        "state_counts": dict(sorted(state_counts.items())),
        "quality_flag_counts": dict(sorted(quality_counts.items())),
        "ledger_fingerprint": snapshot_fingerprint(ledger_rows),
        "snapshots": snapshots,
        "published": False,
    }


def main() -> None:
    raw = sys.stdin.read()
    params = json.loads(raw) if raw.strip() else {}
    print(json.dumps(run_daily_sign_backfill(params), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
