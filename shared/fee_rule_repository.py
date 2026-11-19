"""Persistence for 业务手册 → 计算规则 (migrations 087, 090) over a caller-supplied connection factory.

Never reads the environment or runs DDL. Every change is one short transaction
that locks the version row first and checks ``row_version``, so two people
cannot overwrite each other. Approval re-validates the stored content inside the
locked transaction and refuses a second approved version of the same fee with
the same effective date. Approved versions are never edited. Quotes made from the
back office or the robot are kept in ``agent_fee_quotes``.
"""
from __future__ import annotations

import json
import secrets
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Any, Callable, Iterator, Mapping

from shared.admin_display_names import fetch_admin_display_names
from shared.handbook_fee_rules import FEE_NAME_LIMIT, FeeRuleError, approval_blockers, parse_content, submit_blockers

ConnectionFactory = Callable[[], Any]
_LABEL_LIMIT = 60
_CHECK_LIMIT = 30
# A new fee starts from parameters and a formula people fill in; it computes nothing until edited.
EMPTY_CONTENT = {"schema": 1, "params": [], "formula": {"type": "none", "outcome": "MANUAL", "reason": "规则还没有填写。"},
                 "reminders": []}


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


def normalize_fee_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > FEE_NAME_LIMIT:
        raise FeeRuleError("FEE_RULE_NAME_INVALID", f"费用名称必须填写，且不超过 {FEE_NAME_LIMIT} 字。")
    return name.strip()


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
        return {"rule_id": row["rule_id"], "fee_kind": row["fee_kind"], "fee_name": row.get("fee_name") or row["fee_kind"],
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

    def list_versions(self, *, include_retired: bool = False) -> list[dict[str, Any]]:
        where = "" if include_retired else " WHERE status <> 'RETIRED'"
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_fee_rule_versions" + where +
                           " ORDER BY fee_kind, (status='RETIRED'), (status='APPROVED') DESC, effective_from DESC,"
                           " updated_at DESC LIMIT 200")
            return [self._record(row) for row in self._rows(cursor)]

    def _trial_count(self, cursor: Any, rule_id: str) -> int:
        """Trial runs on this version since its content was last saved (or copied)."""
        cursor.execute("SELECT COUNT(*) AS n FROM agent_fee_rule_events WHERE rule_id=%s AND action='TRIAL' AND event_id >"
                       " COALESCE((SELECT MAX(event_id) FROM agent_fee_rule_events WHERE rule_id=%s"
                       " AND action IN ('SAVE', 'COPY', 'CREATE')), 0)", (rule_id, rule_id))
        return int(self._rows(cursor)[0]["n"])

    def get(self, rule_id: str) -> dict[str, Any]:
        with self._cursor() as cursor:
            record = self._get(cursor, rule_id, lock=False)
            cursor.execute("SELECT action, actor_id, detail_json, created_at FROM agent_fee_rule_events"
                           " WHERE rule_id=%s ORDER BY event_id DESC LIMIT 50", (rule_id,))
            record["events"] = [{"action": row["action"], "actor_id": row["actor_id"], "detail": _load(row["detail_json"]),
                                 "created_at": _iso(row["created_at"])} for row in self._rows(cursor)]
            record["trial_count"] = self._trial_count(cursor, rule_id)
            record["people"] = fetch_admin_display_names(cursor, {
                record["created_by"], record["updated_by"], record["submitted_by"], record["approved_by"],
                *(item["actor_id"] for item in record["events"])})
        record["submit_blockers"] = submit_blockers(record, trial_count=record["trial_count"])
        record["blockers"] = approval_blockers(record)
        return record

    def record_trial(self, rule_id: str, *, actor_id: str, detail: Mapping[str, Any]) -> None:
        """Note one trial calculation in the operation log; trials count towards submission."""
        with self._cursor() as cursor:
            self._get(cursor, rule_id, lock=False)
            self._event(cursor, rule_id, "TRIAL", actor_id, dict(detail))

    def approved(self, fee_kind: str | None = None) -> list[dict[str, Any]]:
        """Approved versions of one fee (or of every fee), for calculations (choose with select_rule by billing date)."""
        with self._cursor() as cursor:
            if fee_kind is None:
                cursor.execute("SELECT * FROM agent_fee_rule_versions WHERE status='APPROVED' ORDER BY fee_kind, effective_from")
            else:
                cursor.execute("SELECT * FROM agent_fee_rule_versions WHERE fee_kind=%s AND status='APPROVED'"
                               " ORDER BY effective_from", (fee_kind,))
            return [self._record(row) for row in self._rows(cursor)]

    def record_quote(self, *, actor_id: str, channel: str, billing_date: date, fee_kinds: list[str],
                     values: Mapping[str, Any], result: Mapping[str, Any], tracking_number: str | None = None) -> str:
        """Keep one quote (what was typed, which versions answered, the result) for later checking."""
        quote_id = str(uuid.uuid4())
        with self._cursor() as cursor:
            cursor.execute("INSERT INTO agent_fee_quotes (quote_id, actor_id, channel, billing_date, fee_kinds_json,"
                           " values_json, result_json, total, complete, tracking_number)"
                           " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                           (quote_id, actor_id, channel, billing_date, _dump(fee_kinds), _dump(values), _dump(result),
                            str(result.get("total") or "0"), bool(result.get("complete")), tracking_number))
        return quote_id

    def recent_quotes(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT quote_id, actor_id, channel, billing_date, fee_kinds_json, values_json, result_json,"
                           " total, complete, tracking_number, created_at FROM agent_fee_quotes"
                           " ORDER BY created_at DESC LIMIT %s", (max(1, min(int(limit), 100)),))
            rows = self._rows(cursor)
            people = fetch_admin_display_names(cursor, {row["actor_id"] for row in rows})
        return [{"quote_id": row["quote_id"], "actor_id": row["actor_id"], "actor": people.get(str(row["actor_id"]), ""),
                 "channel": row["channel"], "billing_date": _iso(row["billing_date"]),
                 "fee_kinds": _load(row["fee_kinds_json"]), "values": _load(row["values_json"]),
                 "result": _load(row["result_json"]), "total": str(row["total"]), "complete": bool(row["complete"]),
                 "tracking_number": row["tracking_number"], "created_at": _iso(row["created_at"])} for row in rows]

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

    def create_fee(self, *, fee_name: Any, actor_id: str, content: Mapping[str, Any] | None = None,
                   checks: Any = None) -> str:
        """A new kind of fee, as an empty draft (or an AI draft's content); it computes nothing until approved."""
        name = normalize_fee_name(fee_name)
        checks = normalize_checks(checks if checks is not None else [{"text": "执行日期以总部通知为准，尚未填写。"}])
        new_id = str(uuid.uuid4())
        with self._cursor() as cursor:
            cursor.execute("SELECT 1 FROM agent_fee_rule_versions WHERE fee_name=%s AND status<>'RETIRED' LIMIT 1", (name,))
            if self._rows(cursor):
                raise FeeRuleError("FEE_RULE_NAME_TAKEN", f"已经有名为“{name}”的费用，请换个名称或在原费用上复制新版本。")
            cursor.execute("INSERT INTO agent_fee_rule_versions (rule_id, fee_kind, fee_name, version_label, status,"
                           " effective_from, content_json, checks_json, created_by, updated_by)"
                           " VALUES (%s, %s, %s, '草稿1', 'DRAFT', NULL, %s, %s, %s, %s)",
                           (new_id, "fee_" + secrets.token_hex(6), name, _dump(content or EMPTY_CONTENT), _dump(checks),
                            actor_id, actor_id))
            self._event(cursor, new_id, "CREATE", actor_id, {"fee_name": name})
        return new_id

    def copy_to_draft(self, rule_id: str, *, actor_id: str) -> str:
        """Start a new draft from any version; approved versions themselves stay unchanged."""
        new_id = str(uuid.uuid4())
        with self._cursor() as cursor:
            source = self._get(cursor, rule_id, lock=False)
            label = normalize_label((source["version_label"] + "-修改")[:_LABEL_LIMIT])
            checks = [{"text": item["text"], "done": False} for item in source["checks"]]
            cursor.execute("INSERT INTO agent_fee_rule_versions (rule_id, fee_kind, fee_name, version_label, status,"
                           " effective_from, content_json, checks_json, based_on, created_by, updated_by)"
                           " VALUES (%s, %s, %s, %s, 'DRAFT', NULL, %s, %s, %s, %s, %s)",
                           (new_id, source["fee_kind"], source["fee_name"], label, _dump(source["content"]), _dump(checks),
                            rule_id, actor_id, actor_id))
            self._event(cursor, new_id, "COPY", actor_id, {"from": rule_id})
        return new_id

    def save_draft(self, rule_id: str, *, version_label: Any, effective_from: Any, content: Any, checks: Any,
                   actor_id: str, expected_row_version: int, fee_name: Any = None) -> None:
        """Save a draft as typed. Incomplete content may be saved; submitting re-checks it.

        Renaming the fee renames every version of it, so the list keeps one name per fee.
        """
        label, effective, checks = normalize_label(version_label), normalize_date(effective_from), normalize_checks(checks)
        if not isinstance(content, Mapping):
            raise FeeRuleError("FEE_RULE_CONTENT_INVALID", "规则内容格式无效。")
        with self._cursor() as cursor:
            record = self._lock(cursor, rule_id, expected_row_version, ("DRAFT",))
            name = record["fee_name"] if fee_name is None else normalize_fee_name(fee_name)
            if name != record["fee_name"]:
                cursor.execute("SELECT 1 FROM agent_fee_rule_versions WHERE fee_name=%s AND fee_kind<>%s"
                               " AND status<>'RETIRED' LIMIT 1", (name, record["fee_kind"]))
                if self._rows(cursor):
                    raise FeeRuleError("FEE_RULE_NAME_TAKEN", f"已经有名为“{name}”的费用，请换个名称。")
                cursor.execute("UPDATE agent_fee_rule_versions SET fee_name=%s WHERE fee_kind=%s", (name, record["fee_kind"]))
            cursor.execute("UPDATE agent_fee_rule_versions SET version_label=%s, effective_from=%s, content_json=%s,"
                           " checks_json=%s, updated_by=%s, row_version=row_version+1 WHERE rule_id=%s",
                           (label, effective, _dump(content), _dump(checks), actor_id, rule_id))
            self._event(cursor, rule_id, "SAVE", actor_id, {"fee_name": name} if name != record["fee_name"] else None)

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
            parse_content(record["content"])
            problems = submit_blockers(record, trial_count=self._trial_count(cursor, rule_id))
            if problems:
                raise FeeRuleError("FEE_RULE_NOT_SUBMITTABLE", "；".join(problems))
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


__all__ = ["EMPTY_CONTENT", "FeeRuleRepository", "normalize_checks", "normalize_date", "normalize_fee_name",
           "normalize_label"]
