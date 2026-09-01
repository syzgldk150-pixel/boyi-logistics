"""统一把运行期资源配置落到独立 Agent MySQL。"""

import os
import re
from collections.abc import Mapping

import pymysql

from shared.runtime_repositories import WorkflowResourceRepository


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WorkflowResourceCatalog(list):
    """List-compatible resource projection with one global health signal."""

    def __init__(
        self,
        values: list[dict[str, str]],
        *,
        resource_pool_available: bool,
        resource_pool_problem: str = "",
    ) -> None:
        super().__init__(values)
        self.resource_pool_available = bool(resource_pool_available)
        self.resource_pool_problem = str(resource_pool_problem or "")


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


def list_workflow_resource_descriptors() -> WorkflowResourceCatalog:
    """Return the closed, non-secret resource pool used by plugin settings.

    Runtime bindings still resolve the complete record through
    :func:`get_workflow_resource`.  The management projection deliberately
    keeps only the exact identifier, a human-readable label and the signed
    resource kind; tokens, table IDs, paths, revision hashes and raw config
    never cross this boundary.
    """

    rows = _repository().list_records(include_config=True)
    feishu_resources: list[tuple[str, Mapping[str, object]]] = []
    for row in rows:
        config = row.get("config")
        if not isinstance(config, Mapping):
            continue
        resource_id = str(row.get("resource_key") or "").strip()
        resource_kind = str(config.get("resource_kind") or "").strip().lower()
        if resource_kind in {"feishu_sheet", "feishu_bitable"} and resource_id:
            feishu_resources.append((resource_id, config))

    live_resources: dict[str, object] = {}
    global_problem = ""
    if feishu_resources:
        from agent.feishu_resource_catalog import resolve_live_feishu_resource_catalog

        live_catalog = resolve_live_feishu_resource_catalog(feishu_resources)
        live_resources = dict(live_catalog.resources)
        global_problem = live_catalog.global_problem

    descriptors: list[dict[str, str]] = []
    for row in rows:
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
        live = live_resources.get(resource_id)
        is_feishu = resource_kind in {"feishu_sheet", "feishu_bitable"}
        raw_name = (
            getattr(live, "name", "")
            if is_feishu
            else config.get("display_name") or config.get("name") or config.get("title")
        )
        name = str(raw_name or "").strip()
        purpose = str(
            getattr(live, "purpose", "")
            or config.get("display_name")
            or config.get("name")
            or config.get("title")
            or "业务数据"
        ).strip()[:80]
        status = str(getattr(live, "status", "available") or "").strip().lower()
        problem_code = str(getattr(live, "problem_code", "") or "").strip().upper()
        if (
            status not in {"available", "unavailable"}
            or (status == "available" and (not name or len(name) > 160))
            or (status == "unavailable" and name)
            or not purpose
        ):
            continue
        descriptors.append(
            {
                "resource_id": resource_id,
                "name": name,
                "kind": resource_kind,
                "status": status,
                "purpose": purpose,
                "problem_code": problem_code,
            }
        )
    return WorkflowResourceCatalog(
        descriptors,
        resource_pool_available=not bool(global_problem),
        resource_pool_problem=global_problem,
    )
