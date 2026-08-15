"""Package-owned Yunda dispatch-forecast pagination and append commit."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
)


ACTION_ID = "sync_yunda_dispatch_forecast"
_ACCOUNT_ROLE = "account_id"
_BITABLE_ROLE = "dispatch_forecast_bitable"
_PAGE_SIZE = 200
_MAX_PAGES = 100
_FIELD_MAP = (
    ("ship_id", "主单号"),
    ("unit_cnt", "开单件数"),
    ("scan_cnt", "扫描件数"),
    ("frgt_wgt", "重量/kg"),
    ("frgt_vol", "体积/m3"),
    ("pkg_lod_typ", "包装类型"),
    ("fld_tm", "清场时间"),
    ("plan_tlns", "规划时效"),
    ("rcv_cust_addr", "开单目的地址"),
    ("est_arv_tm", "预计到达时间"),
    ("due_delv_dt", "应派时间"),
)
_NUMBER_FIELDS = frozenset({"开单件数", "扫描件数", "重量/kg", "体积/m3", "规划时效"})
_ALLOWED_ARGUMENTS = frozenset({"target_date", "dest_brch", "ensure_fields", "dry_run"})


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _target_date(arguments: Mapping[str, object]) -> str:
    raw = str(arguments.get("target_date") or "").strip()
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    return (
        datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    ).isoformat()


def _destination_branch(arguments: Mapping[str, object]) -> str:
    value = str(arguments.get("dest_brch") or "").strip()
    if not value or len(value) > 64:
        raise ValueError("dest_brch is invalid")
    return value


def _number(value: object) -> int | str | None:
    if value in (None, "", "null") or isinstance(value, bool):
        return None
    text = str(value).replace(",", "").strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Yunda dispatch numeric field is invalid") from exc
    if not number.is_finite():
        raise ValueError("Yunda dispatch numeric field is invalid")
    if number == number.to_integral_value():
        return int(number)
    return format(number.normalize(), "f")


def _normalize_row(value: object) -> dict[str, object]:
    row = _object(value, "Yunda dispatch row")
    record: dict[str, object] = {}
    for source, target in _FIELD_MAP:
        raw = row.get(source)
        record[target] = _number(raw) if target in _NUMBER_FIELDS else str(raw or "").strip()
    if not record["主单号"]:
        raise ValueError("Yunda dispatch row has no main waybill identity")
    return record


def _collect(
    *,
    target_date: str,
    dest_brch: str,
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], int, list[str]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    records: list[dict[str, object]] = []
    identities: set[str] = set()
    evidence_refs: list[str] = []
    pages = 0
    while True:
        page = _object(
            broker(
                "browser.invoke",
                action="yunda.dispatch_forecast.read_page",
                role=_ACCOUNT_ROLE,
                arguments={
                    "target_date": target_date,
                    "dest_brch": dest_brch,
                    "cursor": cursor,
                    "page_size": _PAGE_SIZE,
                },
            ),
            "Yunda dispatch page",
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("Yunda dispatch page items are invalid")
        evidence_refs.append(broker_evidence_ref(page, "Yunda dispatch page"))
        for raw in items:
            record = _normalize_row(raw)
            identity = str(record["主单号"])
            if identity in identities:
                raise ValueError("Yunda dispatch source contains duplicate main waybills")
            identities.add(identity)
            records.append(record)
        pages += 1
        if pages > _MAX_PAGES:
            raise ValueError("Yunda dispatch pagination exceeded its signed limit")
        if page.get("pagination_complete") is True:
            if page.get("next_cursor") not in (None, ""):
                raise ValueError("complete Yunda dispatch page returned a cursor")
            return records, pages, evidence_refs
        cursor = str(page.get("next_cursor") or "").strip()
        if not cursor or len(cursor) > 2048 or cursor in seen_cursors:
            raise ValueError("Yunda dispatch pagination cursor is invalid")
        seen_cursors.add(cursor)


def run_action(
    arguments: dict[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    values = _object(arguments, "arguments")
    unknown = set(values) - _ALLOWED_ARGUMENTS
    if unknown:
        raise ValueError("Yunda dispatch arguments contain undeclared fields")
    ensure_fields = values.get("ensure_fields", True)
    if not isinstance(ensure_fields, bool):
        raise ValueError("ensure_fields is invalid")
    dry_run = values.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run is invalid")
    target_date = _target_date(values)
    dest_brch = _destination_branch(values)
    records, page_count, evidence_refs = _collect(
        target_date=target_date,
        dest_brch=dest_brch,
        broker=broker,
    )
    written = 0
    execution_result = "dry_run_complete"
    if not dry_run:
        committed = _object(
            broker(
                "network.request",
                action="feishu.bitable.append_yunda_dispatch_forecast",
                role=_BITABLE_ROLE,
                arguments={
                    "records": records,
                    "target_date": target_date,
                    "ensure_fields": ensure_fields,
                },
            ),
            "Yunda dispatch Bitable append",
        )
        readback_sha256 = str(committed.get("readback_sha256") or "").strip()
        if (
            committed.get("committed") is not True
            or committed.get("verified") is not True
            or committed.get("readback_count") != len(records)
            or len(readback_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in readback_sha256
            )
        ):
            raise ValueError(
                "Yunda dispatch Bitable append was not independently verified"
            )
        evidence_refs.append(
            broker_evidence_ref(committed, "Yunda dispatch Bitable append")
        )
        written = int(committed.get("written") or 0)
        if written != len(records):
            raise ValueError("Yunda dispatch Bitable write count changed")
        execution_result = "append_committed"
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    data = {
        "target_date": target_date,
        "dest_brch": dest_brch,
        "total": len(records),
        "fetched": len(records),
        "append_only": True,
        "deleted": 0,
        "written": written,
        "dry_run": dry_run,
        "evidence": {
            "source": "signed_first_party_plugin",
            "observed_at": observed_at,
            "pagination_complete": True,
            "page_count": page_count,
            "execution_result": execution_result,
        },
    }
    result_ref, result_proof = executor_success_evidence(
        action_id=ACTION_ID,
        data=data,
        observed_at=observed_at,
    )
    evidence_refs.append(result_ref)
    return success_result(
        data=data,
        source_system="yunda+feishu" if not dry_run else "yunda",
        record_count=len(records),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": result_proof},
    )
