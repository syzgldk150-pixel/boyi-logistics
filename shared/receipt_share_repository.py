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

from shared.receipt_share import IMAGE_KINDS, match_rules_sql, normalize_match_rules, shipment_numbers

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
    "s.id, s.customer_id, s.token_version, s.token_sha256, s.access_code_sha256, s.access_code_sealed, s.scope, s.date_from, s.date_to, "
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
                f"SELECT w.waybill_no, {_OPEN_DAY} AS open_day, w.status, w.remark, {link} FROM {table} w "
                f"WHERE {' AND '.join(where)} ORDER BY open_day DESC, w.waybill_no DESC LIMIT %s",
                (*params, int(limit)))
            for row in cursor.fetchall():
                linked = str(row.get("linked_platform") or "")
                photo_platform = platform if platform != "boyi" else (linked if linked in {"ronghui", "yunda"} else "")
                photo_no = str(row["waybill_no"] if platform != "boyi" else (row.get("linked_waybill_no") or ""))
                rows.append({
                    "platform": platform, "waybill_no": str(row["waybill_no"]), "open_day": str(row["open_day"] or ""),
                    "signed": row["status"] == "signed", "shipment_no": shipment_numbers(row.get("remark")),
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
                "INSERT INTO receipt_share_items (share_id, image_id, platform, waybill_no, shipment_no, kind, seq, "
                "shipment_seq, shipment_of, doc_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(int(share_id), int(item["image_id"]), item["platform"], item["waybill_no"],
                  str(item.get("shipment_no") or ""), item["kind"], int(item["seq"]), int(item.get("shipment_seq") or 1),
                  int(item.get("shipment_of") or 1), item.get("doc_date") or None) for item in items])


def mark_share_refresh(connection, share_id: int, *, state: str, message: str, now: datetime) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE receipt_shares SET refresh_state = %s, refresh_message = %s, last_refreshed_at = %s WHERE id = %s",
            (state, message[:500], now, int(share_id)))


# ---------------------------------------------------------------------------
# Management (Console): customers and links.

class ShareConflict(RuntimeError):
    """The row changed since the administrator loaded it, or a unique value is taken."""


def list_customers(connection) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, code, name, short_name, match_json, is_active FROM receipt_share_customers "
                       "ORDER BY is_active DESC, name")
        rows = cursor.fetchall()
    return [{**{key: row[key] for key in ("id", "code", "name", "short_name")}, "is_active": bool(row["is_active"]),
             "match": normalize_match_rules(_json(row["match_json"]))} for row in rows]


def get_customer(connection, customer_id: int) -> dict[str, Any] | None:
    return next((item for item in list_customers(connection) if item["id"] == int(customer_id)), None)


def save_customer(connection, *, customer_id: int | None, name: str, short_name: str, match: Mapping[str, Any],
                  actor_id: str) -> int:
    """Create or update a customer; the code is assigned once and never changes."""

    match_text = json.dumps(normalize_match_rules(match), ensure_ascii=False)
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM receipt_share_customers WHERE name = %s AND id <> %s", (name, int(customer_id or 0)))
        if cursor.fetchone():
            raise ShareConflict("已有同名客户。")
        if customer_id is None:
            cursor.execute(
                "INSERT INTO receipt_share_customers (code, name, short_name, match_json, created_by) "
                "VALUES (%s, %s, %s, %s, %s)", (f"tmp-{name}"[:32], name, short_name, match_text, actor_id))
            cursor.execute("SELECT LAST_INSERT_ID() AS id")
            customer_id = int(cursor.fetchone()["id"])
            cursor.execute("UPDATE receipt_share_customers SET code = %s WHERE id = %s", (f"KH{customer_id:05d}", customer_id))
        else:
            cursor.execute("UPDATE receipt_share_customers SET name = %s, short_name = %s, match_json = %s WHERE id = %s",
                           (name, short_name, match_text, int(customer_id)))
            if cursor.rowcount == 0:
                cursor.execute("SELECT id FROM receipt_share_customers WHERE id = %s", (int(customer_id),))
                if not cursor.fetchone():
                    raise ShareConflict("客户不存在。")
    return int(customer_id)


