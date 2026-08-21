"""Run ordered SQL migrations during deployment, never from service requests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
CONTROL_PLANE_TASK_CUTOVER_CREATED_TABLE = "control_plane_task_cutover_created_014"
SCHEDULED_TASK_CONTRACT_UPGRADE_VERSION = "017"
SCHEDULED_TASK_CONTRACT_UPGRADE_BACKUP_TABLE = "scheduled_task_contract_upgrade_backup_017"
SCHEDULED_TASK_CONTRACT_UPGRADE_CREATED_TABLE = "scheduled_task_contract_upgrade_created_017"
AUTOMATION_PROJECT_AUTHORIZATION_VERSION = "018"
AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE = "scheduled_task_automation_identity_backup_018"
AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE = "automation_project_migration_capture_018"
AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_MAP_TABLE = "automation_project_reviewed_schedule_map_018"
AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_RESOURCE_MAP_TABLE = "automation_project_reviewed_resource_map_018"
AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE = "automation_project_resource_backup_018"
AUTOMATION_PROJECT_AUTHORIZATION_TABLES_REVERSE = (
    "automation_plugin_purge_journal",
    "automation_worker_cleanup_directives",
    "automation_worker_job_messages",
    "automation_worker_jobs",
    "automation_worker_pairing_events",
    "automation_worker_devices",
    "automation_project_generation_leases",
    "automation_project_generation_effects",
    "automation_project_generation_coeffects",
    "automation_project_generations",
    "automation_project_approval_batches",
    "automation_project_policy_events",
    "automation_project_policies",
    "automation_project_events",
    "automation_plugin_package_events",
    "automation_project_configs",
    "automation_projects",
    "automation_plugin_versions",
    "automation_plugin_packages",
    AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_RESOURCE_MAP_TABLE,
    AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_MAP_TABLE,
    # The validated items and restore-in-progress marker are the durable
    # witnesses for a restart after MySQL DDL auto-commit.  Delete them last,
    # in this order, so an interruption can never leave unverifiable items.
    "automation_project_bootstrap_items_018",
    "automation_project_bootstrap_marker_018",
)
AUTOMATION_PROJECT_AUTHORIZATION_AGENT_COMMAND_INDEX = (
    "idx_agent_commands_automation_requested"
)
AUTOMATION_PROJECT_AUTHORIZATION_SCHEDULE_INDEX = (
    "idx_scheduled_tasks_automation"
)
DAILY_SIGN_SINGLE_TMS_VERSION = "016"
DAILY_SIGN_SINGLE_TMS_BACKUP_TABLE = "daily_sign_single_tms_backup_016"
SCHEDULED_TASK_APPROVAL_POLICY_TABLE = "scheduled_task_approval_policies"
SCHEDULED_TASK_APPROVAL_EVENT_TABLE = "scheduled_task_approval_policy_events"
FINANCE_STARTUP_CATCHUP_TASK_ID = "finance_startup_catchup"
FINANCE_STARTUP_CATCHUP_NAME = "财务启动缺口扫描"
FINANCE_STARTUP_CATCHUP_TOOL_NAME = "sync_finance_bills"
FINANCE_STARTUP_CATCHUP_CRON = "@startup"
CONTROL_PLANE_MIGRATION_ACTOR_ID = "system:migration:control-plane-v1"
CONTROL_PLANE_MIGRATION_ACTOR_ROLE = "migration_authority"
CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID = (
    "__control_plane_v1_bootstrap_complete__"
)
CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "boyi:control-plane-v1:bootstrap-complete")
)
CONTROL_PLANE_REVIEWED_MANIFEST_COUNT = 69
CONTROL_PLANE_REVIEWED_ENABLED_COUNT = 67
CONTROL_PLANE_REVIEWED_DISABLED_IDS = frozenset(
    {"finance_bills_0010", "yunda_dispatch_forecast_1700"}
)
SCHEDULED_WRITE_TIMEZONE = ZoneInfo("Asia/Shanghai")
SCHEDULED_WRITE_WINDOW_BEFORE_MINUTES = 60
SCHEDULED_WRITE_WINDOW_AFTER_MINUTES = 45
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
CONTROL_PLANE_OPTIONAL_PROFILE_GROUPS = frozenset(
    {"finance_bills", "finance_startup_catchup", "yunda_dispatch_forecast"}
)
CONTROL_PLANE_OPTIONAL_TASK_IDS = frozenset(
    {"finance_bills_0010", "finance_startup_catchup", "yunda_dispatch_forecast_1700"}
)
CONTROL_PLANE_REVIEWED_CLOCK_IDS = frozenset(
    {"clockin_daxiang_1830", "clockin_daxiang_s_1833"}
)
CONTROL_PLANE_REVIEWED_CLOCK_PROFILE_GROUPS = frozenset(
    {"clockin_daxiang", "clockin_daxiang_s"}
)
CONTROL_PLANE_REVIEWED_EXTERNAL_PROFILE_GROUPS = frozenset(
    {*CONTROL_PLANE_REVIEWED_CLOCK_PROFILE_GROUPS, "r7_arrival_checkin"}
)
CONTROL_PLANE_REVIEWED_R7_IDS = frozenset(
    {
        "r7_arrival_checkin_0900",
        "r7_arrival_checkin_0930",
        "r7_arrival_checkin_1000",
        "r7_arrival_checkin_1030",
        "r7_arrival_checkin_1100",
        "r7_arrival_checkin_1130",
        "r7_arrival_checkin_1200",
        "r7_arrival_checkin_1230",
        "r7_arrival_checkin_1300",
        "r7_arrival_checkin_1330",
        "r7_arrival_checkin_1400",
        "r7_arrival_checkin_1430",
        "r7_arrival_checkin_1900",
    }
)
CONTROL_PLANE_REVIEWED_CLOCK_CRONS = {
    "clockin_daxiang_1830": "30 18 * * *",
    "clockin_daxiang_s_1833": "33 18 * * *",
}
CONTROL_PLANE_STATIC_SEED_TASK_IDS = frozenset(
    {
        "customer_problems_shadow",
        "finance_bills_0010",
        "finance_startup_catchup",
        "yunda_dispatch_forecast_1700",
    }
)
CONTROL_PLANE_REVIEWED_ARRIVE_LOGIN_SITE_SHA256 = (
    "c33492072957c7cc41ad8769d0c790b50d3b5314427e3912609432ea9d320912"
)
CONTROL_PLANE_REVIEWED_ARRIVE_SITE_SHA256 = (
    "5ff8d6c00584886090be588977393764370cbcac7f7d983a2f0b330c5f37b135"
)
CONTROL_PLANE_APPLIED_014_YUNDA_SEND_ID = "yunda_send_waybills_2355"
CONTROL_PLANE_APPLIED_014_YUNDA_DISABLED_MESSAGE_SHA256 = (
    "19129e9c68d5e20050a7d8c8e8489f4f1313f9fb6188adc55229aaeacad9c0e3"
)
CONTROL_PLANE_CLOCK_TOOL_NAMES = frozenset({"tms_query", "clock_in_dual"})
PROJECT_SCHEDULED_WRITE_OPERATION_TYPES = frozenset(
    {"external_write", "financial_write", "destructive"}
)
CONTROL_PLANE_TASK_CANDIDATE_SQL = """
SELECT id, tool_name, tool_params, cron_expression, enabled
FROM scheduled_tasks
WHERE id REGEXP '^(arrive_list_|daily_sign_|delivery_status_|send_order_|site_send_|yunda_send_waybills_|finance_bills_|finance_startup_catchup$|yunda_dispatch_forecast_|clockin_|r7_arrival_checkin_)'
   OR tool_name IN (
       'sync_arrive_list',
       'sync_daily_should_sign',
       'sync_delivery_status',
       'sync_daily_send_orders',
       'sync_site_send_list',
       'sync_yunda_send_waybills',
       'sync_finance_bills',
       'sync_yunda_dispatch_forecast',
       'r7_arrival_checkin',
       'clock_in_dual'
   )
   OR (
       tool_name = 'tms_query'
       AND COALESCE(
           JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.endpoint')),
           JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.params.endpoint')),
           ''
       ) = '/clock_in_dual'
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


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table_name, column_name),
    )
    return cursor.fetchone() is not None


