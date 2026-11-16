"""Persistence for 业务手册 → 计算规则 (migration 087) over a caller-supplied connection factory.

Never reads the environment or runs DDL. Every change is one short transaction
that locks the version row first and checks ``row_version``, so two people
cannot overwrite each other. Approval re-validates the stored content inside the
locked transaction and refuses a second approved version of the same fee with
the same effective date. Approved versions are never edited.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Any, Callable, Iterator, Mapping

from shared.handbook_fee_rules import FEE_KINDS, FeeRuleError, approval_blockers, parse_content

ConnectionFactory = Callable[[], Any]
_LABEL_LIMIT = 60
_CHECK_LIMIT = 30


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def normalize_checks(checks: Any) -> list[dict[str, Any]]:
    if not isinstance(checks, list) or len(checks) > _CHECK_LIMIT:
        raise FeeRuleError("FEE_RULE_CHECKS_INVALID", f"待核对事项最多 {_CHECK_LIMIT} 条。")
    result = []
    for item in checks:
        text = item.get("text") if isinstance(item, Mapping) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "每条待核对事项要填写内容，且不超过 300 字。")
        result.append({"text": text.strip(), "done": item.get("done") is True})
    return result


def normalize_label(label: Any) -> str:
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > _LABEL_LIMIT:
        raise FeeRuleError("FEE_RULE_LABEL_INVALID", f"版本名称必须填写，且不超过 {_LABEL_LIMIT} 字。")
    return label.strip()


def normalize_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise FeeRuleError("FEE_RULE_DATE_INVALID", "执行日期格式无效。") from exc


class FeeRuleRepository:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        resource = self._connection_factory()
        managed = hasattr(resource, "__enter__") and hasattr(resource, "__exit__")
        connection = resource.__enter__() if managed else resource
        begin = getattr(connection, "begin", None)
        if callable(begin):
            begin()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            if managed:
                resource.__exit__(None, None, None)
            else:
                resource.close()

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        names = [column[0] for column in cursor.description or ()]
        return [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]

    @staticmethod
    def _record(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"rule_id": row["rule_id"], "fee_kind": row["fee_kind"], "fee_name": FEE_KINDS.get(row["fee_kind"], row["fee_kind"]),
                "version_label": row["version_label"], "status": row["status"],
                "effective_from": _iso(row["effective_from"]), "content": _load(row["content_json"]),
                "checks": _load(row["checks_json"]) or [], "based_on": row["based_on"],
                "created_by": row["created_by"], "updated_by": row["updated_by"],
                "submitted_by": row["submitted_by"], "submitted_at": _iso(row["submitted_at"]),
                "approved_by": row["approved_by"], "approved_at": _iso(row["approved_at"]),
                "retired_by": row["retired_by"], "retired_at": _iso(row["retired_at"]),
                "row_version": int(row["row_version"]), "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"])}

    @staticmethod
    def _event(cursor: Any, rule_id: str, action: str, actor_id: str, detail: Any = None) -> None:
        cursor.execute("INSERT INTO agent_fee_rule_events (rule_id, action, actor_id, detail_json) VALUES (%s, %s, %s, %s)",
                       (rule_id, action, actor_id, None if detail is None else _dump(detail)))

    # -- reads ------------------------------------------------------------------------

    def list_versions(self) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_fee_rule_versions WHERE status <> 'RETIRED'"
                           " ORDER BY fee_kind, (status='APPROVED') DESC, effective_from DESC, updated_at DESC LIMIT 200")
            return [self._record(row) for row in self._rows(cursor)]

    def get(self, rule_id: str) -> dict[str, Any]:
        with self._cursor() as cursor:
            record = self._get(cursor, rule_id, lock=False)
            cursor.execute("SELECT action, actor_id, detail_json, created_at FROM agent_fee_rule_events"
                           " WHERE rule_id=%s ORDER BY event_id DESC LIMIT 50", (rule_id,))
            record["events"] = [{"action": row["action"], "actor_id": row["actor_id"], "detail": _load(row["detail_json"]),
                                 "created_at": _iso(row["created_at"])} for row in self._rows(cursor)]
        return record

    def approved(self, fee_kind: str) -> list[dict[str, Any]]:
        """Approved versions of one fee, for calculations (choose with select_rule by billing date)."""
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_fee_rule_versions WHERE fee_kind=%s AND status='APPROVED'"
                           " ORDER BY effective_from", (fee_kind,))
            return [self._record(row) for row in self._rows(cursor)]

    # -- writes -----------------------------------------------------------------------

    def _get(self, cursor: Any, rule_id: str, *, lock: bool) -> dict[str, Any]:
        cursor.execute("SELECT * FROM agent_fee_rule_versions WHERE rule_id=%s" + (" FOR UPDATE" if lock else ""), (rule_id,))
        rows = self._rows(cursor)
        if not rows:
            raise FeeRuleError("FEE_RULE_NOT_FOUND", "没有找到这个规则版本。")
        return self._record(rows[0])

    def _lock(self, cursor: Any, rule_id: str, expected_row_version: int, statuses: tuple[str, ...]) -> dict[str, Any]:
        record = self._get(cursor, rule_id, lock=True)
        if record["row_version"] != expected_row_version:
            raise FeeRuleError("FEE_RULE_CONFLICT", "这个版本刚被别人修改过，请刷新后再操作。")
        if record["status"] not in statuses:
            raise FeeRuleError("FEE_RULE_STATE", "当前状态不能进行这个操作，请刷新后再看。")
        return record

    def copy_to_draft(self, rule_id: str, *, actor_id: str) -> str:
        """Start a new draft from any version; approved versions themselves stay unchanged."""
        new_id = str(uuid.uuid4())
        with self._cursor() as cursor:
            source = self._get(cursor, rule_id, lock=False)
            label = normalize_label((source["version_label"] + "-修改")[:_LABEL_LIMIT])
            checks = [{"text": item["text"], "done": False} for item in source["checks"]]
            cursor.execute("INSERT INTO agent_fee_rule_versions (rule_id, fee_kind, version_label, status, effective_from,"
                           " content_json, checks_json, based_on, created_by, updated_by)"
                           " VALUES (%s, %s, %s, 'DRAFT', NULL, %s, %s, %s, %s, %s)",
                           (new_id, source["fee_kind"], label, _dump(source["content"]), _dump(checks), rule_id,
                            actor_id, actor_id))
            self._event(cursor, new_id, "COPY", actor_id, {"from": rule_id})
        return new_id

    def save_draft(self, rule_id: str, *, version_label: Any, effective_from: Any, content: Any, checks: Any,
                   actor_id: str, expected_row_version: int) -> None:
        """Save a draft as typed. Incomplete content may be saved; submitting re-checks it."""
        label, effective, checks = normalize_label(version_label), normalize_date(effective_from), normalize_checks(checks)
        if not isinstance(content, Mapping):
            raise FeeRuleError("FEE_RULE_CONTENT_INVALID", "规则内容格式无效。")
        with self._cursor() as cursor:
            self._lock(cursor, rule_id, expected_row_version, ("DRAFT",))
            cursor.execute("UPDATE agent_fee_rule_versions SET version_label=%s, effective_from=%s, content_json=%s,"
                           " checks_json=%s, updated_by=%s, row_version=row_version+1 WHERE rule_id=%s",
                           (label, effective, _dump(content), _dump(checks), actor_id, rule_id))
            self._event(cursor, rule_id, "SAVE", actor_id)

    def set_checks(self, rule_id: str, *, checks: Any, actor_id: str, expected_row_version: int) -> None:
        """Tick 待核对事项 on a draft or a submitted version (the reviewer checks them before approval)."""
        checks = normalize_checks(checks)
        with self._cursor() as cursor:
            record = self._lock(cursor, rule_id, expected_row_version, ("DRAFT", "PENDING"))
            if [item["text"] for item in record["checks"]] != [item["text"] for item in checks]:
                raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "只能勾选已有的待核对事项；修改内容请先退回草稿。")
            cursor.execute("UPDATE agent_fee_rule_versions SET checks_json=%s, updated_by=%s, row_version=row_version+1"
                           " WHERE rule_id=%s", (_dump(checks), actor_id, rule_id))
            self._event(cursor, rule_id, "CHECK", actor_id, {"done": sum(item["done"] for item in checks)})

    def submit(self, rule_id: str, *, actor_id: str, expected_row_version: int) -> None:
        with self._cursor() as cursor:
            record = self._lock(cursor, rule_id, expected_row_version, ("DRAFT",))
            parse_content(record["fee_kind"], record["content"])
            cursor.execute("UPDATE agent_fee_rule_versions SET status='PENDING', submitted_by=%s, submitted_at=UTC_TIMESTAMP(6),"
                           " updated_by=%s, row_version=row_version+1 WHERE rule_id=%s", (actor_id, actor_id, rule_id))
            self._event(cursor, rule_id, "SUBMIT", actor_id)

    def withdraw(self, rule_id: str, *, actor_id: str, expected_row_version: int) -> None:
        with self._cursor() as cursor:
            self._lock(cursor, rule_id, expected_row_version, ("PENDING",))
            cursor.execute("UPDATE agent_fee_rule_versions SET status='DRAFT', submitted_by=NULL, submitted_at=NULL,"
                           " updated_by=%s, row_version=row_version+1 WHERE rule_id=%s", (actor_id, rule_id))
            self._event(cursor, rule_id, "WITHDRAW", actor_id)

    def approve(self, rule_id: str, *, actor_id: str, expected_row_version: int) -> None:
        with self._cursor() as cursor:
            record = self._lock(cursor, rule_id, expected_row_version, ("PENDING",))
            blockers = approval_blockers(record)
            if blockers:
                raise FeeRuleError("FEE_RULE_NOT_APPROVABLE", "；".join(blockers))
            cursor.execute("SELECT rule_id FROM agent_fee_rule_versions WHERE fee_kind=%s AND status='APPROVED'"
                           " AND effective_from=%s AND rule_id<>%s FOR UPDATE",
                           (record["fee_kind"], record["effective_from"], rule_id))
            if self._rows(cursor):
                raise FeeRuleError("FEE_RULE_DATE_TAKEN", "同一执行日期已有生效版本，请先作废旧版本或改执行日期。")
            cursor.execute("UPDATE agent_fee_rule_versions SET status='APPROVED', approved_by=%s, approved_at=UTC_TIMESTAMP(6),"
                           " updated_by=%s, row_version=row_version+1 WHERE rule_id=%s", (actor_id, actor_id, rule_id))
            self._event(cursor, rule_id, "APPROVE", actor_id, {"effective_from": record["effective_from"]})

    def retire(self, rule_id: str, *, actor_id: str, expected_row_version: int) -> None:
        with self._cursor() as cursor:
            self._lock(cursor, rule_id, expected_row_version, ("DRAFT", "PENDING", "APPROVED"))
            cursor.execute("UPDATE agent_fee_rule_versions SET status='RETIRED', retired_by=%s, retired_at=UTC_TIMESTAMP(6),"
                           " updated_by=%s, row_version=row_version+1 WHERE rule_id=%s", (actor_id, actor_id, rule_id))
            self._event(cursor, rule_id, "RETIRE", actor_id)


__all__ = ["FeeRuleRepository", "normalize_checks", "normalize_date", "normalize_label"]
