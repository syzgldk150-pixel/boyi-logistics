"""Run ordered SQL migrations during deployment, never from service requests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
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
CONTROL_PLANE_TASK_CUTOVER_VERSION = "014"
CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE = "control_plane_task_cutover_backup_014"
CONTROL_PLANE_REVIEWED_PROFILE_GROUPS = frozenset(
    {
        "arrive_list",
        "daily_sign",
        "delivery_status",
        "send_order",
        "site_send",
        "yunda_send_waybills",
    }
)
CONTROL_PLANE_REVIEWED_TASK_COUNT = 51
CONTROL_PLANE_REVIEWED_CLOCK_IDS = frozenset(
    {"clockin_daxiang_1830", "clockin_daxiang_s_1833"}
)
CONTROL_PLANE_STATIC_SEED_TASK_IDS = frozenset(
    {
        "customer_problems_shadow",
        "finance_bills_0010",
        "yunda_dispatch_forecast_1700",
    }
)
CONTROL_PLANE_REVIEWED_ARRIVE_LOGIN_SITE_SHA256 = (
    "c33492072957c7cc41ad8769d0c790b50d3b5314427e3912609432ea9d320912"
)
CONTROL_PLANE_CLOCK_TOOL_NAMES = frozenset({"tms_query", "clock_in_dual"})
CONTROL_PLANE_TASK_CANDIDATE_SQL = """
SELECT id, tool_name, tool_params, cron_expression, enabled
FROM scheduled_tasks
WHERE id REGEXP '^(arrive_list_|daily_sign_|delivery_status_|send_order_|site_send_|yunda_send_waybills_|clockin_)'
   OR tool_name IN (
       'sync_arrive_list',
       'sync_daily_should_sign',
       'sync_delivery_status',
       'sync_daily_send_orders',
       'sync_site_send_list',
       'sync_yunda_send_waybills',
       'clock_in_dual'
   )
