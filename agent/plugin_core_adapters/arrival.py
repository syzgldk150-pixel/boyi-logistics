"""Fresh readback adapters for the signed arrival actions.

The package owns arrival business rules and commit order.  This module owns
only exact managed-resource writes and independent projection/resource reads.
Once a write may have started, every unavailable, ambiguous, incomplete or
mismatching observation is ``WRITE_OUTCOME_UNKNOWN``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, NoReturn, Sequence

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes


logger = logging.getLogger("agent")


ProjectionReplacePort = Callable[[list[dict[str, Any]], str], Mapping[str, Any]]
SnapshotCleanupPort = Callable[[int], Mapping[str, Any]]
ResourceReplacePort = Callable[[str, list[Any], str | None], Mapping[str, Any]]
ResourceRecordReplacePort = Callable[
    [str, str, list[dict[str, Any]], str], Mapping[str, Any]
]
ResourceArchivePort = Callable[
    [str, list[dict[str, Any]], str], Mapping[str, Any]
]


@dataclass(frozen=True)
class ArrivalWritePorts:
    replace_waybill_snapshot: ProjectionReplacePort
    replace_arrival_forecast_snapshot: ProjectionReplacePort
    cleanup_scan_snapshot: SnapshotCleanupPort
    replace_arrival_snapshot: ProjectionReplacePort
    refresh_split_pending_snapshot: ProjectionReplacePort
    replace_arrive_sheet_resource: ResourceReplacePort
    replace_arrival_stats_sheet: ResourceRecordReplacePort
    archive_arrival_stats_sheet: ResourceArchivePort


_ARRIVE_FIELDS = (
    "tracking_number",
    "goods_name",
    "package_type",
    "delivery_method",
    "quantity",
    "receipt_number",
    "actual_weight",
    "volume",
    "remarks",
    "destination_station",
    "recipient_name",
    "recipient_phone",
    "recipient_address",
    "settlement_weight",
    "volumetric_weight",
    "shipping_fee",
    "payment_type",
    "pay_on_arrival",
)
_ARRIVAL_FIELDS = (
    "tracking_number",
    "destination_station",
    "expected_quantity",
    "arrived_quantity",
    "goods_name",
    "package_type",
    "delivery_method",
    "recipient_address",
)
_SPLIT_FIELDS = (
    "tracking_number",
    "source_row_no",
    "destination_station",
    "expected_quantity",
    "arrived_quantity",
    "pending_quantity",
    "problem_type",
    "problem_owner_type",
    "problem_cause",
)
_NUMERIC_RECORD_FIELDS = frozenset(
    {
        "quantity",
        "actual_weight",
        "volume",
        "settlement_weight",
        "volumetric_weight",
        "shipping_fee",
        "pay_on_arrival",
        "source_row_no",
        "expected_quantity",
        "arrived_quantity",
        "pending_quantity",
    }
)
_ARRIVE_NUMERIC_SHEET_COLUMNS = frozenset(
    index for index, field in enumerate(_ARRIVE_FIELDS) if field in _NUMERIC_RECORD_FIELDS
)
_ARRIVE_COMPATIBILITY_TEXT_COLUMNS = frozenset(
    index for index, field in enumerate(_ARRIVE_FIELDS) if field == "remarks"
)
_SHEET_RANGE_RE = re.compile(
    r"(?P<sheet>[^!]+)!(?P<start>[A-Z]+)(?P<start_row>[1-9][0-9]*):"
    r"(?P<end>[A-Z]+)(?P<end_row>[1-9][0-9]*)"
)


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _unknown(message: str, *, cause: Exception | None = None) -> NoReturn:
    error = _error(message, "WRITE_OUTCOME_UNKNOWN")
    if cause is None:
        raise error
    raise error from cause


def _canonical_scalar(value: object) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool) or isinstance(value, (Mapping, list, tuple, set)):
        raise ValueError("non-scalar value")
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("invalid number") from exc
        if not number.is_finite():
            raise ValueError("non-finite number")
        if number == number.to_integral_value():
            return str(int(number))
        return format(number.normalize(), "f")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text).strip()


def _canonical_record_scalar(field: str, value: object) -> str:
    if (
        field not in _NUMERIC_RECORD_FIELDS
        or not isinstance(value, str)
        or not value.strip()
    ):
        return _canonical_scalar(value)
    try:
        return _canonical_scalar(Decimal(value.strip()))
    except InvalidOperation as exc:
        raise ValueError("invalid numeric record field") from exc


def _canonical_records(
    rows: Sequence[Mapping[str, Any]],
    *,
    fields: tuple[str, ...],
    identity_field: str,
) -> list[dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or any(field not in raw for field in fields):
            raise ValueError("record is incomplete")
        record = {
            field: _canonical_record_scalar(field, raw.get(field))
            for field in fields
        }
        identity = record[identity_field]
        if not identity or identity in records:
            raise ValueError("record identity is missing or duplicated")
        records[identity] = record
    return [records[key] for key in sorted(records)]


def _records_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(records))).hexdigest()


def _require_exact_records(
    desired: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
    *,
    fields: tuple[str, ...],
    identity_field: str,
    label: str,
) -> list[dict[str, str]]:
    try:
        expected = _canonical_records(
            desired,
            fields=fields,
            identity_field=identity_field,
        )
        actual = _canonical_records(
            observed,
            fields=fields,
            identity_field=identity_field,
        )
    except (TypeError, ValueError) as exc:
        _unknown(f"{label} fresh readback is incomplete or ambiguous", cause=exc)
    if actual != expected:
        _unknown(f"{label} fresh readback did not match the intended snapshot")
    return actual


def _load_resource(resource_id: str) -> Mapping[str, Any] | None:
    from agent.workflow_resource_store import get_workflow_resource

    return get_workflow_resource(resource_id)


def _exact_sheet_resource(
    resource_id: str,
    *,
    required_any: tuple[tuple[str, ...], ...],
    required: tuple[str, ...],
) -> dict[str, Any]:
    try:
        raw = _load_resource(resource_id)
    except Exception as exc:
        raise _error(
            "the exact arrival sheet resource is unavailable",
            "BROKER_RESOURCE_UNAVAILABLE",
        ) from exc
    if not isinstance(raw, Mapping):
        raise _error(
            "the exact arrival sheet resource no longer exists",
            "BROKER_RESOURCE_UNAVAILABLE",
        )
    resource = dict(raw)
    metadata = resource.get("_meta")
    if (
        resource.get("resource_kind") != "feishu_sheet"
        or not isinstance(metadata, Mapping)
        or str(metadata.get("resource_key") or "").strip() != resource_id
    ):
        raise _error(
            "the exact arrival sheet resource changed kind or identity",
            "BROKER_RESOURCE_MISMATCH",
        )
    if any(not str(resource.get(field) or "").strip() for field in required):
        raise _error(
            "the exact arrival sheet resource is incomplete",
            "BROKER_RESOURCE_INVALID",
        )
    for alternatives in required_any:
        if not any(str(resource.get(field) or "").strip() for field in alternatives):
            raise _error(
                "the exact arrival sheet resource is incomplete",
                "BROKER_RESOURCE_INVALID",
            )
    return resource


def _range_shape(value: object, *, label: str) -> dict[str, Any]:
    text = str(value or "").strip()
    match = _SHEET_RANGE_RE.fullmatch(text)
    if match is None:
        raise _error(f"the exact {label} range is invalid", "BROKER_RESOURCE_INVALID")
    return {
        "range": text,
        "sheet": match.group("sheet"),
        "start_row": int(match.group("start_row")),
        "end_row": int(match.group("end_row")),
    }


def _invoke_feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
    from tools.feishu_cli_tool import feishu_operation

    return feishu_operation(action, params)


def _operation_ok(result: object) -> bool:
    return (
        isinstance(result, Mapping)
        and not result.get("error")
        and not result.get("errors")
    )


def _sheet_values(result: object) -> list[list[Any]]:
    if not _operation_ok(result):
        raise ValueError("sheet request failed")
    assert isinstance(result, Mapping)
    data = result.get("data")
    data_mapping = data if isinstance(data, Mapping) else {}
    value_range = data_mapping.get("valueRange")
    value_mapping = value_range if isinstance(value_range, Mapping) else {}
    nested = data_mapping.get("data")
    nested_mapping = nested if isinstance(nested, Mapping) else {}
    candidates = (
        value_mapping.get("values"),
        nested_mapping.get("values"),
        data_mapping.get("values"),
        result.get("values"),
    )
    for candidate in candidates:
        if isinstance(candidate, list) and all(isinstance(row, list) for row in candidate):
            return [list(row) for row in candidate]
    raise ValueError("sheet response has no values")


def _canonical_rows(
    rows: Sequence[Sequence[object]],
    *,
    width: int,
    numeric_columns: frozenset[int] = frozenset(),
    compatibility_text_columns: frozenset[int] = frozenset(),
) -> list[list[str]]:
    output: list[list[str]] = []
    for row in rows:
        values: list[str] = []
        for index, cell in enumerate(row):
            value = _canonical_scalar(cell)
            if index in compatibility_text_columns:
                value = unicodedata.normalize("NFKC", value)
            if index in numeric_columns and value:
                try:
                    value = _canonical_scalar(Decimal(value))
                except InvalidOperation as exc:
                    raise ValueError("sheet numeric cell is invalid") from exc
            values.append(value)
        if len(values) > width and any(values[width:]):
            raise ValueError("sheet row has unexpected cells")
        output.append(values[:width] + [""] * max(0, width - len(values)))
    while output and not any(output[-1]):
        output.pop()
    return output


def _sheet_mismatch_shape(
    expected: Sequence[Sequence[str]],
    observed: Sequence[Sequence[str]],
) -> dict[str, int]:
    row_count = max(len(expected), len(observed))
    for row_index in range(row_count):
        expected_row = expected[row_index] if row_index < len(expected) else ()
        observed_row = observed[row_index] if row_index < len(observed) else ()
        column_count = max(len(expected_row), len(observed_row))
        for column_index in range(column_count):
            expected_value = expected_row[column_index] if column_index < len(expected_row) else ""
            observed_value = observed_row[column_index] if column_index < len(observed_row) else ""
            if expected_value != observed_value:
                return {
                    "expected_rows": len(expected),
                    "observed_rows": len(observed),
                    "first_row": row_index,
                    "first_column": column_index,
                    "expected_length": len(expected_value),
                    "observed_length": len(observed_value),
                }
    return {
        "expected_rows": len(expected),
        "observed_rows": len(observed),
        "first_row": -1,
        "first_column": -1,
        "expected_length": 0,
        "observed_length": 0,
    }


def _log_sheet_mismatch(
    stage: str,
    expected: Sequence[Sequence[str]],
    observed: Sequence[Sequence[str]],
) -> None:
    shape = _sheet_mismatch_shape(expected, observed)
    logger.warning(
        "Arrive sheet readback mismatch stage=%s expected_rows=%d observed_rows=%d "
        "first_row=%d first_column=%d expected_length=%d observed_length=%d",
        stage,
        shape["expected_rows"],
        shape["observed_rows"],
        shape["first_row"],
        shape["first_column"],
        shape["expected_length"],
        shape["observed_length"],
    )


def _fresh_sheet_rows(
    resource: Mapping[str, Any],
    value_range: str,
    *,
    width: int,
) -> list[list[str]]:
    try:
        raw = _invoke_feishu(
            "read_sheet",
            {
                "spreadsheet_token": str(resource["spreadsheet_token"]),
                "range": value_range,
                "as": "bot",
                "dry_run": False,
            },
        )
        return _canonical_rows(_sheet_values(raw), width=width)
    except Exception as exc:
        _unknown("arrival sheet fresh readback failed", cause=exc)


def _canonical_arrive_readback(
    rows: Sequence[Sequence[object]], *, width: int
) -> list[list[str]]:
    try:
        return _canonical_rows(
            rows,
            width=width,
            numeric_columns=_ARRIVE_NUMERIC_SHEET_COLUMNS,
            compatibility_text_columns=_ARRIVE_COMPATIBILITY_TEXT_COLUMNS,
        )
    except ValueError as exc:
        _unknown("arrive sheet fresh readback has invalid numeric cells", cause=exc)


def _write_sheet_call(action: str, params: dict[str, Any]) -> bool:
    try:
        return _operation_ok(_invoke_feishu(action, params))
    except Exception:
        return False


def _write_waybills(records: list[dict[str, Any]]) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import replace_waybill_records

    return replace_waybill_records(records)


def _read_waybills() -> Sequence[Mapping[str, Any]]:
    from tools.phase7_mysql_store import list_waybill_records

    return list_waybill_records(include_receipt_like=True, include_child_like=True)


def _replace_waybill_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    try:
        _write_waybills(records)
    except Exception:
        pass
    try:
        observed = list(_read_waybills())
    except Exception as exc:
        _unknown("waybill snapshot fresh readback failed", cause=exc)
    actual = _require_exact_records(
        records,
        observed,
        fields=_ARRIVE_FIELDS,
        identity_field="tracking_number",
        label="waybill snapshot",
    )
    return {
        "ok": True,
        "verified": True,
        "record_count": len(actual),
        "target_date": target_date,
        "readback_sha256": _records_sha256(actual),
    }


def _write_forecast(
    business_date: date,
    records: list[dict[str, Any]],
) -> Mapping[str, Any]:
    from tools.daily_sign_store import save_forecast_snapshot

    return save_forecast_snapshot(business_date, records, dry_run=False)


def _write_arrival(
    business_date: date,
    records: list[dict[str, Any]],
) -> Mapping[str, Any]:
    from tools.daily_sign_store import save_arrival_stat_snapshot

    return save_arrival_stat_snapshot(business_date, records, dry_run=False)


def _snapshot_runs(kind: str, target_date: str) -> list[dict[str, Any]]:
    if kind not in {"forecast", "arrival"}:
        raise ValueError("unsupported arrival snapshot kind")
    from tools.phase7_mysql_store import _connect

    run_table = "arrival_forecast_runs" if kind == "forecast" else "arrival_stat_runs"
    item_table = "arrival_forecast_items" if kind == "forecast" else "arrival_stat_items"
    active_clause = "" if kind == "forecast" else " AND run.is_active = TRUE"
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT run.run_id, run.business_date, run.status,
                       run.row_count, run.fingerprint, item.payload_json
                FROM {run_table} AS run
                LEFT JOIN {item_table} AS item ON item.run_id = run.run_id
                WHERE run.business_date = %s{active_clause}
                ORDER BY run.completed_at, run.run_id, item.tracking_number
                """,
                (target_date,),
            )
            rows = cursor.fetchall() or []
    finally:
        connection.close()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("snapshot run has no identity")
        run = grouped.setdefault(
            run_id,
            {
                "run_id": run_id,
                "business_date": str(row.get("business_date") or ""),
                "status": str(row.get("status") or ""),
                "row_count": row.get("row_count"),
                "fingerprint": str(row.get("fingerprint") or ""),
                "items": [],
            },
        )
        payload = row.get("payload_json")
        if payload is None:
            continue
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, Mapping):
            raise ValueError("snapshot item payload is invalid")
        run["items"].append(dict(payload))
    return list(grouped.values())


