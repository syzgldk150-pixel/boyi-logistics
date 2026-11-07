"""Finance sources and the bill files connectors parse (P4).

A finance source names exactly one producer plugin instance; only that
instance may list the source, read its pending files or submit batches for
it. Files are stored in chunks in ``finance_db`` so Console (upload) and Agent
(connector reads) share no file system. A file is readable only while
``pending``; the accepted batch that cites it marks it ``consumed``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any

from shared.finance.db import fetchall, fetchone
from shared.finance.errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from shared.finance.models import FinanceActor, expected_revision, optional_text, parse_account_code, parse_request_id

MAX_FILE_BYTES = 20 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MAX_READ_BYTES = 4 * 1024 * 1024
FUND_ACCOUNT_TYPES = ("cash", "bank", "fund")
_SOURCE_KEY = re.compile(r"^[a-z0-9][a-z0-9_]{1,63}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_]{1,31}$")
_INSTANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SOURCE_COLUMNS = (
    "id, source_key, display_name, kind, provider, producer_instance_id, fund_account, is_enabled, remark, revision,"
    " created_by, created_at"
)


def _time(value: Any) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if isinstance(value, dt.datetime) else None


def source_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**{key: row[key] for key in ("source_key", "display_name", "kind", "provider", "producer_instance_id",
                                          "fund_account", "remark", "created_by")},
            "id": int(row["id"]), "is_enabled": bool(row["is_enabled"]), "revision": int(row["revision"]),
            "created_at": _time(row.get("created_at"))}


def file_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": int(row["id"]), "source_id": int(row["source_id"]), "file_name": row["file_name"],
            "content_type": row["content_type"], "size_bytes": int(row["size_bytes"]), "sha256": row["sha256"],
            "status": row["status"], "batch_id": row["batch_id"], "uploaded_by": row["uploaded_by"],
            "uploaded_at": _time(row.get("uploaded_at"))}


class SourcesMixin:
    # ---------- sources (structure: super administrator) ----------
    @staticmethod
    def _source_row(cursor: Any, source_id: int, *, lock: str = "") -> dict[str, Any]:
        cursor.execute(f"SELECT {SOURCE_COLUMNS} FROM finance_sources WHERE id=%s {lock}", (source_id,))
        row = fetchone(cursor)
        if row is None:
            raise FinanceNotFoundError("财务来源不存在。", code="SOURCE_NOT_FOUND")
        return row

    def _source_fields(self, cursor: Any, payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if not partial or "source_key" in payload:
            key = str(payload.get("source_key") or "").strip()
            if not _SOURCE_KEY.fullmatch(key):
                raise FinanceValidationError("来源代码只能用小写字母、数字和下划线（2–64 位）。")
            fields["source_key"] = key
        if not partial or "display_name" in payload:
            name = optional_text(payload.get("display_name"), "来源名称", 128)
            if not name:
                raise FinanceValidationError("来源名称不能为空。")
            fields["display_name"] = name
        if not partial or "kind" in payload:
            if payload.get("kind") not in ("online", "file"):
                raise FinanceValidationError("接入方式必须是在线抓取或文件导入。")
            fields["kind"] = payload["kind"]
        if not partial or "provider" in payload:
            provider = str(payload.get("provider") or "").strip()
            if not _PROVIDER.fullmatch(provider):
                raise FinanceValidationError("提供方代码只能用小写字母、数字和下划线（例如 ronghui、wechat、bank_icbc）。")
            fields["provider"] = provider
        if "producer_instance_id" in payload:
            instance = str(payload.get("producer_instance_id") or "").strip()
            if instance and not _INSTANCE.fullmatch(instance):
                raise FinanceValidationError("连接器实例编号无效。")
            fields["producer_instance_id"] = instance or None
        if "fund_account" in payload:
            code = payload.get("fund_account")
            if code:
                code = parse_account_code(code)
                cursor.execute("SELECT account_type, is_group, is_active FROM finance_accounts WHERE code=%s FOR SHARE", (code,))
                account = fetchone(cursor)
                if not account or account["is_group"] or not account["is_active"] or account["account_type"] not in FUND_ACCOUNT_TYPES:
                    raise FinanceValidationError("资金科目必须是已启用的末级现金、银行或其他货币资金科目。", code="SOURCE_FUND_ACCOUNT_INVALID")
            fields["fund_account"] = code or None
        if "is_enabled" in payload:
            if not isinstance(payload["is_enabled"], bool):
                raise FinanceValidationError("启用状态必须是开关值。")
            fields["is_enabled"] = int(payload["is_enabled"])
        if "remark" in payload:
            fields["remark"] = optional_text(payload.get("remark"), "备注", 255) or ""
        return fields

    def list_sources(self) -> list[dict[str, Any]]:
        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(
                f"SELECT {SOURCE_COLUMNS},"
                " (SELECT COUNT(*) FROM finance_source_files f WHERE f.source_id=s.id AND f.status='pending') AS pending_files,"
                " (SELECT MAX(b.period_end) FROM finance_source_batches b WHERE b.source_id=s.id AND b.status='accepted') AS covered_until"
                " FROM finance_sources s ORDER BY s.source_key"
            )
            return [{**source_view(row), "pending_files": int(row["pending_files"]),
                     "covered_until": row["covered_until"].isoformat() if row["covered_until"] else None}
                    for row in fetchall(cursor)]

        return self._read(work)

    def create_source(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        allowed = {"source_key", "display_name", "kind", "provider", "producer_instance_id", "fund_account", "remark"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise FinanceValidationError("来源内容包含不支持的字段。")

        def work(cursor: Any) -> dict[str, Any]:
            fields = self._source_fields(cursor, payload, partial=False)
            cursor.execute("SELECT id FROM finance_sources WHERE source_key=%s FOR UPDATE", (fields["source_key"],))
            if cursor.fetchone():
                raise FinanceError("SOURCE_EXISTS", "来源代码已存在。")
            columns = [*fields, "created_by"]
            cursor.execute(f"INSERT INTO finance_sources ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
                           (*fields.values(), actor.label))
            return source_view(self._source_row(cursor, int(cursor.lastrowid)))

        return self._write(request_id=request_id, action="source.create", target_type="source", target_id="new",
                           payload=payload, actor=actor, work=work)

    def update_source(self, source_id: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        source_id = int(source_id)
        allowed = {"display_name", "producer_instance_id", "fund_account", "is_enabled", "remark", "expected_revision"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise FinanceValidationError("来源修改包含不支持的字段（来源代码、接入方式和提供方建立后不能修改）。")
        revision = expected_revision(payload.get("expected_revision"))

        def work(cursor: Any) -> dict[str, Any]:
            current = self._source_row(cursor, source_id, lock="FOR UPDATE")
            if int(current["revision"]) != revision:
                raise FinanceError("FINANCE_REVISION_CONFLICT", "来源已被他人修改，请刷新后重试。")
            fields = self._source_fields(cursor, {k: v for k, v in payload.items() if k != "expected_revision"}, partial=True)
            if not fields:
                raise FinanceValidationError("没有修改。")
            assignments = ", ".join(f"{key}=%s" for key in fields)
            cursor.execute(f"UPDATE finance_sources SET {assignments}, revision=revision+1 WHERE id=%s", (*fields.values(), source_id))
            return source_view(self._source_row(cursor, source_id))

        return self._write(request_id=request_id, action="source.update", target_type="source", target_id=source_id,
                           payload=payload, actor=actor, work=work)

    # ---------- files ----------
    def upload_source_file(
        self, source_id: Any, *, file_name: Any, content_type: Any, data: bytes, request_id: Any, actor: FinanceActor,
    ) -> dict[str, Any]:
        source_id = int(source_id)
        name = optional_text(file_name, "文件名", 255)
        if not name or "/" in name or "\\" in name:
            raise FinanceValidationError("文件名无效。")
        kind = optional_text(content_type, "文件类型", 128) or "application/octet-stream"
        if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_FILE_BYTES:
            raise FinanceValidationError(f"文件不能为空，且不能超过 {MAX_FILE_BYTES // 1024 // 1024}MB。", code="SOURCE_FILE_SIZE")
        digest = hashlib.sha256(data).hexdigest()
        canonical = parse_request_id(request_id)

        def work(cursor: Any) -> dict[str, Any]:
            source = self._source_row(cursor, source_id, lock="FOR SHARE")
            if source["kind"] != "file" or not source["is_enabled"]:
                raise FinanceError("SOURCE_NOT_FILE", "该来源不是启用中的文件导入来源。")
            cursor.execute("SELECT id FROM finance_source_files WHERE source_id=%s AND sha256=%s AND status<>'discarded'",
                           (source_id, digest))
            if cursor.fetchone():
                raise FinanceError("SOURCE_FILE_DUPLICATE", "同一份文件已经上传过。")
            chunks = [bytes(data[i:i + CHUNK_BYTES]) for i in range(0, len(data), CHUNK_BYTES)]
            cursor.execute(
                "INSERT INTO finance_source_files (source_id, file_name, content_type, size_bytes, sha256, chunk_count, request_id,"
                " uploaded_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (source_id, name, kind, len(data), digest, len(chunks), canonical, actor.label),
            )
            file_id = int(cursor.lastrowid)
            for index, chunk in enumerate(chunks):
                cursor.execute("INSERT INTO finance_source_file_chunks (file_id, chunk_no, data) VALUES (%s, %s, %s)",
                               (file_id, index, chunk))
            cursor.execute("SELECT * FROM finance_source_files WHERE id=%s", (file_id,))
            return file_view(fetchone(cursor))

        return self._write(request_id=canonical, action="source_file.upload", target_type="source", target_id=source_id,
                           payload={"file_name": name, "sha256": digest, "size": len(data)}, actor=actor, work=work)

    def list_source_files(self, source_id: Any) -> list[dict[str, Any]]:
        source_id = int(source_id)

        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute("SELECT * FROM finance_source_files WHERE source_id=%s ORDER BY id DESC LIMIT 200", (source_id,))
            return [file_view(row) for row in fetchall(cursor)]

        return self._read(work)

    def discard_source_file(self, file_id: Any, *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        file_id = int(file_id)

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT * FROM finance_source_files WHERE id=%s FOR UPDATE", (file_id,))
            row = fetchone(cursor)
            if row is None:
                raise FinanceNotFoundError("文件不存在。", code="SOURCE_FILE_NOT_FOUND")
            if row["status"] != "pending":
                raise FinanceError("SOURCE_FILE_NOT_PENDING", "只有待解析的文件可以作废。")
            cursor.execute("UPDATE finance_source_files SET status='discarded' WHERE id=%s", (file_id,))
            return {"id": file_id, "status": "discarded"}

        return self._write(request_id=request_id, action="source_file.discard", target_type="source_file", target_id=file_id,
                           payload={}, actor=actor, work=work)

    # ---------- connector-facing (called by the Agent Host handlers) ----------
    def producer_sources(self, automation_id: str) -> list[dict[str, Any]]:
        """Enabled sources this plugin instance produces, with their pending files."""

        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(f"SELECT {SOURCE_COLUMNS} FROM finance_sources WHERE producer_instance_id=%s AND is_enabled=1 ORDER BY source_key",
                           (automation_id,))
            sources = fetchall(cursor)
            result = []
            for source in sources:
                cursor.execute("SELECT * FROM finance_source_files WHERE source_id=%s AND status='pending' ORDER BY id",
                               (source["id"],))
                files = [{key: value for key, value in file_view(row).items() if key in ("id", "file_name", "content_type", "size_bytes", "sha256", "uploaded_at")}
                         for row in fetchall(cursor)]
                cursor.execute("SELECT MAX(period_end) AS covered FROM finance_source_batches WHERE source_id=%s AND status='accepted'",
                               (source["id"],))
                covered = fetchone(cursor)["covered"]
                result.append({"source_key": source["source_key"], "display_name": source["display_name"], "kind": source["kind"],
                               "provider": source["provider"], "covered_until": covered.isoformat() if covered else None,
                               "pending_files": files})
            return result

        return self._read(work)

    def read_source_file(self, automation_id: str, file_id: int, offset: int, length: int) -> dict[str, Any]:
        """A slice of one pending file of a source this instance produces (read-only, bounded)."""

        if offset < 0 or not 1 <= length <= MAX_READ_BYTES:
            raise FinanceValidationError("读取范围无效。", code="SOURCE_FILE_RANGE")

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute(
                "SELECT f.*, s.producer_instance_id, s.is_enabled FROM finance_source_files f"
                " JOIN finance_sources s ON s.id=f.source_id WHERE f.id=%s", (file_id,))
            row = fetchone(cursor)
            if row is None or row["producer_instance_id"] != automation_id or not row["is_enabled"]:
                raise FinanceError("SOURCE_FILE_FORBIDDEN", "该文件不属于本连接器负责的来源。", http_status=403)
            if row["status"] != "pending":
                raise FinanceError("SOURCE_FILE_NOT_PENDING", "文件已被使用或作废，不能再读取。")
            size = int(row["size_bytes"])
            if offset >= size:
                return {"file_id": file_id, "offset": offset, "size_bytes": size, "sha256": row["sha256"], "data": b"", "eof": True}
            end = min(size, offset + length)
            first, last = offset // CHUNK_BYTES, (end - 1) // CHUNK_BYTES
            cursor.execute("SELECT chunk_no, data FROM finance_source_file_chunks WHERE file_id=%s AND chunk_no BETWEEN %s AND %s"
                           " ORDER BY chunk_no", (file_id, first, last))
            blob = b"".join(bytes(item["data"]) for item in fetchall(cursor))
            start = offset - first * CHUNK_BYTES
            data = blob[start:start + (end - offset)]
            if len(data) != end - offset:
                raise FinanceError("SOURCE_FILE_CORRUPT", "文件分块不完整。", http_status=500)
            return {"file_id": file_id, "offset": offset, "size_bytes": size, "sha256": row["sha256"], "data": data, "eof": end >= size}

        return self._read(work)
