"""Apply migration 064 and export every row it removes before removing it.

Each DROP TABLE and DELETE of the migration is preceded by a SELECT of the
exact rows that statement removes, inside the same session and transaction,
and the rows are written to a JSON-lines file in the migration backup
directory. The export is the rollback material for the retired plugin facts;
it is kept until business acceptance and then removed with the release
materials.
"""

import base64
import json
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

VERSION = "064"
_DROP = re.compile(r"DROP TABLE IF EXISTS (?P<table>[a-z0-9_]+)", re.IGNORECASE)
_DELETE = re.compile(
    r"DELETE (?P<alias>[a-z_]+) FROM (?P<table>[a-z0-9_]+) (?P=alias)\b(?P<rest>.*)",
    re.IGNORECASE | re.DOTALL,
)


def _value(value):
    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def export_query(statement):
    """Return (table, SELECT of the rows removed) for a destructive statement."""

    text = statement.strip()
    if text.upper().startswith("DROP TEMPORARY TABLE"):
        return None
    drop = _DROP.fullmatch(text)
    if drop:
        return drop["table"], f"SELECT * FROM `{drop['table']}`"
    delete = _DELETE.fullmatch(text)
    if delete:
        alias = delete["alias"]
        return delete["table"], f"SELECT {alias}.* FROM `{delete['table']}` {alias}{delete['rest']}"
    if re.match(r"(DELETE|DROP|TRUNCATE)\b", text, re.IGNORECASE):
        raise RuntimeError("migration 064 destructive statement has no export form")
    return None


def _table_exists(cursor, table):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (table,),
    )
    return int(cursor.fetchone()["n"]) > 0


def _write(output, record):
    output.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    output.flush()
    os.fsync(output.fileno())


def apply(cursor, sql_path: Path, split_sql, backup_directory: Path) -> Path:
    """Run 064 statement by statement; return the export file path."""

    statements = split_sql(sql_path.read_text(encoding="utf-8"))
    plan = [(statement, export_query(statement)) for statement in statements]
    backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = backup_directory / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-migration-064-removed-rows.jsonl"
    )
    counts: dict[str, int] = {}
    with path.open("x", encoding="utf-8") as output:
        path.chmod(0o600)
        _write(output, {"migration": VERSION, "started_at": datetime.now(timezone.utc).isoformat()})
        for index, (statement, export) in enumerate(plan):
            if export is not None:
                table, query = export
                rows = []
                if _table_exists(cursor, table):
                    cursor.execute(query)
                    rows = [{key: _value(value) for key, value in row.items()} for row in cursor.fetchall()]
                if rows:
                    _write(output, {"statement": index, "table": table, "rows": rows})
                    counts[table] = counts.get(table, 0) + len(rows)
            cursor.execute(statement)
            if statement.strip().upper() == "COMMIT":
                _write(output, {"committed_at": datetime.now(timezone.utc).isoformat()})
        _write(output, {"completed_at": datetime.now(timezone.utc).isoformat(), "removed_rows": counts})
    print(f"migration_064_export={path} removed_rows={sum(counts.values())}")
    return path