def _read_forecast_runs(target_date: str) -> Sequence[Mapping[str, Any]]:
    return _snapshot_runs("forecast", target_date)


def _read_arrival_runs(target_date: str) -> Sequence[Mapping[str, Any]]:
    return _snapshot_runs("arrival", target_date)


def _replace_versioned_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
    *,
    fields: tuple[str, ...],
    label: str,
    writer: Callable[[date, list[dict[str, Any]]], Mapping[str, Any]],
    reader: Callable[[str], Sequence[Mapping[str, Any]]],
    active_replacement: bool,
) -> Mapping[str, Any]:
    business_date = date.fromisoformat(target_date)
    try:
        before = list(reader(target_date))
    except Exception as exc:
        raise _error(f"{label} pre-write observation failed", "BROKER_PROJECTION_FAILED") from exc
    before_ids = {str(run.get("run_id") or "") for run in before}
    write_result: Mapping[str, Any] | None = None
    try:
        raw = writer(business_date, records)
        if isinstance(raw, Mapping):
            write_result = raw
    except Exception:
        pass
    try:
        after = list(reader(target_date))
    except Exception as exc:
        _unknown(f"{label} fresh readback failed", cause=exc)
    candidates = [
        run
        for run in after
        if str(run.get("run_id") or "") not in before_ids
    ]
    if active_replacement:
        candidates = list(after)
        if len(candidates) != 1 or str(candidates[0].get("run_id") or "") in before_ids:
            _unknown(f"{label} fresh readback did not identify one new active run")
    elif len(candidates) != 1:
        _unknown(f"{label} fresh readback found zero or multiple new runs")
    run = candidates[0]
    run_id = str(run.get("run_id") or "").strip()
    if (
        not run_id
        or str(run.get("business_date") or "") != target_date
        or str(run.get("status") or "") != "success"
        or isinstance(run.get("row_count"), bool)
        or run.get("row_count") != len(records)
    ):
        _unknown(f"{label} fresh run metadata is incomplete or mismatched")
    if write_result is not None:
        returned_id = str(write_result.get("run_id") or "").strip()
        if returned_id and returned_id != run_id:
            _unknown(f"{label} write response and fresh run identity disagree")
    actual = _require_exact_records(
        records,
        list(run.get("items") or []),
        fields=fields,
        identity_field="tracking_number",
        label=label,
    )
    from tools.daily_sign_store import snapshot_fingerprint

    expected_fingerprint = snapshot_fingerprint(records)
    if str(run.get("fingerprint") or "") != expected_fingerprint:
        _unknown(f"{label} fresh fingerprint did not match")
    return {
        "ok": True,
        "verified": True,
        "record_count": len(actual),
        "target_date": target_date,
        "run_id_sha256": hashlib.sha256(run_id.encode("utf-8")).hexdigest(),
        "readback_sha256": _records_sha256(actual),
    }


