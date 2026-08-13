"""Run ordered SQL migrations during deployment, never from service requests."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
MIGRATION_NAME_RE = re.compile(r"^(?P<version>\d{3,})_(?P<name>[a-z0-9_]+)\.sql$")
SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(32) PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    checksum CHAR(64) NOT NULL,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
CONTROL_PLANE_TASK_CUTOVER_VERSION = "014"
CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE = "control_plane_task_cutover_backup_014"


def _require_mysql8(cursor) -> str:
    """Fail before migration bookkeeping unless the server is MySQL 8."""

    cursor.execute("SELECT VERSION() AS version")
    row = cursor.fetchone()
    if isinstance(row, dict):
        raw_version = row.get("version") or row.get("VERSION()")
    elif isinstance(row, (list, tuple)) and row:
        raw_version = row[0]
    else:
        raw_version = None

    version = str(raw_version or "").strip()
    if "mariadb" in version.lower():
        raise RuntimeError(f"Migration runner requires MySQL 8; MariaDB is unsupported ({version})")

    match = re.match(r"^(\d+)(?:\.|$)", version)
    if match is None or int(match.group(1)) != 8:
        raise RuntimeError(f"Migration runner requires MySQL 8; found {version or 'unknown'}")
    return version


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    migrations: list[tuple[str, Path]] = []
    for path in migrations_dir.glob("*.sql"):
        match = MIGRATION_NAME_RE.fullmatch(path.name)
        if not match:
            raise RuntimeError(f"Invalid migration filename: {path.name}")
        migrations.append((match.group("version"), path))
    migrations.sort(key=lambda item: item[0])
    versions = [version for version, _ in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Duplicate migration version")
    return migrations


def migration_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def split_sql_statements(text: str) -> list[str]:
    statements: list[str] = []
    fragments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        fragments.append(line)
    for statement in "\n".join(fragments).split(";"):
        normalized = statement.strip()
        if normalized:
            statements.append(normalized)
    return statements


def _connect():
    from dotenv import load_dotenv
    env_file = Path(os.getenv("MIGRATION_ENV_FILE", PROJECT_ROOT / ".env"))
    load_dotenv(env_file)
    import pymysql

    return pymysql.connect(
        host=os.getenv("AGENT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_DB_PORT", "3306")),
        user=os.getenv("AGENT_DB_USER", "agent"),
        password=os.getenv("AGENT_DB_PASS", ""),
        database=os.getenv("AGENT_DB_NAME", "agent_db"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _applied_migrations(cursor) -> dict[str, dict[str, str]]:
    cursor.execute("SELECT version, filename, checksum FROM schema_migrations")
    return {str(row["version"]): row for row in cursor.fetchall()}


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        (table_name,),
    )
    return cursor.fetchone() is not None


def _migration_table_exists(cursor) -> bool:
    return _table_exists(cursor, "schema_migrations")


def _verify_history(cursor, migrations: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    applied = _applied_migrations(cursor)
    pending: list[tuple[str, Path]] = []
    known_versions = {version for version, _ in migrations}
    unexpected = sorted(set(applied) - known_versions)
    if unexpected:
        raise RuntimeError(f"Database contains unknown migration versions: {', '.join(unexpected)}")
    for version, path in migrations:
        expected_checksum = migration_checksum(path)
        applied_row = applied.get(version)
        if applied_row is None:
            pending.append((version, path))
            continue
        if applied_row.get("checksum") != expected_checksum or applied_row.get("filename") != path.name:
            raise RuntimeError(f"Migration history checksum mismatch: {path.name}")
    return pending


def run(*, check_only: bool) -> int:
    migrations = discover_migrations()
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            if check_only:
                pending = _verify_history(cursor, migrations) if _migration_table_exists(cursor) else migrations
                print(f"migration_check=ok pending={len(pending)}")
                return 0
            cursor.execute(SCHEMA_MIGRATIONS_SQL)
            pending = _verify_history(cursor, migrations)
            for version, path in pending:
                for statement in split_sql_statements(path.read_text(encoding="utf-8")):
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) VALUES (%s, %s, %s)",
                    (version, path.name, migration_checksum(path)),
                )
                print(f"migration_applied={path.name}")
    finally:
        connection.close()
    return 0


def restore_control_plane_task_cutover() -> int:
    """Restore scheduler rows when this release attempted migration 014.

    MySQL DDL commits implicitly, so migration 014 can fail after creating and
    filling its backup table but before recording schema history. The release
    script calls this command only when 014 was pending before the release.
    Therefore the backup table, rather than the history row, is the reliable
    indication that task rows may need restoration.
    """

    connection = _connect()
    transaction_started = False
    try:
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            if not _migration_table_exists(cursor):
                print("control_plane_task_cutover_restore=skipped reason=history_missing")
                return 0

            connection.begin()
            transaction_started = True
            if not _table_exists(cursor, CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE):
                connection.rollback()
                transaction_started = False
                print("control_plane_task_cutover_restore=skipped reason=backup_not_created")
                return 0

            cursor.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, name, tool_name, tool_params, cron_expression, enabled,
                    last_run, last_status, last_duration_ms, last_message, created_at
                )
                SELECT
                    id, name, tool_name, tool_params, cron_expression, enabled,
                    last_run, last_status, last_duration_ms, last_message, created_at
                FROM control_plane_task_cutover_backup_014
                WHERE TRUE
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name),
                    tool_name = VALUES(tool_name),
                    tool_params = VALUES(tool_params),
                    cron_expression = VALUES(cron_expression),
                    enabled = VALUES(enabled),
                    last_run = VALUES(last_run),
                    last_status = VALUES(last_status),
                    last_duration_ms = VALUES(last_duration_ms),
                    last_message = VALUES(last_message),
                    created_at = VALUES(created_at)
                """
            )
            cursor.execute(
                "DELETE FROM schema_migrations WHERE version=%s",
                (CONTROL_PLANE_TASK_CUTOVER_VERSION,),
            )
            cursor.execute(f"DELETE FROM {CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE}")
            connection.commit()
            transaction_started = False
            print("control_plane_task_cutover_restore=ok")
    except Exception:
        if transaction_started:
            connection.rollback()
        raise
    finally:
        connection.close()
    return 0


def report_control_plane_task_cutover_status() -> int:
    """Report whether migration 014 is safe to apply, without exposing row data."""

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            applied = False
            if _migration_table_exists(cursor):
                cursor.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s",
                    (CONTROL_PLANE_TASK_CUTOVER_VERSION,),
                )
                applied = cursor.fetchone() is not None
            if applied:
                status = "applied"
            elif _table_exists(cursor, CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE):
                cursor.execute(
                    f"SELECT 1 FROM {CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE} LIMIT 1"
                )
                status = "pending_dirty" if cursor.fetchone() is not None else "pending_clean"
            else:
                status = "pending_clean"
            print(f"control_plane_task_cutover_status={status}")
    finally:
        connection.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Validate migration history without applying changes")
    modes.add_argument(
        "--restore-control-plane-task-cutover",
        action="store_true",
        help="Restore the fixed scheduler rows backed up by migration 014",
    )
    modes.add_argument(
        "--control-plane-task-cutover-status",
        action="store_true",
        help="Report whether migration 014 is pending without returning row data",
    )
    args = parser.parse_args()
    if args.restore_control_plane_task_cutover:
        return restore_control_plane_task_cutover()
    if args.control_plane_task_cutover_status:
        return report_control_plane_task_cutover_status()
    return run(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
