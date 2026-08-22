"""Unified, account-blind result contract for signed action payloads.

The subprocess owns business evidence but never receives an account identity.
The core execution boundary binds the approved account snapshot as a private
coeffect after validating this JSON result against the signed action schema.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone


def utc_observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def broker_evidence_ref(value: object, label: str) -> str:
    """Extract an opaque, core-issued evidence reference from a primitive."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    reference = str(value.get("evidence_ref") or "").strip()
    if not reference or len(reference) > 512:
        raise ValueError(f"{label} has no valid evidence reference")
    return reference


def success_result(
    *,
    data: Mapping[str, object],
    source_system: str,
    record_count: int,
    pagination_complete: bool,
    evidence_refs: Sequence[str],
    observed_at: str | None = None,
    postconditions: Mapping[str, bool] | None = None,
    postcondition_evidence: Mapping[str, Mapping[str, object]] | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, object]:
    """Build the only success shape accepted from a first-party payload."""

    source = str(source_system or "").strip()
    if not source or len(source) > 128:
        raise ValueError("source_system is invalid")
    if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count < 0:
        raise ValueError("record_count is invalid")
    if not isinstance(pagination_complete, bool):
        raise ValueError("pagination_complete is invalid")
    refs = [str(value or "").strip() for value in evidence_refs]
    if not refs or any(not value or len(value) > 512 for value in refs):
        raise ValueError("evidence_refs are invalid")
    if len(set(refs)) != len(refs):
        raise ValueError("evidence_refs must be unique")
    timestamp = str(observed_at or utc_observed_at()).strip()
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    warning_values = [str(value) for value in warnings]
    meta: dict[str, object] = {
        "source_system": source,
        "observed_at": timestamp,
        "record_count": record_count,
        "pagination_complete": pagination_complete,
        "evidence_refs": refs,
    }
    if postconditions is not None:
        meta["postconditions"] = dict(postconditions)
    if postcondition_evidence is not None:
        meta["postcondition_evidence"] = {
            str(key): dict(value) for key, value in postcondition_evidence.items()
        }
    return {
        "status": "SUCCESS",
        "data": dict(data),
        "meta": meta,
        "warnings": warning_values,
        "error": None,
    }


def postcondition_proof(
    *,
    condition: str,
    observed_at: str,
    evidence_ref: str,
    details: Mapping[str, object],
) -> dict[str, object]:
    """Build a source-evidence proof bound to the result observation time."""

    name = str(condition or "").strip()
    reference = str(evidence_ref or "").strip()
    if not name or not reference:
        raise ValueError("postcondition proof identity is invalid")
    return {
        "condition": name,
        "verified": True,
        "observed_at": observed_at,
        "evidence_ref": reference,
        "details": dict(details),
    }


def executor_success_evidence(
    *,
    action_id: str,
    data: Mapping[str, object],
    observed_at: str,
) -> tuple[str, dict[str, object]]:
    """Match the verifier's canonical executor-success evidence digest."""

    name = str(action_id or "").strip()
    if not name:
        raise ValueError("action_id is invalid")
    canonical = json.dumps(
        dict(data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    reference = f"tool-result:{name}:{digest}"
    return (
        reference,
        postcondition_proof(
            condition="executor_reported_success",
            observed_at=observed_at,
            evidence_ref=reference,
            details={"result_sha256": digest},
        ),
    )


def validate_result(result: object) -> dict[str, object]:
    """Fail closed if an action bypasses the unified signed result contract."""

    if not isinstance(result, Mapping):
        raise TypeError("action result must be an object")
    value = dict(result)
    if set(value) != {"status", "data", "meta", "warnings", "error"}:
        raise ValueError("action result fields are invalid")
    if value.get("status") not in {"SUCCESS", "FAILED"}:
        raise ValueError("action result status is invalid")
    if not isinstance(value.get("data"), Mapping) or not isinstance(value.get("meta"), Mapping):
        raise ValueError("action result data and meta must be objects")
    warnings = value.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise ValueError("action result warnings are invalid")
    error = value.get("error")
    if error is not None and not isinstance(error, Mapping):
        raise ValueError("action result error is invalid")
    if value["status"] == "SUCCESS" and error is not None:
        raise ValueError("successful action result cannot contain an error")
    return value
