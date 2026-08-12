"""MySQL persistence for the versioned daily-sign ledger."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Any, Iterable

from tools.daily_sign_rules import TARGET_STATION, business_now, clean_text, parse_datetime, to_int
from tools.phase7_mysql_store import _connect

REQUIRED_TABLES = {
    "arrival_forecast_runs",
    "arrival_forecast_items",
    "arrival_stat_runs",
    "arrival_stat_items",
    "waybill_problem_events",
    "waybill_sign_events",
    "daily_sign_ledger",
    "daily_sign_sync_runs",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def snapshot_fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    material = sorted(
        (_json(row) for row in rows),
    )
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def ensure_daily_sign_tables() -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
            present = {clean_text(row.get("TABLE_NAME")) for row in cur.fetchall() or []}
        missing = sorted(REQUIRED_TABLES - present)
        if missing:
            raise RuntimeError(
                "daily-sign schema is not migrated; run deployment migrations first: "
                + ", ".join(missing)
            )
    finally:
        conn.close()


def save_forecast_snapshot(
    business_date: date,
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    fingerprint = snapshot_fingerprint(records)
    if dry_run:
        return {"ok": True, "skipped": True, "run_id": run_id, "rows": len(records), "fingerprint": fingerprint}
    ensure_daily_sign_tables()
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO arrival_forecast_runs
                    (run_id, business_date, status, row_count, fingerprint, completed_at)
                VALUES (%s, %s, 'success', %s, %s, NOW())
                """,
                (run_id, business_date, len(records), fingerprint),
            )
            if records:
                cur.executemany(
                    """
                    INSERT INTO arrival_forecast_items (
                        run_id, tracking_number, expected_quantity, destination_station,
                        goods_name, package_type, delivery_method, recipient_address, payload_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            run_id,
                            clean_text(row.get("tracking_number")),
                            to_int(row.get("quantity")),
                            clean_text(row.get("destination_station")),
                            clean_text(row.get("goods_name")),
                            clean_text(row.get("package_type")),
                            clean_text(row.get("delivery_method")),
                            clean_text(row.get("recipient_address")),
                            _json(row),
                        )
                        for row in records
                    ],
                )
        conn.commit()
        return {"ok": True, "run_id": run_id, "rows": len(records), "fingerprint": fingerprint}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_arrival_stat_snapshot(
    business_date: date,
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    fingerprint = snapshot_fingerprint(records)
    if dry_run:
        return {"ok": True, "skipped": True, "run_id": run_id, "rows": len(records), "fingerprint": fingerprint}
    ensure_daily_sign_tables()
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE arrival_stat_runs SET is_active = FALSE WHERE business_date = %s AND is_active = TRUE",
                (business_date,),
            )
            cur.execute(
                """
                INSERT INTO arrival_stat_runs
                    (run_id, business_date, status, is_active, row_count, fingerprint, completed_at)
                VALUES (%s, %s, 'success', TRUE, %s, %s, NOW())
                """,
                (run_id, business_date, len(records), fingerprint),
            )
            if records:
                cur.executemany(
                    """
                    INSERT INTO arrival_stat_items (
                        run_id, tracking_number, destination_station, expected_quantity,
                        arrived_quantity, goods_name, package_type, delivery_method,
                        recipient_address, payload_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            run_id,
                            clean_text(row.get("tracking_number")),
                            clean_text(row.get("destination_station")),
                            to_int(row.get("expected_quantity")),
                            to_int(row.get("arrived_quantity")),
                            clean_text(row.get("goods_name")),
                            clean_text(row.get("package_type")),
                            clean_text(row.get("delivery_method")),
                            clean_text(row.get("recipient_address")),
                            _json(row),
                        )
                        for row in records
                    ],
                )
        conn.commit()
        return {"ok": True, "run_id": run_id, "rows": len(records), "fingerprint": fingerprint}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_problem_events(events: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "skipped": True, "upserted": len(events)}
    ensure_daily_sign_tables()
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            if events:
                cur.executemany(
                    """
                    INSERT INTO waybill_problem_events (
                        source, external_id, tracking_number, problem_type, registered_at,
                        registered_site, upload_complete, before_cutoff, postpones_sign, payload_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        tracking_number = VALUES(tracking_number),
                        problem_type = VALUES(problem_type),
                        registered_at = VALUES(registered_at),
                        registered_site = VALUES(registered_site),
                        upload_complete = VALUES(upload_complete),
                        before_cutoff = VALUES(before_cutoff),
                        postpones_sign = VALUES(postpones_sign),
                        payload_json = VALUES(payload_json)
                    """,
                    [
                        (
                            clean_text(row.get("source")),
                            clean_text(row.get("external_id")),
                            clean_text(row.get("tracking_number")),
                            clean_text(row.get("problem_type")),
                            parse_datetime(row.get("registered_at")),
                            clean_text(row.get("registered_site")),
                            bool(row.get("upload_complete")),
                            bool(row.get("before_cutoff")),
                            bool(row.get("postpones_sign")),
                            _json(row.get("payload") or row),
                        )
                        for row in events
                    ],
                )
        conn.commit()
        return {"ok": True, "upserted": len(events)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_sign_events(events: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "skipped": True, "upserted": len(events)}
    ensure_daily_sign_tables()
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            if events:
                cur.executemany(
                    """
                    INSERT INTO waybill_sign_events (
                        source, external_id, tracking_number, scan_code, scan_type,
                        scanned_at, scan_site, is_main_waybill, payload_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        scanned_at = VALUES(scanned_at),
                        scan_site = VALUES(scan_site),
                        payload_json = VALUES(payload_json)
                    """,
                    [
                        (
                            clean_text(row.get("source") or "tms_scan"),
                            clean_text(row.get("external_id")),
                            clean_text(row.get("tracking_number")),
                            clean_text(row.get("scan_code")),
                            clean_text(row.get("scan_type")),
                            parse_datetime(row.get("scanned_at")),
                            clean_text(row.get("scan_site")),
                            bool(row.get("is_main_waybill")),
                            _json(row.get("payload") or row),
                        )
                        for row in events
                    ],
                )
        conn.commit()
        return {"ok": True, "upserted": len(events)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_daily_sign_state() -> dict[str, Any]:
    ensure_daily_sign_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM daily_sign_ledger")
            ledger = {clean_text(row.get("tracking_number")): row for row in cur.fetchall() or []}
            cur.execute(
                """
                SELECT i.*, r.business_date
                FROM arrival_stat_items i
                JOIN arrival_stat_runs r ON r.run_id = i.run_id
                WHERE r.status = 'success' AND r.is_active = TRUE
                ORDER BY r.business_date, i.tracking_number
                """
            )
            arrivals: dict[str, list[dict[str, Any]]] = {}
            target_station_codes: set[str] = set()
            for row in cur.fetchall() or []:
                code = clean_text(row.get("tracking_number"))
                if not code:
                    continue
                arrivals.setdefault(code, []).append(row)
                if clean_text(row.get("destination_station")) == TARGET_STATION:
                    target_station_codes.add(code)
            cur.execute("SELECT * FROM waybill_problem_events ORDER BY registered_at")
            problems: dict[str, list[dict[str, Any]]] = {}
            for row in cur.fetchall() or []:
                problems.setdefault(clean_text(row.get("tracking_number")), []).append(row)
            cur.execute(
                """
                SELECT s.*
                FROM waybill_sign_events s
                JOIN (
                    SELECT tracking_number, MAX(scanned_at) AS scanned_at
                    FROM waybill_sign_events
                    WHERE is_main_waybill = TRUE AND scan_type = '签收'
                    GROUP BY tracking_number
                ) latest
                  ON latest.tracking_number = s.tracking_number
                 AND latest.scanned_at = s.scanned_at
                WHERE s.is_main_waybill = TRUE AND s.scan_type = '签收'
                """
            )
            signs = {clean_text(row.get("tracking_number")): row for row in cur.fetchall() or []}
        return {
            "ledger": ledger,
            "arrivals": arrivals,
            "target_station_codes": target_station_codes,
            "problems": problems,
            "signs": signs,
        }
    finally:
        conn.close()


def upsert_ledger_rows(rows: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "skipped": True, "upserted": len(rows)}
    ensure_daily_sign_tables()
    conn = _connect()
    fields = (
        "tracking_number", "r13_plan_sign_at", "r13_sign_status", "r13_sign_at",
        "first_seen_r13_at", "last_seen_r13_at", "r13_current", "first_arrival_date",
        "completion_date", "expected_quantity", "arrived_quantity", "arrival_status",
        "system_sign_due_at", "tms_signed", "tms_signed_at", "goods_name", "package_type",
        "delivery_method", "recipient_address", "data_quality_flags", "calculation_trace",
    )
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute("UPDATE daily_sign_ledger SET r13_current = FALSE")
            if rows:
                cur.executemany(
                    """
                    INSERT INTO daily_sign_ledger (
                        tracking_number, r13_plan_sign_at, r13_sign_status, r13_sign_at,
                        first_seen_r13_at, last_seen_r13_at, r13_current, first_arrival_date,
                        completion_date, expected_quantity, arrived_quantity, arrival_status,
                        system_sign_due_at, tms_signed, tms_signed_at, goods_name, package_type,
                        delivery_method, recipient_address, data_quality_flags, calculation_trace
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                        r13_plan_sign_at = VALUES(r13_plan_sign_at),
                        r13_sign_status = VALUES(r13_sign_status),
                        r13_sign_at = VALUES(r13_sign_at),
                        first_seen_r13_at = VALUES(first_seen_r13_at),
                        last_seen_r13_at = VALUES(last_seen_r13_at),
                        r13_current = VALUES(r13_current),
                        first_arrival_date = VALUES(first_arrival_date),
                        completion_date = VALUES(completion_date),
                        expected_quantity = VALUES(expected_quantity),
                        arrived_quantity = VALUES(arrived_quantity),
                        arrival_status = VALUES(arrival_status),
                        system_sign_due_at = VALUES(system_sign_due_at),
                        tms_signed = VALUES(tms_signed),
                        tms_signed_at = VALUES(tms_signed_at),
                        goods_name = VALUES(goods_name),
                        package_type = VALUES(package_type),
                        delivery_method = VALUES(delivery_method),
                        recipient_address = VALUES(recipient_address),
                        data_quality_flags = VALUES(data_quality_flags),
                        calculation_trace = VALUES(calculation_trace)
                    """,
                    [
                        tuple(
                            _json(row.get(field)) if field in {"data_quality_flags", "calculation_trace"}
                            else row.get(field)
                            for field in fields
                        )
                        for row in rows
                    ],
                )
        conn.commit()
        return {"ok": True, "upserted": len(rows)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def start_sync_run() -> tuple[str, datetime]:
    run_id = str(uuid.uuid4())
    started_at = business_now()
    ensure_daily_sign_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO daily_sign_sync_runs (run_id, started_at, status) VALUES (%s, %s, 'running')",
                (run_id, started_at),
            )
    finally:
        conn.close()
    return run_id, started_at


def finish_sync_run(run_id: str, values: dict[str, Any]) -> None:
    ensure_daily_sign_tables()
    conn = _connect()
    columns = (
        "status", "degraded", "r13_complete", "problems_complete", "signs_complete",
        "r13_rows", "arrival_rows", "problem_rows", "sign_rows", "candidate_rows",
        "published_rows", "unmatched_rows", "fingerprint", "diagnostics_json", "error_summary",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE daily_sign_sync_runs
                SET completed_at = %s, {', '.join(f'{column} = %s' for column in columns)}
                WHERE run_id = %s
                """,
                (business_now(),)
                + tuple(
                    _json(values.get(column)) if column == "diagnostics_json" else values.get(column)
                    for column in columns
                )
                + (run_id,),
            )
    finally:
        conn.close()


def latest_successful_sync_at() -> datetime | None:
    ensure_daily_sign_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(completed_at) AS completed_at
                FROM daily_sign_sync_runs
                WHERE status IN ('success', 'degraded') AND completed_at IS NOT NULL
                """
            )
            row = cur.fetchone() or {}
        return parse_datetime(row.get("completed_at"))
    finally:
        conn.close()
