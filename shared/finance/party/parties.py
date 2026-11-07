"""Parties (往来单位): customers, platforms, senders, suppliers and employees.

A 月结 customer may be linked to one Boyi sender (exact name + phone) so the
waybill posting rules of P3 can find it; nothing is matched by similarity.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from shared.finance.db import fetchall, fetchone
from shared.finance.errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from shared.finance.models import FinanceActor, expected_revision, optional_text

PARTY_TYPES = ("customer", "platform", "sender", "supplier", "employee", "other")
PARTY_COLUMNS = (
    "id, party_type, name, phone, boyi_sender_name, boyi_sender_phone, remark, is_active, revision, created_by, created_at"
)
_EDITABLE = ("party_type", "name", "phone", "boyi_sender_name", "boyi_sender_phone", "remark", "is_active")


def party_view(row: dict[str, Any]) -> dict[str, Any]:
    view = {key: row[key] for key in ("party_type", "name", "phone", "boyi_sender_name", "boyi_sender_phone", "remark", "created_by")}
    view.update(id=int(row["id"]), is_active=bool(row["is_active"]), revision=int(row["revision"]))
    created = row.get("created_at")
    view["created_at"] = created.isoformat(sep=" ", timespec="seconds") if isinstance(created, dt.datetime) else None
    return view


def _party_fields(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "party_type" in payload or not partial:
        if payload.get("party_type") not in PARTY_TYPES:
            raise FinanceValidationError("往来单位类型无效。")
        fields["party_type"] = payload["party_type"]
    if "name" in payload or not partial:
        name = optional_text(payload.get("name"), "往来单位名称", 128)
        if not name:
            raise FinanceValidationError("往来单位名称不能为空。")
        fields["name"] = name
    for key, label, limit in (("phone", "电话", 64), ("boyi_sender_name", "关联寄件人", 128),
                              ("boyi_sender_phone", "关联寄件人电话", 64), ("remark", "备注", 255)):
        if key in payload:
            fields[key] = optional_text(payload.get(key), label, limit)
    if "remark" in fields:
        fields["remark"] = fields["remark"] or ""
    if "is_active" in payload:
        if not isinstance(payload["is_active"], bool):
            raise FinanceValidationError("启用状态必须是开关值。")
        fields["is_active"] = int(payload["is_active"])
    sender = (fields.get("boyi_sender_name"), fields.get("boyi_sender_phone"))
    if ("boyi_sender_name" in fields or "boyi_sender_phone" in fields) and bool(sender[0]) != bool(sender[1]):
        raise FinanceValidationError("关联寄件人需要同时填写姓名和电话，或同时清空。")
    return fields


class PartiesMixin:
    @staticmethod
    def _party_row(cursor: Any, party_id: int, *, lock: str = "") -> dict[str, Any]:
        cursor.execute(f"SELECT {PARTY_COLUMNS} FROM finance_parties WHERE id=%s {lock}", (party_id,))
        row = fetchone(cursor)
        if row is None:
            raise FinanceNotFoundError("往来单位不存在。", code="PARTY_NOT_FOUND")
        return row

    @staticmethod
    def _duplicate_party_error(exc: Exception) -> FinanceError | None:
        text = str(exc)
        if "uq_finance_parties_name" in text:
            return FinanceError("PARTY_EXISTS", "同类型下已有同名往来单位。")
        if "uq_finance_parties_sender" in text:
            return FinanceError("PARTY_SENDER_LINKED", "该寄件人已关联到其他往来单位。")
        return None

    def list_parties(self, *, party_type: Any = None, include_inactive: bool = False) -> list[dict[str, Any]]:
        if party_type not in (None, "", *PARTY_TYPES):
            raise FinanceValidationError("往来单位类型无效。")

        def work(cursor: Any) -> list[dict[str, Any]]:
            conditions, params = ["1=1"], []
            if party_type:
                conditions.append("party_type=%s")
                params.append(party_type)
            if not include_inactive:
                conditions.append("is_active=1")
            cursor.execute(
                f"SELECT {PARTY_COLUMNS} FROM finance_parties WHERE {' AND '.join(conditions)} ORDER BY party_type, name",
                tuple(params),
            )
            return [party_view(row) for row in fetchall(cursor)]

        return self._read(work)

    def create_party(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - set(_EDITABLE) - {"is_active"}:
            raise FinanceValidationError("往来单位内容包含不支持的字段。")
        fields = _party_fields(payload, partial=False)
        fields.pop("is_active", None)

        def work(cursor: Any) -> dict[str, Any]:
            columns = [*fields, "created_by"]
            try:
                cursor.execute(
                    f"INSERT INTO finance_parties ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
                    (*fields.values(), actor.label),
                )
            except Exception as exc:
                raise self._duplicate_party_error(exc) or exc
            return party_view(self._party_row(cursor, int(cursor.lastrowid)))

        return self._write(request_id=request_id, action="party.create", target_type="party", target_id="new",
                           payload=payload, actor=actor, work=work)

    def update_party(self, party_id: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        party_id = int(party_id)
        if not isinstance(payload, dict) or set(payload) - set(_EDITABLE) - {"expected_revision"}:
            raise FinanceValidationError("往来单位内容包含不支持的字段。")
        revision = expected_revision(payload.get("expected_revision"))
        fields = _party_fields({k: v for k, v in payload.items() if k != "expected_revision"}, partial=True)
        if not fields:
            raise FinanceValidationError("没有修改。")

        def work(cursor: Any) -> dict[str, Any]:
            current = self._party_row(cursor, party_id, lock="FOR UPDATE")
            if int(current["revision"]) != revision:
                raise FinanceError("FINANCE_REVISION_CONFLICT", "往来单位已被他人修改，请刷新后重试。")
            if fields.get("party_type", current["party_type"]) != current["party_type"]:
                cursor.execute("SELECT id FROM finance_party_ledger WHERE party_id=%s LIMIT 1", (party_id,))
                if cursor.fetchone():
                    raise FinanceError("PARTY_HAS_ENTRIES", "往来单位已有往来记录，不能修改类型。")
            assignments = ", ".join(f"{key}=%s" for key in fields)
            try:
                cursor.execute(f"UPDATE finance_parties SET {assignments}, revision=revision+1 WHERE id=%s",
                               (*fields.values(), party_id))
            except Exception as exc:
                raise self._duplicate_party_error(exc) or exc
            return party_view(self._party_row(cursor, party_id))

        return self._write(request_id=request_id, action="party.update", target_type="party", target_id=party_id,
                           payload=payload, actor=actor, work=work)

    def sender_candidates(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """月结 senders on valid Boyi waybills that no party is linked to yet (exact name + phone)."""

        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(
                "SELECT w.sender_name, w.sender_phone, COUNT(*) AS waybills, MAX(w.open_date) AS last_open_date"
                " FROM boyi_waybills w"
                " WHERE w.payment_method='月结' AND COALESCE(w.status, '') <> 'cancelled'"
                " AND COALESCE(w.sender_name, '') <> '' AND COALESCE(w.sender_phone, '') <> ''"
                " AND NOT EXISTS (SELECT 1 FROM finance_parties p WHERE p.boyi_sender_name=w.sender_name"
                " AND p.boyi_sender_phone=w.sender_phone)"
                " GROUP BY w.sender_name, w.sender_phone ORDER BY waybills DESC, w.sender_name LIMIT %s",
                (int(limit),),
            )
            return [{"sender_name": row["sender_name"], "sender_phone": row["sender_phone"],
                     "waybills": int(row["waybills"]), "last_open_date": str(row["last_open_date"] or "")}
                    for row in fetchall(cursor)]

        return self._read(work)