"""
_TIME_SUFFIX_RE = re.compile(r"^(?P<hour>[01]\d|2[0-3])(?P<minute>[0-5]\d)$")
class ControlPlaneTaskCutoverPreflightError(RuntimeError):
    """A safe, value-free reason why scheduler cutover must not proceed."""

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


def _load_control_plane_reviewed_task_contracts() -> dict[str, dict[str, Any]]:
    """Load the staged code-owned scheduler contract without changing sys.path.

    Migration preflight must consume the same reviewed task IDs and canonical
    arguments as runtime policy.  It deliberately does not infer either from
    current database rows.
    """

    contract_path = PROJECT_ROOT.parent / "shared" / "scheduled_task_contracts.py"
    if not contract_path.is_file():
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_MODULE_MISSING")

    module_name = "_boyi_control_plane_scheduled_task_contracts"
    spec = importlib.util.spec_from_file_location(module_name, contract_path)
    if spec is None or spec.loader is None:
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_MODULE_INVALID")
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_MODULE_INVALID") from exc
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    profiles = getattr(module, "APPROVED_SCHEDULED_TASK_PROFILES", None)
    if not isinstance(profiles, Mapping):
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")

    populated_groups = {
        str(group_id)
        for group_id, profile in profiles.items()
        if getattr(profile, "approved_task_ids", frozenset())
    }
    if populated_groups != CONTROL_PLANE_REVIEWED_PROFILE_GROUPS:
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")

    contracts: dict[str, dict[str, Any]] = {}
    for group_id in sorted(CONTROL_PLANE_REVIEWED_PROFILE_GROUPS):
        profile = profiles.get(group_id)
        task_ids = getattr(profile, "approved_task_ids", None)
        arguments = getattr(profile, "approved_arguments", None)
        tool_name = getattr(profile, "tool_name", None)
        operation_type = getattr(profile, "operation_type", None)
        if (
            not isinstance(task_ids, (set, frozenset))
            or not isinstance(arguments, Mapping)
            or type(tool_name) is not str
            or operation_type != "internal_projection_write"
        ):
            raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")
        for task_id in task_ids:
            if type(task_id) is not str or task_id in contracts:
                raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")
            suffix = task_id.rsplit("_", 1)[-1]
            match = _TIME_SUFFIX_RE.fullmatch(suffix)
            if match is None:
                raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")
            hour = int(match.group("hour"))
            minute = int(match.group("minute"))
            contracts[task_id] = {
                "group_id": group_id,
                "tool_name": tool_name,
                "canonical_arguments": dict(arguments),
                "cron_expression": f"{minute} {hour} * * *",
            }

    if len(contracts) != CONTROL_PLANE_REVIEWED_TASK_COUNT:
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")
    return contracts


def _load_control_plane_seed_task_ids() -> tuple[str, ...]:
    """Return the exact code-owned rows a fresh Agent may seed.

    Rollback must never infer deletion authority from database contents.  The
    governed IDs come from the same reviewed runtime contract as preflight;
    the remaining IDs are disabled configuration placeholders declared by the
    Agent seed templates.
    """

    task_ids = set(_load_control_plane_reviewed_task_contracts())
    task_ids.update(CONTROL_PLANE_STATIC_SEED_TASK_IDS)
    expected_count = CONTROL_PLANE_REVIEWED_TASK_COUNT + len(
        CONTROL_PLANE_STATIC_SEED_TASK_IDS
    )
    if len(task_ids) != expected_count:
        raise ControlPlaneTaskCutoverPreflightError("SEED_TASK_SET_INVALID")
    return tuple(sorted(task_ids))


def _strict_json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        return all(_strict_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)


def _decode_task_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ARGUMENTS_INVALID") from exc
    if type(value) is str:
        try:
            payload = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ARGUMENTS_INVALID") from exc
        if isinstance(payload, dict):
            return payload
    raise ControlPlaneTaskCutoverPreflightError("TASK_ARGUMENTS_INVALID")


def _is_legacy_task_arguments(group_id: str, arguments: dict[str, Any]) -> bool:
    if group_id == "arrive_list":
        return (
            set(arguments) == {"account_id", "login_site_code", "site_code", "target_date"}
            and arguments.get("account_id") == "ronghui_default"
            and type(arguments.get("login_site_code")) is str
            and bool(arguments["login_site_code"].strip())
            # ``site_code`` was audited as unconsumed by the legacy
            # sync_arrive_list wrapper.  It is accepted only in this exact
            # legacy shape, is never treated as authority, and is never logged.
            and type(arguments.get("site_code")) is str
            and bool(arguments["site_code"].strip())
            and arguments.get("target_date") == ""
        )
    if group_id == "daily_sign":
        return _strict_json_equal(
            arguments,
            {
                "account_id": "r13_default",
                "detail_account_id": "ronghui_default",
                "r13_account_id": "r13_default",
            },
        )
    if group_id == "delivery_status":
        return arguments == {}
    if group_id == "send_order":
        return _strict_json_equal(
            arguments,
            {"account_id": "price_default", "target_date": ""},
        )
    if group_id == "site_send":
        return _strict_json_equal(arguments, {"account_id": "ronghui_default"})
    if group_id == "yunda_send_waybills":
        return _strict_json_equal(
            arguments,
            {
                "account_id": "yunda_default",
                "ensure_fields": False,
                "session_profile": "yunda",
                "target_date": "",
            },
        )
    return False


def _validate_clock_policy(rows: Sequence[Mapping[str, Any]]) -> None:
    clock_rows = []
    for row in rows:
        task_id = row.get("id")
        if type(task_id) is str and task_id.startswith("clockin_"):
            clock_rows.append(row)
    unknown = [row for row in clock_rows if row.get("id") not in CONTROL_PLANE_REVIEWED_CLOCK_IDS]
    if unknown:
        raise ControlPlaneTaskCutoverPreflightError("CLOCK_TASK_ID_NOT_REVIEWED", count=len(unknown))

    invalid = [
        row
        for row in clock_rows
        if type(row.get("tool_name")) is not str
        or row.get("tool_name") not in CONTROL_PLANE_CLOCK_TOOL_NAMES
        or type(row.get("enabled")) not in {bool, int}
        or row.get("enabled") not in {False, True, 0, 1}
    ]
    if invalid:
        raise ControlPlaneTaskCutoverPreflightError("CLOCK_TASK_SHAPE_NOT_REVIEWED", count=len(invalid))

    enabled = [row for row in clock_rows if bool(row.get("enabled"))]
    if enabled:
        raise ControlPlaneTaskCutoverPreflightError(
            "EXTERNAL_WRITE_SCHEDULE_POLICY_BLOCKED",
            count=len(enabled),
        )


def validate_control_plane_task_cutover(
    rows: Sequence[Mapping[str, Any]],
    *,
    contracts: Mapping[str, Mapping[str, Any]],
    reviewed_login_site_sha256: str = CONTROL_PLANE_REVIEWED_ARRIVE_LOGIN_SITE_SHA256,
) -> dict[str, int]:
    """Validate scheduler rows without returning any persisted values."""

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ControlPlaneTaskCutoverPreflightError("TASK_ROWS_INVALID")
    _validate_clock_policy(rows)
    # A truly empty database has no scheduler state to cut over.  Once any
    # governed-family or external-write candidate exists, the complete 51-row
    # reviewed set becomes mandatory below; partial bootstrap state fails.
    if not rows:
        return {"reviewed_rows": 0, "canonical_rows": 0, "legacy_rows": 0}

    canonical_count = 0
    legacy_count = 0
    reviewed_count = 0
    seen_task_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ControlPlaneTaskCutoverPreflightError("TASK_ROW_INVALID")
        task_id = row.get("id")
        if type(task_id) is not str:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ID_INVALID")
        if task_id.startswith("clockin_"):
            continue
        contract = contracts.get(task_id)
        if not isinstance(contract, Mapping):
            raise ControlPlaneTaskCutoverPreflightError("TASK_ID_NOT_REVIEWED")
        if task_id in seen_task_ids:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ID_DUPLICATE")
        seen_task_ids.add(task_id)

        tool_name = row.get("tool_name")
        cron_expression = row.get("cron_expression")
        enabled = row.get("enabled")
        if type(tool_name) is not str or tool_name != contract.get("tool_name"):
            raise ControlPlaneTaskCutoverPreflightError("TASK_TOOL_NOT_REVIEWED")
        if type(cron_expression) is not str or cron_expression != contract.get("cron_expression"):
            raise ControlPlaneTaskCutoverPreflightError("TASK_CRON_NOT_REVIEWED")
        if type(enabled) not in {bool, int} or enabled not in {False, True, 0, 1}:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ENABLED_TYPE_INVALID")
        if not bool(enabled):
            raise ControlPlaneTaskCutoverPreflightError("PROTECTED_TASK_DISABLED")

        arguments = _decode_task_arguments(row.get("tool_params"))
        canonical_arguments = contract.get("canonical_arguments")
        if not isinstance(canonical_arguments, Mapping):
            raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")
        if _strict_json_equal(arguments, dict(canonical_arguments)):
            canonical_count += 1
        elif _is_legacy_task_arguments(str(contract.get("group_id") or ""), arguments):
            if contract.get("group_id") == "arrive_list":
                login_site_sha256 = hashlib.sha256(
                    arguments["login_site_code"].strip().encode("utf-8")
                ).hexdigest()
                if login_site_sha256 != reviewed_login_site_sha256:
                    raise ControlPlaneTaskCutoverPreflightError(
                        "ARRIVE_LOGIN_SITE_FINGERPRINT_MISMATCH"
                    )
            legacy_count += 1
        else:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ARGUMENTS_NOT_REVIEWED")
        reviewed_count += 1

    missing_count = len(set(contracts) - seen_task_ids)
    if missing_count:
        raise ControlPlaneTaskCutoverPreflightError(
            "REVIEWED_TASK_SET_INCOMPLETE",
            count=missing_count,
        )

    return {
        "reviewed_rows": reviewed_count,
        "canonical_rows": canonical_count,
        "legacy_rows": legacy_count,
    }


def preflight_control_plane_task_cutover() -> int:
    """Run a read-only, value-redacted scheduler cutover check.

    The reviewed login-site binding is compared by a code-owned SHA-256
    fingerprint.  Deployment never opens persisted cookies or credential
    files merely to validate scheduler configuration.
    """

    connection = None
    try:
        contracts = _load_control_plane_reviewed_task_contracts()
        connection = _connect()
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            history_exists = _migration_table_exists(cursor)
            already_applied = False
            history_has_rows = False
            if history_exists:
                cursor.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s",
                    (CONTROL_PLANE_TASK_CUTOVER_VERSION,),
                )
                already_applied = cursor.fetchone() is not None
                if not already_applied:
                    cursor.execute("SELECT 1 FROM schema_migrations LIMIT 1")
                    history_has_rows = cursor.fetchone() is not None

            if already_applied:
                summary = {"reviewed_rows": 0, "canonical_rows": 0, "legacy_rows": 0}
            elif not _table_exists(cursor, "scheduled_tasks"):
                if history_has_rows:
                    raise ControlPlaneTaskCutoverPreflightError(
                        "SCHEDULED_TASKS_TABLE_MISSING"
                    )
                summary = {"reviewed_rows": 0, "canonical_rows": 0, "legacy_rows": 0}
            else:
                cursor.execute(CONTROL_PLANE_TASK_CANDIDATE_SQL)
                rows = cursor.fetchall()
                summary = validate_control_plane_task_cutover(
                    rows,
                    contracts=contracts,
                )
    except ControlPlaneTaskCutoverPreflightError as exc:
        print(
            "control_plane_task_cutover_preflight=blocked "
            f"reason={exc.code} count={exc.count}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "control_plane_task_cutover_preflight=blocked "
            "reason=PREFLIGHT_RUNTIME_ERROR count=1",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            connection.close()

    print(
        "control_plane_task_cutover_preflight=ok "
        f"reviewed_rows={summary['reviewed_rows']} "
        f"canonical_rows={summary['canonical_rows']} "
        f"legacy_rows={summary['legacy_rows']}"
    )
    return 0


def restore_control_plane_task_cutover() -> int:
    """Restore scheduler rows when this release attempted migration 014.

    MySQL DDL commits implicitly, so migration 014 can fail after creating and
    filling its backup table but before recording schema history. The release
    script calls this command only when 014 was pending before the release.
    Therefore the backup table, rather than the history row, is the reliable
    indication that task rows may need restoration.
    """

    seed_task_ids = _load_control_plane_seed_task_ids()
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
            seed_placeholders = ", ".join("%s" for _ in seed_task_ids)
            cursor.execute(
                f"""
                DELETE seeded
                FROM scheduled_tasks AS seeded
                INNER JOIN schema_migrations AS migration
                    ON migration.version=%s
                LEFT JOIN {CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE} AS backup
                    ON backup.id=seeded.id
                WHERE seeded.id IN ({seed_placeholders})
                  AND seeded.created_at >= migration.applied_at
                  AND backup.id IS NULL
                """,
                (CONTROL_PLANE_TASK_CUTOVER_VERSION, *seed_task_ids),
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
    modes.add_argument(
        "--preflight-control-plane-task-cutover",
        action="store_true",
        help="Read-only validation of reviewed scheduler rows before release mutation",
    )
    args = parser.parse_args()
    if args.restore_control_plane_task_cutover:
        return restore_control_plane_task_cutover()
    if args.control_plane_task_cutover_status:
        return report_control_plane_task_cutover_status()
    if args.preflight_control_plane_task_cutover:
        return preflight_control_plane_task_cutover()
    return run(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
