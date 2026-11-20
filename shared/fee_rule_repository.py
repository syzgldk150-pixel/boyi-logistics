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
import re
import secrets
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Any, Callable, Iterator, Mapping

from shared.admin_display_names import fetch_admin_display_names
from shared.fee_rule_lines import fee_lines
from shared.handbook_fee_rules import (FEE_NAME_LIMIT, FeeRuleError, approval_blockers, parse_content, review_lines,
                                       submit_blockers)

ConnectionFactory = Callable[[], Any]
_LABEL_LIMIT = 60
_CHECK_LIMIT = 30
_LINE_LIMIT = 600
_SIG = re.compile(r"^[0-9a-f]{16}$")
_BATCH_LIMIT = 50
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
    """The 核对 list of a version (see ``shared.handbook_fee_rules``): items to tick, questions to
    answer, and the 收费表 lines ticked so far (only ticked lines are kept)."""
    if not isinstance(checks, list):
        raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "核对内容格式无效。")
    result, sigs, others = [], set(), 0
    for item in checks:
        if not isinstance(item, Mapping):
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "核对内容格式无效。")
        kind = item.get("kind") or "item"
        if kind == "line":
            sig = item.get("sig")
            if not isinstance(sig, str) or not _SIG.fullmatch(sig):
                raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "收费表的核对记录无效，请刷新后再试。")
            if item.get("done") is True and sig not in sigs:
                sigs.add(sig)
                result.append({"kind": "line", "sig": sig, "done": True})
            continue
        others += 1
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "每条核对事项或问题都要填写内容，且不超过 300 字。")
        if kind == "item":
            result.append({"text": text.strip(), "done": item.get("done") is True})
            continue
        if kind != "question":
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "核对内容格式无效。")
        options = item.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 6 or any(
                not isinstance(option, str) or not option.strip() or len(option.strip()) > 60 for option in options):
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", f"问题“{text.strip()[:20]}”要有 2 到 6 个答案选项，每个不超过 60 字。")
        options = [option.strip() for option in options]
        if len(set(options)) != len(options):
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", f"问题“{text.strip()[:20]}”的答案选项有重复。")
        answer, note = item.get("answer"), item.get("note") or ""
        if answer is not None and answer not in options:
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", f"问题“{text.strip()[:20]}”的回答不在选项里。")
        if not isinstance(note, str) or len(note) > 200:
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "回答的说明不超过 200 字。")
        if isinstance(answer, str) and answer.startswith("其他") and not note.strip():
            raise FeeRuleError("FEE_RULE_CHECKS_INVALID", f"问题“{text.strip()[:20]}”选了“其他”，请写明怎么做。")
        result.append({"kind": "question", "text": text.strip(), "options": options, "answer": answer, "note": note.strip()})
    if others > _CHECK_LIMIT or len(sigs) > _LINE_LIMIT:
        raise FeeRuleError("FEE_RULE_CHECKS_INVALID", f"核对事项和问题最多 {_CHECK_LIMIT} 条。")
    return result


def _fixed_part(checks: list[dict[str, Any]]) -> list[tuple]:
    """What ticking or answering may not change: each item's text, each question and its options."""
    return [(item["text"], tuple(item.get("options") or ())) for item in checks if item.get("kind") != "line"]


