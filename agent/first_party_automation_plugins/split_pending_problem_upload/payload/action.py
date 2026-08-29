"""Package-owned split/undelivered selection and verified Ronghui writes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation

from boyi_plugin_result import (
    broker_evidence_ref,
    postcondition_proof,
    success_result,
    utc_observed_at,
)


ACTION_ID = "split_pending_problem_upload"
_ACCOUNT_ROLE = "account_id"
_SOURCE_ROLE = "split_pending_source_sheet"
_TARGET_ROLE = "split_pending_target_sheet"
_MAX_SOURCE_ROWS = 5_000
_MAX_SELECTED = 90
_POSTCONDITION = "third_party_split_problem_confirmed"
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_ALLOWED_ARGUMENTS = frozenset(
    {"dry_run", "preview_fingerprint", "selected_bill_codes"}
)
_TARGET_HEADERS = (
    "运单编号",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "回单号",
    "实际重量",
    "体积",
    "备注",
    "目的站点",
    "收件人",
    "收件电话",
    "收件地址",
    "结算重量",
    "体积重",
    "运费",
    "支付类型",
    "到付款",
    "累计到货件数",
)
_SOURCE_FIRST_HEADERS = frozenset({"运单编号", "单号"})
_SOURCE_LAST_HEADERS = frozenset({"累计到货件数", "已到货件数", "到货件数"})


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _text(value: object, label: str, *, maximum: int = 256) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (Mapping, list, tuple, set)):
        raise ValueError(f"{label} is invalid")
    result = str(value).strip()
    if result.startswith("="):
        result = result[1:].strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {"'", '"'}:
        result = result[1:-1].strip()
    if result.endswith(".0") and result[:-2].isdigit():
        result = result[:-2]
    if len(result) > maximum:
        raise ValueError(f"{label} is too long")
    return result


def _waybill(value: object) -> str:
    result = _text(value, "waybill", maximum=128)
    if any(character.isspace() for character in result):
        raise ValueError("waybill contains whitespace")
    return result


def _integer(value: object, label: str) -> int:
    raw = _text(value, label, maximum=64).replace(",", "")
    if not raw:
        raise ValueError(f"{label} is empty")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not an integer") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"{label} is not an integer")
    return int(number)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _normalized_rows(raw_rows: object) -> list[list[object]]:
    if not isinstance(raw_rows, list) or len(raw_rows) > _MAX_SOURCE_ROWS:
        raise ValueError("split source rows are invalid")
    rows: list[list[object]] = []
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) > len(_TARGET_HEADERS):
            raise ValueError("split source row is invalid")
        rows.append(list(raw) + [""] * (len(_TARGET_HEADERS) - len(raw)))
    return rows


def _validate_headers(headers: list[object]) -> None:
    normalized = [_text(value, "source header", maximum=64) for value in headers]
    if normalized[0] not in _SOURCE_FIRST_HEADERS:
        raise ValueError("split source first column is not a waybill")
    for index, expected in enumerate(_TARGET_HEADERS[1:18], start=1):
        if normalized[index] != expected:
            raise ValueError(f"split source column {index + 1} must be {expected}")
    if normalized[18] not in _SOURCE_LAST_HEADERS:
        raise ValueError("split source last column is not an arrival count")


def _classify(rows: list[list[object]]) -> tuple[list[dict[str, object]], int]:
    if not rows:
        raise ValueError("split source has no header row")
    _validate_headers(rows[0])
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    source_rows = 0
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(_text(value, "source cell", maximum=1024) for value in row):
            continue
        bill_code = _waybill(row[0])
        if not bill_code:
            raise ValueError(f"split source row {row_number} has no waybill")
        if bill_code in seen:
            raise ValueError(f"split source contains duplicate waybill {bill_code}")
        seen.add(bill_code)
        source_rows += 1
        expected = _integer(row[4], f"{bill_code} expected quantity")
        arrived = _integer(row[18], f"{bill_code} arrived quantity")
        if expected <= 0 or arrived < 0 or arrived > expected:
            raise ValueError(f"{bill_code} has an invalid arrival quantity")
        if arrived == expected:
            continue
        if arrived == 0:
            problem_type = "有发未到"
            owner = "通知类（不顺延时效）"
            cause = "有发未到"
        else:
            problem_type = "少货/分批"
            owner = "交接异常"
            cause = f"应到{expected}件 实际到{arrived}件"
        sheet_values = [
            _text(value, f"{bill_code} source cell", maximum=1024)
            for value in row[:18]
        ] + [arrived]
        candidates.append(
            {
                "bill_code": bill_code,
                "source_row_no": row_number,
                "destination_station": _text(
                    row[9], f"{bill_code} destination", maximum=256
                ),
                "expected_quantity": expected,
                "arrived_quantity": arrived,
                "pending_quantity": expected - arrived,
                "problem_type": problem_type,
                "problem_owner_type": owner,
                "problem_cause": cause,
                "problem_cause_sha256": hashlib.sha256(cause.encode("utf-8")).hexdigest(),
                "sheet_values": sheet_values,
            }
        )
    if source_rows == 0:
        raise ValueError("split source contains no business rows")
    return candidates, source_rows


def _read_source(
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], int, str]:
    response = _object(
        broker(
            "network.request",
            action="feishu.sheet.read_rows",
            role=_SOURCE_ROLE,
            arguments={"end_column": "S", "max_rows": _MAX_SOURCE_ROWS},
        ),
        "split source response",
    )
    evidence_ref = broker_evidence_ref(response, "split source response")
    if response.get("complete") is not True:
        raise ValueError("split source response is not complete")
    candidates, source_rows = _classify(_normalized_rows(response.get("rows")))
    return candidates, source_rows, evidence_ref


def _stored_state(
    broker: Callable[..., object],
) -> tuple[dict[str, dict[str, str]], str]:
    response = _object(
        broker(
            "projection.invoke",
            action="split_pending.snapshot.read",
            role=_TARGET_ROLE,
            arguments={"max_records": _MAX_SOURCE_ROWS},
        ),
        "split snapshot response",
    )
    evidence_ref = broker_evidence_ref(response, "split snapshot response")
    if response.get("complete") is not True:
        raise ValueError("split snapshot response is not complete")
    raw_records = response.get("records")
    if not isinstance(raw_records, list) or len(raw_records) > _MAX_SOURCE_ROWS:
        raise ValueError("split snapshot records are invalid")
    result: dict[str, dict[str, str]] = {}
    for raw in raw_records:
        row = _object(raw, "split snapshot record")
        if set(row) != {
            "tracking_number",
            "problem_type",
            "upload_status",
            "complaint_status",
        }:
            raise ValueError("split snapshot record schema changed")
        code = _waybill(row.get("tracking_number"))
        problem_type = _text(row.get("problem_type"), "stored problem type", maximum=64)
        upload_status = _text(row.get("upload_status"), "stored upload status", maximum=32)
        complaint_status = _text(
            row.get("complaint_status"), "stored complaint status", maximum=32
        )
        if not code or code in result:
            raise ValueError("split snapshot identity is missing or duplicated")
        result[code] = {
            "problem_type": problem_type,
            "upload_status": upload_status,
            "complaint_status": complaint_status,
        }
    return result, evidence_ref


def _eligible_candidates(
    candidates: list[dict[str, object]],
    stored: Mapping[str, Mapping[str, str]],
) -> tuple[list[dict[str, object]], int]:
    eligible: list[dict[str, object]] = []
    hidden = 0
    for source in candidates:
        candidate = dict(source)
        previous = stored.get(str(candidate["bill_code"]))
        same_type = previous is not None and previous.get("problem_type") == candidate["problem_type"]
        problem_status = previous.get("upload_status", "pending") if same_type else "pending"
        if problem_status not in {"pending", "failed", "success"}:
            raise ValueError(f"{candidate['bill_code']} has an invalid stored problem status")
        complaint_status = "not_applicable"
        complete = problem_status == "success"
        if complete:
            hidden += 1
            continue
        candidate.update(
            {
                "complaint_status": complaint_status,
                "problem_item_status": problem_status,
                "run_problem_item": problem_status != "success",
            }
        )
        eligible.append(candidate)
    return eligible, hidden


def _material(candidate: Mapping[str, object]) -> dict[str, object]:
    return {
        "arrived_quantity": candidate["arrived_quantity"],
        "bill_code": candidate["bill_code"],
        "complaint_status": candidate["complaint_status"],
        "expected_quantity": candidate["expected_quantity"],
        "pending_quantity": candidate["pending_quantity"],
        "problem_cause_sha256": candidate["problem_cause_sha256"],
        "problem_item_status": candidate["problem_item_status"],
        "problem_owner_type": candidate["problem_owner_type"],
        "problem_type": candidate["problem_type"],
    }


def _preview(candidate: Mapping[str, object]) -> dict[str, object]:
    return {
        "arrived_quantity": candidate["arrived_quantity"],
        "bill_code": candidate["bill_code"],
        "complaint_status": candidate["complaint_status"],
        "expected_quantity": candidate["expected_quantity"],
        "pending_quantity": candidate["pending_quantity"],
        "problem_item_status": candidate["problem_item_status"],
        "problem_type": candidate["problem_type"],
        "source_row_no": candidate["source_row_no"],
    }


def _selection(arguments: Mapping[str, object], *, dry_run: bool) -> list[str]:
    raw = arguments.get("selected_bill_codes")
    fingerprint = str(arguments.get("preview_fingerprint") or "").strip()
    if dry_run:
        if raw not in (None, []) or fingerprint:
            raise ValueError("dry_run cannot include a selection or preview_fingerprint")
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("formal execution requires selected_bill_codes")
    if len(raw) > _MAX_SELECTED:
        raise ValueError("selected_bill_codes exceeds its signed limit")
    selected = [_waybill(value) for value in raw]
    if any(not value for value in selected) or len(selected) != len(set(selected)):
        raise ValueError("selected_bill_codes contains an empty or duplicate waybill")
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("formal execution requires a valid preview_fingerprint")
    return selected


def _preflight(
    candidate: Mapping[str, object],
    broker: Callable[..., object],
) -> tuple[dict[str, object], list[str]]:
    evidence_refs: list[str] = []
    problem = _object(
        broker(
            "browser.invoke",
            action="ronghui.problem.query",
            role=_ACCOUNT_ROLE,
            arguments={
                "bill_code": candidate["bill_code"],
                "problem_cause_sha256": candidate["problem_cause_sha256"],
                "problem_owner_type": candidate["problem_owner_type"],
                "problem_type": candidate["problem_type"],
            },
        ),
        "Ronghui problem preflight",
    )
    evidence_refs.append(broker_evidence_ref(problem, "Ronghui problem preflight"))
    if problem.get("ready") is not True or problem.get("bill_code") != candidate["bill_code"]:
        raise ValueError(f"Ronghui problem preflight did not confirm {candidate['bill_code']}")
    problem_ref = str(problem.get("precondition_ref") or "").strip()
    if not problem_ref:
        raise ValueError("Ronghui problem preflight has no precondition")
    existing = problem.get("existing") is True
    existing_result: dict[str, object] | None = None
    if existing:
        external_id = _text(problem.get("external_id"), "existing problem external_id", maximum=256)
        registered_at = _text(problem.get("registered_at"), "existing problem registered_at", maximum=64)
        if not external_id or not registered_at:
            raise ValueError("existing Ronghui problem proof is incomplete")
        existing_result = {
            "external_id": external_id,
            "registered_at": registered_at,
        }
    return {
        "candidate": dict(candidate),
        "problem_precondition_ref": problem_ref,
        "existing_problem": existing_result,
    }, evidence_refs


def _require_committed(result: object, label: str) -> dict[str, object]:
    value = _object(result, label)
    if value.get("committed") is not True:
        raise ValueError(f"{label} was not committed")
    return value


def _verify_problem(
    candidate: Mapping[str, object],
    *,
    external_id: str,
    broker: Callable[..., object],
) -> tuple[dict[str, object], str]:
    result = _object(
        broker(
            "browser.invoke",
            action="ronghui.problem.verify",
            role=_ACCOUNT_ROLE,
            arguments={
                "bill_code": candidate["bill_code"],
                "external_id": external_id,
                "problem_cause_sha256": candidate["problem_cause_sha256"],
                "problem_owner_type": candidate["problem_owner_type"],
                "problem_type": candidate["problem_type"],
            },
        ),
        "Ronghui problem verification",
    )
    evidence_ref = broker_evidence_ref(result, "Ronghui problem verification")
    registered_at = _text(result.get("registered_at"), "problem registered_at", maximum=64)
    if (
        result.get("confirmed") is not True
        or result.get("bill_code") != candidate["bill_code"]
        or result.get("external_id") != external_id
        or result.get("problem_cause_sha256") != candidate["problem_cause_sha256"]
        or result.get("problem_owner_type") != candidate["problem_owner_type"]
        or result.get("problem_type") != candidate["problem_type"]
        or not registered_at
    ):
        raise ValueError(f"Ronghui problem read-back did not confirm {candidate['bill_code']}")
    return {
        "external_id": external_id,
        "registered_at": registered_at,
        "registered_site": _text(result.get("registered_site"), "registered_site", maximum=256),
    }, evidence_ref


def _execute_candidate(
    preflight: Mapping[str, object],
    broker: Callable[..., object],
) -> tuple[dict[str, object], list[str], str]:
    candidate = _object(preflight.get("candidate"), "split candidate")
    evidence_refs: list[str] = []
    existing_problem = preflight.get("existing_problem")
    if candidate["run_problem_item"] is True and not isinstance(existing_problem, Mapping):
        create = _require_committed(
            broker(
                "browser.invoke",
                action="ronghui.problem.create",
                role=_ACCOUNT_ROLE,
                arguments={
                    "bill_code": candidate["bill_code"],
                    "precondition_ref": preflight["problem_precondition_ref"],
                    "problem_cause": candidate["problem_cause"],
                    "problem_owner_type": candidate["problem_owner_type"],
                    "problem_type": candidate["problem_type"],
                    "update_postpone_days": False,
                },
            ),
            "Ronghui problem creation",
        )
        evidence_refs.append(broker_evidence_ref(create, "Ronghui problem creation"))
        problem_external_id = _text(create.get("external_id"), "problem external_id", maximum=256)
        if not problem_external_id:
            raise ValueError("Ronghui problem creation has no external identity")
    elif isinstance(existing_problem, Mapping):
        problem_external_id = _text(
            existing_problem.get("external_id"), "existing problem external_id", maximum=256
        )
    else:
        raise ValueError("stored problem success has no fresh authoritative identity")

    problem, problem_verify_ref = _verify_problem(
        candidate,
        external_id=problem_external_id,
        broker=broker,
    )
    evidence_refs.append(problem_verify_ref)
    event = _require_committed(
        broker(
            "ledger.invoke",
            action="daily_sign.problem_event.upsert",
            role=_ACCOUNT_ROLE,
            arguments={
                "bill_code": candidate["bill_code"],
                "external_id": problem["external_id"],
                "problem_type": candidate["problem_type"],
                "registered_at": problem["registered_at"],
                "registered_site": problem["registered_site"],
            },
        ),
        "problem event ledger",
    )
    evidence_refs.append(broker_evidence_ref(event, "problem event ledger"))
    update = _require_committed(
        broker(
            "projection.invoke",
            action="split_pending.result.upsert",
            role=_TARGET_ROLE,
            arguments={
                "bill_code": candidate["bill_code"],
                "complaint_status": "not_applicable",
                "problem_item_status": "success",
                "problem_type": candidate["problem_type"],
            },
        ),
        "split result projection",
    )
    evidence_refs.append(broker_evidence_ref(update, "split result projection"))
    return {
        "bill_code": candidate["bill_code"],
        "complaint_external_id": "",
        "complaint_status": "not_applicable",
        "problem_external_id": problem["external_id"],
        "problem_item_status": "success",
        "problem_type": candidate["problem_type"],
        "registered_at": problem["registered_at"],
        "verified": True,
    }, evidence_refs, problem_verify_ref


def run_action(
    arguments: Mapping[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    values = _object(arguments, "split arguments")
    unknown = sorted(set(values) - _ALLOWED_ARGUMENTS)
    if unknown:
        raise ValueError(f"split arguments contain undeclared fields: {', '.join(unknown)}")
    dry_run = values.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    selected_codes = _selection(values, dry_run=dry_run)
    snapshot_candidates, source_rows, source_ref = _read_source(broker)
    stored, stored_ref = _stored_state(broker)
    candidates, hidden_completed = _eligible_candidates(snapshot_candidates, stored)
    fingerprint = _canonical_sha256([_material(candidate) for candidate in candidates])
    preview = [_preview(candidate) for candidate in candidates]
    evidence_refs = [source_ref, stored_ref]
    common_data: dict[str, object] = {
        "candidate_count": len(candidates),
        "candidates": preview,
        "complete_count": source_rows - len(snapshot_candidates),
        "hidden_completed_count": hidden_completed,
        "preview_fingerprint": fingerprint,
        "snapshot_count": len(snapshot_candidates),
        "source_rows": source_rows,
        "type_counts": {
            "少货/分批": sum(1 for item in candidates if item["problem_type"] == "少货/分批"),
            "有发未到": sum(1 for item in candidates if item["problem_type"] == "有发未到"),
        },
    }
    observed_at = utc_observed_at()
    if dry_run:
        proof = postcondition_proof(
            condition=_POSTCONDITION,
            observed_at=observed_at,
            evidence_ref=stored_ref,
            details={
                "confirmed_count": 0,
                "dry_run": True,
                "preview_fingerprint": fingerprint,
                "write_attempted": False,
            },
        )
        return success_result(
            data={
                **common_data,
                "dry_run": True,
                "evidence": {
                    "execution_result": "preview_only",
                    "observed_at": observed_at,
                    "source": "signed_first_party_plugin",
                },
                "results": [],
                "selected_bill_codes": [],
            },
            source_system="feishu+mysql",
            record_count=len(candidates),
            pagination_complete=True,
            evidence_refs=evidence_refs,
            observed_at=observed_at,
            postconditions={"0": True},
            postcondition_evidence={"0": proof},
        )

    if str(values["preview_fingerprint"]).strip() != fingerprint:
        raise ValueError("split preview expired before execution")
    by_code = {str(candidate["bill_code"]): candidate for candidate in candidates}
    unavailable = [code for code in selected_codes if code not in by_code]
    if unavailable:
        raise ValueError(f"selected split waybills are unavailable: {', '.join(unavailable)}")
    selected = [by_code[code] for code in selected_codes]

    preflights: list[dict[str, object]] = []
    for candidate in selected:
        preflight, refs = _preflight(candidate, broker)
        preflights.append(preflight)
        evidence_refs.extend(refs)

    snapshot = _require_committed(
        broker(
            "projection.invoke",
            action="split_pending.snapshot.replace",
            role=_TARGET_ROLE,
            arguments={
                "records": [
                    {
                        key: candidate[key]
                        for key in (
                            "arrived_quantity",
                            "bill_code",
                            "destination_station",
                            "expected_quantity",
                            "pending_quantity",
                            "problem_cause",
                            "problem_owner_type",
                            "problem_type",
                            "source_row_no",
                        )
                    }
                    for candidate in snapshot_candidates
                ]
            },
        ),
        "split snapshot replacement",
    )
    evidence_refs.append(broker_evidence_ref(snapshot, "split snapshot replacement"))
    sheet = _require_committed(
        broker(
            "network.request",
            action="feishu.sheet.replace_rows",
            role=_TARGET_ROLE,
            arguments={
                "rows": [list(_TARGET_HEADERS)]
                + [list(candidate["sheet_values"]) for candidate in snapshot_candidates]
            },
        ),
        "split target sheet replacement",
    )
    evidence_refs.append(broker_evidence_ref(sheet, "split target sheet replacement"))

    results: list[dict[str, object]] = []
    verification_refs: list[str] = []
    for preflight in preflights:
        result, refs, verification_ref = _execute_candidate(preflight, broker)
        results.append(result)
        evidence_refs.extend(refs)
        verification_refs.append(verification_ref)

    observed_at = utc_observed_at()
    proof = postcondition_proof(
        condition=_POSTCONDITION,
        observed_at=observed_at,
        evidence_ref=verification_refs[-1],
        details={
            "confirmed_count": len(results),
            "preview_fingerprint": fingerprint,
            "selected_bill_codes": selected_codes,
            "verification_evidence_refs": verification_refs,
        },
    )
    return success_result(
        data={
            **common_data,
            "dry_run": False,
            "evidence": {
                "execution_result": "all_selected_confirmed",
                "observed_at": observed_at,
                "source": "signed_first_party_plugin",
            },
            "results": results,
            "selected_bill_codes": selected_codes,
        },
        source_system="feishu+mysql+ronghui",
        record_count=len(results),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": proof},
    )
