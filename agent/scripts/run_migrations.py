"""Run ordered SQL migrations during deployment, never from service requests.

Besides applying ``agent/migrations/*.sql`` this runner only offers the
read-only release checks the deploy script needs: the scheduled plugin write
window and the running protected-write quiesce check. Executed migration files
keep their exact bytes; the history is verified by checksum.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

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


class ProtectedWriteQuiesceError(RuntimeError):
    """A safe, value-free reason why the release must not stop services yet."""

    def __init__(self, code: str, *, count: int = 1) -> None:
        super().__init__(code)
        self.code = str(code)
        self.count = max(int(count), 1)


def _require_mysql8(cursor) -> str:
    """Fail before bookkeeping unless MySQL enforces the required CHECK guards."""

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

    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\D|$)", version)
    supported = False
    if match is not None and int(match.group(1)) == 8:
        minor = int(match.group(2))
        patch = int(match.group(3))
        supported = minor > 0 or patch >= 16
    if not supported:
        raise RuntimeError(
            "Migration runner requires MySQL 8.0.16 or newer; "
            f"found {version or 'unknown'}"
        )
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
    return _load_script_helper("migration_connection.py").connect(PROJECT_ROOT)

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


def _load_business_module_migration_contract() -> Any:
    """Load the adjacent 027 contract without adding scripts to ``sys.path``."""

    contract_path = Path(__file__).with_name("business_module_migration_contract.py")
    spec = importlib.util.spec_from_file_location(
        "_boyi_business_module_migration_contract", contract_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Business module lifecycle migration contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if (
        str(getattr(module, "BUSINESS_MODULE_LIFECYCLE_VERSION", "")) != "027"
        or not callable(getattr(module, "apply_business_module_lifecycle_migration", None))
    ):
        raise RuntimeError("Business module lifecycle migration contract is invalid")
    return module


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
            business_module_contract = (
                _load_business_module_migration_contract()
                if any(version == "027" for version, _path in pending)
                else None
            )
            for version, path in pending:
                if version >= "055":  # 055 split the entity databases; later SQL targets them.
                    env_path = Path(os.getenv("MIGRATION_ENV_FILE", PROJECT_ROOT / ".env"))
                    backup_directory = Path(os.getenv("MIGRATION_BACKUP_DIRECTORY", str(env_path.parent / "runtime" / "migration-backups")))
                    _load_domain_database_migration().apply_version(cursor, version, path, split_sql_statements, backup_directory)
                elif version == "027":
                    assert business_module_contract is not None
                    business_module_contract.apply_business_module_lifecycle_migration(
                        cursor,
                        path,
                        split_sql_statements,
                    )
                else:
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


def _load_domain_database_migration():
    path = Path(__file__).with_name("migration_055_domain_databases.py")
    spec = importlib.util.spec_from_file_location("_boyi_domain_database_migration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("domain database migration module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_running_protected_writes() -> int:
    """Block quiesce while a protected write is running or verifying."""

    connection = None
    try:
        connection = _connect()
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            if not _table_exists(cursor, "agent_run_steps"):
                running_count = 0
            else:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS running_count
                    FROM agent_run_steps
                    WHERE status IN ('RUNNING', 'VERIFYING')
                      AND operation_type IN (
                          'EXTERNAL_WRITE', 'FINANCIAL_WRITE', 'DESTRUCTIVE'
                      )
                    """
                )
                row = cursor.fetchone()
                running_count = int((row or {}).get("running_count") or 0)
            if _table_exists(cursor, "automation_plugin_invocations"):
                cursor.execute(
                    "SELECT COUNT(*) AS running_count FROM automation_plugin_invocations "
                    "WHERE status IN ('STARTING','RUNNING','CANCELLING')"
                )
                running_count += int((cursor.fetchone() or {}).get("running_count") or 0)
            if running_count:
                raise ProtectedWriteQuiesceError(
                    "PROTECTED_WRITE_RUNNING",
                    count=running_count,
                )
    except ProtectedWriteQuiesceError as exc:
        print(
            "protected_write_quiesce=blocked "
            f"reason={exc.code} count={exc.count}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "protected_write_quiesce=blocked reason=QUIESCE_RUNTIME_ERROR count=1",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            connection.close()

    print("protected_write_quiesce=ok running_writes=0")
    return 0


def _load_script_helper(filename: str):
    path = Path(__file__).with_name(filename)
    module_name = f"boyi_agent_scripts_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script helper: {filename}")
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    previous_shared = {
        name: loaded
        for name, loaded in sys.modules.items()
        if name == "shared" or name.startswith("shared.")
    }
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
        for name in tuple(sys.modules):
            if name == "shared" or name.startswith("shared."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_shared)
    return module


_SCHEDULED_WRITE_WINDOW = _load_script_helper("scheduled_write_window.py")


def check_scheduled_write_window(
    *,
    before_minutes: int = _SCHEDULED_WRITE_WINDOW.SCHEDULED_WRITE_WINDOW_BEFORE_MINUTES,
    after_minutes: int = _SCHEDULED_WRITE_WINDOW.SCHEDULED_WRITE_WINDOW_AFTER_MINUTES,
    now=None,
) -> int:
    """Block release mutation near an enabled scheduled plugin write."""

    return _SCHEDULED_WRITE_WINDOW.check_scheduled_write_window(
        _connect,
        before_minutes=before_minutes,
        after_minutes=after_minutes,
        now=now,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Validate migration history without applying changes")
    modes.add_argument("--domain-database-split-status", action="store_true", help="Report migration 055 state")
    modes.add_argument("--restore-domain-database-split", action="store_true", help="Restore migration 055 before activation; reject changed data")
    modes.add_argument(
        "--check-scheduled-write-window",
        action="store_true",
        help="Block release mutation near an enabled scheduled plugin write",
    )
    modes.add_argument(
        "--check-running-protected-writes",
        action="store_true",
        help="Block quiesce while an external or financial write is running",
    )
    parser.add_argument(
        "--scheduled-write-window-before-minutes",
        type=int,
        default=_SCHEDULED_WRITE_WINDOW.SCHEDULED_WRITE_WINDOW_BEFORE_MINUTES,
    )
    parser.add_argument(
        "--scheduled-write-window-after-minutes",
        type=int,
        default=_SCHEDULED_WRITE_WINDOW.SCHEDULED_WRITE_WINDOW_AFTER_MINUTES,
    )
    args = parser.parse_args()
    if args.domain_database_split_status or args.restore_domain_database_split:
        return _load_domain_database_migration().cli(_connect, _require_mysql8, restore_requested=args.restore_domain_database_split)
    if args.check_scheduled_write_window:
        return check_scheduled_write_window(
            before_minutes=args.scheduled_write_window_before_minutes,
            after_minutes=args.scheduled_write_window_after_minutes,
        )
    if args.check_running_protected_writes:
        return check_running_protected_writes()
    return run(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
