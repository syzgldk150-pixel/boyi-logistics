"""Deploy-only physical database split, exact row verification and rollback.

The old SQL names are explicit INVOKER views, not replicated data. The two
union views are read-only; writers must choose their platform table. Original
split tables remain as clearly named migration archives until acceptance.
"""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re

VERSION = "055"
STATE_TABLE = "domain_database_split_055"
FINANCE_TABLES = (
    "finance_sync_batches", "finance_sync_runs", "finance_fee_items",
    "finance_transactions", "finance_summary_snapshots", "finance_fee_mappings",
    "finance_mapping_audit_logs", "finance_fee_subjects", "finance_review_cases",
    "finance_review_ai_runs", "finance_waybill_facts", "finance_anomalies",
    "finance_knowledge_exports", "finance_source_run_bindings",
)
LOGISTICS_TABLES = (
    "waybill_sequences", "waybill_provider_snapshots", "waybill_source_coverage",
    "receipt_attachments", "receipt_audit_logs", "waybill_sign_events",
    "waybill_problem_events", "waybill_sign_verification_state",
)
SPLIT_TABLES = ("waybills", "boyi_waybills", "receipt_records")
NEW_TABLES = (
    "yunda_waybills", "ronghui_waybills", "boyi_waybills",
    "yunda_receipts", "ronghui_receipts", "boyi_receipts",
)


def quote(identifier):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", identifier):
        raise ValueError("invalid database identifier")
    return f"`{identifier}`"


def names(cursor):
    cursor.execute("SELECT DATABASE() AS name")
    runtime = cursor.fetchone()["name"]
    return database_names(runtime)


def database_names(runtime):
    quote(runtime)
    if runtime == "agent_db":
        return runtime, "waybill_db", "finance_db"
    if not (runtime.startswith("test_") or runtime.endswith("_test")):
        raise ValueError("domain split requires agent_db or an explicitly isolated test database")
    # Keep all three schemas inside the caller's test namespace.
    result = runtime, runtime + "_waybill", runtime + "_finance"
    for name in result:
        quote(name)
    return result


def qualified(schema, table):
    return f"{quote(schema)}.{quote(table)}"


def table_type(cursor, schema, table):
    cursor.execute("SELECT TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (schema, table))
    row = cursor.fetchone()
    return row["TABLE_TYPE"] if row else None


def columns(cursor, schema, table):
    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (schema, table))
    return [row["COLUMN_NAME"] for row in cursor.fetchall()]


def archive(table):
    return "_migration_055_" + table


def status(cursor):
    runtime, _, _ = names(cursor)
    if not table_type(cursor, runtime, STATE_TABLE):
        return "pending_clean"
    cursor.execute(f"SELECT state FROM {quote(STATE_TABLE)} WHERE id=1")
    row = cursor.fetchone()
    return row["state"] if row else "pending_clean"


def preflight_permissions(cursor):
    """Verify this installation's direct schema grants before stopping services."""
    schemas = names(cursor)
    cursor.execute("SHOW GRANTS")
    grants = [str(value) for row in cursor.fetchall() for value in row.values()]
    required = {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "CREATE VIEW", "SHOW VIEW", "REFERENCES"}
    for schema in schemas:
        privileges = set()
        for grant in grants:
            match = re.match(r"GRANT (.+?) ON (.+?) TO ", grant)
            # RDS escapes literal underscores in schema-level SHOW GRANTS.
            if match and match[2].replace('`', '').replace('\\_', '_') in {"*.*", schema + ".*"}:
                privileges.update(match[1].split(", "))
        if "ALL PRIVILEGES" not in privileges and not required <= privileges:
            raise RuntimeError(f"database administrator must grant migration/read/write privileges on {schema} before release")