def _index_exists(cursor, table_name: str, index_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
        """,
        (table_name, index_name),
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


def _load_control_plane_scheduled_task_contract_module() -> Any:
    """Load staged scheduler contracts, including dynamic schema witnesses."""

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
    arguments_for_schema_validation = getattr(
        module, "_arguments_for_schema_validation", None
    )
    if not isinstance(profiles, Mapping) or not callable(
        arguments_for_schema_validation
    ):
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")
    return module


def _load_control_plane_scheduled_task_profiles() -> Mapping[str, Any]:
    module = _load_control_plane_scheduled_task_contract_module()
    return module.APPROVED_SCHEDULED_TASK_PROFILES


def _load_control_plane_tool_registry() -> Any:
    """Load the staged, fully validated registry without changing sys.path."""

    module_path = PROJECT_ROOT / "agent" / "tool_registry.py"
    registry_path = PROJECT_ROOT / "tools" / "registry.yaml"
    module_name = "_boyi_control_plane_tool_registry"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ControlPlaneTaskCutoverPreflightError("TOOL_REGISTRY_MODULE_INVALID")
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        registry = module.ToolRegistry(
            registry_path=registry_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as exc:
        raise ControlPlaneTaskCutoverPreflightError("TOOL_REGISTRY_INVALID") from exc
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
    return registry


def _load_scheduled_task_approval_contract_module() -> Any:
    module_path = PROJECT_ROOT.parent / "shared" / "scheduled_task_approval.py"
    module_name = "_boyi_control_plane_scheduled_task_approval"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ControlPlaneTaskCutoverPreflightError(
            "SCHEDULED_APPROVAL_MODULE_INVALID"
        )
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ControlPlaneTaskCutoverPreflightError(
            "SCHEDULED_APPROVAL_MODULE_INVALID"
        ) from exc
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
    return module


def _load_control_plane_reviewed_task_contracts() -> dict[str, dict[str, Any]]:
    """Return only the 51 reviewed internal-projection task contracts."""

    profiles = _load_control_plane_scheduled_task_profiles()
    populated_groups = {
        str(group_id)
        for group_id, profile in profiles.items()
        if getattr(profile, "approved_task_ids", frozenset())
        and getattr(profile, "operation_type", None) == "internal_projection_write"
        and getattr(profile, "seed_governed_template", True)
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


def _load_control_plane_optional_task_contracts() -> dict[str, dict[str, Any]]:
    """Return the three optional c7 internal schedules reviewed in place."""

    profiles = _load_control_plane_scheduled_task_profiles()
    populated_groups = {
        str(group_id)
        for group_id, profile in profiles.items()
        if getattr(profile, "approved_task_ids", frozenset())
        and getattr(profile, "operation_type", None) == "internal_projection_write"
        and not getattr(profile, "seed_governed_template", True)
    }
    if populated_groups != CONTROL_PLANE_OPTIONAL_PROFILE_GROUPS:
        raise ControlPlaneTaskCutoverPreflightError("OPTIONAL_CONTRACT_SET_INVALID")

    contracts: dict[str, dict[str, Any]] = {}
    for group_id in sorted(CONTROL_PLANE_OPTIONAL_PROFILE_GROUPS):
        profile = profiles.get(group_id)
        task_ids = getattr(profile, "approved_task_ids", None)
        arguments = getattr(profile, "approved_arguments", None)
        tool_name = getattr(profile, "tool_name", None)
        dynamic_rules = getattr(profile, "dynamic_argument_rules", None)
        if (
            not isinstance(task_ids, (set, frozenset))
            or len(task_ids) != 1
            or not isinstance(arguments, Mapping)
            or type(tool_name) is not str
            or not isinstance(dynamic_rules, Mapping)
            or getattr(profile, "operation_type", None) != "internal_projection_write"
        ):
            raise ControlPlaneTaskCutoverPreflightError("OPTIONAL_CONTRACT_SET_INVALID")
        task_id = next(iter(task_ids))
        if (
            type(task_id) is not str
            or task_id not in CONTROL_PLANE_OPTIONAL_TASK_IDS
            or task_id in contracts
        ):
            raise ControlPlaneTaskCutoverPreflightError("OPTIONAL_CONTRACT_SET_INVALID")
        profile_cron = getattr(profile, "cron_expression", None)
        if profile_cron is not None:
            if type(profile_cron) is not str or profile_cron != "@startup":
                raise ControlPlaneTaskCutoverPreflightError("OPTIONAL_CONTRACT_SET_INVALID")
            cron_expression = profile_cron
        else:
            suffix = task_id.rsplit("_", 1)[-1]
            match = _TIME_SUFFIX_RE.fullmatch(suffix)
            if match is None:
                raise ControlPlaneTaskCutoverPreflightError("OPTIONAL_CONTRACT_SET_INVALID")
            hour = int(match.group("hour"))
            minute = int(match.group("minute"))
            cron_expression = f"{minute} {hour} * * *"
        contracts[task_id] = {
            "group_id": group_id,
            "tool_name": tool_name,
            "canonical_arguments": dict(arguments),
            "dynamic_argument_rules": dict(dynamic_rules),
            "cron_expression": cron_expression,
        }

    if set(contracts) != CONTROL_PLANE_OPTIONAL_TASK_IDS:
        raise ControlPlaneTaskCutoverPreflightError("OPTIONAL_CONTRACT_SET_INVALID")
    return contracts


def _load_control_plane_clock_contracts() -> dict[str, dict[str, Any]]:
    """Return the exact optional pair of reviewed external clock schedules."""

    profiles = _load_control_plane_scheduled_task_profiles()
    populated_groups = {
        str(group_id)
        for group_id, profile in profiles.items()
        if getattr(profile, "approved_task_ids", frozenset())
        and getattr(profile, "operation_type", None) == "external_write"
    }
    if populated_groups != CONTROL_PLANE_REVIEWED_EXTERNAL_PROFILE_GROUPS:
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CLOCK_CONTRACT_SET_INVALID")

    contracts: dict[str, dict[str, Any]] = {}
    expected_argument_keys = {
        "account_id",
        "sitecode",
        "sitefbcode",
        "sitename",
        "sitefbname",
        "first_type",
        "second_type",
        "delay_seconds",
    }
    for group_id in sorted(CONTROL_PLANE_REVIEWED_CLOCK_PROFILE_GROUPS):
        profile = profiles.get(group_id)
        task_ids = getattr(profile, "approved_task_ids", None)
        arguments = getattr(profile, "approved_arguments", None)
        tool_name = getattr(profile, "tool_name", None)
        tool_version = getattr(profile, "tool_version", None)
        dynamic_rules = getattr(profile, "dynamic_argument_rules", None)
        if (
            not isinstance(task_ids, (set, frozenset))
            or len(task_ids) != 1
            or not isinstance(arguments, Mapping)
            or set(arguments) != expected_argument_keys
            or type(tool_name) is not str
            or tool_name != "clock_in_dual"
            or type(tool_version) is not str
            or tool_version != "1.1.0"
            or not isinstance(dynamic_rules, Mapping)
            or bool(dynamic_rules)
            or getattr(profile, "operation_type", None) != "external_write"
        ):
            raise ControlPlaneTaskCutoverPreflightError(
                "REVIEWED_CLOCK_CONTRACT_SET_INVALID"
            )
        task_id = next(iter(task_ids))
        if (
            type(task_id) is not str
            or task_id not in CONTROL_PLANE_REVIEWED_CLOCK_IDS
            or task_id in contracts
            or type(arguments.get("account_id")) is not str
            or not arguments["account_id"].strip()
            or type(arguments.get("sitecode")) is not str
            or not arguments["sitecode"].strip()
            or type(arguments.get("sitefbcode")) is not str
            or not arguments["sitefbcode"].strip()
            or type(arguments.get("sitename")) is not str
            or not arguments["sitename"].strip()
            or type(arguments.get("sitefbname")) is not str
            or not arguments["sitefbname"].strip()
            or type(arguments.get("first_type")) is not str
            or not arguments["first_type"].strip()
            or type(arguments.get("second_type")) is not str
            or not arguments["second_type"].strip()
            or type(arguments.get("delay_seconds")) is not int
            or arguments["delay_seconds"] < 0
        ):
            raise ControlPlaneTaskCutoverPreflightError(
                "REVIEWED_CLOCK_CONTRACT_SET_INVALID"
            )
        contracts[task_id] = {
            "group_id": group_id,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "canonical_arguments": dict(arguments),
            "cron_expression": CONTROL_PLANE_REVIEWED_CLOCK_CRONS[task_id],
        }

    if set(contracts) != CONTROL_PLANE_REVIEWED_CLOCK_IDS:
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CLOCK_CONTRACT_SET_INVALID")
    return contracts


def _load_control_plane_r7_contracts() -> dict[str, dict[str, Any]]:
    """Return only the exact 13 production R7 poll schedules."""

    profiles = _load_control_plane_scheduled_task_profiles()
    profile = profiles.get("r7_arrival_checkin")
    task_ids = getattr(profile, "approved_task_ids", None)
    arguments = getattr(profile, "approved_arguments", None)
    if (
        not isinstance(task_ids, (set, frozenset))
        or set(task_ids) != CONTROL_PLANE_REVIEWED_R7_IDS
        or not isinstance(arguments, Mapping)
        or getattr(profile, "tool_name", None) != "r7_arrival_checkin"
        or getattr(profile, "tool_version", None) != "1.0.0"
        or getattr(profile, "operation_type", None) != "external_write"
    ):
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_R7_CONTRACT_SET_INVALID")
    contracts: dict[str, dict[str, Any]] = {}
    for task_id in task_ids:
        match = _TIME_SUFFIX_RE.fullmatch(task_id.rsplit("_", 1)[-1])
        if match is None:
            raise ControlPlaneTaskCutoverPreflightError("REVIEWED_R7_CONTRACT_SET_INVALID")
        contracts[task_id] = {
            "group_id": "r7_arrival_checkin",
            "tool_name": "r7_arrival_checkin",
            "canonical_arguments": dict(arguments),
            "cron_expression": f"{int(match.group('minute'))} {int(match.group('hour'))} * * *",
        }
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
        legacy_shape = (
            set(arguments)
            == {"account_id", "login_site_code", "site_code", "target_date"}
            and type(arguments.get("login_site_code")) is str
            and bool(arguments["login_site_code"].strip())
            and arguments.get("target_date") == ""
        )
        applied_014_shape = set(arguments) == {"account_id", "site_code"}
        return (
            arguments.get("account_id") == "ronghui_default"
            and (legacy_shape or applied_014_shape)
            # ``site_code`` was audited as unconsumed by the arrive-list
            # wrapper.  Both accepted shapes are fingerprint-bound below;
            # migration 017 removes the applied-014 residue transactionally.
            and type(arguments.get("site_code")) is str
            and bool(arguments["site_code"].strip())
        )
    if group_id == "daily_sign":
        return any(
            _strict_json_equal(arguments, legacy_arguments)
            for legacy_arguments in (
                {
                    "account_id": "r13_default",
                    "detail_account_id": "ronghui_default",
                    "r13_account_id": "r13_default",
                },
                {
                    "r13_account_id": "r13_default",
                    "problem_account_id": "ronghui_daxiang_s",
                    "sign_account_id": "ronghui_daxiang_s",
                    "detail_account_id": "ronghui_default",
                    "days": 7,
                },
                {
                    "r13_account_id": "r13_default",
                    "problem_account_id": "ronghui_daxiang_s",
                    "sign_account_id": "ronghui_daxiang_s",
                    "detail_account_id": "ronghui_daxiang_s",
                    "days": 7,
                },
                {
                    "account_id": "r13_default",
                    "r13_account_id": "r13_default",
                    "problem_account_id": "ronghui_daxiang_s",
                    "sign_account_id": "ronghui_daxiang_s",
                    "detail_account_id": "ronghui_default",
                    "days": 7,
                },
            )
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
    if group_id == "finance_bills":
        return any(
            _strict_json_equal(arguments, legacy_arguments)
            for legacy_arguments in (
                {"mode": "sync", "rescan_days": 7},
                {
                    "account_id": "ronghui_default",
                    "mode": "sync",
                    "platform": "ronghui",
                    "rescan_days": 7,
                },
            )
        )
    if group_id == "yunda_dispatch_forecast":
        return _strict_json_equal(
            arguments,
            {"session_profile": "yunda", "dest_brch": "56739382"},
        )
    return False


def _legacy_clock_arguments(
    task_id: str,
    canonical_arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize the exact c7 nested scheduler shape for one clock row."""

    inner_arguments = {
        "mode": "api",
        "site_name": canonical_arguments["sitename"],
        "site_fb_name": canonical_arguments["sitefbname"],
        "first_type": canonical_arguments["first_type"],
        "second_type": canonical_arguments["second_type"],
        "delay_seconds": canonical_arguments["delay_seconds"],
    }
    if task_id == "clockin_daxiang_s_1833":
        inner_arguments["sitecode"] = canonical_arguments["sitecode"]
        inner_arguments["sitefbcode"] = canonical_arguments["sitefbcode"]
    return {
        "endpoint": "/clock_in_dual",
        "params": {
            "timeout_sec": 600,
            "params": inner_arguments,
        },
    }


def _applied_014_clock_arguments(
    task_id: str,
    canonical_arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact production shape left by the already-applied 014.

    This is a release-preflight compatibility contract only. Migration 017
    converts either shape to the current direct ``clock_in_dual`` contract.
    """

    if task_id == "clockin_daxiang_1830":
        return {
            "account_id": canonical_arguments["account_id"],
            "delay_seconds": canonical_arguments["delay_seconds"],
            "first_type": canonical_arguments["first_type"],
            "second_type": canonical_arguments["second_type"],
            "sitefbname": canonical_arguments["sitefbname"],
            "sitename": canonical_arguments["sitename"],
            "timeout_sec": 600,
        }
    if task_id == "clockin_daxiang_s_1833":
        arguments = _legacy_clock_arguments(task_id, canonical_arguments)
        return {
            "account_id": canonical_arguments["account_id"],
            **arguments,
        }
    raise ControlPlaneTaskCutoverPreflightError(
        "REVIEWED_CLOCK_CONTRACT_SET_INVALID"
    )


def _is_clock_candidate(row: Mapping[str, Any]) -> bool:
    task_id = row.get("id")
    tool_name = row.get("tool_name")
    if type(task_id) is str and task_id.startswith("clockin_"):
        return True
    if tool_name == "clock_in_dual":
        return True
    if tool_name != "tms_query":
        return False
    try:
        arguments = _decode_task_arguments(row.get("tool_params"))
    except ControlPlaneTaskCutoverPreflightError:
        return False
    endpoint = arguments.get("endpoint")
    if endpoint == "/clock_in_dual":
        return True
    nested = arguments.get("params")
    return isinstance(nested, Mapping) and nested.get("endpoint") == "/clock_in_dual"


def _validate_clock_policy(
    rows: Sequence[Mapping[str, Any]],
    *,
    contracts: Mapping[str, Mapping[str, Any]],
    allow_reviewed_disabled: bool = False,
) -> dict[str, int]:
    clock_rows = [row for row in rows if isinstance(row, Mapping) and _is_clock_candidate(row)]
    unknown = [row for row in clock_rows if row.get("id") not in contracts]
    if unknown:
        raise ControlPlaneTaskCutoverPreflightError(
            "CLOCK_TASK_ID_NOT_REVIEWED",
            count=len(unknown),
        )
    if not clock_rows:
        return {"reviewed_rows": 0, "canonical_rows": 0, "legacy_rows": 0}

    seen_ids: set[str] = set()
    canonical_count = 0
    legacy_count = 0
    for row in clock_rows:
        task_id = row.get("id")
        if type(task_id) is not str:
            raise ControlPlaneTaskCutoverPreflightError("CLOCK_TASK_ID_NOT_REVIEWED")
        if task_id in seen_ids:
            raise ControlPlaneTaskCutoverPreflightError("CLOCK_TASK_ID_DUPLICATE")
        seen_ids.add(task_id)

        enabled = row.get("enabled")
        if type(enabled) not in {bool, int} or enabled not in {False, True, 0, 1}:
            raise ControlPlaneTaskCutoverPreflightError("CLOCK_TASK_ENABLED_TYPE_INVALID")
        if not bool(enabled) and not allow_reviewed_disabled:
            raise ControlPlaneTaskCutoverPreflightError("PROTECTED_CLOCK_TASK_DISABLED")

        contract = contracts[task_id]
        cron_expression = row.get("cron_expression")
        if (
            type(cron_expression) is not str
            or cron_expression != contract.get("cron_expression")
        ):
            raise ControlPlaneTaskCutoverPreflightError("CLOCK_TASK_CRON_NOT_REVIEWED")
        tool_name = row.get("tool_name")
        if type(tool_name) is not str or tool_name not in CONTROL_PLANE_CLOCK_TOOL_NAMES:
            raise ControlPlaneTaskCutoverPreflightError("CLOCK_TASK_TOOL_NOT_REVIEWED")

        canonical_arguments = contract.get("canonical_arguments")
        if not isinstance(canonical_arguments, Mapping):
            raise ControlPlaneTaskCutoverPreflightError(
                "REVIEWED_CLOCK_CONTRACT_SET_INVALID"
            )
        arguments = _decode_task_arguments(row.get("tool_params"))
        if tool_name == contract.get("tool_name") and _strict_json_equal(
            arguments,
            dict(canonical_arguments),
        ):
            canonical_count += 1
            continue
        if tool_name == "tms_query" and _strict_json_equal(
            arguments,
            _legacy_clock_arguments(task_id, canonical_arguments),
        ):
            legacy_count += 1
            continue
        applied_014_tool = (
            "clock_in_dual" if task_id == "clockin_daxiang_1830" else "tms_query"
        )
        if tool_name == applied_014_tool and _strict_json_equal(
            arguments,
            _applied_014_clock_arguments(task_id, canonical_arguments),
        ):
            legacy_count += 1
            continue
        raise ControlPlaneTaskCutoverPreflightError("CLOCK_TASK_ARGUMENTS_NOT_REVIEWED")

    missing_count = len(set(contracts) - seen_ids)
    if missing_count:
        raise ControlPlaneTaskCutoverPreflightError(
            "REVIEWED_CLOCK_TASK_PAIR_INCOMPLETE",
            count=missing_count,
        )
    return {
        "reviewed_rows": len(clock_rows),
        "canonical_rows": canonical_count,
        "legacy_rows": legacy_count,
    }


def _validate_r7_policy(
    rows: Sequence[Mapping[str, Any]],
    *,
    contracts: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    candidates = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and (
            str(row.get("id") or "").startswith("r7_arrival_checkin_")
            or row.get("tool_name") == "r7_arrival_checkin"
        )
    ]
    if not candidates:
        return ()
    seen: set[str] = set()
    for row in candidates:
        task_id = row.get("id")
        if type(task_id) is not str or task_id not in contracts:
            raise ControlPlaneTaskCutoverPreflightError("R7_TASK_ID_NOT_REVIEWED")
        if task_id in seen:
            raise ControlPlaneTaskCutoverPreflightError("R7_TASK_ID_DUPLICATE")
        seen.add(task_id)
        contract = contracts[task_id]
        if row.get("tool_name") != contract.get("tool_name"):
            raise ControlPlaneTaskCutoverPreflightError("R7_TASK_TOOL_NOT_REVIEWED")
        if row.get("cron_expression") != contract.get("cron_expression"):
            raise ControlPlaneTaskCutoverPreflightError("R7_TASK_CRON_NOT_REVIEWED")
        if row.get("enabled") not in {True, 1}:
            raise ControlPlaneTaskCutoverPreflightError("PROTECTED_R7_TASK_DISABLED")
        arguments = _decode_task_arguments(row.get("tool_params"))
        if not _strict_json_equal(
            arguments,
            dict(contract.get("canonical_arguments") or {}),
        ):
            raise ControlPlaneTaskCutoverPreflightError("R7_TASK_ARGUMENTS_NOT_REVIEWED")
    missing_count = len(set(contracts) - seen)
    if missing_count:
        raise ControlPlaneTaskCutoverPreflightError(
            "REVIEWED_R7_TASK_SET_INCOMPLETE",
            count=missing_count,
        )
    return tuple(
        str(contracts[task_id]["cron_expression"])
        for task_id in sorted(seen)
    )


def validate_control_plane_task_cutover(
    rows: Sequence[Mapping[str, Any]],
    *,
    contracts: Mapping[str, Mapping[str, Any]],
    optional_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    clock_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    r7_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    reviewed_login_site_sha256: str = CONTROL_PLANE_REVIEWED_ARRIVE_LOGIN_SITE_SHA256,
    reviewed_arrive_site_sha256: str = CONTROL_PLANE_REVIEWED_ARRIVE_SITE_SHA256,
    allow_reviewed_disabled: bool = False,
    allow_reviewed_disabled_ids: Collection[str] | None = None,
) -> dict[str, int]:
    """Validate scheduler rows without returning any persisted values."""

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ControlPlaneTaskCutoverPreflightError("TASK_ROWS_INVALID")
    resolved_clock_contracts = (
        _load_control_plane_clock_contracts()
        if clock_contracts is None
        else clock_contracts
    )
    resolved_optional_contracts = (
        _load_control_plane_optional_task_contracts()
        if optional_contracts is None
        else optional_contracts
    )
    resolved_r7_contracts = (
        _load_control_plane_r7_contracts() if r7_contracts is None else r7_contracts
    )
    reviewed_disabled_ids = frozenset(allow_reviewed_disabled_ids or ())
    if not reviewed_disabled_ids.issubset(contracts):
        raise ControlPlaneTaskCutoverPreflightError(
            "REVIEWED_DISABLED_TASK_SET_INVALID"
        )
    clock_summary = _validate_clock_policy(
        rows,
        contracts=resolved_clock_contracts,
        allow_reviewed_disabled=allow_reviewed_disabled,
    )
    # A truly empty database has no scheduler state to cut over.  Once any
    # governed-family or external-write candidate exists, the complete 51-row
    # reviewed set becomes mandatory below; partial bootstrap state fails.
    if not rows:
        return {"reviewed_rows": 0, "canonical_rows": 0, "legacy_rows": 0}

    canonical_count = 0
    legacy_count = 0
    reviewed_count = 0
    seen_task_ids: set[str] = set()
    seen_optional_task_ids: set[str] = set()
    seen_r7_task_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ControlPlaneTaskCutoverPreflightError("TASK_ROW_INVALID")
        task_id = row.get("id")
        if type(task_id) is not str:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ID_INVALID")
        if task_id in resolved_clock_contracts:
            continue
        contract = contracts.get(task_id)
        optional = False
        r7 = False
        if not isinstance(contract, Mapping):
            contract = resolved_optional_contracts.get(task_id)
            optional = isinstance(contract, Mapping)
        if not isinstance(contract, Mapping):
            contract = resolved_r7_contracts.get(task_id)
            r7 = isinstance(contract, Mapping)
        if not isinstance(contract, Mapping):
            raise ControlPlaneTaskCutoverPreflightError("TASK_ID_NOT_REVIEWED")
        target_seen = (
            seen_optional_task_ids
            if optional
            else seen_r7_task_ids
            if r7
            else seen_task_ids
        )
        if task_id in target_seen:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ID_DUPLICATE")
        target_seen.add(task_id)

        tool_name = row.get("tool_name")
        cron_expression = row.get("cron_expression")
        enabled = row.get("enabled")
        if type(tool_name) is not str or tool_name != contract.get("tool_name"):
            raise ControlPlaneTaskCutoverPreflightError("TASK_TOOL_NOT_REVIEWED")
        if type(cron_expression) is not str or cron_expression != contract.get("cron_expression"):
            raise ControlPlaneTaskCutoverPreflightError("TASK_CRON_NOT_REVIEWED")
        if type(enabled) not in {bool, int} or enabled not in {False, True, 0, 1}:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ENABLED_TYPE_INVALID")
        reviewed_disabled = task_id in reviewed_disabled_ids
        if (
            not bool(enabled)
            and not optional
            and not allow_reviewed_disabled
            and not reviewed_disabled
        ):
            raise ControlPlaneTaskCutoverPreflightError("PROTECTED_TASK_DISABLED")
        count_reviewed = bool(enabled) or allow_reviewed_disabled or reviewed_disabled

        arguments = _decode_task_arguments(row.get("tool_params"))
        canonical_arguments = contract.get("canonical_arguments")
        if not isinstance(canonical_arguments, Mapping):
            raise ControlPlaneTaskCutoverPreflightError("REVIEWED_CONTRACT_SET_INVALID")
        is_canonical = _strict_json_equal(arguments, dict(canonical_arguments))
        is_legacy = _is_legacy_task_arguments(
            str(contract.get("group_id") or ""),
            arguments,
        )
        if is_canonical:
            if count_reviewed:
                canonical_count += 1
        elif is_legacy:
            if contract.get("group_id") == "arrive_list":
                expected_fingerprints = {"site_code": reviewed_arrive_site_sha256}
                if "login_site_code" in arguments:
                    expected_fingerprints["login_site_code"] = (
                        reviewed_login_site_sha256
                    )
                for fingerprint_field, expected_fingerprint in (
                    expected_fingerprints.items()
                ):
                    observed_fingerprint = hashlib.sha256(
                        arguments[fingerprint_field].strip().encode("utf-8")
                    ).hexdigest()
                    if observed_fingerprint != expected_fingerprint:
                        raise ControlPlaneTaskCutoverPreflightError(
                            "ARRIVE_LOGIN_SITE_FINGERPRINT_MISMATCH"
                        )
            if count_reviewed:
                legacy_count += 1
        else:
            raise ControlPlaneTaskCutoverPreflightError("TASK_ARGUMENTS_NOT_REVIEWED")
        if count_reviewed:
            reviewed_count += 1

    missing_count = len(set(contracts) - seen_task_ids)
    if missing_count:
        raise ControlPlaneTaskCutoverPreflightError(
            "REVIEWED_TASK_SET_INCOMPLETE",
            count=missing_count,
        )
    if seen_r7_task_ids:
        missing_r7_count = len(set(resolved_r7_contracts) - seen_r7_task_ids)
        if missing_r7_count:
            raise ControlPlaneTaskCutoverPreflightError(
                "REVIEWED_R7_TASK_SET_INCOMPLETE",
                count=missing_r7_count,
            )

    return {
        "reviewed_rows": reviewed_count + clock_summary["reviewed_rows"],
        "canonical_rows": canonical_count + clock_summary["canonical_rows"],
        "legacy_rows": legacy_count + clock_summary["legacy_rows"],
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
        optional_contracts = _load_control_plane_optional_task_contracts()
        r7_contracts = _load_control_plane_r7_contracts()
        clock_contracts = _load_control_plane_clock_contracts()
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
                    optional_contracts=optional_contracts,
                    clock_contracts=clock_contracts,
                    r7_contracts=r7_contracts,
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


def _daily_schedule_minutes(cron_expression: Any) -> int:
    if type(cron_expression) is not str:
        raise ControlPlaneTaskCutoverPreflightError("SCHEDULED_WRITE_CRON_INVALID")
    match = re.fullmatch(
        r"(?P<minute>\d{1,2}) (?P<hour>\d{1,2}) \* \* \*",
        cron_expression.strip(),
    )
    if match is None:
        raise ControlPlaneTaskCutoverPreflightError("SCHEDULED_WRITE_CRON_INVALID")
    minute = int(match.group("minute"))
    hour = int(match.group("hour"))
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ControlPlaneTaskCutoverPreflightError("SCHEDULED_WRITE_CRON_INVALID")
    return hour * 60 + minute


def _scheduled_write_snapshot_cron(
    row: Mapping[str, Any],
) -> str | None:
    if row.get("mode") != "EXACT_SCHEDULE_EXEMPT":
        return None
    snapshot = _decode_task_arguments(row.get("contract_snapshot_json"))
    if snapshot.get("operation_type") != "external_write":
        return None
    if row.get("enabled") not in {True, 1}:
        return None

    task_id = row.get("task_id")
    if type(task_id) is not str or not task_id:
        raise ControlPlaneTaskCutoverPreflightError("SCHEDULED_WRITE_BINDING_INVALID")
    if snapshot.get("task_id") != task_id:
        raise ControlPlaneTaskCutoverPreflightError("SCHEDULED_WRITE_BINDING_INVALID")
    if snapshot.get("enabled") is not True:
        raise ControlPlaneTaskCutoverPreflightError("SCHEDULED_WRITE_BINDING_INVALID")

    cron_expression = row.get("cron_expression")
    snapshot_cron = snapshot.get("cron_expression")
    if type(cron_expression) is not str or snapshot_cron != cron_expression:
        raise ControlPlaneTaskCutoverPreflightError("SCHEDULED_WRITE_BINDING_INVALID")
    snapshot_tool = snapshot.get("tool_name")
    if type(snapshot_tool) is not str or snapshot_tool != row.get("tool_name"):
        raise ControlPlaneTaskCutoverPreflightError("SCHEDULED_WRITE_BINDING_INVALID")
    _daily_schedule_minutes(cron_expression)
    return cron_expression


def _is_within_daily_schedule_window(
    now: datetime,
    cron_expression: str,
    *,
    before_minutes: int,
    after_minutes: int,
) -> bool:
    if before_minutes < 0 or after_minutes < 0:
        raise ValueError("scheduled write window minutes must be non-negative")
    local_now = now.astimezone(SCHEDULED_WRITE_TIMEZONE)
    scheduled_minutes = _daily_schedule_minutes(cron_expression)
    scheduled_hour, scheduled_minute = divmod(scheduled_minutes, 60)
    today = local_now.replace(
        hour=scheduled_hour,
        minute=scheduled_minute,
        second=0,
        microsecond=0,
    )
    return any(
        candidate - timedelta(minutes=before_minutes)
        <= local_now
        <= candidate + timedelta(minutes=after_minutes)
        for candidate in (
            today - timedelta(days=1),
            today,
            today + timedelta(days=1),
        )
    )


def _applied_014_yunda_send_was_enabled(
    cursor: Any,
    *,
    require_configuration_version: bool = False,
) -> bool:
    """Prove that 014 alone disabled the reviewed Yunda send schedule."""

    if not _table_exists(cursor, CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE):
        return False
    configuration_version_predicate = (
        "AND task.configuration_version = 1"
        if require_configuration_version
        else ""
    )
    cursor.execute(
        f"""
        SELECT COUNT(*) AS matched_count
        FROM scheduled_tasks AS task
        INNER JOIN {CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE} AS prior
          ON BINARY prior.id = BINARY task.id
        WHERE task.id = %s
          AND task.tool_name = 'sync_yunda_send_waybills'
          AND task.cron_expression = '55 23 * * *'
          AND task.enabled = FALSE
          AND task.last_status = 'disabled'
          AND SHA2(COALESCE(task.last_message, ''), 256) = %s
          {configuration_version_predicate}
          AND JSON_LENGTH(task.tool_params) = 2
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'yunda_default'
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.ensure_fields')) = 'BOOLEAN'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.ensure_fields')) = 'false'
          AND prior.tool_name = 'sync_yunda_send_waybills'
          AND prior.cron_expression = '55 23 * * *'
          AND prior.enabled = TRUE
          AND JSON_LENGTH(prior.tool_params) = 4
          AND JSON_TYPE(JSON_EXTRACT(prior.tool_params, '$.account_id')) = 'STRING'
          AND JSON_UNQUOTE(JSON_EXTRACT(prior.tool_params, '$.account_id')) = 'yunda_default'
          AND JSON_TYPE(JSON_EXTRACT(prior.tool_params, '$.session_profile')) = 'STRING'
          AND JSON_UNQUOTE(JSON_EXTRACT(prior.tool_params, '$.session_profile')) = 'yunda'
          AND JSON_TYPE(JSON_EXTRACT(prior.tool_params, '$.ensure_fields')) = 'BOOLEAN'
          AND JSON_UNQUOTE(JSON_EXTRACT(prior.tool_params, '$.ensure_fields')) = 'false'
          AND JSON_TYPE(JSON_EXTRACT(prior.tool_params, '$.target_date')) = 'STRING'
          AND JSON_UNQUOTE(JSON_EXTRACT(prior.tool_params, '$.target_date')) = ''
        """,
        (
            CONTROL_PLANE_APPLIED_014_YUNDA_SEND_ID,
            CONTROL_PLANE_APPLIED_014_YUNDA_DISABLED_MESSAGE_SHA256,
        ),
    )
    row = cursor.fetchone()
    if isinstance(row, Mapping):
        matched_count = row.get("matched_count")
    elif isinstance(row, (tuple, list)) and row:
        matched_count = row[0]
    else:
        matched_count = None
    if type(matched_count) is not int or matched_count not in {0, 1}:
        raise ControlPlaneTaskCutoverPreflightError(
            "APPLIED_014_YUNDA_SEND_PROOF_INVALID"
        )
    return matched_count == 1


def _legacy_scheduled_write_crons(
    rows: Sequence[Mapping[str, Any]],
    *,
    allow_reviewed_disabled_ids: Collection[str] = (),
) -> tuple[str, ...]:
    internal_contracts = _load_control_plane_reviewed_task_contracts()
    clock_contracts = _load_control_plane_clock_contracts()
    r7_contracts = _load_control_plane_r7_contracts()
    validate_control_plane_task_cutover(
        rows,
        contracts=internal_contracts,
        clock_contracts=clock_contracts,
        r7_contracts=r7_contracts,
        allow_reviewed_disabled_ids=allow_reviewed_disabled_ids,
    )
    present_clock_ids = {
        row.get("id")
        for row in rows
        if isinstance(row, Mapping) and row.get("id") in clock_contracts
    }
    clock_crons = tuple(
        str(clock_contracts[task_id]["cron_expression"])
        for task_id in sorted(present_clock_ids)
    )
    r7_crons = _validate_r7_policy(rows, contracts=r7_contracts)
    reviewed_yunda_crons = tuple(
        str(internal_contracts[CONTROL_PLANE_APPLIED_014_YUNDA_SEND_ID]["cron_expression"])
        for row in rows
        if isinstance(row, Mapping)
        and row.get("id") == CONTROL_PLANE_APPLIED_014_YUNDA_SEND_ID
        and (
            bool(row.get("enabled"))
            or CONTROL_PLANE_APPLIED_014_YUNDA_SEND_ID
            in allow_reviewed_disabled_ids
        )
    )
    return tuple(
        sorted(set((*clock_crons, *r7_crons, *reviewed_yunda_crons)))
    )


def _typed_project_scheduled_write_crons(cursor: Any) -> tuple[str, ...]:
    """Return current signed project write schedules without exposing payloads."""
    return _AUTOMATION_PROJECT_RELEASE_MANIFEST_HELPER.typed_project_scheduled_write_crons(
        cursor,
        error_class=ControlPlaneTaskCutoverPreflightError,
        scheduled_write_operation_types=PROJECT_SCHEDULED_WRITE_OPERATION_TYPES,
    )


def check_scheduled_write_window(
    *,
    before_minutes: int = SCHEDULED_WRITE_WINDOW_BEFORE_MINUTES,
    after_minutes: int = SCHEDULED_WRITE_WINDOW_AFTER_MINUTES,
    now: datetime | None = None,
) -> int:
    """Block release mutation near an exempt external-write schedule.

    This is deliberately read-only and prints no task IDs, arguments, hashes,
    or actor data. Contract hash verification belongs to the runtime backend;
    release safety uses only the audited snapshot's external-write binding.
    """

    connection = None
    try:
        if before_minutes < 0 or after_minutes < 0:
            raise ControlPlaneTaskCutoverPreflightError(
                "SCHEDULED_WRITE_WINDOW_INVALID"
            )
        connection = _connect()
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            if _table_exists(cursor, SCHEDULED_TASK_APPROVAL_POLICY_TABLE):
                cursor.execute(
                    """
                    SELECT
                        policy.task_id,
                        policy.mode,
                        policy.contract_snapshot_json,
                        task.tool_name,
                        task.cron_expression,
                        task.enabled
                    FROM scheduled_task_approval_policies AS policy
                    INNER JOIN scheduled_tasks AS task ON task.id = policy.task_id
                    WHERE policy.mode = 'EXACT_SCHEDULE_EXEMPT'
                    """
                )
                policy_crons = tuple(
                    cron
                    for row in cursor.fetchall()
                    if (cron := _scheduled_write_snapshot_cron(row)) is not None
                )
                project_crons = ()
                project_tables = (
                    "automation_projects",
                    "automation_project_policies",
                    "automation_project_generations",
                    "automation_project_generation_leases",
                )
                project_table_presence = tuple(
                    _table_exists(cursor, table_name)
                    for table_name in project_tables
                )
                if any(project_table_presence) and not all(project_table_presence):
                    raise ControlPlaneTaskCutoverPreflightError(
                        "PROJECT_SCHEDULE_SCHEMA_INCOMPLETE"
                    )
                if all(project_table_presence):
                    project_crons = _typed_project_scheduled_write_crons(cursor)
                # A failed first control-plane release can leave additive 015
                # tables behind while the old scheduler source is restored.
                # In that state policy rows still default to per-run approval,
                # but the legacy clock pair continues to execute automatically.
                # Always include the exact reviewed pair until it is removed or
                # replaced by a later migration; this may only over-block a
                # release window and can never authorize execution.
                cursor.execute(CONTROL_PLANE_TASK_CANDIDATE_SQL)
                candidate_rows = cursor.fetchall()
                legacy_candidate_rows = tuple(
                    row
                    for row in candidate_rows
                    if not str(row.get("tool_name") or "").startswith("automation.")
                )
                clock_contracts = _load_control_plane_clock_contracts()
                r7_contracts = _load_control_plane_r7_contracts()
                _validate_clock_policy(
                    legacy_candidate_rows,
                    contracts=clock_contracts,
                )
                reviewed_r7_crons = _validate_r7_policy(
                    legacy_candidate_rows,
                    contracts=r7_contracts,
                )
                present_clock_ids = {
                    row.get("id")
                    for row in legacy_candidate_rows
                    if isinstance(row, Mapping) and row.get("id") in clock_contracts
                }
                reviewed_clock_crons = tuple(
                    str(clock_contracts[task_id]["cron_expression"])
                    for task_id in sorted(present_clock_ids)
                )
                reviewed_yunda_crons = ()
                yunda_rows = tuple(
                    row
                    for row in legacy_candidate_rows
                    if isinstance(row, Mapping)
                    and row.get("id") == CONTROL_PLANE_APPLIED_014_YUNDA_SEND_ID
                )
                if len(yunda_rows) > 1:
                    raise ControlPlaneTaskCutoverPreflightError(
                        "APPLIED_014_YUNDA_SEND_PROOF_INVALID"
                    )
                if yunda_rows:
                    yunda_row = yunda_rows[0]
                    enabled = yunda_row.get("enabled")
                    if type(enabled) not in {bool, int} or enabled not in {
                        False,
                        True,
                        0,
                        1,
                    }:
                        raise ControlPlaneTaskCutoverPreflightError(
                            "TASK_ENABLED_TYPE_INVALID"
                        )
                    internal_contract = _load_control_plane_reviewed_task_contracts()[
                        CONTROL_PLANE_APPLIED_014_YUNDA_SEND_ID
                    ]
                    cron_expression = str(internal_contract["cron_expression"])
                    if bool(enabled):
                        arguments = _decode_task_arguments(yunda_row.get("tool_params"))
                        if (
                            yunda_row.get("tool_name")
                            != internal_contract["tool_name"]
                            or yunda_row.get("cron_expression") != cron_expression
                            or not _strict_json_equal(
                                arguments,
                                internal_contract["canonical_arguments"],
                            )
                        ):
                            raise ControlPlaneTaskCutoverPreflightError(
                                "TASK_ARGUMENTS_NOT_REVIEWED"
                            )
                        reviewed_yunda_crons = (cron_expression,)
                    else:
                        cursor.execute(
                            "SELECT 1 FROM schema_migrations WHERE version=%s",
                            (SCHEDULED_TASK_CONTRACT_UPGRADE_VERSION,),
                        )
                        contract_upgrade_applied = cursor.fetchone() is not None
                        if not contract_upgrade_applied:
                            if not _applied_014_yunda_send_was_enabled(
                                cursor,
                                require_configuration_version=True,
                            ):
                                raise ControlPlaneTaskCutoverPreflightError(
                                    "APPLIED_014_YUNDA_SEND_PROOF_INVALID"
                                )
                            reviewed_yunda_crons = (cron_expression,)
                crons = tuple(
                    sorted(
                        set(
                            (
                                *policy_crons,
                                *project_crons,
                                *reviewed_clock_crons,
                                *reviewed_r7_crons,
                                *reviewed_yunda_crons,
                            )
                        )
                    )
                )
            elif not _table_exists(cursor, "scheduled_tasks"):
                crons = ()
            else:
                cursor.execute(CONTROL_PLANE_TASK_CANDIDATE_SQL)
                candidate_rows = cursor.fetchall()
                allowed_reviewed_disabled_ids = (
                    frozenset({CONTROL_PLANE_APPLIED_014_YUNDA_SEND_ID})
                    if _applied_014_yunda_send_was_enabled(cursor)
                    else frozenset()
                )
                crons = _legacy_scheduled_write_crons(
                    candidate_rows,
                    allow_reviewed_disabled_ids=allowed_reviewed_disabled_ids,
                )

        checked_at = now or datetime.now(tz=SCHEDULED_WRITE_TIMEZONE)
        blocked_count = sum(
            _is_within_daily_schedule_window(
                checked_at,
                cron,
                before_minutes=before_minutes,
                after_minutes=after_minutes,
            )
            for cron in crons
        )
        if blocked_count:
            raise ControlPlaneTaskCutoverPreflightError(
                "SCHEDULED_WRITE_WINDOW_ACTIVE",
                count=blocked_count,
            )
    except ControlPlaneTaskCutoverPreflightError as exc:
        print(
            "scheduled_write_window=blocked "
            f"reason={exc.code} count={exc.count}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "scheduled_write_window=blocked "
            "reason=SCHEDULED_WRITE_WINDOW_RUNTIME_ERROR count=1",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            connection.close()

    print(f"scheduled_write_window=ok checked_schedules={len(crons)}")
    return 0


def _load_control_plane_reviewed_manifest_ids() -> frozenset[str]:
    task_ids = {
        *_load_control_plane_reviewed_task_contracts(),
        *_load_control_plane_optional_task_contracts(),
        *_load_control_plane_clock_contracts(),
        *_load_control_plane_r7_contracts(),
    }
    if len(task_ids) != CONTROL_PLANE_REVIEWED_MANIFEST_COUNT:
        raise ControlPlaneTaskCutoverPreflightError("REVIEWED_MANIFEST_INVALID")
    return frozenset(task_ids)


def _validate_control_plane_policy_states(
    policies: Sequence[Mapping[str, Any]],
    *,
    enabled_ids: set[str],
    registry: Any,
    approval_contracts: Any,
    profile_by_task_id: Mapping[str, Any],
    arguments_for_schema_validation: Callable[
        [Mapping[str, Any], Mapping[str, str]], dict[str, Any]
    ],
    require_enabled_exact: bool = False,
) -> None:
    policy_ids = {str(row.get("task_id") or "") for row in policies}
    if not enabled_ids.issubset(policy_ids):
        raise ControlPlaneTaskCutoverPreflightError(
            "CONTROL_PLANE_POLICY_SET_INCOMPLETE",
            count=len(enabled_ids - policy_ids),
        )
    for policy in policies:
        mode = policy.get("mode")
        version = policy.get("version")
        actor_id = policy.get("approved_by_actor_id")
        actor_role = policy.get("approved_by_actor_role")
        if (
            mode not in {"REQUIRE_EACH_RUN", "EXACT_SCHEDULE_EXEMPT"}
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
        ):
            raise ControlPlaneTaskCutoverPreflightError(
                "CONTROL_PLANE_POLICY_STATE_INVALID"
            )
        task_id = str(policy.get("task_id") or "")
        if (
            require_enabled_exact
            and task_id in enabled_ids
            and mode != "EXACT_SCHEDULE_EXEMPT"
        ):
            raise ControlPlaneTaskCutoverPreflightError(
                "INITIAL_ENABLED_TASK_EXACT_POLICY_REQUIRED"
            )
        untouched_default = (
            mode == "REQUIRE_EACH_RUN"
            and version == 1
            and actor_id is None
            and actor_role is None
        )
        if not untouched_default and policy.get("has_explaining_event") not in {
            True,
            1,
        }:
            raise ControlPlaneTaskCutoverPreflightError(
                "CONTROL_PLANE_POLICY_EVENT_MISSING"
            )
        if mode == "REQUIRE_EACH_RUN":
            if any(
                policy.get(field) is not None
                for field in (
                    "contract_hash",
                    "contract_snapshot_json",
                    "tool_contract_hash",
                )
            ):
                raise ControlPlaneTaskCutoverPreflightError(
                    "CONTROL_PLANE_POLICY_STATE_INVALID"
                )
            if task_id in enabled_ids:
                explicit_admin = (
                    policy.get("has_explaining_event") in {True, 1}
                    and actor_role == "super_admin"
                    and type(actor_id) is str
                    and bool(actor_id.strip())
                    and actor_id != CONTROL_PLANE_MIGRATION_ACTOR_ID
                    and policy.get("latest_event_reason") == "console_policy_change"
                )
                credential_safety_downgrade = (
                    policy.get("has_explaining_event") in {True, 1}
                    and actor_id
                    == approval_contracts.ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID
                    and actor_role == "system"
                    and policy.get("latest_event_reason")
                    == approval_contracts.ACCOUNT_CREDENTIAL_CHANGE_REASON
                )
                if not (explicit_admin or credential_safety_downgrade):
                    raise ControlPlaneTaskCutoverPreflightError(
                        "ENABLED_TASK_DEFAULT_POLICY_NOT_ALLOWED"
                    )
            continue
        snapshot = _decode_task_arguments(policy.get("contract_snapshot_json"))
        if (
            str(policy.get("contract_hash") or "")
            != approval_contracts.sha256_json(snapshot)
            or str(policy.get("tool_contract_hash") or "")
            != str(snapshot.get("tool_contract_hash") or "")
            or snapshot.get("task_id") != policy.get("task_id")
            or snapshot.get("enabled") is not True
        ):
            raise ControlPlaneTaskCutoverPreflightError(
                "CONTROL_PLANE_POLICY_STATE_INVALID"
            )
        if task_id not in enabled_ids:
            continue
        profile = profile_by_task_id.get(task_id)
        capability = registry.get_capability(str(policy.get("tool_name") or ""))
        if profile is None or not isinstance(capability, Mapping):
            raise ControlPlaneTaskCutoverPreflightError(
                "CONTROL_PLANE_POLICY_NOT_ACTIVE"
            )
        try:
            arguments = _decode_task_arguments(policy.get("tool_params"))
            validation_arguments = arguments_for_schema_validation(
                arguments,
                profile.dynamic_argument_rules,
            )
            registry.validate_arguments(
                str(policy["tool_name"]),
                validation_arguments,
            )
            if (
                capability.get("version") != profile.tool_version
                or capability.get("operation_type") != profile.operation_type
                or (capability.get("approval") or {}).get("mode")
                != "schedule_allowlist"
            ):
                raise ValueError("staged tool governance changed")
            current_contract = approval_contracts.build_scheduled_task_contract(
                {
                    "id": task_id,
                    "tool_name": policy["tool_name"],
                    "tool_params": arguments,
                    "cron_expression": policy["cron_expression"],
                    "enabled": policy["enabled"],
                    "configuration_version": policy["configuration_version"],
                },
                capability,
                dynamic_argument_rules=profile.dynamic_argument_rules,
                allowed_special_cron=profile.cron_expression,
            )
        except Exception as exc:
            raise ControlPlaneTaskCutoverPreflightError(
                "CONTROL_PLANE_POLICY_NOT_ACTIVE"
            ) from exc
        if (
            policy.get("contract_hash") != current_contract.contract_hash
            or policy.get("tool_contract_hash") != current_contract.tool_contract_hash
            or not _strict_json_equal(snapshot, current_contract.snapshot)
        ):
            raise ControlPlaneTaskCutoverPreflightError(
                "CONTROL_PLANE_POLICY_NOT_ACTIVE"
            )


def check_control_plane_release_manifest(
    *,
    expect_initial_production_manifest: bool = False,
) -> int:
    return _AUTOMATION_PROJECT_RELEASE_MANIFEST_HELPER.check_control_plane_release_manifest(
        globals(),
        expect_initial_production_manifest=expect_initial_production_manifest,
    )


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
            if running_count:
                raise ControlPlaneTaskCutoverPreflightError(
                    "PROTECTED_WRITE_RUNNING",
                    count=running_count,
                )
    except ControlPlaneTaskCutoverPreflightError as exc:
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


def _lock_and_validate_017_restore_state(
    cursor,
    *,
    backup_table: str,
    created_table: str,
) -> None:
    """Lock 017-owned state and require bootstrap cleanup before rollback."""

    cursor.execute(
        f"""
        SELECT marker.task_id
        FROM {created_table} AS marker
        ORDER BY BINARY marker.task_id
        FOR UPDATE
        """
    )
    cursor.fetchall()
    cursor.execute(
        f"""
        SELECT backup.id
        FROM {backup_table} AS backup
        ORDER BY BINARY backup.id
        FOR UPDATE
        """
    )
    cursor.fetchall()
    cursor.execute(
        f"""
        SELECT task.id
        FROM scheduled_tasks AS task
        WHERE BINARY task.id = BINARY %s
           OR EXISTS (
                SELECT 1
                FROM {created_table} AS marker
                WHERE BINARY marker.task_id = BINARY task.id
           )
           OR EXISTS (
                SELECT 1
                FROM {backup_table} AS backup
                WHERE BINARY backup.id = BINARY task.id
           )
        ORDER BY BINARY task.id
        FOR UPDATE
        """,
        (FINANCE_STARTUP_CATCHUP_TASK_ID,),
    )
    cursor.fetchall()

    cursor.execute(
        f"""
        SELECT policy.task_id
        FROM {SCHEDULED_TASK_APPROVAL_POLICY_TABLE} AS policy
        WHERE BINARY policy.task_id = BINARY %s
        FOR UPDATE
        """,
        (FINANCE_STARTUP_CATCHUP_TASK_ID,),
    )
    if cursor.fetchall():
        raise RuntimeError(
            "017 restore requires control-plane bootstrap cleanup first: "
            "finance startup policy remains"
        )

    cursor.execute(
        f"""
        SELECT event.event_id
        FROM {SCHEDULED_TASK_APPROVAL_EVENT_TABLE} AS event
        WHERE BINARY event.task_id = BINARY %s
           OR BINARY event.task_id = BINARY %s
           OR BINARY event.request_id = BINARY %s
        ORDER BY event.event_id
        FOR UPDATE
        """,
        (
            FINANCE_STARTUP_CATCHUP_TASK_ID,
            CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID,
            CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
        ),
    )
    if cursor.fetchall():
        raise RuntimeError(
            "017 restore requires control-plane bootstrap cleanup first: "
            "finance startup audit or completion marker remains"
        )

    cursor.execute(
        """
        SELECT
            domain_event.event_id,
            outbox.outbox_id,
            consumption.consumer_name
        FROM domain_events AS domain_event
        LEFT JOIN outbox_events AS outbox
          ON BINARY outbox.event_id = BINARY domain_event.event_id
        LEFT JOIN event_consumptions AS consumption
          ON BINARY consumption.event_id = BINARY domain_event.event_id
        WHERE BINARY domain_event.event_type =
                BINARY 'scheduled_task.approval_policy_changed'
          AND BINARY domain_event.source_system = BINARY 'agent'
          AND BINARY domain_event.entity_type = BINARY 'scheduled_task'
          AND BINARY domain_event.entity_id = BINARY %s
        FOR UPDATE
        """,
        (FINANCE_STARTUP_CATCHUP_TASK_ID,),
    )
    if cursor.fetchall():
        raise RuntimeError(
            "017 restore requires control-plane bootstrap cleanup first: "
            "finance startup domain or outbox state remains"
        )


def _restore_scheduled_task_rows_from_migration(
    *,
    version: str,
    backup_table: str,
    output_name: str,
    created_table: str | None = None,
) -> int:
    connection = _connect()
    transaction_started = False
    try:
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            if not _migration_table_exists(cursor):
                print(f"{output_name}=skipped reason=history_missing")
                return 0
            backup_exists = _table_exists(cursor, backup_table)
            created_exists = bool(
                created_table and _table_exists(cursor, created_table)
            )
            if not backup_exists and not created_exists:
                print(f"{output_name}=skipped reason=backup_not_created")
                return 0
            if created_exists:
                if not backup_exists:
                    raise RuntimeError(
                        "017 created-task marker table exists without its backup table"
                    )
                required_cleanup_tables = (
                    SCHEDULED_TASK_APPROVAL_POLICY_TABLE,
                    SCHEDULED_TASK_APPROVAL_EVENT_TABLE,
                    "domain_events",
                    "outbox_events",
                    "event_consumptions",
                )
                missing_cleanup_tables = [
                    table_name
                    for table_name in required_cleanup_tables
                    if not _table_exists(cursor, table_name)
                ]
                if missing_cleanup_tables:
                    raise RuntimeError(
                        "017 restore cannot prove bootstrap cleanup state: "
                        "required tables missing"
                    )

            connection.begin()
            transaction_started = True
            if created_exists:
                _lock_and_validate_017_restore_state(
                    cursor,
                    backup_table=backup_table,
                    created_table=created_table,
                )
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS unsafe_count
                    FROM {created_table} AS marker
                    LEFT JOIN scheduled_tasks AS task
                      ON BINARY task.id = BINARY marker.task_id
                    LEFT JOIN {backup_table} AS backup
                      ON BINARY backup.id = BINARY marker.task_id
                    WHERE BINARY marker.task_id <>
                            BINARY 'finance_startup_catchup'
                       OR backup.id IS NOT NULL
                       OR (
                            task.id IS NOT NULL
                            AND NOT (
                                BINARY task.name = BINARY '财务启动缺口扫描'
                                AND BINARY task.tool_name =
                                    BINARY 'sync_finance_bills'
                                AND BINARY task.cron_expression = BINARY '@startup'
                                AND task.enabled = TRUE
                                AND task.last_run IS NULL
                                AND task.last_status IS NULL
                                AND task.last_duration_ms IS NULL
                                AND task.last_message IS NULL
                                AND task.created_at = marker.task_created_at
                                AND task.updated_at = marker.task_updated_at
                                AND task.configuration_version =
                                    marker.task_configuration_version
                                AND COALESCE(
                                    JSON_CONTAINS(
                                        task.tool_params,
                                        JSON_OBJECT(
                                            'mode', 'sync',
                                            'platform', 'ronghui',
                                            'rescan_days', 7,
                                            '_startup_catchup',
                                                CAST('true' AS JSON)
                                        )
                                    )
                                    AND JSON_CONTAINS(
                                        JSON_OBJECT(
                                            'mode', 'sync',
                                            'platform', 'ronghui',
                                            'rescan_days', 7,
                                            '_startup_catchup',
                                                CAST('true' AS JSON)
                                        ),
                                        task.tool_params
                                    ),
                                    FALSE
                                )
                            )
                       )
                    """
                )
                row = cursor.fetchone() or {}
                unsafe_count = int(row.get("unsafe_count") or 0)
                if unsafe_count:
                    raise RuntimeError(
                        "017 created finance startup row no longer matches its marker"
                    )
                cursor.execute(
                    f"""
                    DELETE task
                    FROM scheduled_tasks AS task
                    INNER JOIN {created_table} AS marker
                      ON BINARY marker.task_id = BINARY task.id
                    LEFT JOIN {backup_table} AS backup
                      ON BINARY backup.id = BINARY task.id
                    WHERE BINARY marker.task_id =
                            BINARY 'finance_startup_catchup'
                      AND backup.id IS NULL
                      AND BINARY task.name = BINARY '财务启动缺口扫描'
                      AND BINARY task.tool_name = BINARY 'sync_finance_bills'
                      AND BINARY task.cron_expression = BINARY '@startup'
                      AND task.enabled = TRUE
                      AND task.last_run IS NULL
                      AND task.last_status IS NULL
                      AND task.last_duration_ms IS NULL
                      AND task.last_message IS NULL
                      AND task.created_at = marker.task_created_at
                      AND task.updated_at = marker.task_updated_at
                      AND task.configuration_version =
                            marker.task_configuration_version
                      AND JSON_CONTAINS(
                          task.tool_params,
                          JSON_OBJECT(
                              'mode', 'sync',
                              'platform', 'ronghui',
                              'rescan_days', 7,
                              '_startup_catchup', CAST('true' AS JSON)
                          )
                      )
                      AND JSON_CONTAINS(
                          JSON_OBJECT(
                              'mode', 'sync',
                              'platform', 'ronghui',
                              'rescan_days', 7,
                              '_startup_catchup', CAST('true' AS JSON)
                          ),
                          task.tool_params
                      )
                    """
                )
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS remaining_created_count
                    FROM {created_table} AS marker
                    LEFT JOIN scheduled_tasks AS task
                      ON BINARY task.id = BINARY marker.task_id
                    LEFT JOIN {backup_table} AS backup
                      ON BINARY backup.id = BINARY marker.task_id
                    WHERE BINARY marker.task_id <>
                            BINARY 'finance_startup_catchup'
                       OR backup.id IS NOT NULL
                       OR task.id IS NOT NULL
                    """
                )
                remaining_row = cursor.fetchone() or {}
                if int(remaining_row.get("remaining_created_count") or 0):
                    raise RuntimeError(
                        "017 marker-owned finance startup row remained after delete"
                    )
            if backup_exists:
                cursor.execute(
                    f"""
                    INSERT INTO scheduled_tasks (
                        id, name, tool_name, tool_params, cron_expression, enabled,
                        last_run, last_status, last_duration_ms, last_message, created_at,
                        configuration_version, updated_at
                    )
                    SELECT
                        id, name, tool_name, tool_params, cron_expression, enabled,
                        last_run, last_status, last_duration_ms, last_message, created_at,
                        configuration_version, updated_at
                    FROM {backup_table}
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
                        created_at = VALUES(created_at),
                        configuration_version = VALUES(configuration_version),
                        updated_at = VALUES(updated_at)
                    """
                )
            cursor.execute(
                "DELETE FROM schema_migrations WHERE BINARY version=BINARY %s",
                (version,),
            )
            if backup_exists:
                cursor.execute(f"DELETE FROM {backup_table}")
            if created_exists:
                cursor.execute(f"DELETE FROM {created_table}")
            connection.commit()
            transaction_started = False
            print(f"{output_name}=ok")
    except Exception:
        if transaction_started:
            connection.rollback()
        raise
    finally:
        connection.close()
    return 0


def restore_daily_sign_single_tms_account() -> int:
    """Restore exact pre-016 daily-sign rows and make 016 re-applicable."""
    return _restore_scheduled_task_rows_from_migration(
        version=DAILY_SIGN_SINGLE_TMS_VERSION,
        backup_table=DAILY_SIGN_SINGLE_TMS_BACKUP_TABLE,
        output_name="daily_sign_single_tms_restore",
    )


def restore_scheduled_task_contract_upgrade() -> int:
    """Restore exact pre-017 scheduler rows and make 017 safely re-applicable."""
    return _restore_scheduled_task_rows_from_migration(
        version=SCHEDULED_TASK_CONTRACT_UPGRADE_VERSION,
        backup_table=SCHEDULED_TASK_CONTRACT_UPGRADE_BACKUP_TABLE,
        output_name="scheduled_task_contract_upgrade_restore",
        created_table=SCHEDULED_TASK_CONTRACT_UPGRADE_CREATED_TABLE,
    )


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


_MIGRATION_018_HELPER = _load_script_helper("migration_018_authorization.py")
_AUTOMATION_PROJECT_RELEASE_MANIFEST_HELPER = _load_script_helper(
    "automation_project_release_manifest_preflight.py"
)
check_automation_project_required_resources = _load_script_helper("automation_project_resource_preflight.py").check_automation_project_required_resources
check_automation_project_scheduled_task_identities = _load_script_helper("automation_project_schedule_identity_preflight.py").check_automation_project_scheduled_task_identities
check_automation_plugin_install_ownership = _load_script_helper(
    "automation_plugin_install_ownership_preflight.py"
).check_automation_plugin_install_ownership

def _automation_project_authorization_artifacts(cursor) -> set[str]:
    return _MIGRATION_018_HELPER._automation_project_authorization_artifacts(
        globals(), cursor
    )


def _validate_automation_project_authorization_restore(cursor) -> bool:
    return _MIGRATION_018_HELPER._validate_automation_project_authorization_restore(
        globals(), cursor
    )


def restore_automation_project_authorization() -> int:
    return _MIGRATION_018_HELPER.restore_automation_project_authorization(globals())


def restore_control_plane_policy_bootstrap() -> int:
    """Remove only migration-owned bootstrap state after a failed first release."""

    connection = _connect()
    transaction_started = False
    try:
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            if not all(
                _table_exists(cursor, table_name)
                for table_name in (
                    SCHEDULED_TASK_APPROVAL_POLICY_TABLE,
                    SCHEDULED_TASK_APPROVAL_EVENT_TABLE,
                )
            ):
                print("control_plane_policy_bootstrap_restore=skipped reason=tables_missing")
                return 0

            connection.begin()
            transaction_started = True
            cursor.execute(
                f"""
                SELECT COUNT(*) AS orphan_count
                FROM {SCHEDULED_TASK_APPROVAL_POLICY_TABLE} AS policy
                LEFT JOIN {SCHEDULED_TASK_APPROVAL_EVENT_TABLE} AS event
                  ON event.task_id = policy.task_id
                 AND event.to_mode = policy.mode
                 AND event.contract_hash = policy.contract_hash
                 AND event.actor_id = %s
                 AND event.actor_role = %s
                 AND event.reason = 'control_plane_v1_bootstrap'
                WHERE policy.mode = 'EXACT_SCHEDULE_EXEMPT'
                  AND policy.approved_by_actor_id = %s
                  AND policy.approved_by_actor_role = %s
                  AND event.event_id IS NULL
                """,
                (
                    CONTROL_PLANE_MIGRATION_ACTOR_ID,
                    CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                    CONTROL_PLANE_MIGRATION_ACTOR_ID,
                    CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                ),
            )
            orphan_row = cursor.fetchone() or {}
            if int(orphan_row.get("orphan_count") or 0):
                raise RuntimeError("MIGRATION_EXACT_POLICY_BOOTSTRAP_EVENT_MISSING")
            domain_tables_exist = all(
                _table_exists(cursor, table_name)
                for table_name in ("domain_events", "outbox_events", "event_consumptions")
            )
            if domain_tables_exist:
                actor_params = (
                    CONTROL_PLANE_MIGRATION_ACTOR_ID,
                    CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                )
                cursor.execute(
                    "DELETE consumption FROM event_consumptions AS consumption "
                    "INNER JOIN domain_events AS domain_event "
                    "ON BINARY domain_event.event_id = BINARY consumption.event_id "
                    f"INNER JOIN {SCHEDULED_TASK_APPROVAL_EVENT_TABLE} AS policy_event "
                    "ON BINARY domain_event.source_event_id = BINARY CONCAT("
                    "policy_event.task_id, ':', policy_event.request_id) "
                    "WHERE domain_event.event_type = "
                    "'scheduled_task.approval_policy_changed' "
                    "AND domain_event.source_system = 'agent' "
                    "AND domain_event.entity_type = 'scheduled_task' "
                    "AND BINARY domain_event.entity_id = BINARY policy_event.task_id "
                    "AND policy_event.actor_id = %s "
                    "AND policy_event.actor_role = %s "
                    "AND policy_event.reason = 'control_plane_v1_bootstrap'",
                    actor_params,
                )
                cursor.execute(
                    "DELETE outbox FROM outbox_events AS outbox "
                    "INNER JOIN domain_events AS domain_event "
                    "ON BINARY domain_event.event_id = BINARY outbox.event_id "
                    f"INNER JOIN {SCHEDULED_TASK_APPROVAL_EVENT_TABLE} AS policy_event "
                    "ON BINARY domain_event.source_event_id = BINARY CONCAT("
                    "policy_event.task_id, ':', policy_event.request_id) "
                    "WHERE domain_event.event_type = "
                    "'scheduled_task.approval_policy_changed' "
                    "AND domain_event.source_system = 'agent' "
                    "AND domain_event.entity_type = 'scheduled_task' "
                    "AND BINARY domain_event.entity_id = BINARY policy_event.task_id "
                    "AND policy_event.actor_id = %s "
                    "AND policy_event.actor_role = %s "
                    "AND policy_event.reason = 'control_plane_v1_bootstrap'",
                    actor_params,
                )
                cursor.execute(
                    "DELETE domain_event FROM domain_events AS domain_event "
                    f"INNER JOIN {SCHEDULED_TASK_APPROVAL_EVENT_TABLE} AS policy_event "
                    "ON BINARY domain_event.source_event_id = BINARY CONCAT("
                    "policy_event.task_id, ':', policy_event.request_id) "
                    "WHERE domain_event.event_type = "
                    "'scheduled_task.approval_policy_changed' "
                    "AND domain_event.source_system = 'agent' "
                    "AND domain_event.entity_type = 'scheduled_task' "
                    "AND BINARY domain_event.entity_id = BINARY policy_event.task_id "
                    "AND policy_event.actor_id = %s "
                    "AND policy_event.actor_role = %s "
                    "AND policy_event.reason = 'control_plane_v1_bootstrap'",
                    actor_params,
                )

            cursor.execute(
                f"""
                DELETE policy
                FROM {SCHEDULED_TASK_APPROVAL_POLICY_TABLE} AS policy
                WHERE policy.mode = 'EXACT_SCHEDULE_EXEMPT'
                  AND policy.approved_by_actor_id = %s
                  AND policy.approved_by_actor_role = %s
                  AND EXISTS (
                      SELECT 1
                      FROM {SCHEDULED_TASK_APPROVAL_EVENT_TABLE} AS event
                      WHERE event.task_id = policy.task_id
                        AND event.to_mode = policy.mode
                        AND event.contract_hash = policy.contract_hash
                        AND event.actor_id = %s
                        AND event.actor_role = %s
                        AND event.reason = 'control_plane_v1_bootstrap'
                  )
                """,
                (
                    CONTROL_PLANE_MIGRATION_ACTOR_ID,
                    CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                    CONTROL_PLANE_MIGRATION_ACTOR_ID,
                    CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                ),
            )
            cursor.execute(
                f"""
                DELETE FROM {SCHEDULED_TASK_APPROVAL_EVENT_TABLE}
                WHERE actor_id = %s
                  AND actor_role = %s
                  AND (
                      reason = 'control_plane_v1_bootstrap'
                      OR (
                          task_id = %s
                          AND request_id = %s
                          AND reason = 'control_plane_v1_bootstrap_complete'
                      )
                  )
                """,
                (
                    CONTROL_PLANE_MIGRATION_ACTOR_ID,
                    CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                    CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID,
                    CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
                ),
            )
            connection.commit()
            transaction_started = False
            print("control_plane_policy_bootstrap_restore=ok")
    except Exception:
        if transaction_started:
            connection.rollback()
        raise
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
            if _table_exists(cursor, CONTROL_PLANE_TASK_CUTOVER_CREATED_TABLE):
                cursor.execute(
                    f"""
                    DELETE created_task
                    FROM scheduled_tasks AS created_task
                    INNER JOIN {CONTROL_PLANE_TASK_CUTOVER_CREATED_TABLE} AS marker
                        ON marker.task_id = created_task.id
                    LEFT JOIN {CONTROL_PLANE_TASK_CUTOVER_BACKUP_TABLE} AS backup
                        ON backup.id = created_task.id
                    WHERE marker.task_id = 'finance_startup_catchup'
                      AND backup.id IS NULL
                    """
                )
                cursor.execute(
                    f"""
                    DELETE FROM {CONTROL_PLANE_TASK_CUTOVER_CREATED_TABLE}
                    WHERE task_id = 'finance_startup_catchup'
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


