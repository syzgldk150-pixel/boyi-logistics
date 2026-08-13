"""MySQL persistence for the versioned daily-sign source ledger.

The module never creates schema at runtime.  Migration 010 must already be
applied.  Every connection used here explicitly disables autocommit.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Any, Iterable

from tools.daily_sign_rules import (
    MANUAL_POSTPONE_TYPES,
    TARGET_STATION,
    business_now,
    clean_text,
    is_before_problem_cutoff,
    parse_datetime,
    to_int,
)
from tools.phase7_mysql_store import _connect


REQUIRED_TABLES = frozenset(
    {
        "arrival_forecast_runs",
        "arrival_forecast_items",
        "arrival_stat_runs",
        "arrival_stat_items",
        "waybill_problem_events",
        "waybill_sign_events",
        "daily_sign_ledger",
        "daily_sign_sync_runs",
    }
)
LEDGER_FIELDS = (
    "tracking_number",
    "r13_plan_sign_at",
    "r13_sign_status",
    "r13_sign_at",
    "first_seen_r13_at",
    "last_seen_r13_at",
    "r13_current",
    "first_arrival_date",
    "completion_date",
    "expected_quantity",
    "arrived_quantity",
    "arrival_status",
    "system_sign_due_at",
    "tms_signed",
    "tms_signed_at",
    "goods_name",
    "package_type",
    "delivery_method",
    "recipient_address",
    "data_quality_flags",
    "calculation_trace",
)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )


def _daily_sign_connect():
    connection = _connect()
    connection.autocommit(False)
    return connection


def snapshot_fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    material = sorted(_json(row) for row in rows)
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def ensure_daily_sign_tables() -> None:
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
            present = {
                clean_text(row.get("TABLE_NAME"))
                for row in cursor.fetchall() or []
                if isinstance(row, dict)
            }
        missing = sorted(REQUIRED_TABLES - present)
        if missing:
            raise RuntimeError(
                "daily-sign schema is not migrated; run deployment migrations first: "
                + ", ".join(missing)
            )
    finally:
        connection.rollback()
        connection.close()


def _dedupe_records(
    records: Iterable[dict[str, Any]],
    *,
    identity_fields: tuple[str, ...],
    label: str,
) -> list[dict[str, Any]]:
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"{label}第 {index} 行不是对象")
        row = dict(raw)
        identity = tuple(clean_text(row.get(field)) for field in identity_fields)
        if any(not value for value in identity):
            raise ValueError(f"{label}第 {index} 行缺少唯一键：{', '.join(identity_fields)}")
        previous = unique.get(identity)
        if previous is not None and previous != row:
            raise ValueError(f"{label}存在重复冲突唯一键：{'|'.join(identity)}")
        unique[identity] = row
    return list(unique.values())


def save_forecast_snapshot(
    business_date: date,
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = _dedupe_records(
        records,
        identity_fields=("tracking_number",),
        label="到货预报",
    )
    run_id = str(uuid.uuid4())
    fingerprint = snapshot_fingerprint(normalized)
    if dry_run:
        return {
            "ok": True,
            "skipped": True,
            "run_id": run_id,
            "rows": len(normalized),
            "fingerprint": fingerprint,
        }
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO arrival_forecast_runs
                    (run_id, business_date, status, row_count, fingerprint, completed_at)
                VALUES (%s, %s, 'success', %s, %s, %s)
                """,
                (run_id, business_date, len(normalized), fingerprint, business_now()),
            )
            if normalized:
                cursor.executemany(
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
                        for row in normalized
                    ],
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": True,
        "run_id": run_id,
        "rows": len(normalized),
        "fingerprint": fingerprint,
    }


