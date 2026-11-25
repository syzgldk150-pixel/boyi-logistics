"""派件账单的存储（迁移 093）：派送费规则版本、每单增值费与确认、单行设置。

账单行本身由 Agent 用每日到货表与融辉结算明细即时组成（``shared.finance.delivery_bills``），
这里只保存人做过的事。每次写入都在 ``FinanceRepository._write`` 的一个事务里，按请求标识幂等；
确认时锁住该单的记录、核对版本，并在库里重新选一次派送费规则，与查询时看到的不一致就拒绝。
本期不记账；确认记录保留金额、规则版本与计算过程，供下一期按司机结算、进总账。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any, Mapping, Sequence

from shared.finance.db import fetchall, fetchone, json_text
from shared.finance.delivery_bills import MAX_BATCH, parse_rule_form, parse_settings, segments_text
from shared.finance.errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from shared.finance.models import FinanceActor, expected_revision, parse_request_id
from shared.finance.money import quantize_storage

_ITEM_COLUMNS = (
    "tracking_number, arrival_date, status, billing_date, fee_kinds_json, values_json, manual_json, fee_result_json,"
    " value_added_total, fees_updated_by, fees_updated_at, ronghui_amount, settlement_weight, pieces, delivery_fee,"
    " delivery_rule_id, delivery_steps_json, payable_total, confirmed_by, confirmed_at, revision"
)
_RULE_COLUMNS = (
    "id, effective_from, segments_json, rounding, content_json, status, note, created_by,"
    " created_at, retired_by, retired_at, revision"
)


def _load(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _iso(value: Any) -> str | None:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat(sep=" ", timespec="seconds") if isinstance(value, dt.datetime) else value.isoformat()
    return None if value is None else str(value)


def _amount(value: Any) -> str | None:
    return None if value is None else format(quantize_storage(value), "f")


def _derived(base: str, label: str) -> str:
    return str(uuid.uuid5(uuid.UUID(base), label))


def _rule_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": int(row["id"]), "effective_from": _iso(row["effective_from"]), "segments": _load(row["segments_json"]),
            "rounding": row["rounding"], "content": _load(row["content_json"]), "status": row["status"],
            "note": row.get("note"), "created_by": row["created_by"], "created_at": _iso(row["created_at"]),
            "retired_by": row.get("retired_by"), "retired_at": _iso(row.get("retired_at")), "revision": int(row["revision"])}


class DeliveryBillsMixin:
    """派件账单的读写；``self._read`` / ``self._write`` 来自 ``FinanceRepository``。"""

    # -- 读 --------------------------------------------------------------------------------

    def delivery_settings(self) -> dict[str, Any]:
        def read(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT lookahead_days, include_types_json, exclude_types_json, revision, updated_by, updated_at"
                           " FROM finance_delivery_bill_settings WHERE id=1")
            row = fetchone(cursor)
            if row is None:
                raise FinanceError("DELIVERY_SETTINGS_MISSING", "派件账单设置缺失，请检查迁移 093 是否已执行。", http_status=503)
            return {"lookahead_days": int(row["lookahead_days"]), "include_types": _load(row["include_types_json"]) or [],
                    "exclude_types": _load(row["exclude_types_json"]) or [], "revision": int(row["revision"]),
                    "updated_by": row["updated_by"], "updated_at": _iso(row["updated_at"])}

        return self._read(read)

    def delivery_rules(self) -> list[dict[str, Any]]:
        def read(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(f"SELECT {_RULE_COLUMNS} FROM finance_delivery_fee_rules ORDER BY effective_from DESC, id DESC")
            rules = [_rule_view(row) for row in fetchall(cursor)]
            cursor.execute("SELECT delivery_rule_id, COUNT(*) AS used FROM finance_delivery_bill_items"
                           " WHERE status='confirmed' GROUP BY delivery_rule_id")
            used = {int(row["delivery_rule_id"]): int(row["used"]) for row in fetchall(cursor) if row["delivery_rule_id"]}
            for rule in rules:
                rule["confirmed_count"] = used.get(rule["id"], 0)
            return rules

        return self._read(read)

    def delivery_items(self, tracking_numbers: Sequence[str]) -> dict[str, dict[str, Any]]:
        codes = sorted({str(code) for code in tracking_numbers})
        if not codes:
            return {}

        def read(cursor: Any) -> dict[str, dict[str, Any]]:
            items: dict[str, dict[str, Any]] = {}
            for index in range(0, len(codes), 500):
                chunk = codes[index:index + 500]
                marks = ",".join(["%s"] * len(chunk))
                cursor.execute(f"SELECT {_ITEM_COLUMNS} FROM finance_delivery_bill_items WHERE tracking_number IN ({marks})",
                               tuple(chunk))
                for row in fetchall(cursor):
                    items[row["tracking_number"]] = self._item_view(row)
                cursor.execute("SELECT tracking_number, position, fee_kind, fee_name, rule_id, version_label, outcome,"
                               " system_amount, amount, counted, note FROM finance_delivery_bill_fee_lines"
                               f" WHERE tracking_number IN ({marks}) ORDER BY tracking_number, position", tuple(chunk))
                for row in fetchall(cursor):
                    item = items.get(row["tracking_number"])
                    if item is not None:
                        item["lines"].append({
                            "position": int(row["position"]), "fee_kind": row["fee_kind"], "fee_name": row["fee_name"],
                            "rule_id": row.get("rule_id"), "version": row["version_label"], "outcome": row["outcome"],
                            "system_amount": _amount(row["system_amount"]), "amount": _amount(row["amount"]),
                            "counted": bool(row["counted"]), "note": row["note"]})
            return items

        return self._read(read)

    @staticmethod
    def _item_view(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"tracking_number": row["tracking_number"], "arrival_date": _iso(row["arrival_date"]), "status": row["status"],
                "billing_date": _iso(row["billing_date"]), "fee_kinds": _load(row["fee_kinds_json"]) or [],
                "values": _load(row["values_json"]) or {}, "manual": _load(row["manual_json"]) or {},
                "fee_result": _load(row["fee_result_json"]), "value_added_total": _amount(row["value_added_total"]),
                "fees_updated_by": row.get("fees_updated_by"), "fees_updated_at": _iso(row.get("fees_updated_at")),
                "ronghui_amount": _amount(row["ronghui_amount"]), "settlement_weight": _amount(row["settlement_weight"]),
                "pieces": row["pieces"], "delivery_fee": _amount(row["delivery_fee"]),
                "delivery_rule_id": None if row["delivery_rule_id"] is None else int(row["delivery_rule_id"]),
                "delivery_steps": _load(row["delivery_steps_json"]) or [], "payable_total": _amount(row["payable_total"]),
                "confirmed_by": row.get("confirmed_by"), "confirmed_at": _iso(row.get("confirmed_at")),
                "revision": int(row["revision"]), "lines": []}

    # -- 设置与派送费规则 --------------------------------------------------------------------

    def save_delivery_settings(self, payload: Mapping[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise FinanceValidationError("设置内容无效。")
        revision = expected_revision(payload.get("expected_revision"))
        settings = parse_settings({key: value for key, value in payload.items() if key != "expected_revision"})

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT revision FROM finance_delivery_bill_settings WHERE id=1 FOR UPDATE")
            row = fetchone(cursor)
            if row is None:
                raise FinanceError("DELIVERY_SETTINGS_MISSING", "派件账单设置缺失，请检查迁移 093 是否已执行。", http_status=503)
            if int(row["revision"]) != revision:
                raise FinanceError("DELIVERY_SETTINGS_CHANGED", "设置已被别人修改，请刷新后重试。")
            cursor.execute("UPDATE finance_delivery_bill_settings SET lookahead_days=%s, include_types_json=%s,"
                           " exclude_types_json=%s, revision=revision+1, updated_by=%s WHERE id=1",
                           (settings["lookahead_days"], json_text(settings["include_types"]),
                            json_text(settings["exclude_types"]), actor.label))
            return {**settings, "revision": revision + 1}

        return self._write(request_id=request_id, action="delivery_settings_save", target_type="delivery_bill_settings",
                           target_id=1, payload=dict(payload), actor=actor, work=work)

    def create_delivery_rule(self, form: Mapping[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        rule = parse_rule_form(form)

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT id FROM finance_delivery_fee_rules WHERE status='active' AND effective_from=%s FOR UPDATE",
                           (rule["effective_from"],))
            if fetchone(cursor):
                raise FinanceError("DELIVERY_RULE_DATE_TAKEN", "这个执行日期已有在用的派送费规则，请先作废旧版或换个日期。")
            cursor.execute("INSERT INTO finance_delivery_fee_rules (effective_from, segments_json, rounding, content_json,"
                           " note, created_by) VALUES (%s, %s, %s, %s, %s, %s)",
                           (rule["effective_from"], json_text(segments_text(rule["segments"])), rule["rounding"],
                            json_text(rule["content"]), rule["note"], actor.label))
            rule_id = int(cursor.lastrowid)
            return {"id": rule_id, "effective_from": rule["effective_from"].isoformat()}

        return self._write(request_id=request_id, action="delivery_rule_create", target_type="delivery_fee_rule",
                           target_id=rule["effective_from"].isoformat(), payload=dict(form), actor=actor, work=work)

    def retire_delivery_rule(self, rule_id: int, payload: Mapping[str, Any], *, request_id: Any,
                             actor: FinanceActor) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) - {"expected_revision"}:
            raise FinanceValidationError("作废内容无效。")
        revision = expected_revision(payload.get("expected_revision"))

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT status, revision FROM finance_delivery_fee_rules WHERE id=%s FOR UPDATE", (rule_id,))
            row = fetchone(cursor)
            if row is None:
                raise FinanceNotFoundError("没有找到这版派送费规则。")
            if row["status"] != "active":
                raise FinanceError("DELIVERY_RULE_RETIRED", "这版规则已经作废。")
            if int(row["revision"]) != revision:
                raise FinanceError("DELIVERY_RULE_CHANGED", "规则已被别人修改，请刷新后重试。")
            cursor.execute("SELECT tracking_number FROM finance_delivery_bill_items WHERE delivery_rule_id=%s"
                           " AND status='confirmed' LIMIT 1", (rule_id,))
            if fetchone(cursor):
                raise FinanceError("DELIVERY_RULE_IN_USE", "已有确认的单用了这版规则，不能作废；请新增一版执行日期更晚的规则。")
            cursor.execute("UPDATE finance_delivery_fee_rules SET status='retired', retired_by=%s,"
                           " retired_at=CURRENT_TIMESTAMP(6), revision=revision+1 WHERE id=%s", (actor.label, rule_id))
            return {"id": rule_id, "revision": revision + 1}

        return self._write(request_id=request_id, action="delivery_rule_retire", target_type="delivery_fee_rule",
                           target_id=rule_id, payload=dict(payload), actor=actor, work=work)

    # -- 每单 --------------------------------------------------------------------------------

    @staticmethod
    def _lock_item(cursor: Any, tracking_number: str) -> dict[str, Any] | None:
        cursor.execute("SELECT status, revision, value_added_total FROM finance_delivery_bill_items"
                       " WHERE tracking_number=%s FOR UPDATE", (tracking_number,))
        return fetchone(cursor)

    @staticmethod
    def _check_revision(row: Mapping[str, Any] | None, expected: int | None) -> None:
        current = None if row is None else int(row["revision"])
        if current != expected:
            raise FinanceError("DELIVERY_BILL_CHANGED", "这单已被别人修改，请重新查询后再操作。")

    def save_delivery_fees(self, tracking_number: str, record: Mapping[str, Any], *, request_id: Any,
                           actor: FinanceActor) -> dict[str, Any]:
        """保存一单的增值费（整组替换）。``record`` 由 Agent 在服务端算好：所选费用、条件、人工金额、明细与合计。"""
        lines: list[Mapping[str, Any]] = record["lines"]
        total = Decimal(str(record["total"]))
        expected = record.get("expected_revision")

        def work(cursor: Any) -> dict[str, Any]:
            row = self._lock_item(cursor, tracking_number)
            self._check_revision(row, expected)
            if row is not None and row["status"] == "confirmed":
                raise FinanceError("DELIVERY_BILL_CONFIRMED", "这单已确认，要改增值费请先撤销确认。")
            values = (record["billing_date"], json_text(record["fee_kinds"]), json_text(record["values"]),
                      json_text(record["manual"]), json_text(record["result"]), total, actor.label)
            if row is None:
                cursor.execute("INSERT INTO finance_delivery_bill_items (tracking_number, arrival_date, billing_date,"
                               " fee_kinds_json, values_json, manual_json, fee_result_json, value_added_total,"
                               " fees_updated_by, fees_updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
                               " CURRENT_TIMESTAMP(6))", (tracking_number, record["arrival_date"], *values))
                revision = 1
            else:
                cursor.execute("UPDATE finance_delivery_bill_items SET billing_date=%s, fee_kinds_json=%s, values_json=%s,"
                               " manual_json=%s, fee_result_json=%s, value_added_total=%s, fees_updated_by=%s,"
                               " fees_updated_at=CURRENT_TIMESTAMP(6), revision=revision+1 WHERE tracking_number=%s",
                               (*values, tracking_number))
                revision = int(row["revision"]) + 1
            cursor.execute("DELETE FROM finance_delivery_bill_fee_lines WHERE tracking_number=%s", (tracking_number,))
            if lines:
                cursor.executemany(
                    "INSERT INTO finance_delivery_bill_fee_lines (tracking_number, position, fee_kind, fee_name, rule_id,"
                    " version_label, outcome, system_amount, amount, counted, note) VALUES (%s, %s, %s, %s, %s, %s, %s,"
                    " %s, %s, %s, %s)",
                    [(tracking_number, line["position"], line["fee_kind"], line["fee_name"][:40], line.get("rule_id") or None,
                      str(line.get("version") or "")[:160], line["outcome"], line.get("system_amount"), line.get("amount"),
                      1 if line["counted"] else 0, str(line.get("note") or "")[:400]) for line in lines])
            return {"tracking_number": tracking_number, "revision": revision, "value_added_total": format(total, "f")}

        payload = {"tracking_number": tracking_number, "fee_kinds": record["fee_kinds"], "values": record["values"],
                   "manual": record["manual"], "expected_revision": expected}
        return self._write(request_id=request_id, action="delivery_fees_save", target_type="delivery_bill",
                           target_id=tracking_number, payload=payload, actor=actor, work=work)

    def confirm_delivery_bill(self, row: Mapping[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """确认一单：``row`` 是 Agent 本次查询快照里的账单行（服务端组成，不来自浏览器）。"""
        tracking_number = row["tracking_number"]
        if not row.get("can_confirm"):
            reason = "；".join(row.get("problems") or ()) or "这单现在不能确认。"
            raise FinanceError("DELIVERY_BILL_NOT_CONFIRMABLE", reason)
        arrival = dt.date.fromisoformat(row["arrival_date"])

        def work(cursor: Any) -> dict[str, Any]:
            item = self._lock_item(cursor, tracking_number)
            self._check_revision(item, row.get("item_revision"))
            if item is not None and item["status"] == "confirmed":
                raise FinanceError("DELIVERY_BILL_CONFIRMED", "这单已确认。")
            cursor.execute("SELECT id FROM finance_delivery_fee_rules WHERE status='active' AND effective_from<=%s"
                           " ORDER BY effective_from DESC LIMIT 1 FOR SHARE", (arrival,))
            rule = fetchone(cursor)
            if rule is None or int(rule["id"]) != int(row["delivery_rule_id"]):
                raise FinanceError("DELIVERY_RULE_CHANGED", "派送费规则已变化，请重新查询后再确认。")
            fee = Decimal(row["delivery_fee"])
            value_added = Decimal(str(item["value_added_total"])) if item is not None else Decimal("0")
            payable = fee + value_added
            if Decimal(row["payable_total"]) != payable:
                raise FinanceError("DELIVERY_BILL_CHANGED", "这单的增值费已变化，请重新查询后再确认。")
            confirm = (Decimal(row["ronghui_amount"]), json_text(row["ronghui_types"]),
                       None if row["settlement_weight"] is None else Decimal(row["settlement_weight"]), row.get("pieces"),
                       fee, int(row["delivery_rule_id"]), json_text(row["delivery_steps"]), payable, actor.label)
            if item is None:
                cursor.execute("INSERT INTO finance_delivery_bill_items (tracking_number, arrival_date, status,"
                               " ronghui_amount, ronghui_detail_json, settlement_weight, pieces, delivery_fee, delivery_rule_id,"
                               " delivery_steps_json, payable_total, confirmed_by, confirmed_at) VALUES (%s, %s, 'confirmed',"
                               " %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP(6))",
                               (tracking_number, arrival, *confirm))
                revision = 1
            else:
                cursor.execute("UPDATE finance_delivery_bill_items SET status='confirmed', arrival_date=%s, ronghui_amount=%s,"
                               " ronghui_detail_json=%s, settlement_weight=%s, pieces=%s, delivery_fee=%s, delivery_rule_id=%s,"
                               " delivery_steps_json=%s, payable_total=%s, confirmed_by=%s, confirmed_at=CURRENT_TIMESTAMP(6),"
                               " revision=revision+1 WHERE tracking_number=%s", (arrival, *confirm, tracking_number))
                revision = int(item["revision"]) + 1
            return {"tracking_number": tracking_number, "revision": revision, "payable_total": format(payable, "f")}

        payload = {"tracking_number": tracking_number, "fingerprint": row["fingerprint"]}
        return self._write(request_id=request_id, action="delivery_bill_confirm", target_type="delivery_bill",
                           target_id=tracking_number, payload=payload, actor=actor, work=work)

    def confirm_delivery_bills(self, rows: Sequence[Mapping[str, Any]], *, request_id: Any,
                               actor: FinanceActor) -> dict[str, Any]:
        """确认多单；每单一个事务，请求标识按单号派生，重复提交只回放结果。"""
        if not 1 <= len(rows) <= MAX_BATCH:
            raise FinanceValidationError(f"一次最多确认 {MAX_BATCH} 单。")
        canonical = parse_request_id(request_id)
        results = []
        for row in rows:
            code = row["tracking_number"]
            try:
                result = self.confirm_delivery_bill(row, request_id=_derived(canonical, f"delivery:{code}"), actor=actor)
                results.append({"tracking_number": code, "ok": True, **result})
            except FinanceError as exc:
                results.append({"tracking_number": code, "ok": False, "error_code": exc.code, "message": exc.message})
        succeeded = sum(1 for item in results if item["ok"])
        return {"results": results, "succeeded": succeeded, "failed": len(results) - succeeded}

    def revoke_delivery_bill(self, tracking_number: str, payload: Mapping[str, Any], *, request_id: Any,
                             actor: FinanceActor) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) - {"expected_revision"}:
            raise FinanceValidationError("撤销内容无效。")
        revision = expected_revision(payload.get("expected_revision"))

        def work(cursor: Any) -> dict[str, Any]:
            item = self._lock_item(cursor, tracking_number)
            if item is None or item["status"] != "confirmed":
                raise FinanceError("DELIVERY_BILL_NOT_CONFIRMED", "这单还没有确认。")
            self._check_revision(item, revision)
            cursor.execute("UPDATE finance_delivery_bill_items SET status='open', ronghui_amount=NULL, ronghui_detail_json=NULL,"
                           " settlement_weight=NULL, pieces=NULL, delivery_fee=NULL, delivery_rule_id=NULL,"
                           " delivery_steps_json=NULL, payable_total=NULL, confirmed_by=NULL, confirmed_at=NULL,"
                           " revision=revision+1 WHERE tracking_number=%s", (tracking_number,))
            return {"tracking_number": tracking_number, "revision": revision + 1}

        return self._write(request_id=request_id, action="delivery_bill_revoke", target_type="delivery_bill",
                           target_id=tracking_number, payload={"tracking_number": tracking_number, **payload},
                           actor=actor, work=work)