def _keep_current_lines(checks: list[dict[str, Any]], content: Any) -> list[dict[str, Any]]:
    """Drop ticks of 收费表 lines that changed (a number, tier, condition or source was edited)."""
    current = {line["sig"] for line in fee_lines(content)}
    return [item for item in checks if item.get("kind") != "line" or item["sig"] in current]


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
                       " AND action IN ('SAVE', 'COPY', 'CREATE', 'DRAFT')), 0)", (rule_id, rule_id))
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
        record["lines"] = review_lines(record)
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

    def add_drafted(self, *, fee_name: Any, content: Mapping[str, Any], checks: Any, actor_id: str,
                    version_label: str, detail: Mapping[str, Any]) -> dict[str, Any]:
        """Keep one AI-drafted fee (计算规则 → 从手册起草) as a draft: a new version of the fee with the
        same name, or a new fee. Approved versions are untouched; the draft goes through 核对."""
        name, label, checks = normalize_fee_name(fee_name), normalize_label(version_label), normalize_checks(checks)
        if not isinstance(content, Mapping):
            raise FeeRuleError("FEE_RULE_CONTENT_INVALID", "规则内容格式无效。")
        new_id = str(uuid.uuid4())
        with self._cursor() as cursor:
            cursor.execute("SELECT fee_kind FROM agent_fee_rule_versions WHERE fee_name=%s AND status<>'RETIRED'"
                           " GROUP BY fee_kind", (name,))
            kinds = [row["fee_kind"] for row in self._rows(cursor)]
            if len(kinds) > 1:
                raise FeeRuleError("FEE_RULE_NAME_TAKEN", f"有多种费用都叫“{name}”，请先在计算规则里改名后再保存。")
            kind = kinds[0] if kinds else "fee_" + secrets.token_hex(6)
            cursor.execute("INSERT INTO agent_fee_rule_versions (rule_id, fee_kind, fee_name, version_label, status,"
                           " effective_from, content_json, checks_json, created_by, updated_by)"
                           " VALUES (%s, %s, %s, %s, 'DRAFT', NULL, %s, %s, %s, %s)",
                           (new_id, kind, name, label, _dump(content), _dump(checks), actor_id, actor_id))
            self._event(cursor, new_id, "DRAFT", actor_id, {**dict(detail), "fee_name": name, "new_fee": not kinds})
        return {"rule_id": new_id, "fee_name": name, "new_fee": not kinds}

    def copy_to_draft(self, rule_id: str, *, actor_id: str) -> str:
        """Start a new draft from any version; approved versions themselves stay unchanged."""
        new_id = str(uuid.uuid4())
        with self._cursor() as cursor:
            source = self._get(cursor, rule_id, lock=False)
            label = normalize_label((source["version_label"] + "-修改")[:_LABEL_LIMIT])
            checks = [{**item, "answer": None, "note": ""} if item.get("kind") == "question" else {"text": item["text"], "done": False}
                      for item in source["checks"] if item.get("kind") != "line"]
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
            checks = _keep_current_lines(checks, content)
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

    def set_checks(self, rule_id: str, *, checks: Any, actor_id: str, expected_row_version: int) -> int:
        """Tick items and 收费表 lines, answer questions, on a draft or a submitted version (the
        reviewer may check them before approval). Returns the new ``row_version``."""
        checks = normalize_checks(checks)
        with self._cursor() as cursor:
            record = self._lock(cursor, rule_id, expected_row_version, ("DRAFT", "PENDING"))
            if _fixed_part(record["checks"]) != _fixed_part(checks):
                raise FeeRuleError("FEE_RULE_CHECKS_INVALID", "只能勾选或回答已有的核对事项；修改内容请先保存草稿（审核中请先退回草稿）。")
            current = {line["sig"] for line in fee_lines(record["content"])}
            if any(item.get("kind") == "line" and item["sig"] not in current for item in checks):
                raise FeeRuleError("FEE_RULE_CHECKS_STALE", "收费表已经变了，请刷新后再核对。")
            cursor.execute("UPDATE agent_fee_rule_versions SET checks_json=%s, updated_by=%s, row_version=row_version+1"
                           " WHERE rule_id=%s", (_dump(checks), actor_id, rule_id))
            self._event(cursor, rule_id, "CHECK", actor_id, {
                "done": sum(item.get("done") is True for item in checks if item.get("kind") is None),
                "lines": sum(item.get("kind") == "line" for item in checks),
                "answered": sum(item.get("kind") == "question" and item["answer"] is not None for item in checks)})
            return record["row_version"] + 1

    def set_effective_dates(self, versions: Any, *, effective_from: Any, actor_id: str) -> int:
        """Give several drafts the headquarters effective date at once; all or none."""
        effective = normalize_date(effective_from)
        if effective is None:
            raise FeeRuleError("FEE_RULE_DATE_INVALID", "请选择执行日期。")
        if not isinstance(versions, list) or not 1 <= len(versions) <= _BATCH_LIMIT or any(
                not isinstance(item, Mapping) or not isinstance(item.get("rule_id"), str)
                or type(item.get("row_version")) is not int for item in versions):
            raise FeeRuleError("FEE_RULE_BATCH_INVALID", f"请选择 1 到 {_BATCH_LIMIT} 个草稿。")
        if len({item["rule_id"] for item in versions}) != len(versions):
            raise FeeRuleError("FEE_RULE_BATCH_INVALID", "选择的草稿有重复。")
        with self._cursor() as cursor:
            for item in sorted(versions, key=lambda value: value["rule_id"]):
                self._lock(cursor, item["rule_id"], item["row_version"], ("DRAFT",))
                cursor.execute("UPDATE agent_fee_rule_versions SET effective_from=%s, updated_by=%s,"
                               " row_version=row_version+1 WHERE rule_id=%s", (effective, actor_id, item["rule_id"]))
                self._event(cursor, item["rule_id"], "DATE", actor_id, {"effective_from": effective.isoformat()})
        return len(versions)

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