def save_arrival_stat_snapshot(
    business_date: date,
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = _dedupe_records(
        records,
        identity_fields=("tracking_number",),
        label="实际到货快照",
    )
    run_id = str(uuid.uuid4())
    fingerprint = snapshot_fingerprint(normalized)
    if dry_run:
        return {
            "ok": True,
            "skipped": True,
            "run_id": run_id,
            "rows": len(normalized),
            "fingerprint": fingerprint,
        }
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE arrival_stat_runs
                SET is_active = FALSE
                WHERE business_date = %s AND is_active = TRUE
                """,
                (business_date,),
            )
            cursor.execute(
                """
                INSERT INTO arrival_stat_runs
                    (run_id, business_date, status, is_active, row_count, fingerprint, completed_at)
                VALUES (%s, %s, 'success', TRUE, %s, %s, %s)
                """,
                (run_id, business_date, len(normalized), fingerprint, business_now()),
            )
            if normalized:
                cursor.executemany(
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
                        for row in normalized
                    ],
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": True,
        "run_id": run_id,
        "rows": len(normalized),
        "fingerprint": fingerprint,
    }


def _normalize_problem_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = _dedupe_records(
        events,
        identity_fields=("source", "external_id"),
        label="问题件事件",
    )
    output: list[dict[str, Any]] = []
    for row in normalized:
        tracking_number = clean_text(row.get("tracking_number"))
        problem_type = clean_text(row.get("problem_type"))
        registered_at = parse_datetime(row.get("registered_at"))
        if not tracking_number or not problem_type or registered_at is None:
            raise ValueError("问题件事件缺少运单号、准确类型或登记时间")
        output.append(
            {
                **row,
                "tracking_number": tracking_number,
                "problem_type": problem_type,
                "registered_at": registered_at,
                "upload_complete": bool(row.get("upload_complete")),
                "before_cutoff": is_before_problem_cutoff(registered_at),
                "postpones_sign": problem_type in MANUAL_POSTPONE_TYPES,
            }
        )
    return output


def _normalize_sign_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = _dedupe_records(
        events,
        identity_fields=("source", "external_id"),
        label="主单签收事件",
    )
    output: list[dict[str, Any]] = []
    for row in normalized:
        tracking_number = clean_text(row.get("tracking_number"))
        scan_code = clean_text(row.get("scan_code"))
        scan_type = clean_text(row.get("scan_type"))
        scanned_at = parse_datetime(row.get("scanned_at"))
        scan_site = clean_text(row.get("scan_site"))
        if (
            not tracking_number
            or scan_code != tracking_number
            or scan_type != "签收"
            or scanned_at is None
            or not scan_site
            or row.get("is_main_waybill") is not True
        ):
            raise ValueError("主单签收事件缺少精确主单号、签收类型、时间或网点")
        output.append(
            {
                **row,
                "tracking_number": tracking_number,
                "scan_code": scan_code,
                "scan_type": scan_type,
                "scanned_at": scanned_at,
                "scan_site": scan_site,
                "is_main_waybill": True,
            }
        )
    return output


def _upsert_problem_events(cursor: Any, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    cursor.executemany(
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
                row["tracking_number"],
                row["problem_type"],
                row["registered_at"],
                clean_text(row.get("registered_site")),
                row["upload_complete"],
                row["before_cutoff"],
                row["postpones_sign"],
                _json(row.get("payload") or row),
            )
            for row in events
        ],
    )


def _upsert_sign_events(cursor: Any, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    cursor.executemany(
        """
        INSERT INTO waybill_sign_events (
            source, external_id, tracking_number, scan_code, scan_type,
            scanned_at, scan_site, is_main_waybill, payload_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            tracking_number = VALUES(tracking_number),
            scan_code = VALUES(scan_code),
            scan_type = VALUES(scan_type),
            scanned_at = VALUES(scanned_at),
            scan_site = VALUES(scan_site),
            is_main_waybill = VALUES(is_main_waybill),
            payload_json = VALUES(payload_json)
        """,
        [
            (
                clean_text(row.get("source")),
                clean_text(row.get("external_id")),
                row["tracking_number"],
                row["scan_code"],
                row["scan_type"],
                row["scanned_at"],
                row["scan_site"],
                True,
                _json(row.get("payload") or row),
            )
            for row in events
        ],
    )


def _upsert_ledger_rows(cursor: Any, rows: list[dict[str, Any]]) -> None:
    cursor.execute("UPDATE daily_sign_ledger SET r13_current = FALSE")
    if not rows:
        return
    cursor.executemany(
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
                _json(row.get(field))
                if field in {"data_quality_flags", "calculation_trace"}
                else row.get(field)
                for field in LEDGER_FIELDS
            )
            for row in rows
        ],
    )


def upsert_problem_events(
    events: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_problem_events(events)
    if dry_run:
        return {"ok": True, "skipped": True, "upserted": len(normalized)}
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            _upsert_problem_events(cursor, normalized)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "upserted": len(normalized)}


def upsert_sign_events(
    events: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_sign_events(events)
    if dry_run:
        return {"ok": True, "skipped": True, "upserted": len(normalized)}
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            _upsert_sign_events(cursor, normalized)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "upserted": len(normalized)}


def load_daily_sign_state() -> dict[str, Any]:
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM daily_sign_ledger")
            ledger = {
                clean_text(row.get("tracking_number")): row
                for row in cursor.fetchall() or []
                if clean_text(row.get("tracking_number"))
            }
            cursor.execute(
                """
                SELECT i.*, r.business_date, r.fingerprint
                FROM arrival_stat_items i
                JOIN arrival_stat_runs r ON r.run_id = i.run_id
                WHERE r.status = 'success' AND r.is_active = TRUE
                ORDER BY r.business_date, i.tracking_number
                """
            )
            arrivals: dict[str, list[dict[str, Any]]] = {}
            target_station_codes: set[str] = set()
            arrival_refs: set[str] = set()
            for row in cursor.fetchall() or []:
                code = clean_text(row.get("tracking_number"))
                if not code:
                    continue
                arrivals.setdefault(code, []).append(row)
                if clean_text(row.get("destination_station")) == TARGET_STATION:
                    target_station_codes.add(code)
                arrival_refs.add(
                    f"arrival_stat:{clean_text(row.get('run_id'))}:{clean_text(row.get('fingerprint'))}"
                )
            cursor.execute(
                """
                SELECT i.*, r.business_date, r.fingerprint
                FROM arrival_forecast_items i
                JOIN arrival_forecast_runs r ON r.run_id = i.run_id
                JOIN (
                    SELECT business_date, MAX(id) AS latest_id
                    FROM arrival_forecast_runs
                    WHERE status = 'success'
                    GROUP BY business_date
                ) latest ON latest.latest_id = r.id
                ORDER BY r.business_date, i.tracking_number
                """
            )
            forecast_refs: set[str] = set()
            for row in cursor.fetchall() or []:
                if clean_text(row.get("destination_station")) == TARGET_STATION:
                    target_station_codes.add(clean_text(row.get("tracking_number")))
                forecast_refs.add(
                    f"arrival_forecast:{clean_text(row.get('run_id'))}:{clean_text(row.get('fingerprint'))}"
                )
            cursor.execute(
                """
                SELECT run_id, business_date, row_count, fingerprint, completed_at
                FROM arrival_stat_runs
                WHERE status = 'success' AND is_active = TRUE
                ORDER BY business_date, id
                """
            )
            active_stat_runs = list(cursor.fetchall() or [])
            cursor.execute(
                """
                SELECT r.run_id, r.business_date, r.row_count, r.fingerprint, r.completed_at
                FROM arrival_forecast_runs r
                JOIN (
                    SELECT business_date, MAX(id) AS latest_id
                    FROM arrival_forecast_runs
                    WHERE status = 'success'
                    GROUP BY business_date
                ) latest ON latest.latest_id = r.id
                ORDER BY r.business_date, r.id
                """
            )
            latest_forecast_runs = list(cursor.fetchall() or [])
            for row in active_stat_runs:
                arrival_refs.add(
                    f"arrival_stat:{clean_text(row.get('run_id'))}:{clean_text(row.get('fingerprint'))}"
                )
            for row in latest_forecast_runs:
                forecast_refs.add(
                    f"arrival_forecast:{clean_text(row.get('run_id'))}:{clean_text(row.get('fingerprint'))}"
                )
            cursor.execute("SELECT * FROM waybill_problem_events ORDER BY registered_at, id")
            problems: dict[str, list[dict[str, Any]]] = {}
            for row in cursor.fetchall() or []:
                code = clean_text(row.get("tracking_number"))
                if code:
                    problems.setdefault(code, []).append(row)
            cursor.execute(
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
            signs = {
                clean_text(row.get("tracking_number")): row
                for row in cursor.fetchall() or []
                if clean_text(row.get("tracking_number"))
            }
        return {
            "ledger": ledger,
            "arrivals": arrivals,
            "target_station_codes": target_station_codes,
            "problems": problems,
            "signs": signs,
            "source_refs": sorted(arrival_refs | forecast_refs),
            "arrival_source_proof": {
                "complete": bool(active_stat_runs or latest_forecast_runs),
                "active_stat_runs": len(active_stat_runs),
                "latest_forecast_runs": len(latest_forecast_runs),
                "run_ids": sorted(
                    clean_text(row.get("run_id"))
                    for row in active_stat_runs + latest_forecast_runs
                    if clean_text(row.get("run_id"))
                ),
            },
        }
    finally:
        connection.rollback()
        connection.close()


def persist_daily_sign_snapshot(
    *,
    problem_events: list[dict[str, Any]],
    sign_events: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_problems = _normalize_problem_events(problem_events)
    normalized_signs = _normalize_sign_events(sign_events)
    normalized_ledger = _dedupe_records(
        ledger_rows,
        identity_fields=("tracking_number",),
        label="每日应签账本",
    )
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            _upsert_problem_events(cursor, normalized_problems)
            _upsert_sign_events(cursor, normalized_signs)
            _upsert_ledger_rows(cursor, normalized_ledger)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": True,
        "problem_events": len(normalized_problems),
        "sign_events": len(normalized_signs),
        "ledger_rows": len(normalized_ledger),
        "fingerprint": snapshot_fingerprint(normalized_ledger),
    }


def start_sync_run() -> tuple[str, datetime]:
    ensure_daily_sign_tables()
    run_id = str(uuid.uuid4())
    started_at = business_now()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO daily_sign_sync_runs (run_id, started_at, status)
                VALUES (%s, %s, 'running')
                """,
                (run_id, started_at),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return run_id, started_at


def finish_sync_run(run_id: str, values: dict[str, Any]) -> None:
    columns = (
        "status",
        "degraded",
        "r13_complete",
        "problems_complete",
        "signs_complete",
        "r13_rows",
        "arrival_rows",
        "problem_rows",
        "sign_rows",
        "candidate_rows",
        "published_rows",
        "unmatched_rows",
        "fingerprint",
        "diagnostics_json",
        "error_summary",
    )
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE daily_sign_sync_runs
                SET completed_at = %s, {', '.join(f'{column} = %s' for column in columns)}
                WHERE run_id = %s AND status = 'running'
                """,
                (business_now(),)
                + tuple(
                    _json(values.get(column))
                    if column == "diagnostics_json"
                    else values.get(column)
                    for column in columns
                )
                + (clean_text(run_id),),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("daily-sign sync run is missing or already finished")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def latest_successful_sync_at() -> datetime | None:
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT MAX(completed_at) AS completed_at
                FROM daily_sign_sync_runs
                WHERE status = 'success' AND degraded = FALSE AND completed_at IS NOT NULL
                """
            )
            row = cursor.fetchone() or {}
        return parse_datetime(row.get("completed_at"))
    finally:
        connection.rollback()
        connection.close()


def earliest_relevant_source_date() -> date | None:
    """Return the earliest observed source date; never invent a backfill window."""

    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT MIN(source_date) AS source_date
                FROM (
                    SELECT MIN(business_date) AS source_date
                    FROM arrival_stat_runs
                    WHERE status = 'success'
                    UNION ALL
                    SELECT MIN(DATE(first_seen_r13_at)) AS source_date
                    FROM daily_sign_ledger
                    WHERE tms_signed = FALSE
                ) observed
                """
            )
            row = cursor.fetchone() or {}
        value = row.get("source_date")
        if isinstance(value, date):
            return value
        parsed = parse_datetime(value)
        return parsed.date() if parsed else None
    finally:
        connection.rollback()
        connection.close()
