"""统一把运行期资源配置落到独立 Agent MySQL。"""

import json
import os

import pymysql
from dotenv import load_dotenv


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


def _connect():
    return pymysql.connect(
        host=os.getenv("AGENT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_DB_PORT", "3306")),
        user=os.getenv("AGENT_DB_USER", "agent"),
        password=os.getenv("AGENT_DB_PASS", ""),
        database=os.getenv("AGENT_DB_NAME", "agent_db"),
        charset="utf8mb4",
        autocommit=True,
    )


def _ensure_table():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_resources (
                    resource_key VARCHAR(128) PRIMARY KEY,
                    config_json JSON NOT NULL,
                    source VARCHAR(128),
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
    finally:
        conn.close()


def upsert_workflow_resource(resource_key: str, config: dict, source: str = "manual") -> None:
    _ensure_table()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_resources (resource_key, config_json, source)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    config_json = VALUES(config_json),
                    source = VALUES(source)
                """,
                (resource_key, json.dumps(config, ensure_ascii=False), source),
            )
    finally:
        conn.close()


def get_workflow_resource(resource_key: str) -> dict | None:
    _ensure_table()
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT config_json, source, updated_at, created_at FROM workflow_resources WHERE resource_key=%s",
                (resource_key,),
            )
            row = cur.fetchone()
        if not row:
            return None
        config = json.loads(row["config_json"])
        config["_meta"] = {
            "source": row.get("source"),
            "updated_at": row["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("updated_at") else None,
            "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("created_at") else None,
        }
        return config
    finally:
        conn.close()


def list_workflow_resources() -> list[dict]:
    _ensure_table()
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT resource_key, source, updated_at, created_at FROM workflow_resources ORDER BY resource_key"
            )
            rows = cur.fetchall()
        for row in rows:
            for field in ("updated_at", "created_at"):
                if row.get(field):
                    row[field] = row[field].strftime("%Y-%m-%d %H:%M:%S")
        return rows
    finally:
        conn.close()
