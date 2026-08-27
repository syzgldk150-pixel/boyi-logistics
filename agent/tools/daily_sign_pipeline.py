"""Authoritative daily-sign collection and persistence pipeline.

This module is invoked only by the governed ``sync_daily_should_sign`` tool.
It rejects incomplete source data and never treats an R13 sign flag as proof
that the TMS main waybill was signed.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from datetime import date, datetime, time as datetime_time, timezone
from typing import Any

from tools.daily_sign_rules import (
    BUSINESS_TIMEZONE,
    build_ledger_row,
    business_now,
    clean_text,
    parse_date,
    parse_datetime,
)
from tools.daily_sign_store import (
    earliest_relevant_source_date,
    finish_sync_run,
    load_daily_sign_state,
    persist_daily_sign_snapshot,
    snapshot_fingerprint,
    start_sync_run,
)
from tools.phase7_sync_common import (
    sync_bitable_snapshot,
    sync_sheet_snapshot,
    tms_auth_error_result,
)
from tools.tms_tool import call_http_service


class DailySignSyncError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = clean_text(code) or "DAILY_SIGN_SYNC_FAILED"
        self.retryable = retryable


def _required_account_id(params: dict[str, Any], field: str) -> str:
    value = clean_text(params.get(field))
    if not value:
        raise DailySignSyncError(
            "ACCOUNT_AMBIGUOUS",
            f"每日应签同步必须显式提供 {field}，禁止选择默认账号。",
        )
    return value


def _bounded_int(
    value: Any,
    *,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise DailySignSyncError("INVALID_ARGUMENT", f"{field} 必须是整数。")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise DailySignSyncError("INVALID_ARGUMENT", f"{field} 必须是整数。") from exc
    if parsed < minimum or parsed > maximum:
        raise DailySignSyncError(
            "INVALID_ARGUMENT",
            f"{field} 必须在 {minimum} 到 {maximum} 之间。",
        )
    return parsed


def _scope_name(prefix: str, account_id: str) -> str:
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _observed_at_iso(value: datetime) -> str:
    aware = value.replace(tzinfo=BUSINESS_TIMEZONE)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unified_failure(
    *,
    code: str,
    message: str,
    observed_at: datetime,
    run_id: str | None,
    retryable: bool,
) -> dict[str, Any]:
    evidence_refs = [f"mysql:daily_sign_sync_runs:{run_id}"] if run_id else []
    return {
        "status": "FAILED",
        "data": {"source_run_id": run_id} if run_id else {},
        "meta": {
            "source_system": "daily_sign_authoritative_sources",
            "account_id": "multi_account",
            "observed_at": _observed_at_iso(observed_at),
            "record_count": 0,
            "pagination_complete": False,
            "evidence_refs": evidence_refs,
            "postconditions": {"0": False},
        },
        "warnings": [],
        "error": {
            "code": clean_text(code) or "DAILY_SIGN_SYNC_FAILED",
            "message": clean_text(message) or "每日应签同步失败。",
            "retryable": bool(retryable),
        },
    }


def _unified_success(
    *,
    run_id: str,
    observed_at: datetime,
    ledger_rows: list[dict[str, Any]],
    legacy_candidate_keys: list[str],
    diagnostics: dict[str, Any],
    evidence_refs: list[str],
) -> dict[str, Any]:
    observed_at_iso = _observed_at_iso(observed_at)
    completion_ref = f"mysql:daily_sign_sync_runs:{run_id}"
    return {
        "status": "SUCCESS",
        "data": {
            "source_run_id": run_id,
            "legacy_candidate_keys": legacy_candidate_keys,
            "diagnostics": diagnostics,
        },
        "meta": {
            "source_system": "daily_sign_authoritative_sources",
            "account_id": "multi_account",
            "observed_at": observed_at_iso,
            "record_count": len(ledger_rows),
            "pagination_complete": True,
            "evidence_refs": evidence_refs,
            "postconditions": {"0": True},
            "postcondition_evidence": {
                "0": {
                    "condition": "authoritative_snapshot_committed",
                    "verified": True,
                    "observed_at": observed_at_iso,
                    "evidence_ref": completion_ref,
                    "details": {"source_run_id": run_id},
                }
            },
            "source_run_id": run_id,
        },
        "warnings": [],
        "error": None,
    }


def _raise_for_source_error(payload: Any, *, label: str) -> None:
    if auth_error := tms_auth_error_result(payload):
        raise DailySignSyncError(
            clean_text(auth_error.get("error_code")) or "AUTH_REQUIRED",
            f"{label}登录态不可用。",
        )
    if not isinstance(payload, dict):
        return
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if nested.get("ok") is False or nested.get("status") == "FAILED" or nested.get("error"):
        error = nested.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        raise DailySignSyncError(
            clean_text(nested.get("error_code")) or "SOURCE_QUERY_FAILED",
            f"{label}失败：{clean_text(message or nested.get('message')) or '原系统返回失败'}",
            retryable=True,
        )


def _extract_rows(payload: Any, *, label: str) -> list[dict[str, Any]]:
    _raise_for_source_error(payload, label=label)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        rows = payload["data"]
    else:
        raise DailySignSyncError(
            "SOURCE_RESPONSE_INVALID",
            f"{label}返回格式异常，缺少记录数组。",
        )
    if any(not isinstance(row, dict) for row in rows):
        raise DailySignSyncError(
            "SOURCE_RESPONSE_INVALID",
            f"{label}返回了非对象记录。",
        )
    return rows


def _r13_rows_by_code(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        code = clean_text(row.get("billNumberMain"))
        if not code:
            raise DailySignSyncError(
                "SOURCE_FIELD_MISSING",
                f"R13 第 {index} 行缺少 billNumberMain。",
            )
        previous = output.get(code)
        if previous is not None and previous != row:
            raise DailySignSyncError(
                "SOURCE_DUPLICATE_CONFLICT",
                f"R13 运单 {code} 存在冲突重复记录。",
            )
        output[code] = dict(row)
    return output


def _source_query_window(
    params: dict[str, Any],
    *,
    r13_rows: list[dict[str, Any]],
    state: dict[str, Any],
    observed_at: datetime,
) -> tuple[datetime, datetime]:
    explicit_start = clean_text(params.get("source_start"))
    explicit_end = clean_text(params.get("source_end"))
    if explicit_end and not explicit_start:
        raise DailySignSyncError(
            "INVALID_ARGUMENT",
            "source_end 不能脱离 source_start 单独提供。",
        )
    if explicit_start:
        start = parse_datetime(explicit_start)
        end = parse_datetime(explicit_end) if explicit_end else observed_at
        if start is None or end is None or start > end:
            raise DailySignSyncError(
                "INVALID_ARGUMENT",
                "source_start/source_end 时间范围无效。",
            )
        if end > observed_at:
            raise DailySignSyncError(
                "INVALID_ARGUMENT",
                "source_end 不能晚于本轮实际观测时间。",
            )
        return start, end

    source_dates: list[date] = [observed_at.date()]
    earliest = earliest_relevant_source_date()
    if earliest is not None:
        source_dates.append(earliest)
    for row in r13_rows:
        planned = parse_date(row.get("planSignTime"))
        if planned is not None:
            source_dates.append(planned)
    for row in state.get("ledger", {}).values():
        if bool(row.get("tms_signed")):
            continue
        for field in ("first_seen_r13_at", "first_arrival_date", "r13_plan_sign_at"):
            value = parse_date(row.get(field))
            if value is not None:
                source_dates.append(value)
    return datetime.combine(min(source_dates), datetime_time.min), observed_at


def _problem_payload(response: Any) -> dict[str, Any]:
    _raise_for_source_error(response, label="融辉问题件查询")
    if not isinstance(response, dict):
        raise DailySignSyncError("SOURCE_RESPONSE_INVALID", "融辉问题件查询返回格式异常。")
    payload = response.get("data") if isinstance(response.get("data"), dict) else response
    if not isinstance(payload.get("rows"), list) or not isinstance(payload.get("stats"), dict):
        raise DailySignSyncError(
            "SOURCE_RESPONSE_INVALID",
            "融辉问题件查询缺少 rows 或 stats。",
        )
    return payload


def _collect_problem_events(
    params: dict[str, Any],
    *,
    account_id: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_size = _bounded_int(
        params.get("problem_page_size"),
        field="problem_page_size",
        default=200,
        minimum=1,
        maximum=200,
    )
    max_pages = _bounded_int(
        params.get("problem_max_pages"),
        field="problem_max_pages",
        default=500,
        minimum=1,
        maximum=5000,
    )
    attempts = _bounded_int(
        params.get("problem_retry_attempts"),
        field="problem_retry_attempts",
        default=3,
        minimum=1,
        maximum=5,
    )
    timeout_sec = _bounded_int(
        params.get("problem_timeout_sec"),
        field="problem_timeout_sec",
        default=600,
        minimum=30,
        maximum=7200,
    )
    seen: dict[str, dict[str, Any]] = {}
    declared_total: int | None = None
    returned_count = 0
    for page in range(1, max_pages + 1):
        payload: dict[str, Any] | None = None
        last_error: DailySignSyncError | None = None
        for attempt in range(1, attempts + 1):
            response = call_http_service(
                "/customer_service_problem",
                {
                    "params": {
                        "action": "query",
                        "platform": "ronghui",
                        "direction": "registered",
                        "account_id": account_id,
                        "filters": {
                            "direction": "registered",
                            "date_from": start.strftime("%Y-%m-%d"),
                            "date_to": end.strftime("%Y-%m-%d"),
                            "start_time": start.strftime("%H:%M:%S"),
                            "end_time": end.strftime("%H:%M:%S"),
                            "page": page,
                            "page_size": page_size,
                        },
                    },
                    "timeout_sec": timeout_sec,
                },
            )
            try:
                payload = _problem_payload(response)
                break
            except DailySignSyncError as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(min(attempt, 2))
        if payload is None:
            assert last_error is not None
            raise last_error

        stats = payload["stats"]
        if stats.get("total_authoritative") is not True:
            raise DailySignSyncError(
                "PAGINATION_INCOMPLETE",
                "融辉问题件响应没有权威 total，不能证明分页完整。",
            )
        try:
            page_total = int(str(stats.get("total")).strip())
        except (TypeError, ValueError) as exc:
            raise DailySignSyncError(
                "SOURCE_RESPONSE_INVALID",
                "融辉问题件 total 不是整数。",
            ) from exc
        if page_total < 0:
            raise DailySignSyncError(
                "SOURCE_RESPONSE_INVALID",
                "融辉问题件 total 不能为负数。",
            )
        if declared_total is None:
            declared_total = page_total
        elif page_total != declared_total:
            raise DailySignSyncError(
                "PAGINATION_CHANGED",
                "融辉问题件分页过程中 total 发生变化。",
                retryable=True,
            )

        page_rows = payload["rows"]
        returned_count += len(page_rows)
        if returned_count > declared_total:
            raise DailySignSyncError(
                "PAGINATION_INCOMPLETE",
                "融辉问题件累计行数超过权威 total。",
            )
        for index, row in enumerate(page_rows, start=1):
            if not isinstance(row, dict):
                raise DailySignSyncError(
                    "SOURCE_RESPONSE_INVALID",
                    f"融辉问题件第 {page} 页第 {index} 行不是对象。",
                )
            external_id = clean_text(row.get("external_id"))
            tracking_number = clean_text(row.get("waybill_no"))
            problem_type = clean_text(row.get("problem_type"))
            registered_at = parse_datetime(row.get("registered_at"))
            if not external_id or not tracking_number or not problem_type or registered_at is None:
                raise DailySignSyncError(
                    "SOURCE_FIELD_MISSING",
                    "融辉问题件缺少唯一 ID、运单号、准确类型或登记时间。",
                )
            normalized = {
                "source": _scope_name("ronghui_problem", account_id),
                "external_id": external_id,
                "tracking_number": tracking_number,
                "problem_type": problem_type,
                "registered_at": registered_at,
                "registered_site": clean_text(row.get("registered_site")),
                "upload_complete": True,
                "payload": {
                    "platform": "ronghui",
                    "account_scope": _scope_name("account", account_id),
                    "source_direction": clean_text(row.get("source_direction")),
                    "external_id": external_id,
                },
            }
            if external_id in seen and seen[external_id] != normalized:
                raise DailySignSyncError(
                    "SOURCE_DUPLICATE_CONFLICT",
                    f"融辉问题件分页出现内容冲突的重复唯一 ID：{external_id}。",
                    retryable=True,
                )
            seen[external_id] = normalized
        if returned_count == declared_total:
            return list(seen.values()), {
                "rows": len(seen),
                "declared_total": declared_total,
                "pages": page,
                "complete": True,
            }
        if not page_rows:
            raise DailySignSyncError(
                "PAGINATION_INCOMPLETE",
                "融辉问题件在达到权威 total 前返回空页。",
            )
    raise DailySignSyncError(
        "PAGINATION_INCOMPLETE",
        f"融辉问题件达到 max_pages={max_pages} 后仍未完整。",
    )


def _collect_sign_events(
    params: dict[str, Any],
    *,
    account_id: str,
    start: datetime,
    end: datetime,
    known_codes: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_size = _bounded_int(
        params.get("sign_page_size"),
        field="sign_page_size",
        default=200,
        minimum=1,
        maximum=200,
    )
    max_pages = _bounded_int(
        params.get("sign_max_pages"),
        field="sign_max_pages",
        default=500,
        minimum=1,
        maximum=5000,
    )
    response = call_http_service(
        "/get_sign_records",
        {
            "params": {
                "start": start.strftime("%Y/%m/%d %H:%M:%S"),
                "end": end.strftime("%Y/%m/%d %H:%M:%S"),
                "page_size": page_size,
                "max_pages": max_pages,
                "chunk_days": _bounded_int(
                    params.get("sign_chunk_days"),
                    field="sign_chunk_days",
                    default=31,
                    minimum=1,
                    maximum=366,
                ),
                "retry_attempts": _bounded_int(
                    params.get("sign_retry_attempts"),
                    field="sign_retry_attempts",
                    default=3,
                    minimum=1,
                    maximum=5,
                ),
                "account_id": account_id,
            },
            "timeout_sec": _bounded_int(
                params.get("sign_timeout_sec"),
                field="sign_timeout_sec",
                default=1200,
                minimum=30,
                maximum=7200,
            ),
        },
    )
    rows = _extract_rows(response, label="融辉主单签收查询")
    source = _scope_name("ronghui_sign", account_id)
    events: list[dict[str, Any]] = []
    seen_identity: dict[tuple[str, str], str] = {}
    for index, row in enumerate(rows, start=1):
        scan_code = clean_text(row.get("扫描单号") or row.get("bill_code"))
        scan_type = clean_text(row.get("扫描类型") or row.get("scan_type"))
        scanned_at_text = clean_text(row.get("扫描时间") or row.get("scan_time"))
        scan_site = clean_text(row.get("扫描网点") or row.get("scan_site"))
        scanned_at = parse_datetime(scanned_at_text)
        if not scan_code or scan_type != "签收" or scanned_at is None or not scan_site:
            raise DailySignSyncError(
                "SOURCE_FIELD_MISSING",
                f"融辉主单签收第 {index} 行缺少准确单号、签收类型、时间或网点。",
            )
        identity = (scan_code, scanned_at.isoformat())
        previous_site = seen_identity.get(identity)
        if previous_site is not None and previous_site != scan_site:
            raise DailySignSyncError(
                "SOURCE_DUPLICATE_CONFLICT",
                f"主单签收记录 {scan_code} {scanned_at_text} 的网点冲突。",
            )
        seen_identity[identity] = scan_site
        if scan_code not in known_codes or previous_site is not None:
            continue
        external_id = hashlib.sha256(
            "|".join((scan_code, scan_type, scanned_at.isoformat(), scan_site)).encode("utf-8")
        ).hexdigest()
        events.append(
            {
                "source": source,
                "external_id": external_id,
                "tracking_number": scan_code,
                "scan_code": scan_code,
                "scan_type": "签收",
                "scanned_at": scanned_at,
                "scan_site": scan_site,
                "is_main_waybill": True,
                "payload": {
                    "source": "ronghui_sign_query",
                    "account_scope": _scope_name("account", account_id),
                },
            }
        )
    return events, {
        "source_rows": len(rows),
        "matched_main_rows": len(events),
        "complete": True,
    }


def _resolve_r13_request(params: dict[str, Any], account_id: str) -> dict[str, Any]:
    # Imported lazily to avoid a module cycle: the public tool delegates here.
    from tools.daily_sign_sync_tool import build_daily_sign_request_body

    try:
        request = build_daily_sign_request_body({**params, "r13_account_id": account_id})
    except ValueError as exc:
        raise DailySignSyncError("INVALID_ARGUMENT", str(exc)) from exc
    except Exception as exc:
        code = clean_text(getattr(exc, "code", ""))
        if code:
            raise DailySignSyncError(
                code,
                "R13 账号不可用，请在账号管理中恢复真实账号会话。",
                retryable=code in {"AUTH_REQUIRED", "LOGIN_FAILED", "SESSION_UNAVAILABLE"},
            ) from exc
        raise
    start = clean_text(request.get("start"))
    end = clean_text(request.get("end"))
    if bool(start) != bool(end):
        raise DailySignSyncError(
            "INVALID_ARGUMENT",
            "R13 start 与 end 必须同时提供。",
        )
    if not start:
        requested_days = request.get("days")
        if requested_days in (None, ""):
            raise DailySignSyncError(
                "INVALID_ARGUMENT",
                "每日应签同步必须显式提供 R13 start/end 或 days 查询范围。",
            )
        request["days"] = _bounded_int(
            requested_days,
            field="days",
            default=7,
            minimum=1,
            maximum=366,
        )
    request["page"] = 1
    request["fetch_all"] = True
    request["page_size"] = _bounded_int(
        request.get("page_size") or request.get("pageSize"),
        field="R13 page_size",
        default=100,
        minimum=1,
        maximum=500,
    )
    request["max_pages"] = _bounded_int(
        request.get("max_pages") or request.get("maxPages"),
        field="R13 max_pages",
        default=500,
        minimum=1,
        maximum=5000,
    )
    return request


def _merge_problem_events(
    existing: dict[str, list[dict[str, Any]]],
    incoming: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    output = {code: list(rows) for code, rows in existing.items()}
    identities = {
        (clean_text(row.get("source")), clean_text(row.get("external_id")))
        for rows in output.values()
        for row in rows
    }
    for row in incoming:
        identity = (clean_text(row.get("source")), clean_text(row.get("external_id")))
        if identity in identities:
            continue
        identities.add(identity)
        output.setdefault(clean_text(row.get("tracking_number")), []).append(row)
    return output


def _merge_sign_events(
    existing: dict[str, dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    output = dict(existing)
    for row in incoming:
        code = clean_text(row.get("tracking_number"))
        previous = output.get(code)
        previous_at = parse_datetime(previous.get("scanned_at")) if previous else None
        incoming_at = parse_datetime(row.get("scanned_at"))
        if incoming_at is not None and (previous_at is None or incoming_at >= previous_at):
            output[code] = row
    return output


def _legacy_candidate_keys(rows: list[dict[str, Any]], observed_at: datetime) -> list[str]:
    keys: set[str] = set()
    for row in rows:
        tracking_number = clean_text(row.get("billNumberMain"))
        planned = parse_date(row.get("planSignTime"))
        if (
            tracking_number
            and not clean_text(row.get("dispTime"))
            and planned is not None
            and planned <= observed_at.date()
        ):
            keys.add(f"daily_sign:{tracking_number}")
    return sorted(keys)


def _finish_failed_run(
    run_id: str,
    diagnostics: dict[str, Any],
    *,
    message: str,
) -> dict[str, Any]:
    values = {
        "status": "failed",
        "degraded": False,
        "r13_complete": bool(diagnostics.get("r13_complete")),
        "problems_complete": bool(diagnostics.get("problems_complete")),
        "signs_complete": bool(diagnostics.get("signs_complete")),
        "r13_rows": diagnostics.get("r13_rows", 0),
        "arrival_rows": diagnostics.get("arrival_rows", 0),
        "problem_rows": diagnostics.get("problem_rows", 0),
        "sign_rows": diagnostics.get("sign_rows", 0),
        "candidate_rows": diagnostics.get("candidate_rows", 0),
        "published_rows": 0,
        "unmatched_rows": diagnostics.get("unmatched_rows", 0),
        "fingerprint": diagnostics.get("fingerprint"),
        "diagnostics_json": diagnostics,
        "error_summary": clean_text(message)[:500],
    }
    finish_sync_run(run_id, values)
    return values


def run_authoritative_daily_sign_sync(params: dict[str, Any]) -> dict[str, Any]:
    """Collect all authoritative inputs, persist one complete source run, and publish."""

    from tools.daily_sign_sync_tool import (
        DAILY_SIGN_SHEET_RESOURCE_KEY,
        _build_records,
        _build_sheet_values,
        _enrich_rows_with_arrival_quantities,
        _enrich_rows_with_detail_addresses,
        _sheet_params_for_values,
        _sort_rows_by_plan_sign_time,
    )

    params = params if isinstance(params, dict) else {}
    observed_at = business_now()
    run_id: str | None = None
    diagnostics: dict[str, Any] = {
        "r13_complete": False,
        "problems_complete": False,
        "signs_complete": False,
    }
    try:
        if params.get("dry_run"):
            raise DailySignSyncError(
                "INVALID_ARGUMENT",
                "权威每日应签同步不支持跳过持久化的 dry_run。",
            )
        r13_account_id = _required_account_id(params, "r13_account_id")
        account_id = _required_account_id(params, "account_id")

        run_id, started_at = start_sync_run()
        observed_at = started_at
        diagnostics["run_id"] = run_id
        state = load_daily_sign_state()
        arrival_source_proof = state.get("arrival_source_proof")
        if (
            not isinstance(arrival_source_proof, dict)
            or arrival_source_proof.get("complete") is not True
        ):
            raise DailySignSyncError(
                "INCOMPLETE_SOURCE_EVIDENCE",
                "到货快照没有可验证的成功运行，禁止生成每日应签权威投影。",
            )
        diagnostics["arrival_rows"] = sum(
            len(rows) for rows in state.get("arrivals", {}).values()
        )
        diagnostics["arrival_source_proof"] = arrival_source_proof

        r13_request = _resolve_r13_request(params, r13_account_id)
        r13_response = call_http_service("/get_qianshou", r13_request)
        r13_rows = _extract_rows(r13_response, label="R13 应签查询")
        r13_by_code = _r13_rows_by_code(r13_rows)
        diagnostics.update({"r13_complete": True, "r13_rows": len(r13_rows)})

        source_start, source_end = _source_query_window(
            params,
            r13_rows=r13_rows,
            state=state,
            observed_at=observed_at,
        )
        problem_events, problem_proof = _collect_problem_events(
            params,
            account_id=account_id,
            start=source_start,
            end=source_end,
        )
        diagnostics.update(
            {
                "problems_complete": True,
                "problem_rows": len(problem_events),
                "problem_proof": problem_proof,
            }
        )

        known_codes = (
            set(r13_by_code)
            | set(state.get("ledger", {}))
            | set(state.get("target_station_codes", set()))
        )
        sign_events, sign_proof = _collect_sign_events(
            params,
            account_id=account_id,
            start=source_start,
            end=source_end,
            known_codes=known_codes,
        )
        signs_by_code = _merge_sign_events(state.get("signs", {}), sign_events)
        diagnostics.update(
            {
                "signs_complete": True,
                "sign_rows": len(signs_by_code),
                "sign_proof": sign_proof,
            }
        )

        enriched_r13_rows, address_result = _enrich_rows_with_detail_addresses(
            r13_rows,
            {**params, "enrich_addresses": True, "account_id": account_id},
        )
        if address_result.get("error"):
            raise DailySignSyncError(
                clean_text(address_result.get("error_code")) or "SOURCE_QUERY_FAILED",
                clean_text(address_result.get("error")) or "运单地址补全失败。",
                retryable=True,
            )
        r13_by_code = _r13_rows_by_code(enriched_r13_rows)
        problems_by_code = _merge_problem_events(
            state.get("problems", {}),
            problem_events,
        )
        candidate_codes = (
            set(r13_by_code)
            | {
                code
                for code, row in state.get("ledger", {}).items()
                if not bool(row.get("tms_signed"))
            }
            | set(state.get("target_station_codes", set()))
        )
        ledger_rows = [
            build_ledger_row(
                code,
                r13_row=r13_by_code.get(code),
                previous_row=state.get("ledger", {}).get(code),
                arrival_history=state.get("arrivals", {}).get(code, []),
                problem_events=problems_by_code.get(code, []),
                sign_event=signs_by_code.get(code),
                observed_at=observed_at,
            )
            for code in sorted(candidate_codes)
        ]
        fingerprint = snapshot_fingerprint(ledger_rows)
        diagnostics.update(
            {
                "candidate_rows": len(candidate_codes),
                "unmatched_rows": sum(
                    "r13_without_arrival_history" in (row.get("data_quality_flags") or [])
                    for row in ledger_rows
                ),
                "fingerprint": fingerprint,
                "source_window": {
                    "start": source_start.isoformat(),
                    "end": source_end.isoformat(),
                },
                "quality_flag_counts": dict(
                    sorted(
                        Counter(
                            clean_text(flag)
                            for row in ledger_rows
                            for flag in (row.get("data_quality_flags") or [])
                            if clean_text(flag)
                        ).items()
                    )
                ),
            }
        )
        ledger_result = persist_daily_sign_snapshot(
            problem_events=problem_events,
            sign_events=sign_events,
            ledger_rows=ledger_rows,
        )

        legacy_rows = [row for row in enriched_r13_rows if not clean_text(row.get("dispTime"))]
        legacy_rows, arrival_enrichment = _enrich_rows_with_arrival_quantities(
            legacy_rows,
            {**params, "enrich_arrival_counts": True},
        )
        if arrival_enrichment.get("error"):
            raise DailySignSyncError(
                "SOURCE_QUERY_FAILED",
                clean_text(arrival_enrichment.get("error")) or "到达件数补全失败。",
                retryable=True,
            )
        legacy_rows = _sort_rows_by_plan_sign_time(legacy_rows)
        records = _build_records(legacy_rows)
        sheet_values = _build_sheet_values(legacy_rows)
        bitable_result = sync_bitable_snapshot(
            "phase7.daily_sign_bitable",
            records,
            params,
        )
        if bitable_result.get("error"):
            raise DailySignSyncError(
                "PROJECTION_WRITE_FAILED",
                clean_text(bitable_result.get("error")) or "每日应签多维表写入失败。",
                retryable=True,
            )
        sheet_result = sync_sheet_snapshot(
            DAILY_SIGN_SHEET_RESOURCE_KEY,
            sheet_values,
            _sheet_params_for_values(params, sheet_values),
        )
        if sheet_result.get("error"):
            raise DailySignSyncError(
                "PROJECTION_WRITE_FAILED",
                clean_text(sheet_result.get("error")) or "每日应签电子表格写入失败。",
                retryable=True,
            )

        legacy_keys = _legacy_candidate_keys(enriched_r13_rows, observed_at)
        diagnostics.update(
            {
                "published_rows": len(legacy_rows),
                "legacy_candidate_rows": len(legacy_keys),
                "legacy_candidate_hash": snapshot_fingerprint(
                    [{"dedupe_key": key} for key in legacy_keys]
                ),
                "ledger_result": ledger_result,
                "address_enrichment": address_result,
                "arrival_enrichment": arrival_enrichment,
                "bitable_written": bitable_result.get("written", 0),
                "sheet_rows": sheet_result.get("rows", 0),
            }
        )
        finish_sync_run(
            run_id,
            {
                "status": "success",
                "degraded": False,
                "r13_complete": True,
                "problems_complete": True,
                "signs_complete": True,
                "r13_rows": len(r13_rows),
                "arrival_rows": diagnostics["arrival_rows"],
                "problem_rows": len(problem_events),
                "sign_rows": len(signs_by_code),
                "candidate_rows": len(candidate_codes),
                "published_rows": len(legacy_rows),
                "unmatched_rows": diagnostics["unmatched_rows"],
                "fingerprint": fingerprint,
                "diagnostics_json": diagnostics,
                "error_summary": None,
            },
        )
        evidence_refs = sorted(
            set(state.get("source_refs", []))
            | {
                f"mysql:daily_sign_sync_runs:{run_id}",
                f"r13:complete:{snapshot_fingerprint(r13_rows)}",
                f"ronghui_problems:complete:{snapshot_fingerprint(problem_events)}",
                f"ronghui_signs:complete:{snapshot_fingerprint(sign_events)}",
                f"mysql:daily_sign_ledger:{fingerprint}",
            }
        )
        return _unified_success(
            run_id=run_id,
            observed_at=observed_at,
            ledger_rows=ledger_rows,
            legacy_candidate_keys=legacy_keys,
            diagnostics=diagnostics,
            evidence_refs=evidence_refs,
        )
    except DailySignSyncError as exc:
        if run_id:
            try:
                _finish_failed_run(run_id, diagnostics, message=str(exc))
            except Exception:
                return _unified_failure(
                    code="SYNC_RUN_PERSIST_FAILED",
                    message="每日应签同步失败，且同步运行状态无法持久化。",
                    observed_at=observed_at,
                    run_id=run_id,
                    retryable=True,
                )
        return _unified_failure(
            code=exc.code,
            message=str(exc),
            observed_at=observed_at,
            run_id=run_id,
            retryable=exc.retryable,
        )
    except Exception as exc:
        safe_message = f"每日应签同步发生未分类错误：{type(exc).__name__}。"
        if run_id:
            try:
                _finish_failed_run(run_id, diagnostics, message=safe_message)
            except Exception:
                return _unified_failure(
                    code="SYNC_RUN_PERSIST_FAILED",
                    message="每日应签同步失败，且同步运行状态无法持久化。",
                    observed_at=observed_at,
                    run_id=run_id,
                    retryable=True,
                )
        return _unified_failure(
            code="DAILY_SIGN_SYNC_FAILED",
            message=safe_message,
            observed_at=observed_at,
            run_id=run_id,
            retryable=False,
        )
