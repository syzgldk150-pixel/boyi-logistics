"""Voucher drafts: parsing, saving, reading. Submission lives in ``posting.py``.

Drafts may be unbalanced while being edited; ``submit_voucher`` enforces every
posting rule. A draft never touches the GL and can be edited or deleted.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from shared.finance.db import fetchall, fetchone
from shared.finance.errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from shared.finance.models import (
    DIMENSIONS,
    FEE_LEVELS,
    MANUAL_VOUCHER_TYPES,
    FinanceActor,
    expected_revision,
    month_of,
    optional_text,
    parse_account_code,
    parse_amount,
    parse_date,
    parse_month,
    parse_request_id,
)

MAX_LINES = 200
VOUCHER_COLUMNS = (
    "id, voucher_no, voucher_type, status, posting_date, period_month, summary, ref_type, ref_id, reversal_of,"
    " total_debit, total_credit, revision, created_by, created_at, submitted_by, submitted_at, cancelled_by,"
    " cancelled_at, cancel_reason"
)
LINE_COLUMNS = (
    "id, voucher_id, line_no, account_code, debit, credit, summary, site_code, source_id, business_line,"
    " fee_level, waybill_no, party_id, source_line_id"
)
_LINE_FIELDS = {"account_code", "debit", "credit", "summary", "party_id", *DIMENSIONS}
_DIMENSION_LIMITS = {"site_code": 64, "source_id": 64, "business_line": 64, "waybill_no": 128}


def amount_text(value: Any) -> str | None:
    return None if value is None else f"{Decimal(value):.4f}"


def _time_text(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return None


def voucher_view(row: dict[str, Any], lines: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    view = {
        "id": int(row["id"]),
        "voucher_no": row["voucher_no"],
        "voucher_type": row["voucher_type"],
        "status": row["status"],
        "posting_date": row["posting_date"].isoformat(),
        "period_month": row["period_month"],
        "summary": row["summary"],
        "ref_type": row["ref_type"],
        "ref_id": row["ref_id"],
        "reversal_of": row["reversal_of"],
        "total_debit": amount_text(row["total_debit"]),
        "total_credit": amount_text(row["total_credit"]),
        "revision": int(row["revision"]),
        "created_by": row["created_by"],
        "created_at": _time_text(row["created_at"]),
        "submitted_by": row["submitted_by"],
        "submitted_at": _time_text(row["submitted_at"]),
        "cancelled_by": row["cancelled_by"],
        "cancelled_at": _time_text(row["cancelled_at"]),
        "cancel_reason": row["cancel_reason"],
    }
    if lines is not None:
        view["lines"] = [line_view(line) for line in lines]
    return view


def line_view(row: dict[str, Any]) -> dict[str, Any]:
    view = {key: row[key] for key in ("line_no", "account_code", "summary", *DIMENSIONS, "party_id", "source_line_id")}
    view["debit"] = amount_text(row["debit"])
    view["credit"] = amount_text(row["credit"])
    for key in ("account_name", "party_name"):
        if key in row:
            view[key] = row[key]
    return view


def parse_voucher(payload: Any) -> dict[str, Any]:
    """Validate a hand-entered voucher; amounts become Decimals, never floats."""

    if not isinstance(payload, dict):
        raise FinanceValidationError("凭证内容无效。")
    unknown = set(payload) - {"voucher_type", "posting_date", "summary", "lines", "expected_revision"}
    if unknown:
        raise FinanceValidationError("凭证内容包含不支持的字段。")
    voucher_type = payload.get("voucher_type", "manual")
    if voucher_type not in MANUAL_VOUCHER_TYPES:
        raise FinanceValidationError("手工只能录入记账凭证或期初凭证。", code="VOUCHER_TYPE_INVALID")
    posting_date = parse_date(payload.get("posting_date"), "记账日期")
    summary = optional_text(payload.get("summary"), "凭证摘要", 255) or ""
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise FinanceValidationError("凭证至少需要一行分录。", code="VOUCHER_LINES_REQUIRED")
    if len(raw_lines) > MAX_LINES:
        raise FinanceValidationError(f"一张凭证最多 {MAX_LINES} 行分录。")
    lines = []
    for index, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, dict) or set(raw) - _LINE_FIELDS:
            raise FinanceValidationError(f"第 {index} 行分录内容无效。")
        debit = parse_amount(raw.get("debit"), f"第 {index} 行借方金额")
        credit = parse_amount(raw.get("credit"), f"第 {index} 行贷方金额")
        if (debit > 0) == (credit > 0):
            raise FinanceValidationError(f"第 {index} 行必须且只能在借方或贷方填写一个大于 0 的金额。", code="VOUCHER_LINE_SIDE")
        line = {
            "line_no": index,
            "account_code": parse_account_code(raw.get("account_code")),
            "debit": debit,
            "credit": credit,
            "summary": optional_text(raw.get("summary"), f"第 {index} 行摘要", 255) or summary,
        }
        for field, limit in _DIMENSION_LIMITS.items():
            line[field] = optional_text(raw.get(field), f"第 {index} 行{field}", limit)
        fee_level = raw.get("fee_level") or None
        if fee_level is not None and fee_level not in FEE_LEVELS:
            raise FinanceValidationError(f"第 {index} 行费用层级无效。")
        line["fee_level"] = fee_level
        party_id = raw.get("party_id")
        if party_id in (None, ""):
            line["party_id"] = None
        elif isinstance(party_id, int) and not isinstance(party_id, bool) and party_id > 0:
            line["party_id"] = party_id
        else:
            raise FinanceValidationError(f"第 {index} 行往来单位无效。")
        lines.append(line)
    return {
        "voucher_type": voucher_type,
        "posting_date": posting_date,
        "period_month": month_of(posting_date),
        "summary": summary,
        "lines": lines,
    }


def _comparable(voucher: dict[str, Any]) -> dict[str, Any]:
    """The request payload as stored for idempotency (Decimals as text)."""

    return {
        **{key: voucher[key] for key in ("voucher_type", "period_month", "summary")},
        "posting_date": voucher["posting_date"].isoformat(),
        "lines": [{**line, "debit": format(line["debit"], "f"), "credit": format(line["credit"], "f")} for line in voucher["lines"]],
    }


class VouchersMixin:
    @staticmethod
    def _voucher_row(cursor: Any, voucher_id: int, *, lock: str = "") -> dict[str, Any]:
        cursor.execute(f"SELECT {VOUCHER_COLUMNS} FROM finance_vouchers WHERE id=%s {lock}", (voucher_id,))
        row = fetchone(cursor)
        if row is None:
            raise FinanceNotFoundError("凭证不存在。", code="VOUCHER_NOT_FOUND")
        return row

    @staticmethod
    def _voucher_lines(cursor: Any, voucher_id: int) -> list[dict[str, Any]]:
        cursor.execute(
            "SELECT l.line_no, l.account_code, a.name AS account_name, l.debit, l.credit, l.summary, l.site_code,"
            " l.source_id, l.business_line, l.fee_level, l.waybill_no, l.party_id, p.name AS party_name, l.source_line_id, l.id"
            " FROM finance_voucher_lines l JOIN finance_accounts a ON a.code=l.account_code"
            " LEFT JOIN finance_parties p ON p.id=l.party_id"
            " WHERE l.voucher_id=%s ORDER BY l.line_no",
            (voucher_id,),
        )
        return fetchall(cursor)

    def _require_known_accounts(self, cursor: Any, voucher: dict[str, Any]) -> None:
        codes = sorted({line["account_code"] for line in voucher["lines"]})
        cursor.execute(
            f"SELECT code FROM finance_accounts WHERE code IN ({', '.join(['%s'] * len(codes))})", tuple(codes)
        )
        missing = set(codes) - {row["code"] for row in fetchall(cursor)}
        if missing:
            raise FinanceValidationError(f"科目不存在：{', '.join(sorted(missing))}。", code="ACCOUNT_NOT_FOUND")
        party_ids = sorted({line["party_id"] for line in voucher["lines"] if line["party_id"]})
        if party_ids:
            cursor.execute(
                f"SELECT id FROM finance_parties WHERE id IN ({', '.join(['%s'] * len(party_ids))})", tuple(party_ids)
            )
            if len(fetchall(cursor)) != len(party_ids):
                raise FinanceValidationError("所选往来单位不存在。", code="PARTY_NOT_FOUND")

    def _require_not_before_start(self, cursor: Any, voucher: dict[str, Any]) -> None:
        start_month = self._load_settings(cursor, lock="FOR SHARE")["start_month"]
        if start_month and voucher["period_month"] < start_month:
            raise FinanceError("BEFORE_START_MONTH", f"记账日期早于总账启用月 {start_month}。")

    @staticmethod
    def _insert_lines(cursor: Any, voucher_id: int, voucher: dict[str, Any]) -> None:
        cursor.executemany(
            "INSERT INTO finance_voucher_lines (voucher_id, line_no, account_code, debit, credit, summary, site_code,"
            " source_id, business_line, fee_level, waybill_no, party_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (voucher_id, line["line_no"], line["account_code"], format(line["debit"], "f"), format(line["credit"], "f"),
                 line["summary"], line["site_code"], line["source_id"], line["business_line"], line["fee_level"], line["waybill_no"],
                 line["party_id"])
                for line in voucher["lines"]
            ],
        )

    def _insert_draft(
        self, cursor: Any, voucher: dict[str, Any], request_id: str, actor: FinanceActor,
        *, ref_type: str | None = None, ref_id: str | None = None, reversal_of: int | None = None,
    ) -> int:
        """Insert a draft voucher with its lines; returns the new voucher id."""

        total_debit, total_credit = self._totals(voucher)
        cursor.execute(
            "INSERT INTO finance_vouchers (voucher_type, status, posting_date, period_month, summary, total_debit,"
            " total_credit, ref_type, ref_id, reversal_of, request_id, created_by)"
            " VALUES (%s, 'draft', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (voucher["voucher_type"], voucher["posting_date"], voucher["period_month"], voucher["summary"],
             total_debit, total_credit, ref_type, ref_id, reversal_of, request_id, actor.label),
        )
        voucher_id = int(cursor.lastrowid)
        self._insert_lines(cursor, voucher_id, voucher)
        return voucher_id

    @staticmethod
    def _totals(voucher: dict[str, Any]) -> tuple[str, str]:
        debit = sum((line["debit"] for line in voucher["lines"]), Decimal("0"))
        credit = sum((line["credit"] for line in voucher["lines"]), Decimal("0"))
        return format(debit, "f"), format(credit, "f")

    def _full_voucher(self, cursor: Any, voucher_id: int) -> dict[str, Any]:
        return voucher_view(self._voucher_row(cursor, voucher_id), self._voucher_lines(cursor, voucher_id))

    def get_voucher(self, voucher_id: int) -> dict[str, Any]:
        return self._read(lambda cursor: self._full_voucher(cursor, int(voucher_id)))

    def list_vouchers(self, *, month: Any, status: Any = None, with_lines: bool = False) -> list[dict[str, Any]]:
        month = parse_month(month)
        if status not in (None, "", "draft", "submitted", "cancelled"):
            raise FinanceValidationError("凭证状态筛选无效。")

        def work(cursor: Any) -> list[dict[str, Any]]:
            condition, params = "period_month=%s", [month]
            if status:
                condition += " AND status=%s"
                params.append(status)
            cursor.execute(
                f"SELECT {VOUCHER_COLUMNS} FROM finance_vouchers WHERE {condition}"
                " ORDER BY voucher_no IS NULL, voucher_no, posting_date, id",
                tuple(params),
            )
            rows = fetchall(cursor)
            if not with_lines:
                return [voucher_view(row) for row in rows]
            return [voucher_view(row, self._voucher_lines(cursor, int(row["id"]))) for row in rows]

        return self._read(work)

    def create_draft(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        voucher = parse_voucher(payload)
        if "expected_revision" in payload:
            raise FinanceValidationError("新建凭证不应携带版本号。")

        def work(cursor: Any) -> dict[str, Any]:
            self._require_not_before_start(cursor, voucher)
            self._require_known_accounts(cursor, voucher)
            return self._full_voucher(cursor, self._insert_draft(cursor, voucher, request_id_value, actor))

        request_id_value = parse_request_id(request_id)
        return self._write(request_id=request_id_value, action="voucher.create", target_type="voucher", target_id="new",
                           payload=_comparable(voucher), actor=actor, work=work)

    def update_draft(self, voucher_id: int, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        voucher_id = int(voucher_id)
        revision = expected_revision(payload.get("expected_revision") if isinstance(payload, dict) else None)
        voucher = parse_voucher(payload)

        def work(cursor: Any) -> dict[str, Any]:
            current = self._voucher_row(cursor, voucher_id, lock="FOR UPDATE")
            self._require_draft(current, revision)
            self._require_not_before_start(cursor, voucher)
            self._require_known_accounts(cursor, voucher)
            total_debit, total_credit = self._totals(voucher)
            cursor.execute("DELETE FROM finance_voucher_lines WHERE voucher_id=%s", (voucher_id,))
            cursor.execute(
                "UPDATE finance_vouchers SET voucher_type=%s, posting_date=%s, period_month=%s, summary=%s,"
                " total_debit=%s, total_credit=%s, revision=revision+1 WHERE id=%s",
                (voucher["voucher_type"], voucher["posting_date"], voucher["period_month"], voucher["summary"],
                 total_debit, total_credit, voucher_id),
            )
            self._insert_lines(cursor, voucher_id, voucher)
            return self._full_voucher(cursor, voucher_id)

        return self._write(request_id=request_id, action="voucher.update", target_type="voucher", target_id=voucher_id,
                           payload={**_comparable(voucher), "expected_revision": revision}, actor=actor, work=work)

    def delete_draft(self, voucher_id: int, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        voucher_id = int(voucher_id)
        revision = expected_revision((payload or {}).get("expected_revision"))

        def work(cursor: Any) -> dict[str, Any]:
            current = self._voucher_row(cursor, voucher_id, lock="FOR UPDATE")
            self._require_draft(current, revision)
            cursor.execute("DELETE FROM finance_vouchers WHERE id=%s", (voucher_id,))
            return {"id": voucher_id, "deleted": True}

        return self._write(request_id=request_id, action="voucher.delete", target_type="voucher", target_id=voucher_id,
                           payload={"expected_revision": revision}, actor=actor, work=work)

    @staticmethod
    def _require_draft(current: dict[str, Any], revision: int) -> None:
        if current["status"] != "draft":
            raise FinanceError("VOUCHER_NOT_DRAFT", "只有草稿凭证可以修改或删除；已提交的凭证请冲销。")
        if int(current["revision"]) != revision:
            raise FinanceError("FINANCE_REVISION_CONFLICT", "凭证已被他人修改，请刷新后重试。")