def _backup(cursor, runtime, directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-domain-055.sql.gz")
    # Only business tables are backed up here. Credential/configuration tables
    # are deliberately excluded. No record contents are printed.
    with path.open("xb") as raw:
        path.chmod(0o600)
        with gzip.open(raw, "wt", encoding="utf-8") as output:
            output.write(f"USE {quote(runtime)};\nSET FOREIGN_KEY_CHECKS=0;\n")
            for table in (*SPLIT_TABLES, *LOGISTICS_TABLES, *FINANCE_TABLES):
                cursor.execute(f"SHOW CREATE TABLE {quote(table)}")
                ddl = cursor.fetchone()["Create Table"]
                output.write(ddl + ";\n")
                cursor.execute(f"SELECT * FROM {quote(table)}")
                rows = cursor.fetchall()
                if rows:
                    cols = list(rows[0])
                    insert = f"INSERT INTO {quote(table)} ({','.join(map(quote, cols))}) VALUES ({','.join('%s' for _ in cols)});\n"
                    for row in rows:
                        output.write(cursor.mogrify(insert, [row[col] for col in cols]))
            output.write("SET FOREIGN_KEY_CHECKS=1;\n")
    summary_path = path.with_suffix(".json")
    summary_path.write_text(json.dumps(_metrics(cursor, runtime), default=str, sort_keys=True), encoding="utf-8")
    summary_path.chmod(0o600)
    return path


def _metrics(cursor, runtime):
    result = {}
    for table in (*LOGISTICS_TABLES, *FINANCE_TABLES):
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND DATA_TYPE='decimal' ORDER BY ORDINAL_POSITION", (runtime, table))
        amounts = [row["COLUMN_NAME"] for row in cursor.fetchall()]
        expressions = ["COUNT(*) AS row_count"]
        for col in amounts:
            expressions.extend(f"{fn}({quote(col)}) AS {quote(col + '_' + fn.lower())}" for fn in ("SUM", "MIN", "MAX", "COUNT"))
        cursor.execute(f"SELECT {','.join(expressions)} FROM {qualified(runtime, table)}")
        result[table] = cursor.fetchone()
    return json.loads(json.dumps(result, default=str, sort_keys=True))


def _assert_equal(cursor, left, right, column_names, left_where="1", right_where="1"):
    """Compare full values, including NULL and binary-exact text, in SQL."""
    cursor.execute(f"SELECT COUNT(*) AS n FROM {left} WHERE {left_where}")
    expected = int(cursor.fetchone()["n"])
    cursor.execute(f"SELECT COUNT(*) AS n FROM {right} WHERE {right_where}")
    if int(cursor.fetchone()["n"]) != expected:
        raise RuntimeError("domain split row count mismatch")
    equal = " AND ".join(f"(BINARY a.{quote(col)} <=> BINARY b.{quote(col)})" for col in column_names)
    cursor.execute(f"SELECT COUNT(*) AS n FROM (SELECT * FROM {left} WHERE {left_where}) a LEFT JOIN (SELECT * FROM {right} WHERE {right_where}) b ON a.id=b.id WHERE b.id IS NULL OR NOT ({equal})")
    if int(cursor.fetchone()["n"]):
        raise RuntimeError("domain split record contents mismatch")
    return expected


def _partitions(cursor, runtime, waybill, *, moved):
    def original(table):
        return qualified(waybill, archive(table)) if moved else qualified(runtime, table)
    wb_columns = columns(cursor, waybill if moved else runtime, archive("waybills") if moved else "waybills")
    boyi_columns = columns(cursor, waybill if moved else runtime, archive("boyi_waybills") if moved else "boyi_waybills")
    receipt_columns = columns(cursor, waybill if moved else runtime, archive("receipt_records") if moved else "receipt_records")
    return [
        (original("waybills"), "yunda_waybills", wb_columns, "source='yunda'", "1"),
        (original("waybills"), "ronghui_waybills", wb_columns, "source='ronghui'", "1"),
        (original("waybills"), "boyi_waybills", wb_columns, "source='ocr'", "source='ocr'"),
        (original("boyi_waybills"), "boyi_waybills", boyi_columns, "1", "source='manual'"),
        *[(original("receipt_records"), f"{platform}_receipts", receipt_columns, f"platform='{platform}'", "1")
          for platform in ("yunda", "ronghui", "boyi")],
    ]


def _verify_split(cursor, runtime, waybill, *, moved):
    counts = {}
    for left, table, cols, left_where, right_where in _partitions(cursor, runtime, waybill, moved=moved):
        counts[table + ":" + right_where] = _assert_equal(cursor, left, qualified(waybill, table), cols, left_where, right_where)
    return counts


def _views(cursor, runtime, waybill, finance):
    for schema, tables in ((finance, FINANCE_TABLES), (waybill, (*LOGISTICS_TABLES, *NEW_TABLES, "receipt_sequences"))):
        for table in tables:
            cursor.execute(f"CREATE OR REPLACE ALGORITHM=MERGE SQL SECURITY INVOKER VIEW {qualified(runtime, table)} AS SELECT * FROM {qualified(schema, table)}")
    wb_cols = ','.join(map(quote, columns(cursor, waybill, "ronghui_waybills")))
    cursor.execute(f"CREATE OR REPLACE SQL SECURITY INVOKER VIEW {qualified(runtime, 'waybills')} AS SELECT {wb_cols} FROM {qualified(waybill, 'ronghui_waybills')} UNION ALL SELECT {wb_cols} FROM {qualified(waybill, 'yunda_waybills')} UNION ALL SELECT {wb_cols} FROM {qualified(waybill, 'boyi_waybills')} WHERE source='ocr'")
    cursor.execute(f"CREATE OR REPLACE SQL SECURITY INVOKER VIEW {qualified(runtime, 'receipt_records')} AS " + " UNION ALL ".join(f"SELECT * FROM {qualified(waybill, platform + '_receipts')}" for platform in ("yunda", "ronghui", "boyi")))


def apply(cursor, sql_path, split_sql, backup_directory):
    runtime, waybill, finance = names(cursor)
    current = status(cursor)
    if current == "ACTIVE":
        return
    if current not in {"pending_clean", "PREPARED"}:
        raise RuntimeError("domain split restore is unfinished")
    if current == "pending_clean":
        for schema in (waybill, finance):
            cursor.execute("SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (schema,))
            if cursor.fetchone()["n"]:
                raise RuntimeError("target database is not empty; refusing to adopt existing tables")
        for table in (*SPLIT_TABLES, *LOGISTICS_TABLES, *FINANCE_TABLES):
            if table_type(cursor, runtime, table) != "BASE TABLE":
                raise RuntimeError(f"required source table is missing: {table}")
        cursor.execute("SELECT COUNT(*) AS n FROM waybills WHERE source NOT IN ('yunda','ronghui','ocr') OR source IS NULL")
        if cursor.fetchone()["n"]:
            raise RuntimeError("unsupported historical waybill source")
        cursor.execute("SELECT COUNT(*) AS n FROM boyi_waybills WHERE source<>'manual' OR source IS NULL")
        if cursor.fetchone()["n"]:
            raise RuntimeError("unsupported historical Boyi source")
        cursor.execute("SELECT COUNT(*) AS n FROM receipt_records WHERE platform NOT IN ('yunda','ronghui','boyi')")
        if cursor.fetchone()["n"]:
            raise RuntimeError("unsupported historical receipt platform")
        cursor.execute("SELECT COUNT(*) AS n FROM waybills w JOIN boyi_waybills b ON b.id=w.id WHERE w.source='ocr'")
        if cursor.fetchone()["n"]:
            raise RuntimeError("OCR and Boyi IDs conflict; explicit reconciliation required")
        cursor.execute("SELECT COUNT(*) AS n FROM receipt_attachments a LEFT JOIN receipt_records r ON r.id=a.record_id WHERE r.id IS NULL")
        if cursor.fetchone()["n"]:
            raise RuntimeError("orphan receipt attachment found")
        backup = _backup(cursor, runtime, Path(backup_directory))
        cursor.execute("SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (runtime,))
        collation = cursor.fetchone()["DEFAULT_COLLATION_NAME"]
        quote(collation)
        sql = sql_path.read_text(encoding="utf-8").replace("{{waybill_db}}", quote(waybill)).replace("{{finance_db}}", quote(finance)).replace("{{collation}}", collation)
        for statement in split_sql(sql):
            cursor.execute(statement)
        cursor.execute(f"INSERT INTO {quote(STATE_TABLE)} (id,state,backup_path) VALUES (1,'PREPARED',%s)", (str(backup),))
    cursor.execute(f"CREATE TABLE IF NOT EXISTS {qualified(waybill, 'receipt_sequences')} (sequence_key VARCHAR(64) PRIMARY KEY, current_value BIGINT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    moved = table_type(cursor, waybill, archive("waybills")) == "BASE TABLE"
    if not moved:
        for table in NEW_TABLES:
            source = "receipt_records" if table.endswith("receipts") else "boyi_waybills" if table == "boyi_waybills" else "waybills"
            cursor.execute(f"CREATE TABLE IF NOT EXISTS {qualified(waybill, table)} LIKE {qualified(runtime, source)}")
            constraint = "chk_domain_" + table
            cursor.execute("SELECT COUNT(*) AS n FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s", (waybill, table, constraint))
            if not cursor.fetchone()["n"]:
                platform = table.split('_', 1)[0]
                predicate = "source IN ('manual','ocr')" if table == 'boyi_waybills' else f"{'platform' if table.endswith('receipts') else 'source'}='{platform}'"
                cursor.execute(f"ALTER TABLE {qualified(waybill, table)} ADD CONSTRAINT {quote(constraint)} CHECK ({predicate})")
        cursor.execute("START TRANSACTION")
        try:
            for left, table, cols, where, _ in _partitions(cursor, runtime, waybill, moved=False):
                cols_sql = ','.join(map(quote, cols))
                cursor.execute(f"INSERT INTO {qualified(waybill, table)} ({cols_sql}) SELECT {cols_sql} FROM {left} a WHERE ({where}) AND NOT EXISTS (SELECT 1 FROM {qualified(waybill, table)} b WHERE b.id=a.id)")
            _verify_split(cursor, runtime, waybill, moved=False)
            cursor.execute(f"INSERT INTO {qualified(waybill, 'receipt_sequences')} SELECT 'receipt_record', COALESCE(MAX(id),0) FROM receipt_records ON DUPLICATE KEY UPDATE current_value=VALUES(current_value)")
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise
        renames = [(qualified(runtime, table), qualified(waybill, archive(table))) for table in SPLIT_TABLES]
        renames += [(qualified(runtime, table), qualified(waybill, table)) for table in LOGISTICS_TABLES]
        renames += [(qualified(runtime, table), qualified(finance, table)) for table in FINANCE_TABLES]
        cursor.execute("RENAME TABLE " + ', '.join(f"{old} TO {new}" for old, new in renames))
    counts = _verify_split(cursor, runtime, waybill, moved=True)
    _views(cursor, runtime, waybill, finance)
    cursor.execute(f"SELECT backup_path FROM {quote(STATE_TABLE)} WHERE id=1")
    summary_path = Path(cursor.fetchone()["backup_path"]).with_suffix(".json")
    if _metrics(cursor, runtime) != json.loads(summary_path.read_text(encoding="utf-8")):
        raise RuntimeError("relocated table counts or financial totals differ from the backup")
    cursor.execute(f"UPDATE {quote(STATE_TABLE)} SET state='ACTIVE' WHERE id=1")
    print("domain_database_split_verified=" + json.dumps(counts, sort_keys=True))


def restore(cursor):
    """Before release activation only; refuse if partition contents changed."""
    runtime, waybill, finance = names(cursor)
    if status(cursor) == "pending_clean":
        return
    moved = table_type(cursor, waybill, archive("waybills")) == "BASE TABLE"
    if moved:
        _verify_split(cursor, runtime, waybill, moved=True)
        cursor.execute(f"UPDATE {quote(STATE_TABLE)} SET state='RESTORING' WHERE id=1")
        for table in (*SPLIT_TABLES, *LOGISTICS_TABLES, *FINANCE_TABLES, *NEW_TABLES, "receipt_sequences"):
            if table_type(cursor, runtime, table) == "VIEW":
                cursor.execute(f"DROP VIEW {qualified(runtime, table)}")
        renames = [(qualified(waybill, archive(table)), qualified(runtime, table)) for table in SPLIT_TABLES]
        renames += [(qualified(waybill, table), qualified(runtime, table)) for table in LOGISTICS_TABLES]
        renames += [(qualified(finance, table), qualified(runtime, table)) for table in FINANCE_TABLES]
        cursor.execute("RENAME TABLE " + ', '.join(f"{old} TO {new}" for old, new in renames))
    for table in (*NEW_TABLES, "receipt_sequences"):
        if table_type(cursor, waybill, table) == "BASE TABLE":
            cursor.execute(f"DROP TABLE {qualified(waybill, table)}")
    cursor.execute("DELETE FROM schema_migrations WHERE version='055'")
    cursor.execute(f"DROP TABLE {quote(STATE_TABLE)}")
    # Leave the newly created, now empty schemas and the on-disk backup intact.
    print("domain_database_split_restored=1")


def cli(connect, require_mysql8, *, restore_requested):
    connection = connect()
    try:
        with connection.cursor() as cursor:
            require_mysql8(cursor)
            if restore_requested:
                restore(cursor)
            else:
                current = status(cursor)
                if current == "pending_clean":
                    preflight_permissions(cursor)
                print("domain_database_split_status=" + current)
    finally:
        connection.close()
    return 0
