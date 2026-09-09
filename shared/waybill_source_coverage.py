"""Source identity and complete-day publication for direct waybill queries.

Scope values must come from a reviewed provider protocol. Account IDs never
participate in entity identity. This module neither discovers credentials nor
guesses a scope for old records.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from shared.runtime_repositories import WAYBILL_FIELDS, WaybillRepository, _connection, _cursor

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class WaybillSourceScope:
    source: str
    source_scope: str
    permission_scope: str
    account_id: str

    def __post_init__(self) -> None:
        if self.source not in {"ronghui", "yunda"}:
            raise ValueError("unsupported waybill source")
        for field in ("source_scope", "permission_scope", "account_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 128:
                raise ValueError(f"verified {field} is required")


def utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("source observation time must include timezone")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def coverage_satisfies(row: Mapping[str, Any] | None, scope: WaybillSourceScope,
                       business_date: date, *, requested_at: datetime) -> bool:
    """Never call a still-open day complete, even after a 23:55 collection."""
    if not row:
        return False
    if any(str(row.get(key) or "") != getattr(scope, key)
           for key in ("source", "source_scope", "permission_scope", "account_id")):
        return False
    if str(row.get("business_date"))[:10] != business_date.isoformat():
        return False
    if business_date >= requested_at.astimezone(SHANGHAI).date():
        return False
    return row.get("final_day") in (True, 1) and row.get("complete_through") is not None


class WaybillSourceRepository:
    def __init__(self, connection_factory: Callable[[], Any]):
        self._connection_factory = connection_factory

    def coverage(self, scope: WaybillSourceScope, business_date: date) -> dict[str, Any] | None:
        with _connection(self._connection_factory) as connection, _cursor(connection, None) as cursor:
            cursor.execute("""SELECT * FROM waybill_source_coverage WHERE source=%s
                AND source_scope=%s AND permission_scope=%s AND business_date=%s""",
                (scope.source, scope.source_scope, scope.permission_scope, business_date))
            return cursor.fetchone()

    def read_cached(self, *, source: str, date_from: date | None = None,
                    date_to: date | None = None, waybill_no: str = "") -> dict[str, Any]:
        where, args = ["waybill_no<>''"], []
        if source != "all":
            where.append("source=%s")
            args.append(source)
        if date_from:
            where.append("open_date>=%s")
            args.append(date_from.isoformat())
        if date_to:
            where.append("open_date<=%s")
            args.append(date_to.isoformat())
        if waybill_no:
            where.append("BINARY waybill_no=%s")
            args.append(waybill_no)
        predicate = " AND ".join(where)
        with _connection(self._connection_factory) as connection, _cursor(connection, None) as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            cursor.execute(f"SELECT COUNT(*) AS total FROM waybills WHERE {predicate}", args)
            total = int(cursor.fetchone()["total"])
            cursor.execute(f"SELECT * FROM waybills WHERE {predicate} ORDER BY open_date DESC,id DESC LIMIT 100", args)
            rows = [WaybillRepository._row_to_dict(row) for row in cursor.fetchall()]
        return {"rows": rows, "total": total, "returned": len(rows), "snapshot": "local_database"}

    def read_scope(self, scope: WaybillSourceScope, business_date: date) -> list[dict[str, Any]]:
        with _connection(self._connection_factory) as connection, _cursor(connection, None) as cursor:
            cursor.execute("""SELECT * FROM waybills WHERE source=%s AND source_scope=%s
                AND source_permission_scope=%s AND open_date=%s ORDER BY source_record_id""",
                (scope.source, scope.source_scope, scope.permission_scope, business_date.isoformat()))
            return [WaybillRepository._row_to_dict(row) for row in cursor.fetchall()]

    def publish(self, records: Sequence[Mapping[str, Any]], *, scope: WaybillSourceScope,
                business_date: date, captured_at: datetime, complete: bool,
                expected_total: int | None = None) -> dict[str, Any]:
        """Atomically upsert identities; only a proven complete scope may delete.

        A failed fetch must not call this function. Partial/point queries cannot
        advance coverage and cannot delete rows. Unknown legacy rows are untouched.
        """
        observed = utc_naive(captured_at)
        normalized: list[tuple[str, dict[str, str]]] = []
        identities: set[str] = set()
        for record in records:
            identity = str(record.get("source_record_id") or "").strip()
            row = WaybillRepository._normalized_record(dict(record))
            if not identity or len(identity) > 128 or row is None:
                raise ValueError("waybill source record identity is missing")
            if identity in identities or row["open_date"] != business_date.isoformat():
                raise ValueError("waybill source identities or dates do not match the query")
            identities.add(identity)
            normalized.append((identity, row))
        if complete and (type(expected_total) is not int or expected_total != len(normalized)):
            raise ValueError("complete publication requires an exact authoritative total")
        final_day = complete and business_date < captured_at.astimezone(SHANGHAI).date()
        day_end = utc_naive(datetime.combine(business_date, time.max, SHANGHAI))
        through = min(observed, day_end) if complete else None
        created = updated = deleted = 0
        with _connection(self._connection_factory) as connection, _cursor(connection, None) as cursor:
            # Serialize only this provider scope/day. A placeholder never claims coverage.
            cursor.execute("""INSERT INTO waybill_source_coverage
                (source,source_scope,permission_scope,business_date,account_id,captured_at,
                 complete_through,final_day,record_count,revision)
                VALUES(%s,%s,%s,%s,%s,%s,NULL,FALSE,0,1)
                ON DUPLICATE KEY UPDATE revision=revision""",
                (scope.source, scope.source_scope, scope.permission_scope, business_date,
                 scope.account_id, observed))
            cursor.execute("""SELECT * FROM waybill_source_coverage WHERE source=%s AND source_scope=%s
                AND permission_scope=%s AND business_date=%s FOR UPDATE""",
                (scope.source, scope.source_scope, scope.permission_scope, business_date))
            previous = cursor.fetchone()
            if previous and previous["captured_at"] > observed:
                raise ValueError("an older source snapshot cannot replace a newer publication")
            for identity, row in sorted(normalized, key=lambda item: item[0]):
                cursor.execute("""SELECT id FROM waybills WHERE source=%s AND source_scope=%s
                    AND source_record_id=%s FOR UPDATE""", (scope.source, scope.source_scope, identity))
                existing = cursor.fetchone()
                if existing:
                    fields = [field for field in WAYBILL_FIELDS if field != "status"]
                    cursor.execute(f"""UPDATE waybills SET {', '.join(field+'=%s' for field in fields)},
                        status=CASE WHEN status='cancelled' THEN status ELSE %s END,
                        source_account_id=%s,source_permission_scope=CASE WHEN %s THEN %s ELSE source_permission_scope END,
                        updated_at=UTC_TIMESTAMP(6) WHERE id=%s""",
                        [*[row[field] for field in fields], row["status"], scope.account_id, complete,
                         scope.permission_scope, existing["id"]])
                    updated += 1
                else:
                    columns = [*WAYBILL_FIELDS, "source", "source_scope", "source_record_id", "source_account_id", "source_permission_scope"]
                    cursor.execute(f"""INSERT INTO waybills ({','.join(columns)},created_at,updated_at)
                        VALUES({','.join('%s' for _ in columns)},UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                        [*[row[field] for field in WAYBILL_FIELDS], scope.source, scope.source_scope,
                         identity, scope.account_id, scope.permission_scope if complete else None])
                    created += 1
            if complete:
                deletion = "DELETE FROM waybills WHERE source=%s AND source_scope=%s AND source_permission_scope=%s AND open_date=%s AND status<>'cancelled'"
                args: list[Any] = [scope.source, scope.source_scope, scope.permission_scope, business_date.isoformat()]
                if identities:
                    deletion += f" AND source_record_id NOT IN ({','.join('%s' for _ in identities)})"
                    args.extend(sorted(identities))
                cursor.execute(deletion, args)
                deleted = cursor.rowcount
                cursor.execute("""UPDATE waybill_source_coverage SET account_id=%s,captured_at=%s,
                    complete_through=%s,final_day=%s,record_count=%s,revision=revision+1
                    WHERE source=%s AND source_scope=%s AND permission_scope=%s AND business_date=%s""",
                    (scope.account_id, observed, through, final_day, len(normalized), scope.source,
                     scope.source_scope, scope.permission_scope, business_date))
            else:
                cursor.execute("""UPDATE waybill_source_coverage SET revision=revision+1, captured_at=%s,
                    final_day=FALSE, complete_through=NULL
                    WHERE source=%s AND source_scope=%s AND permission_scope=%s AND business_date=%s""",
                    (observed, scope.source, scope.source_scope, scope.permission_scope, business_date))
        return {"ok": True, "source": scope.source, "upserted": created+updated,
                "creates": created, "updates": updated, "deleted_stale": deleted,
                "target_date": business_date.isoformat(), "complete": complete, "final_day": final_day}