def sender_candidates(connection, keyword: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Sender names containing ``keyword`` with how many waybills each has, to help write a rule."""

    escaped = keyword.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    counts: dict[tuple[str, str], int] = {}
    for table in _WAYBILL_TABLES.values():
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT sender_name, sender_phone, COUNT(*) AS n FROM {table} WHERE sender_name LIKE %s ESCAPE '!' "
                "AND status <> 'cancelled' GROUP BY sender_name, sender_phone ORDER BY n DESC LIMIT %s",
                (f"%{escaped}%", int(limit)))
            for row in cursor.fetchall():
                key = (str(row["sender_name"] or ""), str(row["sender_phone"] or ""))
                counts[key] = counts.get(key, 0) + int(row["n"])
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{"sender_name": name, "sender_phone": phone, "waybills": count} for (name, phone), count in ranked]


def add_event(connection, *, share_id: int | None, customer_id: int | None, action: str, actor_id: str,
              actor_name: str, detail: Mapping[str, Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO receipt_share_events (share_id, customer_id, action, actor_id, actor_name, detail_json) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (share_id, customer_id, action, actor_id, actor_name[:128], json.dumps(dict(detail), ensure_ascii=False)))


_LIST_COLUMNS = _SHARE_COLUMNS + ", (SELECT COUNT(*) FROM receipt_share_items i WHERE i.share_id = s.id) AS item_count"


def list_shares(connection) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {_LIST_COLUMNS} FROM receipt_shares s JOIN receipt_share_customers c "
                       "ON c.id = s.customer_id ORDER BY c.name, s.id DESC")
        return [_share(row) for row in cursor.fetchall()]


def get_share(connection, share_id: int, *, for_update: bool = False) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {_SHARE_COLUMNS} FROM receipt_shares s JOIN receipt_share_customers c "
                       "ON c.id = s.customer_id WHERE s.id = %s" + (" FOR UPDATE" if for_update else ""), (int(share_id),))
        return _share(cursor.fetchone())


def share_by_request(connection, request_id: str) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM receipt_shares WHERE request_id = %s", (request_id,))
        row = cursor.fetchone()
    return get_share(connection, int(row["id"])) if row else None


def create_share(connection, *, request_id: str, customer_id: int, scope: str, date_from: Any, date_to: Any,
                 image_types: Sequence[str], naming_template: str, expires_at: datetime | None, actor_id: str,
                 token_digest: Any, access_code_digest: Any, access_code_sealed: Any = None) -> dict[str, Any]:
    """Insert a link; ``token_digest``, ``access_code_digest`` and ``access_code_sealed`` see the new id.

    The link code depends on the row id, so the row is inserted with a
    placeholder digest and completed before the caller commits.
    """

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO receipt_shares (customer_id, request_id, token_sha256, scope, date_from, date_to, "
            "image_types_json, naming_template, expires_at, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (int(customer_id), request_id, ("pending-" + request_id).ljust(64, "0")[:64], scope, date_from or None,
             date_to or None, json.dumps(list(image_types)), naming_template, expires_at, actor_id))
        cursor.execute("SELECT LAST_INSERT_ID() AS id")
        share_id = int(cursor.fetchone()["id"])
        cursor.execute("UPDATE receipt_shares SET token_sha256 = %s, access_code_sha256 = %s, access_code_sealed = %s "
                       "WHERE id = %s", (token_digest(share_id, 1), access_code_digest(share_id),
                                         access_code_sealed(share_id) if access_code_sealed else None, share_id))
    return get_share(connection, share_id)


def update_share(connection, share_id: int, *, expected_version: int, **changes: Any) -> dict[str, Any]:
    """Compare-and-set a link's status, expiry or code; unknown columns are refused."""

    allowed = {"status", "expires_at", "token_version", "token_sha256", "access_code_sha256"}
    if not changes or not set(changes) <= allowed:
        raise ValueError("unsupported share change")
    assignments = ", ".join(f"{column} = %s" for column in changes)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE receipt_shares SET {assignments}, record_version = record_version + 1 "
            "WHERE id = %s AND record_version = %s", (*changes.values(), int(share_id), int(expected_version)))
        if cursor.rowcount != 1:
            raise ShareConflict("这条链接已被修改，请刷新后重试。")
    return get_share(connection, share_id)


