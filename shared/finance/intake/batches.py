"""Batch intake (P4): store a validated ``finance.source_batch/v1`` batch and post it.

Called by the Agent Host handler with the producing plugin instance and the
read-only HTTP evidence references the Host observed in the same invocation.
One transaction: source producer check → evidence check → period/identity
conflicts → immutable batch and lines → supersede the previous batch of the
same period → post changed days (open periods) or record drift (closed).
A refused batch is recorded separately and returned as ``rejected``.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any, Collection

from shared.finance.db import fetchall, fetchone, json_text
from shared.finance.errors import FinanceError
from shared.finance.intake.contract import IntakeError, validate_batch
from shared.finance.models import FinanceActor, month_of

_KEY_CHUNK = 500


def _text(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.4f}"


def _batch_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": int(row["id"]), "source_id": int(row["source_id"]), "status": row["status"],
            "period_start": row["period_start"].isoformat(), "period_end": row["period_end"].isoformat(),
            "row_count": int(row["row_count"]), "inflow": _text(Decimal(row["inflow"])), "outflow": _text(Decimal(row["outflow"])),
            "opening_balance": _text(row["opening_balance"]), "closing_balance": _text(row["closing_balance"]),
            "content_sha256": row["content_sha256"], "evidence_kind": row["evidence_kind"], "file_id": row["file_id"],
            "automation_id": row["automation_id"], "plugin_version": row["plugin_version"],
            "error_code": row["error_code"], "error_message": row["error_message"], "superseded_by": row["superseded_by"],
            "checks": json.loads(row["checks_json"]) if isinstance(row["checks_json"], (str, bytes)) else row["checks_json"],
            "created_at": row["created_at"].isoformat(sep=" ", timespec="seconds")}


class BatchIntakeMixin:
    def submit_source_batch(
        self, *, automation_id: str, plugin_version: str, payload: Any, observed_read_refs: Collection[str],
    ) -> dict[str, Any]:
        """Validate, store and post one batch; never raises for a contract refusal (returns ``rejected``)."""

        actor = FinanceActor(f"plugin:{automation_id}", f"连接器 {automation_id}"[:128])
        try:
            batch = validate_batch(payload)
            return self._read(lambda cursor: self._accept_batch(cursor, batch, automation_id, plugin_version, set(observed_read_refs), actor))
        except IntakeError as exc:
            return self._reject_batch(payload, automation_id, plugin_version, exc.code, exc.message)
        except FinanceError as exc:
            return self._reject_batch(payload, automation_id, plugin_version, exc.code, exc.message)

    def _reject_batch(self, payload: Any, automation_id: str, plugin_version: str, code: str, message: str) -> dict[str, Any]:
        source_key = payload.get("source_key") if isinstance(payload, dict) else None

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT id, producer_instance_id FROM finance_sources WHERE source_key=%s", (str(source_key or ""),))
            source = fetchone(cursor)
            if source is None or source["producer_instance_id"] != automation_id:
                # Nothing to attach a record to; the plugin still gets the code.
                return {"status": "rejected", "batch_id": None, "error_code": code, "message": message}
            period = payload.get("period") if isinstance(payload, dict) else None
            try:
                start = dt.date.fromisoformat(period["start"])
                end = dt.date.fromisoformat(period["end"])
            except (TypeError, KeyError, ValueError):
                start = end = dt.date.today()
            cursor.execute(
                "INSERT INTO finance_source_batches (source_id, period_start, period_end, status, declares_json, checks_json, row_count,"
                " inflow, outflow, content_sha256, evidence_kind, evidence_json, automation_id, plugin_version, error_code, error_message)"
                " VALUES (%s, %s, %s, 'rejected', %s, %s, 0, 0, 0, %s, %s, %s, %s, %s, %s, %s)",
                (source["id"], start, end, json_text((payload or {}).get("declares") if isinstance(payload, dict) else {}),
                 json_text([]), "0" * 64,
                 "file" if isinstance(payload, dict) and (payload.get("evidence") or {}).get("kind") == "file" else "http_observation",
                 json_text({}), automation_id, str(plugin_version)[:64], code[:64], message[:512]),
            )
            return {"status": "rejected", "batch_id": int(cursor.lastrowid), "error_code": code, "message": message}

        return self._read(work)

    def _accept_batch(self, cursor: Any, batch: dict[str, Any], automation_id: str, plugin_version: str,
                      observed: set[str], actor: FinanceActor) -> dict[str, Any]:
        cursor.execute("SELECT * FROM finance_sources WHERE source_key=%s FOR UPDATE", (batch["source_key"],))
        source = fetchone(cursor)
        if source is None or not source["is_enabled"] or source["producer_instance_id"] != automation_id:
            raise IntakeError("INTAKE_SOURCE", "来源不存在、未启用，或本连接器不是该来源的当前生产者")
        evidence = batch["evidence"]
        file_id = None
        if source["kind"] == "online":
            if evidence["kind"] != "http_observation" or not set(evidence["refs"]) <= observed:
                raise IntakeError("INTAKE_EVIDENCE", "在线来源的证据必须是本次调用中宿主观测到的只读请求")
        else:
            if evidence["kind"] != "file" or len(evidence["refs"]) != 1 or not evidence["refs"][0].isdigit():
                raise IntakeError("INTAKE_EVIDENCE", "文件来源的证据必须是唯一一个上传文件编号")
            file_id = int(evidence["refs"][0])
            cursor.execute("SELECT source_id, status, batch_id FROM finance_source_files WHERE id=%s FOR UPDATE", (file_id,))
            file_row = fetchone(cursor)
            if file_row is not None and file_row["status"] == "consumed" and file_row["batch_id"]:
                cursor.execute("SELECT id, status, content_sha256, row_count FROM finance_source_batches WHERE id=%s", (file_row["batch_id"],))
                earlier = fetchone(cursor)
                if earlier and earlier["status"] == "accepted" and earlier["content_sha256"] == batch["content_sha256"]:
                    # The same file parsed again into the same content: the accepted batch stands.
                    return {"status": "accepted", "batch_id": int(earlier["id"]), "replayed": True, "row_count": int(earlier["row_count"])}
            if file_row is None or int(file_row["source_id"]) != int(source["id"]) or file_row["status"] != "pending":
                raise IntakeError("INTAKE_EVIDENCE", "证据文件不存在、不属于该来源或已被使用")
        cursor.execute(
            "SELECT * FROM finance_source_batches WHERE source_id=%s AND status='accepted' AND period_start <= %s AND period_end >= %s",
            (source["id"], batch["period_end"], batch["period_start"]),
        )
        overlapping = fetchall(cursor)
        previous = None
        for row in overlapping:
            if row["period_start"] == batch["period_start"] and row["period_end"] == batch["period_end"]:
                previous = row
            else:
                raise IntakeError("INTAKE_PERIOD_OVERLAP", f"与已接受批次 {row['id']} 的期间部分重叠")
        if previous is not None and previous["content_sha256"] == batch["content_sha256"] and file_id is None:
            return {"status": "accepted", "batch_id": int(previous["id"]), "replayed": True, "row_count": int(previous["row_count"])}
        keys = [row["source_key"] for row in batch["rows"]]
        by_key = {row["source_key"]: row for row in batch["rows"]}
        for start in range(0, len(keys), _KEY_CHUNK):
            chunk = keys[start:start + _KEY_CHUNK]
            cursor.execute(
                f"SELECT source_key, row_sha256, batch_id FROM finance_source_lines WHERE source_id=%s AND is_active=1"
                f" AND source_key IN ({', '.join(['%s'] * len(chunk))})", (source["id"], *chunk),
            )
            for row in fetchall(cursor):
                if previous is not None and int(row["batch_id"]) == int(previous["id"]):
                    continue
                if row["row_sha256"] != by_key[row["source_key"]]["row_sha256"]:
                    raise IntakeError("INTAKE_CONFLICT", f"流水 {row['source_key']} 已在批次 {row['batch_id']} 中以不同内容存在")
                raise IntakeError("INTAKE_DUPLICATE", f"流水 {row['source_key']} 已在批次 {row['batch_id']} 中存在")
        cursor.execute(
            "INSERT INTO finance_source_batches (source_id, period_start, period_end, status, declares_json, checks_json, row_count,"
            " inflow, outflow, opening_balance, closing_balance, content_sha256, evidence_kind, evidence_json, file_id, automation_id,"
            " plugin_version) VALUES (%s, %s, %s, 'accepted', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (source["id"], batch["period_start"], batch["period_end"], json_text(batch["declares"]), json_text(batch["checks"]),
             len(batch["rows"]), batch["inflow"], batch["outflow"], batch["opening_balance"], batch["closing_balance"],
             batch["content_sha256"], evidence["kind"], json_text(evidence), file_id, automation_id, str(plugin_version)[:64]),
        )
        batch_id = int(cursor.lastrowid)
        rows = batch["rows"]
        for start in range(0, len(rows), 1000):
            cursor.executemany(
                "INSERT INTO finance_source_lines (batch_id, source_id, source_key, business_date, posted_at, amount, item_l1, item_l2,"
                " counterparty, waybill_no, reference, balance_before, balance_after, raw_json, row_sha256)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(batch_id, source["id"], r["source_key"], r["business_date"], r["posted_at"], r["amount"], r["item_l1"], r["item_l2"],
                  r["counterparty"], r["waybill_no"], r["reference"], r["balance_before"], r["balance_after"], json_text(r["raw"]),
                  r["row_sha256"]) for r in rows[start:start + 1000]],
            )
        if file_id is not None:
            cursor.execute("UPDATE finance_source_files SET status='consumed', batch_id=%s WHERE id=%s", (batch_id, file_id))
        result = self._post_batch(cursor, source, batch_id, previous, actor)
        self._refresh_reviews(cursor, int(source["id"]))
        return {"status": "accepted", "batch_id": batch_id, "replayed": False, "row_count": len(rows),
                "superseded_batch_id": int(previous["id"]) if previous else None, **result}

    def _post_batch(self, cursor: Any, source: dict[str, Any], batch_id: int, previous: dict[str, Any] | None,
                    actor: FinanceActor) -> dict[str, Any]:
        old_by_day: dict[dt.date, list[dict[str, Any]]] = {}
        if previous is not None:
            cursor.execute("SELECT * FROM finance_source_lines WHERE batch_id=%s FOR UPDATE", (previous["id"],))
            for line in fetchall(cursor):
                old_by_day.setdefault(line["business_date"], []).append(line)
            cursor.execute("UPDATE finance_source_lines SET is_active=0 WHERE batch_id=%s", (previous["id"],))
            cursor.execute("UPDATE finance_source_batches SET status='superseded', superseded_by=%s WHERE id=%s", (batch_id, previous["id"]))
        cursor.execute("SELECT * FROM finance_source_lines WHERE batch_id=%s", (batch_id,))
        new_by_day: dict[dt.date, list[dict[str, Any]]] = {}
        for line in fetchall(cursor):
            new_by_day.setdefault(line["business_date"], []).append(line)
        posted_days, kept_days, drift_days = [], [], []
        base = str(uuid.uuid5(uuid.NAMESPACE_URL, f"finance-batch:{batch_id}"))
        for day in sorted(set(old_by_day) | set(new_by_day)):
            old, new = old_by_day.get(day, []), new_by_day.get(day, [])
            same = {(l["source_key"], l["row_sha256"]) for l in old} == {(l["source_key"], l["row_sha256"]) for l in new}
            old_vouchers = [int(l["voucher_id"]) for l in old if l["voucher_id"]]
            if same and old and all(l["posting_status"] != "unposted" for l in old):
                by_key = {l["source_key"]: l for l in old}
                cursor.executemany("UPDATE finance_source_lines SET posting_status=%s, voucher_id=%s, rule_id=%s WHERE id=%s",
                                   [(by_key[l["source_key"]]["posting_status"], by_key[l["source_key"]]["voucher_id"],
                                     by_key[l["source_key"]]["rule_id"], l["id"]) for l in new])
                kept_days.append(day.isoformat())
                continue
            if self._period_is_closed(cursor, month_of(day)):
                if new:
                    cursor.execute("UPDATE finance_source_lines SET posting_status='closed_period' WHERE batch_id=%s AND business_date=%s",
                                   (batch_id, day))
                if old or new:
                    self._record_anomaly(cursor, "CLOSED_PERIOD_DRIFT", source_id=int(source["id"]), batch_id=batch_id,
                                         period_month=month_of(day),
                                         detail={"day": day.isoformat(), "old_lines": len(old), "new_lines": len(new),
                                                 "message": "已封账期间的流水发生变化，未改账"})
                drift_days.append(day.isoformat())
                continue
            self._repost_day(cursor, source, day, actor, str(uuid.uuid5(uuid.UUID(base), day.isoformat())), extra_vouchers=old_vouchers)
            posted_days.append(day.isoformat())
        return {"posted_days": posted_days, "kept_days": kept_days, "closed_period_days": drift_days}

    # ---------- reads ----------
    def list_source_batches(self, source_id: Any) -> list[dict[str, Any]]:
        source_id = int(source_id)

        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute("SELECT * FROM finance_source_batches WHERE source_id=%s ORDER BY id DESC LIMIT 200", (source_id,))
            return [_batch_view(row) for row in fetchall(cursor)]

        return self._read(work)

    def list_source_lines(self, batch_id: Any, *, page: int = 1, page_size: int = 200) -> dict[str, Any]:
        batch_id = int(batch_id)
        page = max(1, int(page))
        page_size = min(500, max(1, int(page_size)))

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT COUNT(*) AS n FROM finance_source_lines WHERE batch_id=%s", (batch_id,))
            total = int(fetchone(cursor)["n"])
            cursor.execute(
                "SELECT l.id, l.source_key, l.business_date, l.posted_at, l.amount, l.item_l1, l.item_l2, l.counterparty, l.waybill_no,"
                " l.reference, l.balance_after, l.is_active, l.posting_status, l.voucher_id, v.voucher_no"
                " FROM finance_source_lines l LEFT JOIN finance_vouchers v ON v.id=l.voucher_id WHERE l.batch_id=%s"
                " ORDER BY l.id LIMIT %s OFFSET %s", (batch_id, page_size, (page - 1) * page_size),
            )
            rows = [{**row, "id": int(row["id"]), "business_date": row["business_date"].isoformat(),
                     "amount": _text(Decimal(row["amount"])), "balance_after": _text(row["balance_after"]),
                     "is_active": bool(row["is_active"])} for row in fetchall(cursor)]
            return {"batch_id": batch_id, "total": total, "page": page, "page_size": page_size, "rows": rows}

        return self._read(work)
