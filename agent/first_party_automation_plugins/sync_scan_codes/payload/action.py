"""Package-owned scan pagination, classification and verified batch execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
)


ACTION_ID = "sync_scan_codes"
_ACCOUNT_ROLE = "account_id"
_MAX_SOURCE_PAGES = 500
_SOURCE_PAGE_SIZE = 200
_DEFAULT_BATCH_SIZE = 50
_MAX_BATCH_SIZE = 200
_MAX_BATCHES = 499
_MAX_ITEMS = 100_000
_MAX_BROKER_CALLS = 1000
_R_CHILD_TRACKING_RE = re.compile(r"^(?:R\d{11}|RC\d{10})\d{4}$")
_RONGHUI_NUMERIC_CHILD_TRACKING_RE = re.compile(r"^200\d{11}$")
_ALLOWED_ARGUMENTS = frozenset(
    {
        "target_date",
        "child_item_limit",
        "batch_size",
        "max_batches",
        "skip_bill_codes",
        "dry_run",
    }
)
_SOURCE_FIELDS = frozenset(
    {"bill_code", "destination", "scan_type", "scan_time", "scan_site"}
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _text(value: object, label: str, *, maximum: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label} must be text")
    result = str(value).strip()
    if len(result) > maximum:
        raise ValueError(f"{label} is too long")
    return result


def _positive_int(
    value: object,
    *,
    default: int,
    maximum: int,
    label: str,
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if str(result) != str(value).strip() or not 1 <= result <= maximum:
        raise ValueError(f"{label} is outside its signed limit")
    return result


def _target_date(arguments: Mapping[str, object]) -> str:
    raw = _text(arguments.get("target_date"), "target_date", maximum=10)
    if not raw:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("target_date must use YYYY-MM-DD") from exc


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_source_row(raw: object) -> dict[str, str]:
    row = _object(raw, "scan source row")
    if set(row) != _SOURCE_FIELDS:
        raise ValueError("scan source row schema changed")
    bill_code = _text(row.get("bill_code"), "bill_code", maximum=128)
    if not bill_code:
        raise ValueError("scan source row has no bill identity")
    return {
        "bill_code": bill_code,
        "destination": _text(row.get("destination"), "destination", maximum=256),
        "scan_type": _text(row.get("scan_type"), "scan_type", maximum=128),
        "scan_time": _text(row.get("scan_time"), "scan_time", maximum=64),
        "scan_site": _text(row.get("scan_site"), "scan_site", maximum=256),
    }


def _collect_source(
    target_date: str,
    broker: Callable[..., object],
) -> tuple[list[dict[str, str]], int, list[str]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    rows: dict[str, dict[str, str]] = {}
    pages = 0
    evidence_refs: list[str] = []
    while True:
        page = _object(
            broker(
                "browser.invoke",
                action="ronghui.scan.read_page",
                role=_ACCOUNT_ROLE,
                arguments={
                    "target_date": target_date,
                    "cursor": cursor,
                    "page_size": _SOURCE_PAGE_SIZE,
                },
            ),
            "scan source page",
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("scan source page items are invalid")
        evidence_refs.append(broker_evidence_ref(page, "scan source page"))
        for raw in items:
            row = _normalize_source_row(raw)
            identity = row["bill_code"]
            previous = rows.get(identity)
            # Ronghui may return the same bill more than once when its scan
            # timestamp/site metadata differs.  The persisted scan index owns
            # only bill identity and destination, so equivalent destination
            # duplicates are one business record; a destination conflict is
            # still ambiguous and must fail closed.
            if previous is not None and previous["destination"] != row["destination"]:
                raise ValueError("scan source returned conflicting duplicate destinations")
            if previous is not None:
                continue
            rows[identity] = row
        if len(rows) > _MAX_ITEMS:
            raise ValueError("scan source exceeds its signed row limit")
        pages += 1
        if pages > _MAX_SOURCE_PAGES:
            raise ValueError("scan source pagination exceeds its signed limit")
        if page.get("pagination_complete") is True:
            if page.get("next_cursor") not in (None, ""):
                raise ValueError("complete scan source page returned a cursor")
            return [rows[key] for key in sorted(rows)], pages, evidence_refs
        cursor = _text(page.get("next_cursor"), "next_cursor", maximum=2048)
        if not cursor or cursor in seen_cursors:
            raise ValueError("scan source pagination cursor is invalid")
        seen_cursors.add(cursor)


def _is_child(code: str, known_codes: set[str]) -> bool:
    if len(code) <= 4 or code.upper().startswith("H"):
        return False
    return bool(
        _R_CHILD_TRACKING_RE.fullmatch(code)
        or _RONGHUI_NUMERIC_CHILD_TRACKING_RE.fullmatch(code)
        or code[:-4] in known_codes
    )


def _normalize_snapshot(source_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    known_codes = {row["bill_code"] for row in source_rows}
    records: list[dict[str, str]] = []
    for row in source_rows:
        raw_code = row["bill_code"]
        if raw_code.upper().startswith("H"):
            continue
        child = _is_child(raw_code, known_codes)
        records.append(
            {
                "raw_code": raw_code,
                "destination": row["destination"],
                "code_type": "child" if child else "main",
                "main_tracking": raw_code[:-4] if child else raw_code,
            }
        )
    return records


def _skip_codes(value: object) -> set[str]:
    if value in (None, ""):
        return set()
    if not isinstance(value, list):
        raise ValueError("skip_bill_codes must be an array")
    result = {
        _text(item, "skip_bill_code", maximum=128)
        for item in value
    }
    if "" in result or len(result) != len(value):
        raise ValueError("skip_bill_codes contains an empty or duplicate identity")
    return result


def _candidate_items(
    snapshot: list[dict[str, str]],
    *,
    skipped: set[str],
) -> list[dict[str, str]]:
    return sorted(
        (
            {"bill_code": row["raw_code"], "station_name": row["destination"]}
            for row in snapshot
            if row["code_type"] == "child"
            and row["destination"]
            and row["raw_code"] not in skipped
        ),
        key=lambda item: (item["station_name"], item["bill_code"]),
    )


def _batch_plan(
    items: list[dict[str, str]],
    arguments: Mapping[str, object],
) -> tuple[list[list[dict[str, str]]], int]:
    limit = _positive_int(
        arguments.get("child_item_limit"),
        default=_MAX_ITEMS,
        maximum=_MAX_ITEMS,
        label="child_item_limit",
    )
    selected = items[:limit]
    batch_size = _positive_int(
        arguments.get("batch_size"),
        default=_DEFAULT_BATCH_SIZE,
        maximum=_MAX_BATCH_SIZE,
        label="batch_size",
    )
    all_batches = [
        selected[index : index + batch_size]
        for index in range(0, len(selected), batch_size)
    ]
    max_batches = _positive_int(
        arguments.get("max_batches"),
        default=_MAX_BATCHES,
        maximum=_MAX_BATCHES,
        label="max_batches",
    )
    batches = all_batches[:max_batches]
    scheduled = sum(len(batch) for batch in batches)
    return batches, len(items) - scheduled


def _submitted_batch(
    items: list[dict[str, str]],
    broker: Callable[..., object],
) -> tuple[int, list[str], list[str]]:
    submit = _object(
        broker(
            "browser.invoke",
            action="ronghui.scan_next.submit",
            role=_ACCOUNT_ROLE,
            arguments={"items": items},
        ),
        "scan-next submit result",
    )
    operation_id = _text(submit.get("operation_id"), "operation_id", maximum=2048)
    items_sha256 = _text(submit.get("items_sha256"), "items_sha256", maximum=64)
    expected_sha256 = _canonical_sha256(items)
    submitted = submit.get("submitted")
    scanned = submit.get("scanned")
    skipped = submit.get("skipped_signed_codes")
    if (
        not operation_id
        or items_sha256 != expected_sha256
        or isinstance(submitted, bool)
        or not isinstance(submitted, int)
        or submitted != len(items)
        or isinstance(scanned, bool)
        or not isinstance(scanned, int)
        or not isinstance(skipped, list)
    ):
        raise ValueError("scan-next submit proof is invalid")
    skipped_codes = [
        _text(item, "skipped_signed_code", maximum=128)
        for item in skipped
    ]
    requested_codes = {item["bill_code"] for item in items}
    if (
        "" in skipped_codes
        or len(skipped_codes) != len(set(skipped_codes))
        or not set(skipped_codes).issubset(requested_codes)
        or scanned + len(skipped_codes) != len(items)
    ):
        raise ValueError("scan-next submitted identities are inconsistent")
    verify = _object(
        broker(
            "browser.invoke",
            action="ronghui.scan_next.verify",
            role=_ACCOUNT_ROLE,
            arguments={
                "operation_id": operation_id,
                "items_sha256": items_sha256,
                "submitted": submitted,
                "scanned": scanned,
                "skipped_signed_codes": skipped_codes,
            },
        ),
        "scan-next verification",
    )
    if (
        verify.get("verified") is not True
        or verify.get("items_sha256") != expected_sha256
        or verify.get("submitted") != submitted
        or verify.get("scanned") != scanned
        or verify.get("skipped_signed_codes") != skipped_codes
        or verify.get("postcondition") != "server_ledger_verified"
        or verify.get("readback_count") != scanned
    ):
        raise ValueError("scan-next postcondition was not verified")
    return (
        scanned,
        skipped_codes,
        [
            broker_evidence_ref(submit, "scan-next submit"),
            broker_evidence_ref(verify, "scan-next verification"),
        ],
    )


def run_action(
    arguments: dict[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    values = _object(arguments, "arguments")
    if set(values) - _ALLOWED_ARGUMENTS:
        raise ValueError("scan arguments contain undeclared fields")
    dry_run = values.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    target_date = _target_date(values)
    source_rows, source_pages, evidence_refs = _collect_source(target_date, broker)
    snapshot = _normalize_snapshot(source_rows)
    candidates = _candidate_items(snapshot, skipped=_skip_codes(values.get("skip_bill_codes")))
    batches, omitted = _batch_plan(candidates, values)
    estimated_calls = source_pages + (0 if dry_run else 1 + (2 * len(batches)))
    if estimated_calls > _MAX_BROKER_CALLS:
        raise ValueError("scan execution exceeds its signed broker-call budget")

    scanned = 0
    skipped_signed_codes: list[str] = []
    execution_result = "dry_run_complete"
    if not dry_run:
        projection = _object(
            broker(
                "projection.invoke",
                action="scan.snapshot.replace",
                role=_ACCOUNT_ROLE,
                arguments={"records": snapshot, "target_date": target_date},
            ),
            "scan snapshot result",
        )
        if projection.get("committed") is not True:
            raise ValueError("scan snapshot was not committed")
        evidence_refs.append(broker_evidence_ref(projection, "scan snapshot"))
        for batch in batches:
            batch_scanned, skipped, batch_refs = _submitted_batch(batch, broker)
            scanned += batch_scanned
            skipped_signed_codes.extend(skipped)
            evidence_refs.extend(batch_refs)
        if len(skipped_signed_codes) != len(set(skipped_signed_codes)):
            raise ValueError("scan-next batches returned duplicate skipped identities")
        execution_result = (
            "no_data_cleared" if not snapshot else "snapshot_and_batches_verified"
        )

    scheduled = sum(len(batch) for batch in batches)
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    data = {
        "dry_run": dry_run,
        "target_date": target_date,
        "fetched": len(source_rows),
        "normalized": len(snapshot),
        "candidate_items": len(candidates),
        "scheduled_items": scheduled,
        "omitted_items": omitted,
        "truncated": omitted > 0,
        "batches": len(batches),
        "scanned": scanned,
        "skipped_signed_count": len(skipped_signed_codes),
        "skipped_signed_codes": skipped_signed_codes,
        "evidence": {
            "source": "signed_first_party_plugin",
            "observed_at": observed_at,
            "pagination_complete": True,
            "page_count": source_pages,
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
        source_system="ronghui" if dry_run else "ronghui+internal_projection",
        record_count=len(snapshot),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": result_proof},
    )