# ---------------------------------------------------------------------------
# Public share service: read-only, always scoped to one share id.

def public_share(connection, token_digest: str) -> dict[str, Any] | None:
    """The share a link code digest belongs to, whatever its state; the service decides what to show."""

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT s.id, s.token_version, s.access_code_sha256, s.image_types_json, s.naming_template, s.expires_at, "
            "s.status, s.last_refreshed_at, c.name AS customer_name, c.short_name AS customer_short_name, "
            "c.is_active AS customer_active FROM receipt_shares s JOIN receipt_share_customers c ON c.id = s.customer_id "
            "WHERE s.token_sha256 = %s", (token_digest,))
        row = cursor.fetchone()
    if row is None:
        return None
    result = dict(row)
    chosen = _json(result.pop("image_types_json")) or []
    result["image_types"] = [kind for kind in IMAGE_KINDS if kind in chosen]
    return result


def _public_filter(share_id: int, kinds: Sequence[str], keyword: str, date_from: str, date_to: str) -> tuple[str, list[Any]]:
    wanted = [kind for kind in IMAGE_KINDS if kind in kinds]
    if not wanted:
        return "1 = 0", []
    where = ["i.share_id = %s", f"i.kind IN ({','.join(['%s'] * len(wanted))})"]
    params: list[Any] = [int(share_id), *wanted]
    if keyword:
        pattern = "%" + keyword.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        where.append("(i.waybill_no LIKE %s ESCAPE '!' OR i.shipment_no LIKE %s ESCAPE '!')")
        params.extend([pattern, pattern])
    if date_from:
        where.append("i.doc_date >= %s")
        params.append(date_from)
    if date_to:
        where.append("i.doc_date <= %s")
        params.append(date_to)
    return " AND ".join(where), params


def public_items(connection, share_id: int, *, kinds: Sequence[str], keyword: str = "", date_from: str = "",
                 date_to: str = "", limit: int = 24, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
    """One page of a share's pictures with how many of the same kind each waybill has (for file names)."""

    where, params = _public_filter(share_id, kinds, keyword, date_from, date_to)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS n FROM receipt_share_items i WHERE {where}", params)
        total = int(cursor.fetchone()["n"])
        cursor.execute(
            "SELECT i.image_id, i.waybill_no, i.shipment_no, i.kind, i.seq, i.shipment_seq, i.shipment_of, i.doc_date, "
            "(SELECT COUNT(*) FROM receipt_share_items o WHERE o.share_id = i.share_id AND o.waybill_no = i.waybill_no "
            " AND o.kind = i.kind) AS same_kind FROM receipt_share_items i "
            f"WHERE {where} ORDER BY i.doc_date DESC, i.waybill_no DESC, i.kind, i.seq, i.image_id LIMIT %s OFFSET %s",
            (*params, int(limit), int(offset)))
        return total, [dict(row) for row in cursor.fetchall()]


def public_image(connection, share_id: int, image_id: int) -> dict[str, Any] | None:
    """A picture only if this share lists it; the archive path never leaves the server."""

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT i.image_id, i.waybill_no, i.shipment_no, i.kind, i.seq, i.shipment_seq, i.shipment_of, i.doc_date, m.nas_path, m.mime_type, m.content_sha256, "
            "(SELECT COUNT(*) FROM receipt_share_items o WHERE o.share_id = i.share_id AND o.waybill_no = i.waybill_no "
            " AND o.kind = i.kind) AS same_kind FROM receipt_share_items i JOIN receipt_share_images m ON m.id = i.image_id "
            "WHERE i.share_id = %s AND i.image_id = %s", (int(share_id), int(image_id)))
        row = cursor.fetchone()
    return dict(row) if row else None


def record_access(connection, share_id: int, *, action: str, client_ip: str, user_agent: str, now: datetime) -> None:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO receipt_share_access_logs (share_id, action, client_ip, user_agent) VALUES (%s, %s, %s, %s)",
                       (int(share_id), action, client_ip[:64], user_agent[:255]))
        if action == "view":
            cursor.execute("UPDATE receipt_shares SET view_count = view_count + 1, last_viewed_at = %s WHERE id = %s",
                           (now, int(share_id)))