def _report_migration_capture_status(
    *,
    version: str,
    backup_table: str,
    output_name: str,
    created_table: str | None = None,
) -> int:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            applied = False
            if _migration_table_exists(cursor):
                cursor.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s",
                    (version,),
                )
                applied = cursor.fetchone() is not None
            if applied:
                status = "applied"
            else:
                capture_tables = (backup_table,) + (
                    (created_table,) if created_table else ()
                )
                dirty = False
                for table_name in capture_tables:
                    if not _table_exists(cursor, table_name):
                        continue
                    cursor.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
                    if cursor.fetchone() is not None:
                        dirty = True
                        break
                status = "pending_dirty" if dirty else "pending_clean"
            print(f"{output_name}={status}")
    finally:
        connection.close()
    return 0


def report_daily_sign_single_tms_status() -> int:
    return _report_migration_capture_status(
        version=DAILY_SIGN_SINGLE_TMS_VERSION,
        backup_table=DAILY_SIGN_SINGLE_TMS_BACKUP_TABLE,
        output_name="daily_sign_single_tms_status",
    )


def report_scheduled_task_contract_upgrade_status() -> int:
    return _report_migration_capture_status(
        version=SCHEDULED_TASK_CONTRACT_UPGRADE_VERSION,
        backup_table=SCHEDULED_TASK_CONTRACT_UPGRADE_BACKUP_TABLE,
        output_name="scheduled_task_contract_upgrade_status",
        created_table=SCHEDULED_TASK_CONTRACT_UPGRADE_CREATED_TABLE,
    )


