from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _coerce_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    table_events: str = "r7_task_events"
    table_status: str = "r7_task_status"


def load_mysql_config(*, config_path: Optional[str] = None) -> Optional[MySQLConfig]:
    """
    Resolve MySQL config from environment variables (preferred) or optional JSON file.

    Env vars:
      - MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
      - R7 task tables are fixed by migration 006; arbitrary table-name
        overrides are intentionally unsupported.
    """

    host = _env("MYSQL_HOST")
    user = _env("MYSQL_USER")
    password = _env("MYSQL_PASSWORD")
    database = _env("MYSQL_DATABASE") or _env("MYSQL_DB")
    port = _coerce_int(_env("MYSQL_PORT"), default=3306)
    if host and user and password and database:
        return MySQLConfig(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
        )

    if not config_path:
        return None
    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None

    mysql_cfg = cfg.get("mysql") if isinstance(cfg, dict) else None
    if not isinstance(mysql_cfg, dict):
        return None

    host = str(mysql_cfg.get("host") or "").strip()
    user = str(mysql_cfg.get("user") or "").strip()
    password = str(mysql_cfg.get("password") or "").strip()
    database = str(mysql_cfg.get("database") or mysql_cfg.get("db") or "").strip()
    if not (host and user and password and database):
        return None

    port = _coerce_int(mysql_cfg.get("port"), default=3306)
    return MySQLConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
    )


class MySQLSink:
    """
    Very small MySQL sink. Uses PyMySQL if installed.

    Install:
      pip install pymysql
    """

    def __init__(self, cfg: MySQLConfig, *, create_tables: bool = False):
        self.cfg = cfg
        self.create_tables = bool(create_tables)
        self._conn = None

    def _require_driver(self):
        try:
            import pymysql  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("PyMySQL not installed. Run: pip install pymysql") from exc
        return pymysql

    def connect(self) -> None:
        if self._conn is not None:
            return
        pymysql = self._require_driver()
        self._conn = pymysql.connect(
            host=self.cfg.host,
            port=int(self.cfg.port),
            user=self.cfg.user,
            password=self.cfg.password,
            database=self.cfg.database,
            charset="utf8mb4",
            autocommit=True,
        )
        if self.create_tables:
            self.ensure_tables()

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        finally:
            self._conn = None

    def ensure_tables(self) -> None:
        self.connect()
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN (%s, %s)
                """,
                (self.cfg.table_events, self.cfg.table_status),
            )
            rows = cur.fetchall()
        names = {
            str(row.get("TABLE_NAME") or row.get("table_name") or "")
            for row in rows
        }
        required = {self.cfg.table_events, self.cfg.table_status}
        missing = sorted(required - names)
        if missing:
            raise RuntimeError(
                "Missing R7 runtime tables; run deployment migrations first: "
                + ", ".join(missing)
            )

    def insert_event(
        self,
        *,
        event_type: str,
        task_number: Optional[str] = None,
        class_name: Optional[str] = None,
        task_status: Optional[int] = None,
        task_status_name: Optional[str] = None,
        plan_go_time: Optional[str] = None,
        plan_arrive_time: Optional[str] = None,
        ok: Optional[bool] = None,
        manual_arrive_time: Optional[str] = None,
        message: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        event_ts: Optional[datetime] = None,
    ) -> None:
        self.connect()
        assert self._conn is not None
        if event_ts is None:
            event_ts = datetime.now()

        payload = json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"))
        ok_value = None if ok is None else (1 if ok else 0)

        sql = (
            f"INSERT INTO `{self.cfg.table_events}`"
            " (`event_ts`,`event_type`,`task_number`,`class_name`,`task_status`,`task_status_name`,"
            "  `plan_go_time`,`plan_arrive_time`,`ok`,`manual_arrive_time`,`message`,`detail_json`)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        with self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    event_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    str(event_type),
                    task_number,
                    class_name,
                    task_status,
                    task_status_name,
                    plan_go_time,
                    plan_arrive_time,
                    ok_value,
                    manual_arrive_time,
                    message,
                    payload,
                ),
            )

    def upsert_status(
        self,
        *,
        task_number: str,
        class_name: Optional[str],
        task_status: Optional[int],
        task_status_name: Optional[str],
        plan_go_time: Optional[str],
        plan_arrive_time: Optional[str],
        checkin_success: bool,
        manual_arrive_time: Optional[str],
        detail: Optional[Dict[str, Any]] = None,
        seen_ts: Optional[datetime] = None,
    ) -> None:
        self.connect()
        assert self._conn is not None
        if seen_ts is None:
            seen_ts = datetime.now()

        payload = json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"))
        checkin_val = 1 if checkin_success else 0
        sql = (
            f"INSERT INTO `{self.cfg.table_status}`"
            " (`task_number`,`class_name`,`task_status`,`task_status_name`,`plan_go_time`,`plan_arrive_time`,"
            "  `last_seen_ts`,`checkin_success`,`manual_arrive_time`,`detail_json`)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE"
            " `class_name`=VALUES(`class_name`),"
            " `task_status`=VALUES(`task_status`),"
            " `task_status_name`=VALUES(`task_status_name`),"
            " `plan_go_time`=VALUES(`plan_go_time`),"
            " `plan_arrive_time`=VALUES(`plan_arrive_time`),"
            " `last_seen_ts`=VALUES(`last_seen_ts`),"
            " `checkin_success`=GREATEST(`checkin_success`, VALUES(`checkin_success`)),"
            " `manual_arrive_time`=COALESCE(VALUES(`manual_arrive_time`), `manual_arrive_time`),"
            " `detail_json`=VALUES(`detail_json`)"
        )
        with self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    str(task_number),
                    class_name,
                    task_status,
                    task_status_name,
                    plan_go_time,
                    plan_arrive_time,
                    seen_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    checkin_val,
                    manual_arrive_time,
                    payload,
                ),
            )