def _replace_arrival_forecast_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    return _replace_versioned_snapshot(
        records,
        target_date,
        fields=_ARRIVE_FIELDS,
        label="arrival forecast snapshot",
        writer=_write_forecast,
        reader=_read_forecast_runs,
        active_replacement=False,
    )


def _replace_arrival_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    return _replace_versioned_snapshot(
        records,
        target_date,
        fields=_ARRIVAL_FIELDS,
        label="arrival statistics snapshot",
        writer=_write_arrival,
        reader=_read_arrival_runs,
        active_replacement=True,
    )


def _cleanup_scans(retention_days: int) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import cleanup_scan_codes

    return cleanup_scan_codes(retention_days)


def _observe_cleanup(retention_days: int) -> Sequence[Mapping[str, Any]]:
    from tools.phase7_mysql_store import _connect

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot_date, raw_code, destination, code_type, main_tracking,
                       last_seen_at, seen_count,
                       (snapshot_date < CURDATE() - INTERVAL %s DAY) AS stale
                FROM scan_codes
                ORDER BY snapshot_date, raw_code
                """,
                (retention_days,),
            )
            return cursor.fetchall() or []
    finally:
        connection.close()


def _cleanup_scan_snapshot(retention_days: int) -> Mapping[str, Any]:
    try:
        before = list(_observe_cleanup(retention_days))
    except Exception as exc:
        raise _error("scan cleanup pre-write observation failed", "BROKER_PROJECTION_FAILED") from exc
    try:
        raw = _cleanup_scans(retention_days)
    except Exception:
        raw = None
    if retention_days <= 0:
        try:
            _observe_cleanup(retention_days)
        except Exception as exc:
            _unknown("scan cleanup fresh observation failed", cause=exc)
        return {
            "ok": True,
            "verified": True,
            "deleted": 0,
            "skipped": True,
        }
    try:
        after = list(_observe_cleanup(retention_days))
    except Exception as exc:
        _unknown("scan cleanup fresh readback failed", cause=exc)
    if any(bool(row.get("stale")) for row in after):
        _unknown("scan cleanup fresh readback still contains expired identities")
    after_ids = {
        (str(row.get("snapshot_date") or ""), str(row.get("raw_code") or ""))
        for row in after
    }
    retained = {
        (str(row.get("snapshot_date") or ""), str(row.get("raw_code") or ""))
        for row in before
        if not bool(row.get("stale"))
    }
    if not retained.issubset(after_ids):
        _unknown("scan cleanup removed a non-expired identity")
    deleted = sum(1 for row in before if bool(row.get("stale")))
    if isinstance(raw, Mapping) and raw.get("deleted") not in (None, deleted):
        _unknown("scan cleanup response and fresh readback disagree")
    return {
        "ok": True,
        "verified": True,
        "deleted": deleted,
        "skipped": False,
    }


def _classify_split(records: list[dict[str, Any]], target_date: str) -> tuple[list[dict[str, Any]], list[list[Any]]]:
    from tools.phase7_mysql_store import render_stats_sheet_values
    from tools.split_pending_snapshot import TARGET_HEADERS, classify_sheet_values

    counts = {
        str(row.get("tracking_number") or ""): row.get("arrived_quantity")
        for row in records
    }
    values = render_stats_sheet_values(records, counts, target_date=target_date)
    candidates, _source_rows = classify_sheet_values(values)
    rows = [list(TARGET_HEADERS), *[list(item["sheet_values"]) for item in candidates]]
    return candidates, rows


def _write_split_projection(records: list[dict[str, Any]]) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import replace_split_pending_problem_items

    return replace_split_pending_problem_items(records)


def _read_split_projection() -> Sequence[Mapping[str, Any]]:
    from tools.phase7_mysql_store import list_split_pending_problem_items

    return list_split_pending_problem_items()


def _refresh_split_pending_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    candidates, _rows = _classify_split(records, target_date)
    try:
        _write_split_projection(candidates)
    except Exception:
        pass
    try:
        observed = list(_read_split_projection())
    except Exception as exc:
        _unknown("split-pending projection fresh readback failed", cause=exc)
    actual = _require_exact_records(
        candidates,
        observed,
        fields=_SPLIT_FIELDS,
        identity_field="tracking_number",
        label="split-pending projection",
    )
    return {
        "ok": True,
        "verified": True,
        "record_count": len(records),
        "candidate_count": len(actual),
        "target_date": target_date,
        "readback_sha256": _records_sha256(actual),
    }


def _replace_arrive_sheet(
    resource_id: str,
    rows: list[Any],
    target_date: str | None,
) -> Mapping[str, Any]:
    if target_date is None:
        raise _error("arrive sheet requires its signed target date", "BROKER_ARGUMENT_INVALID")
    resource = _exact_sheet_resource(
        resource_id,
        required_any=(),
        required=("spreadsheet_token", "range", "clear_range"),
    )
    template = _range_shape(resource["range"], label="arrive sheet template")
    clear = _range_shape(resource["clear_range"], label="arrive sheet clear")
    title_range = str(resource.get("title_range") or "").strip()
    title = (
        _range_shape(title_range, label="arrive sheet title")
        if title_range
        else None
    )
    if template["sheet"] != clear["sheet"] or (
        title is not None and title["sheet"] != template["sheet"]
    ):
        raise _error("the exact arrive sheet ranges changed identity", "BROKER_RESOURCE_MISMATCH")
    row_offset = template["start_row"] - clear["start_row"]
    if row_offset < 0:
        raise _error(
            "the arrive sheet template starts before its managed clear range",
            "BROKER_RESOURCE_MISMATCH",
        )
    from tools.arrive_list_sync_tool import _build_title
    from tools.phase7_sync_common import build_range_from_template, parse_a1_range

    expected_rows = [list(row) for row in rows]
    width = 18
    expected_title = [_build_title({"target_date": target_date})] if title is not None else []
    try:
        expected_canonical = _canonical_rows(
            expected_rows,
            width=width,
            numeric_columns=_ARRIVE_NUMERIC_SHEET_COLUMNS,
            compatibility_text_columns=_ARRIVE_COMPATIBILITY_TEXT_COLUMNS,
        )
        expected_snapshot = (
            [[""] * width for _ in range(row_offset)] + expected_canonical
            if expected_canonical
            else []
        )
        title_canonical = _canonical_rows(expected_title, width=width)
    except ValueError as exc:
        raise _error("arrive sheet arguments are invalid", "BROKER_ARGUMENT_INVALID") from exc
    clear_shape = parse_a1_range(clear["range"])
    blank_values = [
        ["" for _ in range(clear_shape["col_count"])]
        for _ in range(clear_shape["row_count"])
    ]
    _write_sheet_call(
        "write_sheet",
        {
            "spreadsheet_token": resource["spreadsheet_token"],
            "range": clear["range"],
            "values": blank_values,
            "as": "bot",
            "dry_run": False,
        },
    )
    observed_rows = _fresh_sheet_rows(resource, clear["range"], width=width)
    observed_rows = _canonical_arrive_readback(observed_rows, width=width)
    if observed_rows == []:
        if expected_rows:
            _write_sheet_call(
                "write_sheet",
                {
                    "spreadsheet_token": resource["spreadsheet_token"],
                    "range": build_range_from_template(template["range"], len(expected_rows), width),
                    "values": expected_rows,
                    "as": "bot",
                    "dry_run": False,
                },
            )
            observed_rows = _fresh_sheet_rows(resource, clear["range"], width=width)
            observed_rows = _canonical_arrive_readback(observed_rows, width=width)
            if observed_rows != expected_snapshot:
                _log_sheet_mismatch("data_write", expected_snapshot, observed_rows)
                _unknown("arrive sheet data write was not confirmed by fresh readback")
    elif observed_rows != expected_snapshot:
        _log_sheet_mismatch("clear", expected_snapshot, observed_rows)
        _unknown("arrive sheet clear was not confirmed by fresh readback")

    if title is not None:
        _write_sheet_call(
            "write_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": title["range"],
                "values": expected_title,
                "as": "bot",
                "dry_run": False,
            },
        )
        observed_title = _fresh_sheet_rows(resource, title["range"], width=width)
        if observed_title != title_canonical:
            _log_sheet_mismatch("title", title_canonical, observed_title)
            _unknown("arrive sheet title write was not confirmed by fresh readback")
    else:
        observed_title = []
    return {
        "ok": True,
        "verified": True,
        "record_count": len(expected_rows),
        "target_date": target_date,
        "readback_sha256": _records_sha256(
            [{"row": row} for row in [*observed_title, *observed_rows]]
        ),
    }


def _stats_values(
    layout: str,
    records: list[dict[str, Any]],
    target_date: str,
) -> list[list[Any]]:
    if layout in {"stats", "split_pending"}:
        if layout == "split_pending":
            _candidates, rows = _classify_split(records, target_date)
            return rows
        from tools.phase7_mysql_store import render_stats_sheet_values

        counts = {
            str(row.get("tracking_number") or ""): row.get("arrived_quantity")
            for row in records
        }
        return render_stats_sheet_values(records, counts, target_date=target_date)
    if layout == "pending":
        from tools.phase7_mysql_store import render_pending_sheet_values

        return render_pending_sheet_values(records)
    raise _error("arrival sheet layout is not signed", "BROKER_RESOURCE_DENIED")


def _replace_arrival_stats_sheet(
    resource_id: str,
    layout: str,
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    if layout == "split_pending":
        resource = _exact_sheet_resource(
            resource_id,
            required_any=(),
            required=("spreadsheet_token", "sheet_id", "range", "clear_range"),
        )
        values = _stats_values(layout, records, target_date)
        sheet_id = str(resource["sheet_id"])
        clear = _range_shape(resource["clear_range"], label="split-pending clear")
        title = _range_shape(resource["range"], label="split-pending title")
        if clear["sheet"] != sheet_id or title["sheet"] != sheet_id:
            raise _error(
                "the split-pending sheet ranges changed identity",
                "BROKER_RESOURCE_MISMATCH",
            )
        clear_result = _write_sheet_call(
            "clear_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": clear["range"],
                "as": "bot",
                "dry_run": False,
            },
        )
        if clear_result:
            _write_sheet_call(
                "write_sheet",
                {
                    "spreadsheet_token": resource["spreadsheet_token"],
                    "range": f"{sheet_id}!A1:S{len(values)}",
                    "values": values,
                    "as": "bot",
                    "dry_run": False,
                },
            )
        managed_range = f"{sheet_id}!A1:S{clear['end_row']}"
        observed = _fresh_sheet_rows(resource, managed_range, width=19)
        try:
            expected = _canonical_rows(values, width=19)
        except ValueError as exc:
            raise _error("split-pending sheet arguments are invalid", "BROKER_ARGUMENT_INVALID") from exc
        if observed != expected:
            _unknown("split-pending sheet fresh readback did not match")
        return {
            "ok": True,
            "verified": True,
            "record_count": len(records),
            "target_date": target_date,
            "readback_sha256": _records_sha256([{"row": row} for row in observed]),
        }

    resource = _exact_sheet_resource(
        resource_id,
        required_any=(("snapshot_range", "range"),),
        required=("spreadsheet_token", "clear_range"),
    )
    values = _stats_values(layout, records, target_date)
    from tools.arrival_stats_sync_tool import (
        _stats_clear_range,
        _stats_title_range,
        _values_for_stats_write,
    )
    from tools.phase7_sync_common import build_range_from_template, parse_a1_range

    template_range = str(resource.get("snapshot_range") or resource.get("range"))
    template = _range_shape(template_range, label="arrival statistics template")
    clear_target = _stats_clear_range(str(resource["clear_range"]), template_range, values)
    clear = _range_shape(clear_target, label="arrival statistics clear")
    title_range = _stats_title_range(resource.get("title_range"), template_range, values)
    title = _range_shape(title_range, label="arrival statistics title") if title_range else None
    if clear["sheet"] != template["sheet"] or (title and title["sheet"] != template["sheet"]):
        raise _error(
            "the arrival statistics sheet ranges changed identity",
            "BROKER_RESOURCE_MISMATCH",
        )
    clear_shape = parse_a1_range(clear_target)
    blank_values = [
        ["" for _ in range(clear_shape["col_count"])]
        for _ in range(clear_shape["row_count"])
    ]
    write_ok = _write_sheet_call(
        "write_sheet",
        {
            "spreadsheet_token": resource["spreadsheet_token"],
            "range": clear_target,
            "values": blank_values,
            "as": "bot",
            "dry_run": False,
        },
    )
    write_values = _values_for_stats_write(template_range, values)
    if write_ok and title is not None:
        write_ok = _write_sheet_call(
            "write_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": title["range"],
                "values": [values[0]],
                "as": "bot",
                "dry_run": False,
            },
        )
    if write_ok and write_values:
        _write_sheet_call(
            "write_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": build_range_from_template(
                    template_range,
                    len(write_values),
                    max(len(row) for row in write_values),
                ),
                "values": write_values,
                "as": "bot",
                "dry_run": False,
            },
        )
    width = len(values[0]) if values else 1
    observed_data = _fresh_sheet_rows(resource, clear_target, width=width)
    observed_title = (
        _fresh_sheet_rows(resource, title["range"], width=width)
        if title is not None
        else []
    )
    try:
        expected_data = _canonical_rows(write_values, width=width)
        expected_title = _canonical_rows([values[0]], width=width) if title is not None else []
    except ValueError as exc:
        raise _error("arrival statistics sheet arguments are invalid", "BROKER_ARGUMENT_INVALID") from exc
    if observed_data != expected_data or observed_title != expected_title:
        _unknown("arrival statistics sheet fresh readback did not match")
    return {
        "ok": True,
        "verified": True,
        "record_count": len(records),
        "target_date": target_date,
        "readback_sha256": _records_sha256(
            [{"row": row} for row in [*observed_title, *observed_data]]
        ),
    }


def _archive_arrival_stats_sheet(
    resource_id: str,
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    resource = _exact_sheet_resource(
        resource_id,
        required_any=(("default_write_range", "source_snapshot_range"),),
        required=("spreadsheet_token",),
    )
    values = _stats_values("stats", records, target_date)
    from tools import arrival_stats_sync_tool as archive_tool

    try:
        sheet_info = archive_tool._find_archive_sheet(resource, target_date, refresh=True)
    except Exception as exc:
        raise _error("arrival archive pre-write lookup failed", "BROKER_RESOURCE_UNAVAILABLE") from exc
    if sheet_info is not None and str(sheet_info.get("title") or "").strip() != target_date:
        raise _error(
            "the exact arrival archive target-date sheet changed identity",
            "BROKER_RESOURCE_MISMATCH",
        )
    if sheet_info is None:
        try:
            add_result = _invoke_feishu(
                "add_sheet",
                {
                    "spreadsheet_token": resource["spreadsheet_token"],
                    "title": target_date,
                    "dry_run": False,
                },
            )
        except Exception:
            add_result = None
        acknowledged_sheet_id = (
            archive_tool._sheet_id_from_add_result(add_result)
            if isinstance(add_result, dict)
            else None
        )
        try:
            sheet_info = archive_tool._find_archive_sheet(
                resource,
                target_date,
                refresh=True,
            )
        except Exception as exc:
            _unknown("arrival archive sheet creation readback failed", cause=exc)
        if sheet_info is None:
            _unknown("arrival archive sheet creation found no exact target-date sheet")
        sheet_id = str(sheet_info.get("sheet_id") or "").strip()
        if not sheet_id:
            _unknown("arrival archive sheet creation returned no exact sheet identity")
        if acknowledged_sheet_id and str(acknowledged_sheet_id).strip() != sheet_id:
            _unknown("arrival archive creation acknowledgement changed sheet identity")
    else:
        sheet_id = str(sheet_info["sheet_id"])
    if str(sheet_info.get("title") or "").strip() != target_date:
        _unknown("arrival archive target-date identity changed")

    clear_range = archive_tool._archive_clear_range(resource, sheet_id, values, sheet_info)
    clear_shape = _range_shape(clear_range, label="arrival archive clear")
    if clear_shape["sheet"] != sheet_id:
        raise _error("arrival archive range changed identity", "BROKER_RESOURCE_MISMATCH")
    from tools.phase7_sync_common import build_range_from_template, parse_a1_range

    parsed_clear = parse_a1_range(clear_range)
    blank_values = [
        ["" for _ in range(parsed_clear["col_count"])]
        for _ in range(parsed_clear["row_count"])
    ]
    write_ok = _write_sheet_call(
        "write_sheet",
        {
            "spreadsheet_token": resource["spreadsheet_token"],
            "range": clear_range,
            "values": blank_values,
            "as": "bot",
            "dry_run": False,
        },
    )
    template_range = archive_tool._resolve_archive_template_range(resource, sheet_id)
    if write_ok:
        _write_sheet_call(
            "write_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": build_range_from_template(
                    template_range,
                    len(values),
                    max(len(row) for row in values),
                ),
                "values": values,
                "as": "bot",
                "dry_run": False,
            },
        )
    observed = _fresh_sheet_rows(resource, clear_range, width=len(values[0]))
    try:
        expected = _canonical_rows(values, width=len(values[0]))
    except ValueError as exc:
        raise _error("arrival archive arguments are invalid", "BROKER_ARGUMENT_INVALID") from exc
    if observed != expected:
        _unknown("arrival archive fresh readback did not match the target-date snapshot")
    return {
        "ok": True,
        "verified": True,
        "record_count": len(records),
        "target_date": target_date,
        "readback_sha256": _records_sha256([{"row": row} for row in observed]),
    }


def build_production_arrival_write_ports() -> ArrivalWritePorts:
    return ArrivalWritePorts(
        replace_waybill_snapshot=_replace_waybill_snapshot,
        replace_arrival_forecast_snapshot=_replace_arrival_forecast_snapshot,
        cleanup_scan_snapshot=_cleanup_scan_snapshot,
        replace_arrival_snapshot=_replace_arrival_snapshot,
        refresh_split_pending_snapshot=_refresh_split_pending_snapshot,
        replace_arrive_sheet_resource=_replace_arrive_sheet,
        replace_arrival_stats_sheet=_replace_arrival_stats_sheet,
        archive_arrival_stats_sheet=_archive_arrival_stats_sheet,
    )


__all__ = ["ArrivalWritePorts", "build_production_arrival_write_ports"]