def report_automation_project_authorization_status() -> int:
    """Report applied/clean/partial state without exposing project data."""

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            applied = False
            if _migration_table_exists(cursor):
                cursor.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s",
                    (AUTOMATION_PROJECT_AUTHORIZATION_VERSION,),
                )
                applied = cursor.fetchone() is not None
            if applied:
                status = "applied"
            else:
                status = (
                    "pending_dirty"
                    if _automation_project_authorization_artifacts(cursor)
                    else "pending_clean"
                )
            print(f"automation_project_authorization_status={status}")
    finally:
        connection.close()
    return 0


def report_control_plane_policy_bootstrap_marker_status() -> int:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _require_mysql8(cursor)
            present = False
            if _table_exists(cursor, SCHEDULED_TASK_APPROVAL_EVENT_TABLE):
                cursor.execute(
                    f"""
                    SELECT 1
                    FROM {SCHEDULED_TASK_APPROVAL_EVENT_TABLE}
                    WHERE task_id=%s
                      AND request_id=%s
                      AND actor_id=%s
                      AND actor_role=%s
                      AND reason='control_plane_v1_bootstrap_complete'
                    LIMIT 1
                    """,
                    (
                        CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID,
                        CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
                        CONTROL_PLANE_MIGRATION_ACTOR_ID,
                        CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                    ),
                )
                present = cursor.fetchone() is not None
            status = "present" if present else "absent"
            print(f"control_plane_policy_bootstrap_marker_status={status}")
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
        "--restore-scheduled-task-contract-upgrade",
        action="store_true",
        help="Restore the exact scheduler rows backed up by migration 017",
    )
    modes.add_argument(
        "--restore-automation-project-authorization",
        action="store_true",
        help="Remove only migration-owned 018 state and restore its schema changes",
    )
    modes.add_argument(
        "--restore-daily-sign-single-tms-account",
        action="store_true",
        help="Restore the exact daily-sign rows backed up by migration 016",
    )
    modes.add_argument(
        "--restore-control-plane-policy-bootstrap",
        action="store_true",
        help="Remove only migration-owned one-time policy bootstrap state",
    )
    modes.add_argument(
        "--control-plane-task-cutover-status",
        action="store_true",
        help="Report whether migration 014 is pending without returning row data",
    )
    modes.add_argument(
        "--daily-sign-single-tms-status",
        action="store_true",
        help="Report applied, pending_clean, or pending_dirty for migration 016",
    )
    modes.add_argument(
        "--scheduled-task-contract-upgrade-status",
        action="store_true",
        help="Report applied, pending_clean, or pending_dirty for migration 017",
    )
    modes.add_argument(
        "--automation-project-authorization-status",
        action="store_true",
        help="Report applied, pending_clean, or pending_dirty for migration 018",
    )
    modes.add_argument(
        "--check-automation-project-required-resources",
        action="store_true",
        help=(
            "Read-only validation of the eight installation-specific resources "
            "required by migration 018"
        ),
    )
    modes.add_argument("--check-automation-project-scheduled-task-identities", action="store_true", help="Read-only validation of pre-018 scheduled task IDs and tools")
    modes.add_argument(
        "--control-plane-policy-bootstrap-marker-status",
        action="store_true",
        help="Report whether the one-time bootstrap completion marker is present",
    )
    modes.add_argument(
        "--preflight-control-plane-task-cutover",
        action="store_true",
        help="Read-only validation of reviewed scheduler rows before release mutation",
    )
    modes.add_argument(
        "--check-scheduled-write-window",
        action="store_true",
        help="Block release mutation near an exempt external-write schedule",
    )
    modes.add_argument(
        "--check-control-plane-release-manifest",
        action="store_true",
        help="Validate the legacy or post-018 project release manifest",
    )
    modes.add_argument(
        "--check-running-protected-writes",
        action="store_true",
        help="Block quiesce while an external or financial write is running",
    )
    modes.add_argument(
        "--check-automation-plugin-install-ownership",
        action="store_true",
        help="Report safe deterministic identities owned before plugin installation",
    )
    parser.add_argument("--automation-plugin-install-root")
    parser.add_argument(
        "--expect-initial-production-manifest",
        action="store_true",
        help="Require the one-time post-018 71 reviewed / 68 enabled shape",
    )
    parser.add_argument(
        "--scheduled-write-window-before-minutes",
        type=int,
        default=SCHEDULED_WRITE_WINDOW_BEFORE_MINUTES,
    )
    parser.add_argument(
        "--scheduled-write-window-after-minutes",
        type=int,
        default=SCHEDULED_WRITE_WINDOW_AFTER_MINUTES,
    )
    args = parser.parse_args()
    if (
        args.expect_initial_production_manifest
        and not args.check_control_plane_release_manifest
    ):
        parser.error(
            "--expect-initial-production-manifest requires "
            "--check-control-plane-release-manifest"
        )
    if bool(args.automation_plugin_install_root) != bool(
        args.check_automation_plugin_install_ownership
    ):
        parser.error(
            "--automation-plugin-install-root requires "
            "--check-automation-plugin-install-ownership"
        )
    if args.restore_control_plane_task_cutover:
        return restore_control_plane_task_cutover()
    if args.restore_scheduled_task_contract_upgrade:
        return restore_scheduled_task_contract_upgrade()
    if args.restore_automation_project_authorization:
        return restore_automation_project_authorization()
    if args.restore_daily_sign_single_tms_account:
        return restore_daily_sign_single_tms_account()
    if args.restore_control_plane_policy_bootstrap:
        return restore_control_plane_policy_bootstrap()
    if args.control_plane_task_cutover_status:
        return report_control_plane_task_cutover_status()
    if args.daily_sign_single_tms_status:
        return report_daily_sign_single_tms_status()
    if args.scheduled_task_contract_upgrade_status:
        return report_scheduled_task_contract_upgrade_status()
    if args.automation_project_authorization_status:
        return report_automation_project_authorization_status()
    if args.check_automation_project_required_resources:
        return check_automation_project_required_resources(_connect)
    if args.check_automation_project_scheduled_task_identities: return check_automation_project_scheduled_task_identities(_connect)
    if args.control_plane_policy_bootstrap_marker_status:
        return report_control_plane_policy_bootstrap_marker_status()
    if args.preflight_control_plane_task_cutover:
        return preflight_control_plane_task_cutover()
    if args.check_scheduled_write_window:
        return check_scheduled_write_window(
            before_minutes=args.scheduled_write_window_before_minutes,
            after_minutes=args.scheduled_write_window_after_minutes,
        )
    if args.check_control_plane_release_manifest:
        return check_control_plane_release_manifest(
            expect_initial_production_manifest=args.expect_initial_production_manifest,
        )
    if args.check_running_protected_writes:
        return check_running_protected_writes()
    if args.check_automation_plugin_install_ownership:
        return check_automation_plugin_install_ownership(
            _connect,
            args.automation_plugin_install_root,
        )
    return run(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
