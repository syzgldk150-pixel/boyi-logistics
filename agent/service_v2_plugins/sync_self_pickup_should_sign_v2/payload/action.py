"""Publish the selected self-pickup account without changing the shared ledger."""

from business.daily_sign_pipeline import (
    DailySignSyncError,
    _extract_rows,
    _observed_at_iso,
    _r13_rows_by_code,
    _resolve_r13_request,
)
from business.daily_sign_rules import build_ledger_row, business_now, clean_text
from business.daily_sign_sync_tool import (
    _apply_latest_stat_quantity,
    _apply_r13_publication_fields,
    _sort_rows,
    _sync_sheet,
)
from daily_sign_io import (
    call_http_service,
    get_workflow_resource,
    load_daily_sign_state,
    use_broker,
)


def _publish(observed_at):
    target = get_workflow_resource("phase7.daily_sign_sheet")
    request = _resolve_r13_request({"r13_account_id": "daily_sign_r13"}, "daily_sign_r13")
    source_rows = _extract_rows(call_http_service("/get_qianshou", request), label="R13 自提部应签查询")
    r13_by_code = _r13_rows_by_code(source_rows)
    state = load_daily_sign_state(tracking_numbers=sorted(r13_by_code))
    arrival_proof = state.get("arrival_source_proof")
    if not isinstance(arrival_proof, dict) or arrival_proof.get("complete") is not True:
        raise DailySignSyncError(
            "INCOMPLETE_SOURCE_EVIDENCE", "到货快照没有可验证的成功运行，已保留自提部原表。"
        )

    rows = []
    for code in sorted(r13_by_code):
        history = state.get("arrivals", {}).get(code, [])
        # The shared ledger and historical problem/sign events belong to other
        # account workflows. Only the current R13 page and arrival snapshots
        # contribute to this independent publication.
        row = build_ledger_row(
            code,
            r13_row=r13_by_code[code],
            previous_row=None,
            arrival_history=history,
            problem_events=[],
            sign_event=None,
            observed_at=observed_at,
        )
        row = _apply_latest_stat_quantity(row, history, observed_at)
        try:
            rows.append(_apply_r13_publication_fields(row, r13_by_code[code]))
        except ValueError as exc:
            raise DailySignSyncError("SOURCE_FIELD_MISSING", str(exc)) from exc
    rows = _sort_rows(rows)
    sheet = _sync_sheet(rows, target)
    if sheet.get("error"):
        raise DailySignSyncError(
            clean_text(sheet.get("error_code")) or "PROJECTION_WRITE_FAILED",
            clean_text(sheet.get("error")) or "自提部应签明细写入失败。",
        )
    readback = sheet.get("readback")
    if (
        not isinstance(readback, dict)
        or readback.get("verified") is not True
        or readback.get("record_count") != len(rows)
        or len(clean_text(readback.get("snapshot_sha256"))) != 64
    ):
        raise DailySignSyncError("WRITE_OUTCOME_UNKNOWN", "自提部应签明细缺少完整的新鲜回读证明。")
    format_readback = sheet.get("format_readback")
    if not isinstance(format_readback, dict) or format_readback.get("verified") is not True:
        raise DailySignSyncError("WRITE_OUTCOME_UNKNOWN", "自提部应签明细黄色条件格式未通过回读核验。")
    return {
        "sheet_rows": len(rows),
        "sheet_written": sheet["written"],
        "sheet_unchanged": sheet["unchanged"],
        "sheet_readback": readback,
        "sheet_format_readback": format_readback,
        "diagnostics": {
            "r13_rows": len(source_rows),
            "r13_complete": True,
            "arrival_rows": sum(len(items) for items in state.get("arrivals", {}).values()),
            "arrival_source_proof": arrival_proof,
            "secondary_sources": {"status": "not_requested", "publication_source": "r13"},
        },
    }


def run_action(arguments, broker):
    if arguments != {}:
        raise ValueError("INVALID_ARGUMENT")
    observed_at = business_now()
    meta = {
        "source_system": "r13_self_pickup_daily_sign",
        "observed_at": _observed_at_iso(observed_at),
        "record_count": 0,
        "pagination_complete": False,
    }
    try:
        with use_broker(broker):
            data = _publish(observed_at)
    except DailySignSyncError as exc:
        return {
            "status": "FAILED",
            "data": {},
            "meta": meta,
            "warnings": [],
            "error": {"code": exc.code, "message": str(exc), "retryable": False},
        }
    return {
        "status": "SUCCESS",
        "data": data,
        "meta": {**meta, "record_count": data["sheet_rows"], "pagination_complete": True},
        "warnings": [],
        "error": None,
    }
