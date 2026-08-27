"""MySQL persistence for the versioned daily-sign source ledger.

The module never creates schema at runtime.  Migration 010 must already be
applied.  Every connection used here explicitly disables autocommit.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Any, Iterable, Mapping

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
        "waybill_sign_verification_state",
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
_LEDGER_BOOLEAN_FIELDS = frozenset({"r13_current", "tms_signed"})
_LEDGER_INTEGER_FIELDS = frozenset({"expected_quantity", "arrived_quantity"})
_LEDGER_JSON_FIELDS = frozenset({"data_quality_flags", "calculation_trace"})
_LEDGER_TEMPORAL_FIELDS = frozenset(
    {
        "r13_plan_sign_at",
        "r13_sign_at",
        "first_seen_r13_at",
        "last_seen_r13_at",
        "first_arrival_date",
        "completion_date",
        "system_sign_due_at",
        "tms_signed_at",
    }
)


class DailySignPersistenceReadbackError(RuntimeError):
    """A daily-sign persistence terminal state cannot be proven."""


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
    set_autocommit = getattr(connection, "autocommit", None)
    if callable(set_autocommit):
        set_autocommit(False)
    return connection


def snapshot_fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    material = sorted(_json(row) for row in rows)
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise DailySignPersistenceReadbackError(
                "daily-sign JSON readback is invalid"
            ) from exc
    return value


def _temporal_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        # Daily-sign migrations use MySQL DATETIME without fractional seconds.
        # Fingerprints must bind the value that MySQL can actually persist.
        return value.replace(microsecond=0).isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _canonical_ledger_row(row: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for field in LEDGER_FIELDS:
        value = row.get(field)
        if field in _LEDGER_JSON_FIELDS:
            output[field] = _json_value(value)
        elif field in _LEDGER_BOOLEAN_FIELDS:
            output[field] = _canonical_bool(value)
        elif field in _LEDGER_INTEGER_FIELDS:
            output[field] = to_int(value)
        elif field in _LEDGER_TEMPORAL_FIELDS:
            output[field] = _temporal_value(value)
        else:
            output[field] = None if value is None else str(value).strip()
    if not output["tracking_number"]:
        raise DailySignPersistenceReadbackError(
            "daily-sign ledger readback identity is missing"
        )
    return output


def _canonical_json_object(value: Any) -> dict[str, Any]:
    decoded = _json_value(value)
    if not isinstance(decoded, dict):
        raise DailySignPersistenceReadbackError(
            "daily-sign run readback diagnostics are invalid"
        )
    return json.loads(_json(decoded))


def _canonical_payload(row: Mapping[str, Any]) -> Any:
    if "payload_json" in row:
        value = row.get("payload_json")
    else:
        value = row.get("payload") or dict(row)
    return json.loads(_json(_json_value(value)))


def _canonical_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    parsed = to_int(value)
    if parsed not in (0, 1):
        raise DailySignPersistenceReadbackError(
            "daily-sign boolean readback is invalid"
        )
    return bool(parsed)


def _problem_event_material(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source": clean_text(row.get("source")),
            "external_id": clean_text(row.get("external_id")),
            "tracking_number": clean_text(row.get("tracking_number")),
            "problem_type": clean_text(row.get("problem_type")),
            "registered_at": _temporal_value(row.get("registered_at")),
            "registered_site": clean_text(row.get("registered_site")),
            "upload_complete": _canonical_bool(row.get("upload_complete")),
            "before_cutoff": _canonical_bool(row.get("before_cutoff")),
            "postpones_sign": _canonical_bool(row.get("postpones_sign")),
            "payload": _canonical_payload(row),
        }
        for row in rows
    ]


def _sign_event_material(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source": clean_text(row.get("source")),
            "external_id": clean_text(row.get("external_id")),
            "tracking_number": clean_text(row.get("tracking_number")),
            "scan_code": clean_text(row.get("scan_code")),
            "scan_type": clean_text(row.get("scan_type")),
            "scanned_at": _temporal_value(row.get("scanned_at")),
            "scan_site": clean_text(row.get("scan_site")),
            "is_main_waybill": _canonical_bool(row.get("is_main_waybill")),
            "payload": _canonical_payload(row),
        }
        for row in rows
    ]


def _verification_material(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "tracking_number": clean_text(row.get("tracking_number")),
            "last_checked_at": _temporal_value(row.get("last_checked_at")),
            "last_result": clean_text(row.get("last_result")),
            "next_check_at": _temporal_value(row.get("next_check_at")),
            "consecutive_not_signed": to_int(row.get("consecutive_not_signed")),
            "last_error": clean_text(row.get("last_error")) or None,
        }
        for row in rows
    ]


def build_daily_sign_persistence_marker(
    *,
    problem_events: Iterable[dict[str, Any]],
    sign_events: Iterable[dict[str, Any]],
    ledger_rows: Iterable[dict[str, Any]],
    sign_verification_states: Iterable[dict[str, Any]],
    publication_rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Bind one atomic persistence transaction to every intended row set."""

    normalized_problems = _normalize_problem_events(problem_events)
    normalized_signs = _normalize_sign_events(sign_events)
    normalized_ledger = _dedupe_records(
        ledger_rows,
        identity_fields=("tracking_number",),
        label="每日应签账本",
    )
    normalized_verifications = _normalize_sign_verification_states(
        sign_verification_states
    )
    publication = _dedupe_records(
        publication_rows,
        identity_fields=("tracking_number",),
        label="每日应签发布集合",
    )
    canonical_publication = [_canonical_ledger_row(row) for row in publication]
    if any(row["tms_signed"] for row in canonical_publication):
        raise ValueError("每日应签发布集合不得包含已签收主单")
    material = {
        "schema_version": 1,
        "problem_events": {
            "count": len(normalized_problems),
            "sha256": snapshot_fingerprint(
                _problem_event_material(normalized_problems)
            ),
        },
        "sign_events": {
            "count": len(normalized_signs),
            "sha256": snapshot_fingerprint(_sign_event_material(normalized_signs)),
        },
        "sign_verification_states": {
            "count": len(normalized_verifications),
            "sha256": snapshot_fingerprint(
                _verification_material(normalized_verifications)
            ),
        },
        "ledger_rows": {
            "count": len(normalized_ledger),
            "sha256": snapshot_fingerprint(
                [_canonical_ledger_row(row) for row in normalized_ledger]
            ),
        },
        "publication_rows": {
            "count": len(canonical_publication),
            "sha256": snapshot_fingerprint(canonical_publication),
        },
    }
    return {
        **material,
        "marker_sha256": snapshot_fingerprint([material]),
    }


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


