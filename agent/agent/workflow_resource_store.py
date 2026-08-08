"""统一把运行期资源配置落到独立 Agent MySQL。"""

import os

import pymysql
from dotenv import load_dotenv

from shared.runtime_repositories import WorkflowResourceRepository


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


def _repository() -> WorkflowResourceRepository:
    return WorkflowResourceRepository(_connect, cursor_factory=pymysql.cursors.DictCursor)


def upsert_workflow_resource(resource_key: str, config: dict, source: str = "manual") -> None:
    _repository().upsert(resource_key, config, source=source)


def get_workflow_resource(resource_key: str) -> dict | None:
    row = _repository().get_record(resource_key)
    if not row:
        return None
    config = dict(row.get("config") or {})
    config["_meta"] = {
        "source": row.get("source"),
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
    }
    return config


def list_workflow_resources() -> list[dict]:
    return _repository().list_records(include_config=False)
