"""Published customer details and independent operator fields on shared MySQL."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Mapping, Sequence
from datetime import datetime

from shared.data_sources import DataSourceError, DataSourceRepository, required_text, row_dict


SOURCE_FIELDS = frozenset({"platform", "source_direction", "external_id", "waybill_no", "status",
    "problem_type", "problem_text", "reply_text", "created_at", "registered_at",
    "registration_saved_at", "registered_site", "updated_at", "resolved", "resolution_reason", "queue_included"})


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise DataSourceError("SOURCE_TIMESTAMP_INVALID")
    return value.isoformat(timespec="microseconds")


class CustomerServiceRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.sources = DataSourceRepository(connection)

    def publish(self, *, source_id: str, producer_instance_id: str, producer_generation: int,
                source_revision: int, run_id: str, records: Sequence[Mapping[str, Any]],
                pagination_complete: bool, require_runtime_provenance: bool = False) -> dict[str, Any]:
        if pagination_complete is not True:
            raise DataSourceError("CUSTOMER_SNAPSHOT_INCOMPLETE")
        source = self.sources.assert_producer(source_id, producer_instance_id=producer_instance_id,
            producer_generation=producer_generation, revision=source_revision)
        if source["module"] != "customer_service":
            raise DataSourceError("SOURCE_MODULE_INVALID")
        producer_snapshot = self.sources.producer_snapshot(producer_instance_id, producer_generation,
            required=require_runtime_provenance)
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in records:
            if not isinstance(raw, Mapping):
                raise DataSourceError("CUSTOMER_RECORD_INVALID")
            row = {key: value for key, value in raw.items() if key in SOURCE_FIELDS}
            external_id = required_text(row.get("external_id"), "external_id", 256)
            direction = required_text(row.get("source_direction"), "source_direction", 32)
            if row.get("platform") != source["provider"] or direction not in {"received", "registered", "query", "published"}:
                raise DataSourceError("CUSTOMER_SOURCE_MISMATCH")
            if not isinstance(row.get("resolved"), bool):
                raise DataSourceError("CUSTOMER_RESOLUTION_MISSING")
            key = (external_id, direction)
            if key in rows:
                raise DataSourceError("CUSTOMER_DUPLICATE_IDENTITY")
            rows[key] = row
        digest = hashlib.sha256(json.dumps([rows[key] for key in sorted(rows)], sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        run = required_text(run_id, "run_id")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT * FROM customer_problem_publications WHERE source_id=%s AND run_id=%s", (source_id, run))
            previous = row_dict(cursor, cursor.fetchone())
            if previous:
                if previous["content_sha256"] != digest:
                    raise DataSourceError("CUSTOMER_RUN_REPLAY_CHANGED")
                return previous
            publication_id = str(uuid.uuid4())
            cursor.execute("""INSERT INTO customer_problem_publications
                (publication_id,source_id,run_id,producer_instance_id,producer_generation,source_revision,
                 record_count,content_sha256,published_at,producer_snapshot_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6),%s)""",
                (publication_id, source_id, run, producer_instance_id, producer_generation, source_revision, len(rows), digest,
                 json.dumps(producer_snapshot) if producer_snapshot is not None else None))
            for (external_id, direction), row in rows.items():
                cursor.execute("""INSERT INTO customer_problem_records
                    (source_id,external_id,source_direction,publication_id,waybill_no,source_status,resolved,
                     source_updated_at,source_json,first_published_at,last_published_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))
                    ON DUPLICATE KEY UPDATE publication_id=VALUES(publication_id),waybill_no=VALUES(waybill_no),
                    source_status=VALUES(source_status),resolved=VALUES(resolved),source_updated_at=VALUES(source_updated_at),
                    source_json=VALUES(source_json),last_published_at=VALUES(last_published_at)""",
                    (source_id, external_id, direction, publication_id, str(row.get("waybill_no") or ""),
                     str(row.get("status") or ""), int(row["resolved"]), str(row.get("updated_at") or ""),
                     json.dumps(row, ensure_ascii=False)))
            # Disappearing records remain until the existing exact detail recheck
            # supplies explicit resolution; an empty list never deletes history.
            cursor.execute("""UPDATE module_data_sources SET latest_publication_id=%s,
                latest_published_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6) WHERE source_id=%s""", (publication_id, source_id))
            cursor.execute("SELECT COUNT(*) AS n FROM customer_problem_records WHERE publication_id=%s", (publication_id,))
            observed = row_dict(cursor, cursor.fetchone())
            if observed is None or int(observed["n"]) != len(rows):
                raise DataSourceError("CUSTOMER_PUBLISH_ROW_COUNT_MISMATCH")
        return {"publication_id": publication_id, "source_id": source_id, "record_count": len(rows), "content_sha256": digest}

    def query(self, *, source_ids: Sequence[str] = (), account_ids: Sequence[str] = (),
              platform: str = "", direction: str = "", keyword: str = "",
              start_date: str = "", end_date: str = "", limit: int = 200, offset: int = 0) -> dict[str, Any]:
        if isinstance(limit, bool) or not 1 <= limit <= 1000 or offset < 0:
            raise DataSourceError("CUSTOMER_PAGE_INVALID")
        clauses, params = ["s.module='customer_service'",
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(r.source_json,'$.queue_included')),'true')='true'"], []
        if source_ids:
            selected = tuple(dict.fromkeys(required_text(value, "source_id", 36) for value in source_ids))
            clauses.append("s.source_id IN (" + ",".join(["%s"] * len(selected)) + ")")
            params.extend(selected)
        if account_ids:
            accounts = tuple(dict.fromkeys(required_text(value, "account_id") for value in account_ids))
            clauses.append("EXISTS(SELECT 1 FROM module_data_source_accounts a WHERE a.source_id=s.source_id AND a.account_id IN (" + ",".join(["%s"] * len(accounts)) + "))")
            params.extend(accounts)
        if platform:
            if platform not in {"ronghui", "yunda"}:
                raise DataSourceError("SOURCE_PROVIDER_UNSUPPORTED")
            clauses.append("s.provider=%s")
            params.append(platform)
        if direction:
            directions = {"published_to_me": ("received", "query"), "my_published": ("registered", "published")}
            values = directions.get(direction, (direction,))
            if any(value not in {"received", "query", "registered", "published"} for value in values):
                raise DataSourceError("CUSTOMER_DIRECTION_INVALID")
            clauses.append("r.source_direction IN (" + ",".join(["%s"] * len(values)) + ")")
            params.extend(values)
        if keyword:
            clauses.append("(r.waybill_no LIKE %s OR r.external_id LIKE %s OR JSON_UNQUOTE(JSON_EXTRACT(r.source_json,'$.problem_text')) LIKE %s)")
            params.extend(["%" + required_text(keyword, "keyword", 100) + "%"] * 3)
        if start_date:
            clauses.append("r.source_updated_at >= %s")
            params.append(start_date)
        if end_date:
            clauses.append("r.source_updated_at <= %s")
            params.append(end_date + " 23:59:59")
        base = """FROM customer_problem_records r JOIN module_data_sources s ON s.source_id=r.source_id
            LEFT JOIN customer_problem_manual_fields m ON m.source_id=r.source_id
             AND m.external_id=r.external_id AND m.source_direction=r.source_direction WHERE """ + " AND ".join(clauses)
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS row_count,COALESCE(SUM(r.resolved=0),0) AS open_count,COALESCE(SUM(r.resolved=1),0) AS resolved_count " + base, tuple(params))
            stats = row_dict(cursor, cursor.fetchone())
            stats = {name: int(stats.get(name) or 0) for name in ("row_count", "open_count", "resolved_count")}
            cursor.execute("SELECT r.source_json,r.source_id,s.display_name,s.status AS source_state,s.latest_published_at,m.note,m.assigned_to " + base + " ORDER BY r.source_updated_at DESC,r.source_id,r.external_id,r.source_direction LIMIT %s OFFSET %s", (*params, limit, offset))
            raw_results = [row_dict(cursor, row) for row in cursor.fetchall()]
            aliases_sql = "SELECT source_id,account_id FROM module_data_source_accounts WHERE module='customer_service'"
            aliases_params = []
            if account_ids:
                aliases_sql += " AND account_id IN (" + ",".join(["%s"] * len(account_ids)) + ")"
                aliases_params.extend(account_ids)
            cursor.execute(aliases_sql, tuple(aliases_params))
            aliases_by_source = {}
            for alias_value in cursor.fetchall():
                alias = row_dict(cursor, alias_value)
                aliases_by_source.setdefault(alias["source_id"], []).append(alias["account_id"])
            rows = []
            for value in raw_results:
                result = row_dict(cursor, value)
                source_json = result.pop("source_json")
                result["latest_published_at"] = _timestamp(result["latest_published_at"])
                aliases = aliases_by_source.get(result["source_id"], [])
                data = {**(json.loads(source_json) if isinstance(source_json, str) else source_json), **result}
                data["account_label"] = result["display_name"]
                if len(aliases) == 1:
                    data["account_id"] = aliases[0]
                else:
                    data["action_blocked_reason"] = "来源绑定多个账号，请按账号筛选后处理。"
                rows.append(data)
        statuses, errors = [], []
        for source in self.sources.list_sources("customer_service"):
            if source_ids and source["source_id"] not in source_ids:
                continue
            if account_ids and source["source_id"] not in aliases_by_source:
                continue
            if platform and source["provider"] != platform:
                continue
            status = {"source_id": source["source_id"], "source_label": source["display_name"],
                "source_state": source["status"], "collection_status": source["latest_collection_status"],
                "last_attempt_at": _timestamp(source["latest_collection_at"]), "last_published_at": _timestamp(source["latest_published_at"])}
            statuses.append(status)
            code = str(source["latest_collection_error_code"] or "")
            if code or str(source["latest_collection_status"]).startswith("FAILED"):
                errors.append({**status, "error_code": code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "COLLECTION_FAILED",
                    "message": "最近采集未成功；已发布历史仍可查询。"})
        if errors:
            stats["error_count"] = len(errors)
        return {"ok": True, "rows": rows, "stats": stats, "errors": errors,
            "source_statuses": statuses, "query_source": "published_local"}
