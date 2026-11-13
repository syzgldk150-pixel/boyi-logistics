"""SQL of customer share links, on the caller's connection and transaction.

Used by the Agent refresh job, the Console management pages and the public
share service (each with its own database account). Rules live in
``shared.receipt_share``; this module only reads and writes rows through the
runtime views. Every function takes an open DictCursor connection and leaves
commit/rollback to the caller.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from shared.receipt_share import IMAGE_KINDS, match_rules_sql, normalize_match_rules

# Waybill tables a customer's sender rules are matched against.
_WAYBILL_TABLES = {"ronghui": "ronghui_waybills", "yunda": "yunda_waybills", "boyi": "boyi_waybills"}
_OPEN_DAY = "LEFT(REPLACE(w.open_date, '/', '-'), 10)"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _share(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    chosen = _json(result.pop("image_types_json")) or []
    result["image_types"] = [kind for kind in IMAGE_KINDS if kind in chosen]
    if "match_json" in result:
        result["match"] = normalize_match_rules(_json(result.pop("match_json")))
    return result


_SHARE_COLUMNS = (
    "s.id, s.customer_id, s.token_version, s.token_sha256, s.access_code_sha256, s.scope, s.date_from, s.date_to, "
    "s.image_types_json, s.naming_template, s.expires_at, s.status, s.view_count, s.last_viewed_at, "
    "s.refresh_state, s.refresh_message, s.last_refreshed_at, s.record_version, s.created_at, "
    "c.code AS customer_code, c.name AS customer_name, c.short_name AS customer_short_name, c.match_json, "
    "c.is_active AS customer_active"
)


def list_refreshable_shares(connection, *, now: datetime, share_id: int | None = None) -> list[dict[str, Any]]:
    """Active, unexpired shares of active customers; optionally just one."""

    sql = (f"SELECT {_SHARE_COLUMNS} FROM receipt_shares s JOIN receipt_share_customers c ON c.id = s.customer_id "
           "WHERE s.status = 'active' AND c.is_active = 1 AND (s.expires_at IS NULL OR s.expires_at > %s)")
    params: list[Any] = [now]
    if share_id is not None:
        sql += " AND s.id = %s"
        params.append(int(share_id))
    with connection.cursor() as cursor:
        cursor.execute(sql + " ORDER BY s.id", params)
        return [_share(row) for row in cursor.fetchall()]


def matching_waybills(connection, share: Mapping[str, Any], *, limit: int = 5000) -> list[dict[str, Any]]:
    """Waybills of the share's customer inside its date scope, newest first.

    ``photo_platform``/``photo_waybill_no`` name where the pictures are read:
    the platform waybill itself, or the platform waybill a Boyi order is linked
    to. A Boyi order without a Ronghui/Yunda link has no platform pictures.
    """

    predicate, rule_params = match_rules_sql(share["match"])
    rows: list[dict[str, Any]] = []
    for platform, table in _WAYBILL_TABLES.items():
        where, params = [predicate, "w.status <> 'cancelled'", "w.waybill_no <> ''"], list(rule_params)
        if share["scope"] == "by_date":
            if share.get("date_from"):
                where.append(f"{_OPEN_DAY} >= %s")
                params.append(_day(share["date_from"]))
            if share.get("date_to"):
                where.append(f"{_OPEN_DAY} <= %s")
                params.append(_day(share["date_to"]))
        if platform == "boyi":
            where.append("w.source = 'manual'")
            link = "w.linked_platform AS linked_platform, w.linked_waybill_no AS linked_waybill_no"
        else:
            link = "NULL AS linked_platform, NULL AS linked_waybill_no"
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT w.waybill_no, {_OPEN_DAY} AS open_day, w.status, {link} FROM {table} w "
                f"WHERE {' AND '.join(where)} ORDER BY open_day DESC, w.waybill_no DESC LIMIT %s",
                (*params, int(limit)))
            for row in cursor.fetchall():
                linked = str(row.get("linked_platform") or "")
                photo_platform = platform if platform != "boyi" else (linked if linked in {"ronghui", "yunda"} else "")
                photo_no = str(row["waybill_no"] if platform != "boyi" else (row.get("linked_waybill_no") or ""))
                rows.append({
                    "platform": platform, "waybill_no": str(row["waybill_no"]), "open_day": str(row["open_day"] or ""),
                    "signed": row["status"] == "signed",
                    "photo_platform": photo_platform if photo_no else "", "photo_waybill_no": photo_no if photo_platform else "",
                })
    rows.sort(key=lambda item: (item["open_day"], item["waybill_no"]), reverse=True)
    return rows[:limit]


def _day(value: Any) -> str:
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value)[:10]


def approved_receipt_waybills(connection, platform: str, waybill_numbers: Sequence[str]) -> set[str]:
    """Waybills whose receipt passed the platform audit (scope "仅审核通过")."""

    numbers = sorted(set(waybill_numbers))
    approved: set[str] = set()
    for start in range(0, len(numbers), 500):
        batch = numbers[start:start + 500]
        marks = ",".join(["%s"] * len(batch))
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT DISTINCT waybill_no FROM receipt_records WHERE platform = %s AND waybill_no IN ({marks}) "
                "AND audit_status LIKE %s AND audit_status NOT LIKE %s",
                (platform, *batch, "%通过%", "%不通过%"))
            approved.update(str(row["waybill_no"]) for row in cursor.fetchall())
    return approved


def waybill_states(connection, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    wanted = sorted(set(keys))
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for start in range(0, len(wanted), 300):
        batch = wanted[start:start + 300]
        marks = ",".join(["(%s,%s)"] * len(batch))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT platform, waybill_no, sign_count, receipt_count, check_count, last_checked_at "
                f"FROM receipt_share_waybill_state WHERE (platform, waybill_no) IN ({marks})",
                tuple(part for key in batch for part in key))
            for row in cursor.fetchall():
                result[(str(row["platform"]), str(row["waybill_no"]))] = dict(row)
    return result


def record_waybill_check(connection, platform: str, waybill_no: str, *, sign_count: int, receipt_count: int,
                         error: str, now: datetime) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO receipt_share_waybill_state (platform, waybill_no, sign_count, receipt_count, check_count, "
            "last_error, last_checked_at) VALUES (%s, %s, %s, %s, 1, %s, %s) ON DUPLICATE KEY UPDATE "
            "sign_count = GREATEST(sign_count, VALUES(sign_count)), "
            "receipt_count = GREATEST(receipt_count, VALUES(receipt_count)), check_count = check_count + 1, "
            "last_error = VALUES(last_error), last_checked_at = VALUES(last_checked_at)",
            (platform, waybill_no, int(sign_count), int(receipt_count), error[:200], now))


def known_image_sources(connection, platform: str, waybill_no: str) -> set[tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT kind, source_key FROM receipt_share_images WHERE platform = %s AND waybill_no = %s",
                       (platform, waybill_no))
        return {(str(row["kind"]), str(row["source_key"])) for row in cursor.fetchall()}


def insert_image(connection, *, platform: str, waybill_no: str, kind: str, source_key: str, seq: int,
                 content_sha256: str, nas_path: str, mime_type: str, file_size: int, doc_date: str | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO receipt_share_images (platform, waybill_no, kind, source_key, seq, content_sha256, "
            "nas_path, mime_type, file_size, doc_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (platform, waybill_no, kind, source_key, int(seq), content_sha256, nas_path, mime_type, int(file_size),
             doc_date or None))


def images_of_waybills(connection, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    wanted = sorted(set(keys))
    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for start in range(0, len(wanted), 300):
        batch = wanted[start:start + 300]
        marks = ",".join(["(%s,%s)"] * len(batch))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, platform, waybill_no, kind, seq, doc_date FROM receipt_share_images "
                f"WHERE (platform, waybill_no) IN ({marks}) ORDER BY kind, seq, id",
                tuple(part for key in batch for part in key))
            for row in cursor.fetchall():
                result.setdefault((str(row["platform"]), str(row["waybill_no"])), []).append(dict(row))
    return result


def replace_share_items(connection, share_id: int, items: Sequence[Mapping[str, Any]]) -> None:
    """Swap what a link shows in the caller's transaction: readers see the old set or the new one."""

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM receipt_share_items WHERE share_id = %s", (int(share_id),))
        if items:
            cursor.executemany(
                "INSERT INTO receipt_share_items (share_id, image_id, platform, waybill_no, kind, seq, doc_date) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(int(share_id), int(item["image_id"]), item["platform"], item["waybill_no"], item["kind"],
                  int(item["seq"]), item.get("doc_date") or None) for item in items])


def mark_share_refresh(connection, share_id: int, *, state: str, message: str, now: datetime) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE receipt_shares SET refresh_state = %s, refresh_message = %s, last_refreshed_at = %s WHERE id = %s",
            (state, message[:500], now, int(share_id)))
