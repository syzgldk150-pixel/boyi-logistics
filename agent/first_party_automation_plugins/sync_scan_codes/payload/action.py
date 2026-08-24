"""Package-owned scan pagination, classification and verified batch execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from boyi_plugin_result import (
    broker_evidence_ref,
    postcondition_proof,
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
_PREVIEW_EVIDENCE_CONTRACT_VERSION = 1
_PREVIEW_POSTCONDITION = "authoritative_scan_preview_returned"
_FORMAL_POSTCONDITION = "scan_formal_execution_verified"
_SCAN_PREVIEW_BINDING_FIELD = "_scan_preview_binding"
_SCAN_PREVIEW_BINDING_FIELDS = frozenset(
    {
        "contract_version",
        "plugin_id",
        "preview_run_id",
        "preview_step_id",
        "preview_result_sha256",
        "project_instance_id",
        "generation",
        "contract_digest",
        "configuration_version",
        "target_date",
        "observed_at",
        "expires_at",
        "source_page_count",
        "normalized_record_count",
        "source_snapshot_sha256",
        "source_evidence_count",
        "source_evidence_refs_sha256",
        "selection_count",
        "selection_sha256",
        "batch_count",
        "batch_plan_sha256",
        "formal_arguments_sha256",
        "context_sha256",
    }
)
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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
        _SCAN_PREVIEW_BINDING_FIELD,
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


def _binding_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"scan preview {label} is invalid")
    return value


def _binding_digest(value: object, label: str) -> str:
    result = _text(value, label, maximum=64)
    if not _HEX_SHA256_RE.fullmatch(result):
        raise ValueError(f"scan preview {label} is invalid")
    return result


def _binding_timestamp(value: object, label: str) -> datetime:
    raw = _text(value, label, maximum=64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"scan preview {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"scan preview {label} is invalid")
    return parsed.astimezone(timezone.utc)


def _validate_preview_binding(
    raw: object,
    *,
    formal_arguments: Mapping[str, object],
    target_date: str,
    source_pages: int,
    snapshot: list[dict[str, str]],
    batches: list[list[dict[str, str]]],
) -> dict[str, object]:
    binding = _object(raw, "scan preview binding")
    if set(binding) != _SCAN_PREVIEW_BINDING_FIELDS:
        raise ValueError("scan preview binding schema is invalid")
    supplied_context_sha256 = _binding_digest(
        binding.get("context_sha256"),
        "context_sha256",
    )
    unhashed = dict(binding)
    unhashed.pop("context_sha256")
    if _canonical_sha256(unhashed) != supplied_context_sha256:
        raise ValueError("scan preview binding digest is stale")
    if binding.get("contract_version") != _PREVIEW_EVIDENCE_CONTRACT_VERSION:
        raise ValueError("scan preview binding version is unsupported")
    if binding.get("plugin_id") != ACTION_ID:
        raise ValueError("scan preview binding plugin identity is invalid")
    for field in (
        "preview_run_id",
        "preview_step_id",
        "project_instance_id",
    ):
        if not _text(binding.get(field), field, maximum=128):
            raise ValueError(f"scan preview {field} is missing")
    for field in (
        "preview_result_sha256",
        "contract_digest",
        "source_snapshot_sha256",
        "source_evidence_refs_sha256",
        "selection_sha256",
        "batch_plan_sha256",
        "formal_arguments_sha256",
    ):
        _binding_digest(binding.get(field), field)
    _binding_int(binding.get("generation"), "generation", minimum=1, maximum=2**63 - 1)
    _binding_int(
        binding.get("configuration_version"),
        "configuration_version",
        minimum=1,
        maximum=2**63 - 1,
    )
    expected_source_pages = _binding_int(
        binding.get("source_page_count"),
        "source_page_count",
        minimum=1,
        maximum=_MAX_SOURCE_PAGES,
    )
    expected_normalized = _binding_int(
        binding.get("normalized_record_count"),
        "normalized_record_count",
        minimum=0,
        maximum=_MAX_ITEMS,
    )
    _binding_int(
        binding.get("source_evidence_count"),
        "source_evidence_count",
        minimum=1,
        maximum=_MAX_SOURCE_PAGES,
    )
    expected_selection = _binding_int(
        binding.get("selection_count"),
        "selection_count",
        minimum=0,
        maximum=_MAX_ITEMS,
    )
    expected_batches = _binding_int(
        binding.get("batch_count"),
        "batch_count",
        minimum=0,
        maximum=_MAX_BATCHES,
    )
    observed_at = _binding_timestamp(binding.get("observed_at"), "observed_at")
    expires_at = _binding_timestamp(binding.get("expires_at"), "expires_at")
    if expires_at != observed_at + timedelta(minutes=15):
        raise ValueError("scan preview binding expiry is invalid")
    if datetime.now(timezone.utc) >= expires_at:
        raise ValueError("scan preview binding expired before formal execution")
    if binding.get("target_date") != target_date:
        raise ValueError("scan preview target date changed before formal execution")
    if _canonical_sha256(formal_arguments) != binding["formal_arguments_sha256"]:
        raise ValueError("scan preview formal arguments changed before execution")

    planned_items = [item for batch in batches for item in batch]
    comparisons = {
        "source_page_count": (source_pages, expected_source_pages),
        "normalized_record_count": (len(snapshot), expected_normalized),
        "source_snapshot_sha256": (
            _canonical_sha256(snapshot),
            binding["source_snapshot_sha256"],
        ),
        "selection_count": (len(planned_items), expected_selection),
        "selection_sha256": (
            _canonical_sha256(planned_items),
            binding["selection_sha256"],
        ),
        "batch_count": (len(batches), expected_batches),
        "batch_plan_sha256": (
            _canonical_sha256(batches),
            binding["batch_plan_sha256"],
        ),
    }
    changed = [name for name, (actual, expected) in comparisons.items() if actual != expected]
    if changed:
        raise ValueError(
            "scan preview authoritative revalidation changed: " + ", ".join(changed)
        )
    return {
        "verified": True,
        "preview_run_id": binding["preview_run_id"],
        "preview_step_id": binding["preview_step_id"],
        "context_sha256": supplied_context_sha256,
        "target_date": target_date,
        "source_snapshot_sha256": binding["source_snapshot_sha256"],
        "selection_sha256": binding["selection_sha256"],
        "batch_plan_sha256": binding["batch_plan_sha256"],
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


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


def _preview_evidence(
    *,
    target_date: str,
    snapshot: list[dict[str, str]],
    batches: list[list[dict[str, str]]],
    source_pages: int,
    source_evidence_refs: list[str],
    observed_at: str,
) -> dict[str, object]:
    planned_items = [
        {
            "bill_code": item["bill_code"],
            "station_name": item["station_name"],
        }
        for batch in batches
        for item in batch
    ]
    return {
        "contract_version": _PREVIEW_EVIDENCE_CONTRACT_VERSION,
        "target_date": target_date,
        "observed_at": observed_at,
        "pagination_complete": True,
        "source_page_count": source_pages,
        "normalized_record_count": len(snapshot),
        "source_snapshot_sha256": _canonical_sha256(snapshot),
        "source_evidence_refs": list(source_evidence_refs),
        "selection_count": len(planned_items),
        "selection_sha256": _canonical_sha256(planned_items),
        "batch_count": len(batches),
        "batch_plan_sha256": _canonical_sha256(batches),
        "items": planned_items,
    }


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
    raw_preview_binding = values.get(_SCAN_PREVIEW_BINDING_FIELD)
    if dry_run and raw_preview_binding is not None:
        raise ValueError("dry-run scan cannot receive a formal preview binding")
    if not dry_run and raw_preview_binding is None:
        raise ValueError("formal scan execution requires a preview binding")
    target_date = _target_date(values)
    source_rows, source_pages, evidence_refs = _collect_source(target_date, broker)
    snapshot = _normalize_snapshot(source_rows)
    candidates = _candidate_items(snapshot, skipped=_skip_codes(values.get("skip_bill_codes")))
    batches, omitted = _batch_plan(candidates, values)
    preview_revalidation: dict[str, object] | None = None
    if not dry_run:
        formal_arguments = dict(values)
        formal_arguments.pop(_SCAN_PREVIEW_BINDING_FIELD)
        preview_revalidation = _validate_preview_binding(
            raw_preview_binding,
            formal_arguments=formal_arguments,
            target_date=target_date,
            source_pages=source_pages,
            snapshot=snapshot,
            batches=batches,
        )
    estimated_calls = source_pages + (0 if dry_run else 1 + (2 * len(batches)))
    if estimated_calls > _MAX_BROKER_CALLS:
        raise ValueError("scan execution exceeds its signed broker-call budget")

    scanned = 0
    skipped_signed_codes: list[str] = []
    projection_evidence_ref: str | None = None
    submit_evidence_refs: list[str] = []
    verification_evidence_refs: list[str] = []
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
        expected_projection_sha256 = _canonical_sha256(
            sorted(snapshot, key=lambda row: row["raw_code"])
        )
        if (
            projection.get("committed") is not True
            or projection.get("verified") is not True
            or projection.get("record_count") != len(snapshot)
            or projection.get("identities_sha256") != expected_projection_sha256
        ):
            raise ValueError("scan snapshot was not independently verified")
        projection_evidence_ref = broker_evidence_ref(projection, "scan snapshot")
        evidence_refs.append(projection_evidence_ref)
        for batch in batches:
            batch_scanned, skipped, batch_refs = _submitted_batch(batch, broker)
            scanned += batch_scanned
            skipped_signed_codes.extend(skipped)
            evidence_refs.extend(batch_refs)
            submit_evidence_refs.append(batch_refs[0])
            verification_evidence_refs.append(batch_refs[1])
        if len(skipped_signed_codes) != len(set(skipped_signed_codes)):
            raise ValueError("scan-next batches returned duplicate skipped identities")
        execution_result = (
            "no_data_cleared" if not snapshot else "snapshot_and_batches_verified"
        )

    scheduled = sum(len(batch) for batch in batches)
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    data = {
        "phase": "preview" if dry_run else "formal",
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
    if dry_run:
        preview_evidence = _preview_evidence(
            target_date=target_date,
            snapshot=snapshot,
            batches=batches,
            source_pages=source_pages,
            source_evidence_refs=evidence_refs,
            observed_at=observed_at,
        )
        data["preview_evidence"] = preview_evidence
        preview_source_evidence_refs = preview_evidence["source_evidence_refs"]
        primary_evidence_ref = preview_source_evidence_refs[-1]
        proof = postcondition_proof(
            condition=_PREVIEW_POSTCONDITION,
            observed_at=observed_at,
            evidence_ref=primary_evidence_ref,
            details={
                "phase": "preview",
                "pagination_complete": True,
                "source_page_count": preview_evidence["source_page_count"],
                "normalized_record_count": preview_evidence["normalized_record_count"],
                "source_snapshot_sha256": preview_evidence["source_snapshot_sha256"],
                "source_evidence_refs": list(preview_source_evidence_refs),
                "selection_count": preview_evidence["selection_count"],
                "selection_sha256": preview_evidence["selection_sha256"],
                "batch_count": preview_evidence["batch_count"],
                "batch_plan_sha256": preview_evidence["batch_plan_sha256"],
                "write_attempted": False,
            },
        )
    else:
        data["preview_revalidation"] = preview_revalidation
        if projection_evidence_ref is None:
            raise ValueError("scan snapshot proof is missing")
        primary_evidence_ref = (
            verification_evidence_refs[-1]
            if verification_evidence_refs
            else projection_evidence_ref
        )
        proof = postcondition_proof(
            condition=_FORMAL_POSTCONDITION,
            observed_at=observed_at,
            evidence_ref=primary_evidence_ref,
            details={
                "phase": "formal",
                "preview_revalidation_matched": True,
                "preview_context_sha256": preview_revalidation["context_sha256"],
                "projection_evidence_ref": projection_evidence_ref,
                "projection_record_count": len(snapshot),
                "projection_snapshot_sha256": expected_projection_sha256,
                "batch_count": len(batches),
                "scheduled_items": scheduled,
                "scanned": scanned,
                "skipped_signed_count": len(skipped_signed_codes),
                "submit_evidence_refs": submit_evidence_refs,
                "verification_evidence_refs": verification_evidence_refs,
                "external_write_attempted": bool(batches),
            },
        )
    return success_result(
        data=data,
        source_system="ronghui" if dry_run else "ronghui+internal_projection",
        record_count=len(snapshot),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": proof},
    )
