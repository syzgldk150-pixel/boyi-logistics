"""R7 transport departure check-in automation.

This script logs in to R7 independently from the shared TMS SMS session, opens
the transport task page, filters the last 3 days, matches scheduled departure
rows, checks their boxes, clicks "装车待发", and verifies the status change.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from typing import Any, Optional

from auto_checkin_r7 import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    HOME_URL,
    _as_bool,
    _click_first_visible,
    _count,
    _exists,
    _is_checkbox_checked,
    _now_iso,
    _row_cell_text,
    _visible_button_snapshot,
    _wait_loading_mask_clear,
    apply_last_3_days_filter,
    build_auth,
    click_confirm_twice,
    click_search,
    do_login,
    find_checkbox_locator_for_row,
    launch_browser,
    log,
    navigate_to_transport_task_management,
)


DEFAULT_STATUS_TEXT = "已调度"
DEFAULT_VERIFY_STATUS_TEXT = "装车待发"
DEFAULT_CLASS_NAME = "邵阳操作场-长沙"
DEFAULT_PLATE_NUMBER = "湘AK6980"
DEFAULT_DEPARTURE_TIME = "21:30:00"

XPATH_DEPARTURE_WAIT_LOAD_BUTTON = (
    '//button[.//span[normalize-space(.)="装车待发"] or normalize-space(.)="装车待发"]'
    '|//*[@role="button"][.//span[normalize-space(.)="装车待发"] or normalize-space(.)="装车待发"]'
)


def _today_date_str(today: _dt.date | None = None) -> str:
    return (today or _dt.datetime.now().date()).strftime("%Y-%m-%d")


def expected_departure_time(
    value: str | None = None,
    *,
    fixed_time: str = DEFAULT_DEPARTURE_TIME,
    today: _dt.date | None = None,
) -> str:
    text = str(value or "").strip()
    date_part = _today_date_str(today)
    time_part = str(fixed_time or DEFAULT_DEPARTURE_TIME).strip() or DEFAULT_DEPARTURE_TIME
    if text:
        parts = text.split()
        if len(parts) >= 2:
            if ":" in parts[1]:
                time_part = parts[1].strip()
        elif ":" in parts[0]:
            time_part = parts[0].strip()
    return f"{date_part} {time_part}"


def normalize_plate_numbers(value: Any, *, default: str = DEFAULT_PLATE_NUMBER) -> list[str]:
    if value in (None, ""):
        raw_items: list[Any] = [default]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        text = str(value)
        for sep in ("，", "、", ";", "；", "\n", "\r", "\t"):
            text = text.replace(sep, ",")
        raw_items = text.split(",")

    plates: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        plate = str(item or "").strip()
        if not plate or plate in seen:
            continue
        seen.add(plate)
        plates.append(plate)
    return plates or [default]


def _norm(value: Any) -> str:
    return "".join(str(value or "").split())


def _parse_datetime_loose(value: Any) -> _dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?",
        text,
    )
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return _dt.datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second or 0),
        )
    except ValueError:
        return None


def _same_departure_time(actual: Any, expected: Any) -> bool:
    actual_dt = _parse_datetime_loose(actual)
    expected_dt = _parse_datetime_loose(expected)
    if actual_dt is not None and expected_dt is not None:
        return actual_dt.replace(second=0, microsecond=0) == expected_dt.replace(second=0, microsecond=0)
    return _norm(actual) == _norm(expected)


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in ("task_no", "status", "departure_time", "route_name", "class_name", "plate_number")
    }


def collect_transport_rows(page) -> list[dict[str, Any]]:
    row_locator = page.locator(
        'xpath=//div[contains(@class,"el-table__body-wrapper")]'
        '//tr[contains(@class,"el-table__row")]'
    )
    rows: list[dict[str, Any]] = []
    for index in range(_count(row_locator)):
        row = row_locator.nth(index)
        rows.append(
            {
                "index": index,
                "row": row,
                "task_no": _row_cell_text(row, column_index=2),
                "status": _row_cell_text(row, column_index=3),
                "departure_time": _row_cell_text(row, column_index=4),
                "route_name": _row_cell_text(row, column_index=6),
                "class_name": _row_cell_text(row, column_index=7),
                "plate_number": _row_cell_text(row, column_index=8),
            }
        )
    return rows


def _record_matches(
    record: dict[str, Any],
    *,
    status_text: str,
    departure_time_text: str,
    class_name: str,
    plate_number: str,
) -> bool:
    return (
        _norm(record.get("status")) == _norm(status_text)
        and _same_departure_time(record.get("departure_time"), departure_time_text)
        and _norm(record.get("class_name")) == _norm(class_name)
        and _norm(record.get("plate_number")) == _norm(plate_number)
    )


def select_departure_targets(
    rows: list[dict[str, Any]],
    *,
    status_text: str,
    departure_time_text: str,
    class_name: str,
    plate_numbers: list[str],
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for plate in normalize_plate_numbers(plate_numbers):
        matches = [
            row
            for row in rows
            if _record_matches(
                row,
                status_text=status_text,
                departure_time_text=departure_time_text,
                class_name=class_name,
                plate_number=plate,
            )
        ]
        if len(matches) != 1:
            errors.append(
                {
                    "plate_number": plate,
                    "match_count": len(matches),
                    "candidates": [_public_record(item) for item in matches[:10]],
                }
            )
            continue
        targets.append(matches[0])

    if errors:
        return {
            "ok": False,
            "stage": "target_match_failed",
            "message": "目标车牌未唯一命中，已停止执行",
            "errors": errors,
            "targets": [],
        }
    return {"ok": True, "stage": "target_matched", "targets": targets, "errors": []}


def ensure_checkbox_checked_on_page(page, checkbox, *, click_timeout_ms: int = 10_000) -> bool:
    if checkbox is None or not _exists(checkbox):
        return False

    inner = checkbox.first
    if _is_checkbox_checked(inner):
        return True

    try:
        inner.scroll_into_view_if_needed()
    except Exception:
        pass

    try:
        inner.click(timeout=click_timeout_ms)
    except Exception:
        try:
            inner.click(timeout=click_timeout_ms, force=True)
        except Exception:
            return _is_checkbox_checked(inner)

    deadline = time.time() + 1.5
    while time.time() < deadline:
        if _is_checkbox_checked(inner):
            return True
        page.wait_for_timeout(150)
    return _is_checkbox_checked(inner)


def run_once(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    params = params or {}
    started = time.time()

    status_text = str(params.get("status_text") or DEFAULT_STATUS_TEXT).strip() or DEFAULT_STATUS_TEXT
    verify_status_text = (
        str(params.get("verify_status_text") or DEFAULT_VERIFY_STATUS_TEXT).strip()
        or DEFAULT_VERIFY_STATUS_TEXT
    )
    class_name = str(params.get("class_name") or DEFAULT_CLASS_NAME).strip() or DEFAULT_CLASS_NAME
    plate_numbers = normalize_plate_numbers(
        params.get("plate_numbers")
        if params.get("plate_numbers") not in (None, "")
        else params.get("plate_number")
    )
    departure_time_text = expected_departure_time(
        params.get("plan_departure_time"),
        fixed_time=str(params.get("departure_time_fixed") or DEFAULT_DEPARTURE_TIME),
    )

    username = str(params.get("username") or DEFAULT_USERNAME)
    password = str(params.get("password") or DEFAULT_PASSWORD)
    headless = _as_bool(params.get("headless"), default=True)
    slow_mo_ms = int(params.get("slow_mo_ms") or 0)
    max_login_attempts = int(params.get("max_login_attempts") or 6)
    after_search_delay_ms = int(params.get("after_search_delay_ms") or 0)
    after_action_delay_ms = int(params.get("after_action_delay_ms") or 1500)
    confirm_clicks_max = int(params.get("confirm_clicks_max") or 1)
    confirm_timeout_ms = int(params.get("confirm_timeout_ms") or 20_000)
    do_departure_checkin = _as_bool(params.get("do_departure_checkin"), default=True)

    p = browser = context = page = None
    stage = "start"
    try:
        p, browser, context, page = launch_browser(
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            use_tms_storage_state=False,
        )
        auth = build_auth(max_attempts=max_login_attempts)

        stage = "login"
        do_login(page, auth, username=username, password=password)

        stage = "navigate"
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        navigate_to_transport_task_management(page)
        apply_last_3_days_filter(page)
        click_search(page)
        _wait_loading_mask_clear(page)
        if after_search_delay_ms > 0:
            page.wait_for_timeout(after_search_delay_ms)

        stage = "target_match"
        rows = collect_transport_rows(page)
        selected = select_departure_targets(
            rows,
            status_text=status_text,
            departure_time_text=departure_time_text,
            class_name=class_name,
            plate_numbers=plate_numbers,
        )
        if not selected.get("ok"):
            return {
                "ok": False,
                "stage": selected.get("stage") or stage,
                "message": selected.get("message") or "目标行匹配失败",
                "detail": {
                    "status_text": status_text,
                    "verify_status_text": verify_status_text,
                    "class_name": class_name,
                    "departure_time": departure_time_text,
                    "plate_numbers": plate_numbers,
                    "errors": selected.get("errors") or [],
                    "visible_rows": [_public_record(row) for row in rows[:20]],
                    "url": page.url,
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        targets = selected.get("targets") or []
        checkbox_pairs: list[tuple[dict[str, Any], Any]] = []
        for target in targets:
            checkbox = find_checkbox_locator_for_row(page, target.get("row"))
            if checkbox is None or not _exists(checkbox):
                return {
                    "ok": False,
                    "stage": "checkbox_not_found",
                    "message": "目标行复选框未找到，已停止执行",
                    "detail": {
                        "target": _public_record(target),
                        "status_text": status_text,
                        "class_name": class_name,
                        "departure_time": departure_time_text,
                        "plate_numbers": plate_numbers,
                        "url": page.url,
                    },
                    "ts": _now_iso(),
                    "cost_sec": round(time.time() - started, 3),
                }
            checkbox_pairs.append((target, checkbox))

        stage = "checkbox"
        for target, checkbox in checkbox_pairs:
            if not ensure_checkbox_checked_on_page(page, checkbox):
                return {
                    "ok": False,
                    "stage": "checkbox_not_checked",
                    "message": "目标行复选框未能勾选，已停止执行",
                    "detail": {
                        "target": _public_record(target),
                        "status_text": status_text,
                        "class_name": class_name,
                        "departure_time": departure_time_text,
                        "plate_numbers": plate_numbers,
                        "url": page.url,
                    },
                    "ts": _now_iso(),
                    "cost_sec": round(time.time() - started, 3),
                }

        if not do_departure_checkin:
            return {
                "ok": True,
                "stage": "checkbox_checked",
                "message": "checkbox checked",
                "detail": {
                    "status_text": status_text,
                    "verify_status_text": verify_status_text,
                    "class_name": class_name,
                    "departure_time": departure_time_text,
                    "plate_numbers": plate_numbers,
                    "targets": [_public_record(target) for target, _ in checkbox_pairs],
                    "url": page.url,
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        stage = "departure_action_clicked"
        _wait_loading_mask_clear(page)
        _click_first_visible(page, XPATH_DEPARTURE_WAIT_LOAD_BUTTON, label="装车待发")
        _wait_loading_mask_clear(page)

        stage = "departure_confirm_clicked"
        confirm_clicks = click_confirm_twice(
            page,
            timeout_ms=confirm_timeout_ms,
            times=confirm_clicks_max,
        )
        if confirm_clicks < 1:
            return {
                "ok": False,
                "stage": stage,
                "message": "未出现装车待发确认弹窗",
                "detail": {
                    "status_text": status_text,
                    "class_name": class_name,
                    "departure_time": departure_time_text,
                    "plate_numbers": plate_numbers,
                    "confirm_clicks": confirm_clicks,
                    "url": page.url,
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        stage = "verify_search"
        try:
            click_search(page)
            _wait_loading_mask_clear(page)
        except Exception:
            pass
        if after_action_delay_ms > 0:
            page.wait_for_timeout(after_action_delay_ms)

        stage = "verify_status"
        verify_rows = collect_transport_rows(page)
        verify_selected = select_departure_targets(
            verify_rows,
            status_text=verify_status_text,
            departure_time_text=departure_time_text,
            class_name=class_name,
            plate_numbers=plate_numbers,
        )
        if not verify_selected.get("ok"):
            return {
                "ok": False,
                "stage": stage,
                "message": "装车待发验证失败",
                "detail": {
                    "status_text": status_text,
                    "verify_status_text": verify_status_text,
                    "class_name": class_name,
                    "departure_time": departure_time_text,
                    "plate_numbers": plate_numbers,
                    "targets": [_public_record(target) for target, _ in checkbox_pairs],
                    "errors": verify_selected.get("errors") or [],
                    "visible_rows": [_public_record(row) for row in verify_rows[:20]],
                    "confirm_clicks": confirm_clicks,
                    "url": page.url,
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        return {
            "ok": True,
            "stage": "done",
            "message": "success",
            "detail": {
                "status_text": status_text,
                "verify_status_text": verify_status_text,
                "class_name": class_name,
                "departure_time": departure_time_text,
                "plate_numbers": plate_numbers,
                "targets": [_public_record(target) for target, _ in checkbox_pairs],
                "verify_targets": [
                    _public_record(target) for target in (verify_selected.get("targets") or [])
                ],
                "confirm_clicks": confirm_clicks,
                "url": page.url,
            },
            "ts": _now_iso(),
            "cost_sec": round(time.time() - started, 3),
        }
    except BaseException as exc:
        detail = {
            "status_text": status_text,
            "verify_status_text": verify_status_text,
            "class_name": class_name,
            "departure_time": departure_time_text,
            "plate_numbers": plate_numbers,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        if page is not None:
            try:
                detail["url"] = page.url
            except Exception:
                pass
            buttons = _visible_button_snapshot(page)
            if buttons:
                detail["available_buttons"] = buttons
        return {
            "ok": False,
            "stage": stage,
            "message": f"{type(exc).__name__}: {exc}",
            "detail": detail,
            "ts": _now_iso(),
            "cost_sec": round(time.time() - started, 3),
        }
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass


def main() -> int:
    try:
        params = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"参数 JSON 解析失败：{exc.msg}"}, ensure_ascii=False))
        return 1
    result = run_once(params if isinstance(params, dict) else {})
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