def load_completed_arrival_trackings_before(
    business_date: date,
) -> tuple[set[str], dict[str, Any]]:
    """Load waybills complete in their latest valid snapshot before the target day."""

    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(DISTINCT business_date) AS prior_successful_dates
                FROM arrival_stat_runs
                WHERE status = 'success'
                  AND is_active = TRUE
                  AND business_date < %s
                """,
                (business_date,),
            )
            run_row = cursor.fetchone() or {}
            cursor.execute(
                """
                SELECT DISTINCT i.tracking_number
                FROM arrival_stat_items AS i
                INNER JOIN arrival_stat_runs AS r ON r.run_id = i.run_id
                INNER JOIN (
                    SELECT latest_i.tracking_number,
                           MAX(latest_r.business_date) AS latest_business_date
                    FROM arrival_stat_items AS latest_i
                    INNER JOIN arrival_stat_runs AS latest_r
                        ON latest_r.run_id = latest_i.run_id
                    WHERE latest_r.status = 'success'
                      AND latest_r.is_active = TRUE
                      AND latest_r.business_date < %s
                    GROUP BY latest_i.tracking_number
                ) AS latest
                  ON latest.tracking_number = i.tracking_number
                 AND latest.latest_business_date = r.business_date
                WHERE r.status = 'success'
                  AND r.is_active = TRUE
                  AND r.business_date < %s
                  AND i.expected_quantity IS NOT NULL
                  AND i.expected_quantity > 0
                  AND i.arrived_quantity IS NOT NULL
                  AND i.arrived_quantity >= i.expected_quantity
                """,
                (business_date, business_date),
            )
            completed = {
                tracking
                for row in (cursor.fetchall() or [])
                if (tracking := clean_text(row.get("tracking_number")))
            }
    finally:
        connection.close()
    return completed, {
        "ok": True,
        "source": "arrival_stat_active_snapshots",
        "target_date": business_date.isoformat(),
        "prior_successful_dates": int(run_row.get("prior_successful_dates") or 0),
        "completed_tracking_numbers": len(completed),
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


def _normalize_sign_verification_states(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = _dedupe_records(
        rows,
        identity_fields=("tracking_number",),
        label="主单签收精确核验状态",
    )
    output: list[dict[str, Any]] = []
    for row in normalized:
        tracking_number = clean_text(row.get("tracking_number"))
        last_checked_at = parse_datetime(row.get("last_checked_at"))
        last_result = clean_text(row.get("last_result"))
        next_raw = row.get("next_check_at")
        next_check_at = parse_datetime(next_raw)
        consecutive = to_int(row.get("consecutive_not_signed"))
        last_error = clean_text(row.get("last_error"))[:500] or None
        if last_checked_at is None or last_result not in {"signed", "not_signed", "error"}:
            raise ValueError("主单签收精确核验状态缺少核验时间或结果类型无效")
        if next_raw not in (None, "") and next_check_at is None:
            raise ValueError("主单签收精确核验状态的下次核验时间无效")
        if consecutive is None or consecutive < 0:
            raise ValueError("主单签收精确核验状态的连续未签次数必须是非负整数")
        if last_result == "signed":
            if next_check_at is not None or consecutive != 0:
                raise ValueError("已签收核验状态不得保留下次核验时间或连续未签次数")
        elif next_check_at is None:
            raise ValueError("未签收或错误核验状态必须提供下次核验时间")
        if last_result == "not_signed" and consecutive < 1:
            raise ValueError("未签收核验状态的连续未签次数必须至少为 1")
        if last_result == "error" and not last_error:
            raise ValueError("错误核验状态必须提供可审计的错误摘要")
        # Migration 013 uses MySQL DATETIME (second precision).  Canonicalize
        # before building the persistence marker so the intended snapshot and
        # the fresh database readback describe the same stored values.
        last_checked_at = last_checked_at.replace(microsecond=0)
        if next_check_at is not None:
            next_check_at = next_check_at.replace(microsecond=0)
        output.append(
            {
                "tracking_number": tracking_number,
                "last_checked_at": last_checked_at,
                "last_result": last_result,
                "next_check_at": next_check_at,
                "consecutive_not_signed": consecutive,
                "last_error": last_error,
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


def _upsert_sign_verification_states(
    cursor: Any,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    cursor.executemany(
        """
        INSERT INTO waybill_sign_verification_state (
            tracking_number, last_checked_at, last_result, next_check_at,
            consecutive_not_signed, last_error
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            last_checked_at = VALUES(last_checked_at),
            last_result = VALUES(last_result),
            next_check_at = VALUES(next_check_at),
            consecutive_not_signed = VALUES(consecutive_not_signed),
            last_error = VALUES(last_error)
        """,
        [
            (
                row["tracking_number"],
                row["last_checked_at"],
                row["last_result"],
                row["next_check_at"],
                row["consecutive_not_signed"],
                row["last_error"],
            )
            for row in rows
        ],
    )


def _upsert_ledger_rows(
    cursor: Any,
    rows: list[dict[str, Any]],
    *,
    prune_missing: bool = False,
) -> None:
    cursor.execute("UPDATE daily_sign_ledger SET r13_current = FALSE")
    if not rows:
        if prune_missing:
            cursor.execute("DELETE FROM daily_sign_ledger")
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
    if prune_missing:
        placeholders = ", ".join(["%s"] * len(rows))
        cursor.execute(
            f"DELETE FROM daily_sign_ledger WHERE tracking_number NOT IN ({placeholders})",
            tuple(clean_text(row.get("tracking_number")) for row in rows),
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


def upsert_sign_verification_states(
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_sign_verification_states(rows)
    if dry_run:
        return {"ok": True, "skipped": True, "upserted": len(normalized)}
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            _upsert_sign_verification_states(cursor, normalized)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "upserted": len(normalized)}


def upsert_ledger_rows(
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = _dedupe_records(
        rows,
        identity_fields=("tracking_number",),
        label="每日应签账本",
    )
    if dry_run:
        return {"ok": True, "skipped": True, "upserted": len(normalized)}
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            _upsert_ledger_rows(cursor, normalized)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": True,
        "upserted": len(normalized),
        "fingerprint": snapshot_fingerprint(normalized),
    }


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
            cursor.execute("SELECT * FROM waybill_sign_verification_state")
            sign_verifications = {
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
            "sign_verifications": sign_verifications,
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
    sign_verification_states: list[dict[str, Any]] | None = None,
    publication_rows: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    persistence_marker: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_problems = _normalize_problem_events(problem_events)
    normalized_signs = _normalize_sign_events(sign_events)
    normalized_ledger = _dedupe_records(
        ledger_rows,
        identity_fields=("tracking_number",),
        label="每日应签账本",
    )
    normalized_verifications = _normalize_sign_verification_states(
        sign_verification_states or []
    )
    marker: dict[str, Any] | None = None
    if any(value is not None for value in (publication_rows, run_id, persistence_marker)):
        if publication_rows is None or not clean_text(run_id) or persistence_marker is None:
            raise ValueError(
                "daily-sign authoritative persistence marker arguments are incomplete"
            )
        marker = build_daily_sign_persistence_marker(
            problem_events=normalized_problems,
            sign_events=normalized_signs,
            ledger_rows=normalized_ledger,
            sign_verification_states=normalized_verifications,
            publication_rows=publication_rows,
        )
        if marker != dict(persistence_marker):
            raise ValueError("daily-sign authoritative persistence marker changed")
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            _upsert_problem_events(cursor, normalized_problems)
            _upsert_sign_events(cursor, normalized_signs)
            _upsert_sign_verification_states(cursor, normalized_verifications)
            # This transaction represents a complete authoritative snapshot.
            # Remove rows left behind by older candidate rules.  The Feishu
            # publication is a due-date-filtered subset of this full ledger.
            _upsert_ledger_rows(cursor, normalized_ledger, prune_missing=True)
            if marker is not None:
                cursor.execute(
                    """
                    UPDATE daily_sign_sync_runs
                    SET diagnostics_json = %s
                    WHERE run_id = %s AND status = 'running'
                    """,
                    (
                        _json({"persistence_commit": marker}),
                        clean_text(run_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "daily-sign sync run is missing before persistence commit"
                    )
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
        "sign_verification_states": len(normalized_verifications),
        "ledger_rows": len(normalized_ledger),
        "fingerprint": snapshot_fingerprint(normalized_ledger),
        "persistence_marker": marker,
    }


def _select_exact_identity_rows(
    cursor: Any,
    *,
    table: str,
    fields: tuple[str, ...],
    identity_fields: tuple[str, ...],
    identities: list[tuple[str, ...]],
) -> list[dict[str, Any]]:
    if not identities:
        return []
    placeholders = ", ".join(
        "(" + ", ".join(["%s"] * len(identity_fields)) + ")"
        for _identity in identities
    )
    cursor.execute(
        f"SELECT {', '.join(fields)} FROM {table} "
        f"WHERE ({', '.join(identity_fields)}) IN ({placeholders})",
        tuple(value for identity in identities for value in identity),
    )
    return list(cursor.fetchall() or [])


def _verify_row_set(
    *,
    label: str,
    expected: list[dict[str, Any]],
    observed: list[dict[str, Any]],
    marker: Any,
    identity_fields: tuple[str, ...],
) -> dict[str, Any]:
    key = lambda row: tuple(clean_text(row.get(field)) for field in identity_fields)
    expected_rows = sorted(expected, key=key)
    observed_rows = sorted(observed, key=key)
    observed_identities = [key(row) for row in observed_rows]
    if (
        any(not all(identity) for identity in observed_identities)
        or len(set(observed_identities)) != len(observed_identities)
        or observed_rows != expected_rows
    ):
        raise DailySignPersistenceReadbackError(
            f"daily-sign {label} fresh readback changed"
        )
    observed_sha256 = snapshot_fingerprint(observed_rows)
    if (
        not isinstance(marker, dict)
        or marker.get("count") != len(observed_rows)
        or marker.get("sha256") != observed_sha256
    ):
        raise DailySignPersistenceReadbackError(
            f"daily-sign {label} marker does not bind the fresh readback"
        )
    return {
        "record_count": len(observed_rows),
        "sha256": observed_sha256,
        "identities_sha256": snapshot_fingerprint(
            [dict(zip(identity_fields, identity)) for identity in observed_identities]
        ),
    }


def _select_publication_readback_rows(
    expected_publication: list[dict[str, Any]],
    observed_ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    publication_identities = {
        clean_text(row.get("tracking_number")) for row in expected_publication
    }
    return [
        row
        for row in observed_ledger
        if clean_text(row.get("tracking_number")) in publication_identities
    ]


def verify_daily_sign_persistence(
    *,
    run_id: str,
    problem_events: list[dict[str, Any]],
    sign_events: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
    sign_verification_states: list[dict[str, Any]],
    publication_rows: list[dict[str, Any]],
    persistence_marker: Mapping[str, Any],
) -> dict[str, Any]:
    """Freshly prove every event, ledger, verification, and publication row."""

    normalized_problems = _normalize_problem_events(problem_events)
    normalized_signs = _normalize_sign_events(sign_events)
    normalized_ledger = _dedupe_records(
        ledger_rows,
        identity_fields=("tracking_number",),
        label="每日应签账本",
    )
    normalized_verifications = _normalize_sign_verification_states(
        sign_verification_states
    )
    normalized_publication = _dedupe_records(
        publication_rows,
        identity_fields=("tracking_number",),
        label="每日应签发布集合",
    )
    expected_marker = build_daily_sign_persistence_marker(
        problem_events=normalized_problems,
        sign_events=normalized_signs,
        ledger_rows=normalized_ledger,
        sign_verification_states=normalized_verifications,
        publication_rows=normalized_publication,
    )
    if expected_marker != json.loads(_json(dict(persistence_marker))):
        raise DailySignPersistenceReadbackError(
            "daily-sign persistence marker does not bind the expected row sets"
        )
    expected_problems = _problem_event_material(normalized_problems)
    expected_signs = _sign_event_material(normalized_signs)
    expected_verifications = _verification_material(normalized_verifications)
    expected_ledger = [_canonical_ledger_row(row) for row in normalized_ledger]
    expected_publication = [
        _canonical_ledger_row(row) for row in normalized_publication
    ]
    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, diagnostics_json
                FROM daily_sign_sync_runs
                WHERE run_id = %s
                """,
                (clean_text(run_id),),
            )
            run_rows = list(cursor.fetchall() or [])
            raw_problems = _select_exact_identity_rows(
                cursor,
                table="waybill_problem_events",
                fields=(
                    "source",
                    "external_id",
                    "tracking_number",
                    "problem_type",
                    "registered_at",
                    "registered_site",
                    "upload_complete",
                    "before_cutoff",
                    "postpones_sign",
                    "payload_json",
                ),
                identity_fields=("source", "external_id"),
                identities=[
                    (row["source"], row["external_id"])
                    for row in expected_problems
                ],
            )
            raw_signs = _select_exact_identity_rows(
                cursor,
                table="waybill_sign_events",
                fields=(
                    "source",
                    "external_id",
                    "tracking_number",
                    "scan_code",
                    "scan_type",
                    "scanned_at",
                    "scan_site",
                    "is_main_waybill",
                    "payload_json",
                ),
                identity_fields=("source", "external_id"),
                identities=[
                    (row["source"], row["external_id"])
                    for row in expected_signs
                ],
            )
            raw_verifications = _select_exact_identity_rows(
                cursor,
                table="waybill_sign_verification_state",
                fields=(
                    "tracking_number",
                    "last_checked_at",
                    "last_result",
                    "next_check_at",
                    "consecutive_not_signed",
                    "last_error",
                ),
                identity_fields=("tracking_number",),
                identities=[
                    (row["tracking_number"],) for row in expected_verifications
                ],
            )
            cursor.execute(
                f"SELECT {', '.join(LEDGER_FIELDS)} FROM daily_sign_ledger "
                "ORDER BY tracking_number"
            )
            raw_ledger = list(cursor.fetchall() or [])
    finally:
        connection.rollback()
        connection.close()
    if len(run_rows) != 1 or clean_text(run_rows[0].get("status")) != "running":
        raise DailySignPersistenceReadbackError(
            "daily-sign persistence run marker is missing or not unique"
        )
    diagnostics = _canonical_json_object(run_rows[0].get("diagnostics_json"))
    if diagnostics != {"persistence_commit": expected_marker}:
        raise DailySignPersistenceReadbackError(
            "daily-sign persistence run marker changed"
        )
    problem_proof = _verify_row_set(
        label="problem events",
        expected=expected_problems,
        observed=_problem_event_material(raw_problems),
        marker=expected_marker.get("problem_events"),
        identity_fields=("source", "external_id"),
    )
    sign_proof = _verify_row_set(
        label="sign events",
        expected=expected_signs,
        observed=_sign_event_material(raw_signs),
        marker=expected_marker.get("sign_events"),
        identity_fields=("source", "external_id"),
    )
    verification_proof = _verify_row_set(
        label="sign verification states",
        expected=expected_verifications,
        observed=_verification_material(raw_verifications),
        marker=expected_marker.get("sign_verification_states"),
        identity_fields=("tracking_number",),
    )
    observed_ledger = [_canonical_ledger_row(row) for row in raw_ledger]
    publication_proof = _verify_row_set(
        label="publication rows",
        expected=expected_publication,
        observed=_select_publication_readback_rows(
            expected_publication,
            observed_ledger,
        ),
        marker=expected_marker.get("publication_rows"),
        identity_fields=("tracking_number",),
    )
    ledger_proof = _verify_row_set(
        label="ledger rows",
        expected=expected_ledger,
        observed=observed_ledger,
        marker=expected_marker.get("ledger_rows"),
        identity_fields=("tracking_number",),
    )
    marker_sha256 = expected_marker.get("marker_sha256")
    marker_material = {
        key: expected_marker[key]
        for key in (
            "schema_version",
            "problem_events",
            "sign_events",
            "sign_verification_states",
            "ledger_rows",
            "publication_rows",
        )
    }
    if (
        not isinstance(marker_sha256, str)
        or len(marker_sha256) != 64
        or marker_sha256 != snapshot_fingerprint([marker_material])
    ):
        raise DailySignPersistenceReadbackError(
            "daily-sign persistence marker hash is invalid"
        )
    return {
        "verified": True,
        "record_count": ledger_proof["record_count"],
        "problem_events": problem_proof,
        "sign_events": sign_proof,
        "sign_verification_states": verification_proof,
        "ledger_rows": ledger_proof,
        "publication_rows": publication_proof,
        "ledger_sha256": ledger_proof["sha256"],
        "publication_sha256": publication_proof["sha256"],
        "persistence_sha256": marker_sha256,
    }


