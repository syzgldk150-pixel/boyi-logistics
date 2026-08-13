"""Pure business rules for the shared daily-sign ledger."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
TARGET_STATION = "邵阳大祥S站"
MANUAL_POSTPONE_TYPES = frozenset({"客户要求延迟派送", "联系不上收件人"})
SPLIT_PROBLEM_TYPE = "少货/分批"
PROBLEM_CUTOFF = time(17, 0, 0)


def business_now() -> datetime:
    return datetime.now(BUSINESS_TIMEZONE).replace(tzinfo=None)


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def to_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = clean_text(value).replace("T", " ").replace("Z", "")
    if not text:
        return None
    text = text.split(".", 1)[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: Any) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def end_of_day(value: date) -> datetime:
    return datetime.combine(value, time(23, 59, 59))


def is_before_problem_cutoff(value: Any) -> bool:
    parsed = parse_datetime(value)
    return bool(parsed and parsed.time() < PROBLEM_CUTOFF)


def is_valid_problem_event(event: dict[str, Any]) -> bool:
    return bool(event.get("upload_complete")) and is_before_problem_cutoff(event.get("registered_at"))


def _arrival_state(history: Iterable[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        (item for item in history if parse_date(item.get("business_date"))),
        key=lambda item: parse_date(item.get("business_date")) or date.min,
    )
    first_arrival_date: date | None = None
    first_partial_date: date | None = None
    completion_date: date | None = None
    latest_expected: int | None = None
    cumulative_arrived = 0
    latest_row: dict[str, Any] = {}

    for item in ordered:
        expected = to_int(item.get("expected_quantity"))
        if expected is not None:
            latest_expected = expected

    for item in ordered:
        business_date = parse_date(item.get("business_date"))
        arrived = to_int(item.get("arrived_quantity"))
        if business_date is None:
            continue
        latest_row = item
        if arrived is not None and arrived > 0:
            cumulative_arrived += arrived
        if arrived is not None and arrived > 0 and first_arrival_date is None:
            first_arrival_date = business_date
        if (
            cumulative_arrived > 0
            and latest_expected is not None
            and latest_expected > cumulative_arrived
            and first_partial_date is None
        ):
            first_partial_date = business_date
        if (
            completion_date is None
            and latest_expected is not None
            and latest_expected > 0
            and cumulative_arrived >= latest_expected
        ):
            completion_date = business_date

    if first_arrival_date is None:
        status = "not_arrived"
    elif completion_date is not None:
        status = "completed"
    elif cumulative_arrived > 0:
        status = "partial"
    else:
        status = "unknown"
    return {
        "first_arrival_date": first_arrival_date,
        "first_partial_date": first_partial_date,
        "completion_date": completion_date,
        "expected_quantity": latest_expected,
        "arrived_quantity": cumulative_arrived if first_arrival_date is not None else None,
        "arrival_status": status,
        "latest_row": latest_row,
    }


def calculate_system_sign_due(
    arrival_history: Iterable[dict[str, Any]],
    problem_events: Iterable[dict[str, Any]],
) -> tuple[datetime | None, dict[str, Any]]:
    state = _arrival_state(arrival_history)
    first_arrival = state["first_arrival_date"]
    first_partial = state["first_partial_date"]
    completion = state["completion_date"]
    valid_events = [event for event in problem_events if is_valid_problem_event(event)]
    valid_split = [event for event in valid_events if clean_text(event.get("problem_type")) == SPLIT_PROBLEM_TYPE]

    due: datetime | None = None
    reason = "no_actual_arrival"
    if first_arrival is not None:
        if completion is not None and first_partial is None:
            due = end_of_day(completion + timedelta(days=1))
            reason = "complete_on_first_arrival"
        elif completion is not None:
            due = end_of_day(completion)
            reason = "partial_then_completed"
        elif first_partial is not None and valid_split:
            due = None
            reason = "valid_split_problem_while_incomplete"
        elif first_partial is not None:
            due = end_of_day(first_partial + timedelta(days=1))
            reason = "partial_without_valid_split_problem"
        else:
            due = end_of_day(first_arrival + timedelta(days=1))
            reason = "actual_arrival_without_complete_quantity"

    applied_manual_events: list[str] = []
    for event in sorted(valid_events, key=lambda item: parse_datetime(item.get("registered_at")) or datetime.min):
        problem_type = clean_text(event.get("problem_type"))
        registered_at = parse_datetime(event.get("registered_at"))
        if problem_type not in MANUAL_POSTPONE_TYPES or registered_at is None:
            continue
        candidate_due = end_of_day(registered_at.date() + timedelta(days=1))
        if due is not None and candidate_due > due:
            due = candidate_due
            applied_manual_events.append(clean_text(event.get("external_id")) or registered_at.isoformat())

    trace = {
        "reason": reason,
        "valid_split_events": len(valid_split),
        "applied_manual_events": applied_manual_events,
    }
    return due, {**state, "trace": trace}


def build_ledger_row(
    tracking_number: str,
    *,
    r13_row: dict[str, Any] | None,
    previous_row: dict[str, Any] | None,
    arrival_history: Iterable[dict[str, Any]],
    problem_events: Iterable[dict[str, Any]],
    sign_event: dict[str, Any] | None,
    observed_at: datetime,
) -> dict[str, Any]:
    r13_row = r13_row or {}
    previous_row = previous_row or {}
    due, state = calculate_system_sign_due(arrival_history, problem_events)
    latest_arrival = state.get("latest_row") or {}
    r13_current = bool(r13_row)
    sign_at = parse_datetime(sign_event.get("scanned_at")) if sign_event else None

    def prefer(*values: Any) -> Any:
        for value in values:
            if value not in (None, ""):
                return value
        return None

    flags: list[str] = []
    if r13_current and state["first_arrival_date"] is None:
        flags.append("r13_without_arrival_history")
    if clean_text(r13_row.get("signTime") or r13_row.get("signSiteName")) and sign_at is None:
        flags.append("r13_signed_without_tms_scan")
    if not r13_current and not sign_at:
        flags.append("missing_from_current_r13")
    if (
        state["arrived_quantity"] is not None
        and state["expected_quantity"] is not None
        and state["arrived_quantity"] > state["expected_quantity"]
    ):
        flags.append("arrived_quantity_exceeds_expected")

    return {
        "tracking_number": tracking_number,
        "r13_plan_sign_at": prefer(r13_row.get("planSignTime"), previous_row.get("r13_plan_sign_at")),
        "r13_sign_status": prefer(r13_row.get("isSigns"), previous_row.get("r13_sign_status")),
        "r13_sign_at": prefer(r13_row.get("signTime"), previous_row.get("r13_sign_at")),
        "first_seen_r13_at": prefer(previous_row.get("first_seen_r13_at"), observed_at if r13_current else None),
        "last_seen_r13_at": observed_at if r13_current else previous_row.get("last_seen_r13_at"),
        "r13_current": r13_current,
        "first_arrival_date": state["first_arrival_date"],
        "completion_date": state["completion_date"],
        "expected_quantity": prefer(state["expected_quantity"], r13_row.get("pcs"), previous_row.get("expected_quantity")),
        "arrived_quantity": state["arrived_quantity"],
        "arrival_status": state["arrival_status"],
        "system_sign_due_at": due,
        "tms_signed": sign_at is not None,
        "tms_signed_at": sign_at,
        "goods_name": prefer(latest_arrival.get("goods_name"), r13_row.get("goodsName"), previous_row.get("goods_name")),
        "package_type": prefer(latest_arrival.get("package_type"), r13_row.get("packTypeDesc"), previous_row.get("package_type")),
        "delivery_method": prefer(latest_arrival.get("delivery_method"), r13_row.get("dispatchMode"), previous_row.get("delivery_method")),
        "recipient_address": prefer(latest_arrival.get("recipient_address"), r13_row.get("dispAddress"), previous_row.get("recipient_address")),
        "data_quality_flags": flags,
        "calculation_trace": state["trace"],
    }


def ledger_row_is_due(row: dict[str, Any], target_date: date) -> bool:
    if bool(row.get("tms_signed")):
        return False
    system_due = parse_datetime(row.get("system_sign_due_at"))
    if system_due is not None:
        return system_due.date() <= target_date
    r13_due = parse_datetime(row.get("r13_plan_sign_at"))
    return r13_due is not None and r13_due.date() <= target_date
