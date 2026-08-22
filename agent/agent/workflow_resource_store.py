"""统一把运行期资源配置落到独立 Agent MySQL。"""

import os
import re
from collections.abc import Mapping

import pymysql

from shared.runtime_repositories import WorkflowResourceRepository


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        "resource_key": row.get("resource_key"),
        "source": row.get("source"),
        "configuration_version": row.get("configuration_version"),
        "config_sha256": row.get("config_sha256"),
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
    }
    return config


def list_workflow_resources() -> list[dict]:
    return _repository().list_records(include_config=False)


def list_workflow_resource_descriptors() -> list[dict[str, str]]:
    """Return the closed, non-secret resource pool used by plugin settings.

    Runtime bindings still resolve the complete record through
    :func:`get_workflow_resource`.  The management projection deliberately
    keeps only the exact identifier, a human-readable label and the signed
    resource kind; tokens, table IDs, paths, revision hashes and raw config
    never cross this boundary.
    """

    descriptors: list[dict[str, str]] = []
    for row in _repository().list_records(include_config=True):
        config = row.get("config")
        if not isinstance(config, Mapping):
            continue
        resource_id = str(row.get("resource_key") or "").strip()
        resource_kind = str(config.get("resource_kind") or "").strip().lower()
        source = str(row.get("source") or "").strip()
        configuration_version = row.get("configuration_version")
        config_sha256 = str(row.get("config_sha256") or "").strip().lower()
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", resource_id)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", resource_kind)
            or not source
            or type(configuration_version) is not int
            or configuration_version <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", config_sha256)
        ):
            continue
        raw_name = config.get("display_name") or config.get("name") or config.get("title")
        name = str(raw_name or resource_id).strip()
        if not name or len(name) > 160:
            name = resource_id
        descriptors.append(
            {
                "resource_id": resource_id,
                "name": name,
                "kind": resource_kind,
                "status": "available",
            }
        )
    return descriptors