def verify_daily_sign_completed_run(
    *,
    run_id: str,
    expected_values: Mapping[str, Any],
) -> dict[str, Any]:
    """Freshly prove the final successful run row and its bound diagnostics."""

    ensure_daily_sign_tables()
    connection = _daily_sign_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, degraded, r13_complete, problems_complete,
                       signs_complete, r13_rows, arrival_rows, problem_rows,
                       sign_rows, candidate_rows, published_rows, unmatched_rows,
                       fingerprint, diagnostics_json, error_summary
                FROM daily_sign_sync_runs
                WHERE run_id = %s
                """,
                (clean_text(run_id),),
            )
            rows = list(cursor.fetchall() or [])
    finally:
        connection.rollback()
        connection.close()
    if len(rows) != 1:
        raise DailySignPersistenceReadbackError(
            "daily-sign completed run is missing or not unique"
        )
    row = rows[0]
    boolean_fields = (
        "degraded",
        "r13_complete",
        "problems_complete",
        "signs_complete",
    )
    integer_fields = (
        "r13_rows",
        "arrival_rows",
        "problem_rows",
        "sign_rows",
        "candidate_rows",
        "published_rows",
        "unmatched_rows",
    )
    if clean_text(row.get("status")) != clean_text(expected_values.get("status")):
        raise DailySignPersistenceReadbackError(
            "daily-sign completed run status changed"
        )
    if any(
        bool(row.get(field)) != bool(expected_values.get(field))
        for field in boolean_fields
    ) or any(
        to_int(row.get(field)) != to_int(expected_values.get(field))
        for field in integer_fields
    ):
        raise DailySignPersistenceReadbackError(
            "daily-sign completed run counts changed"
        )
    if (
        clean_text(row.get("fingerprint"))
        != clean_text(expected_values.get("fingerprint"))
        or row.get("error_summary") != expected_values.get("error_summary")
        or _canonical_json_object(row.get("diagnostics_json"))
        != _canonical_json_object(expected_values.get("diagnostics_json"))
    ):
        raise DailySignPersistenceReadbackError(
            "daily-sign completed run evidence changed"
        )
    diagnostics = _canonical_json_object(row.get("diagnostics_json"))
    marker = diagnostics.get("persistence_commit")
    marker_sha256 = marker.get("marker_sha256") if isinstance(marker, dict) else None
    if clean_text(row.get("status")) == "success":
        publication = (
            marker.get("publication_rows") if isinstance(marker, dict) else None
        )
        if (
            not isinstance(publication, dict)
            or publication.get("count") != to_int(row.get("published_rows"))
            or publication.get("sha256") != clean_text(row.get("fingerprint"))
            or not isinstance(marker_sha256, str)
            or len(marker_sha256) != 64
        ):
            raise DailySignPersistenceReadbackError(
                "daily-sign completed run is not bound to the published row set"
            )
    return {
        "verified": True,
        "record_count": to_int(row.get("published_rows")) or 0,
        "publication_sha256": clean_text(row.get("fingerprint")),
        "persistence_sha256": marker_sha256 or "",
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
