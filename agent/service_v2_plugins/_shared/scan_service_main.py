"""Closed service-v2 subprocess entrypoint for verified scan synchronization."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone

from boyi_plugin_sdk import broker_call
from plugin import (
    CONTRIBUTION_TARGETS,
    EXECUTE_OPERATION,
    EXECUTE_PREFLIGHT_SERVICES,
    MUTATING_CONNECTOR_OPERATIONS,
    PLUGIN_ID,
    PREVIEW_OPERATION,
    PREVIEW_PREFLIGHT_SERVICES,
    SERVICE_NAME,
    _connector_target,
    service_invoke_adapter,
)


_REQUEST_FIELDS = {
    "schema_version",
    "runtime_model",
    "automation_id",
    "plugin_id",
    "plugin_version",
    "entrypoint",
    "target",
    "governance",
    "arguments",
}
_ALLOWED_ARGUMENTS = frozenset(
    {
        "target_date",
        "child_item_limit",
        "batch_size",
        "max_batches",
        "skip_bill_codes",
        "dry_run",
        "_scan_preview_binding",
    }
)
_SCAN_PREVIEW_BINDING_FIELD = "_scan_preview_binding"
_PREVIEW_POSTCONDITION = "authoritative_scan_preview_returned"
_FORMAL_POSTCONDITION = "scan_formal_execution_verified"
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_KEYS = ("password", "cookie", "credential", "secret", "token", "session")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_sensitive(value: object) -> None:
    """Reject broker-owned identities and credentials in package data."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if (
                key in {"account_id", "account_ids", "resource_id", "resource_ids"}
                or key.endswith(
                    (
                        "_account_id",
                        "_account_ids",
                        "_resource_id",
                        "_resource_ids",
                    )
                )
                or any(marker in key for marker in _FORBIDDEN_KEYS)
            ):
                raise ValueError("plugin data contains a broker-owned field")
            _reject_sensitive(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive(child)


def _error_code(exc: BaseException, fallback: str) -> str:
    candidate = str(exc).strip().upper()
    return candidate if _SAFE_ERROR_CODE.fullmatch(candidate) else fallback


class _ExecutionTracker:
    """Keep opaque Host Evidence and the write-boundary marker."""

    def __init__(self) -> None:
        self.host_refs: list[str] = []
        self.mutating_started = False


def _operation_preflight(operation: str) -> tuple[str, ...]:
    if operation == PREVIEW_OPERATION:
        return PREVIEW_PREFLIGHT_SERVICES
    if operation == EXECUTE_OPERATION:
        return EXECUTE_PREFLIGHT_SERVICES
    raise ValueError("scan operation is invalid")


def _normalize_operation_arguments(
    operation: str,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    values = dict(arguments)
    if set(values) - _ALLOWED_ARGUMENTS:
        raise ValueError("scan arguments contain undeclared fields")
    if operation == PREVIEW_OPERATION:
        expected_dry_run = True
    elif operation == EXECUTE_OPERATION:
        expected_dry_run = False
    else:
        raise ValueError("scan operation is invalid")
    supplied_dry_run = values.get("dry_run", expected_dry_run)
    if supplied_dry_run is not expected_dry_run:
        raise ValueError("scan operation phase is invalid")
    values["dry_run"] = expected_dry_run
    if operation == PREVIEW_OPERATION:
        if _SCAN_PREVIEW_BINDING_FIELD in values:
            raise ValueError("preview cannot include a scan preview binding")
    else:
        binding = values.get(_SCAN_PREVIEW_BINDING_FIELD)
        if not isinstance(binding, Mapping) or not binding:
            raise ValueError("execute requires a scan preview binding")
        values[_SCAN_PREVIEW_BINDING_FIELD] = dict(binding)
    _reject_sensitive(values)
    return values


def _record_host_evidence(result: object, tracker: _ExecutionTracker) -> object:
    if not isinstance(result, Mapping):
        raise ValueError("Host Connector result is invalid")
    reference = str(result.get("evidence_ref") or "").strip()
    if not reference or len(reference) > 512:
        raise ValueError("Host Connector evidence is missing")
    tracker.host_refs.append(reference)
    if len(tracker.host_refs) != len(set(tracker.host_refs)):
        raise ValueError("Host Connector evidence is duplicated")
    return result


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"scan {label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"scan {label} is invalid")
    return value


def _evidence_ref_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"scan {label} is invalid")
    refs: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 512:
            raise ValueError(f"scan {label} is invalid")
        refs.append(raw)
    if len(refs) != len(set(refs)):
        raise ValueError(f"scan {label} is duplicated")
    return refs


def _validated_counts(
    data: Mapping[str, object],
    *,
    operation: str,
) -> dict[str, int]:
    counts = {
        name: _nonnegative_int(data.get(name), name)
        for name in (
            "candidate_items",
            "scheduled_items",
            "omitted_items",
            "batches",
            "scanned",
            "skipped_signed_count",
        )
    }
    skipped = data.get("skipped_signed_codes")
    if (
        not isinstance(skipped, list)
        or any(not isinstance(item, str) or not item.strip() for item in skipped)
        or len(skipped) != len(set(skipped))
        or len(skipped) != counts["skipped_signed_count"]
    ):
        raise ValueError("scan skipped identities are inconsistent")
    if counts["candidate_items"] != (
        counts["scheduled_items"] + counts["omitted_items"]
    ):
        raise ValueError("scan candidate count conservation failed")
    if data.get("truncated") is not (counts["omitted_items"] > 0):
        raise ValueError("scan truncation proof is inconsistent")
    if counts["batches"] == 0:
        if any(
            counts[name]
            for name in ("candidate_items", "scheduled_items", "omitted_items")
        ):
            raise ValueError("scan zero-batch proof is inconsistent")
    elif counts["batches"] > counts["scheduled_items"]:
        raise ValueError("scan batch count is inconsistent")
    if operation == PREVIEW_OPERATION:
        if counts["scanned"] != 0 or counts["skipped_signed_count"] != 0:
            raise ValueError("scan preview contains execution counts")
    elif operation == EXECUTE_OPERATION:
        if counts["scheduled_items"] != (
            counts["scanned"] + counts["skipped_signed_count"]
        ):
            raise ValueError("scan execution count conservation failed")
    else:
        raise ValueError("scan operation is invalid")
    return counts


def _validated_result_context(
    data: Mapping[str, object],
    meta: Mapping[str, object],
    refs: list[str],
    *,
    operation: str,
) -> tuple[dict[str, int], Mapping[str, object], int, str]:
    expected_phase = "preview" if operation == PREVIEW_OPERATION else "formal"
    expected_dry_run = operation == PREVIEW_OPERATION
    if data.get("phase") != expected_phase or data.get("dry_run") is not expected_dry_run:
        raise ValueError("scan result phase is invalid")
    counts = _validated_counts(data, operation=operation)
    normalized = _nonnegative_int(data.get("normalized"), "normalized count")
    _nonnegative_int(data.get("fetched"), "fetched count")
    if counts["candidate_items"] > normalized:
        raise ValueError("scan candidate count exceeds the normalized snapshot")
    if meta.get("record_count") != normalized or meta.get("pagination_complete") is not True:
        raise ValueError("scan result record proof is invalid")
    evidence = data.get("evidence")
    if not isinstance(evidence, Mapping) or evidence.get("pagination_complete") is not True:
        raise ValueError("scan source evidence is invalid")
    page_count = _nonnegative_int(evidence.get("page_count"), "source page count")
    if page_count < 1 or page_count > len(refs):
        raise ValueError("scan source page evidence is invalid")
    observed_at = str(meta.get("observed_at") or "").strip()
    if not observed_at or evidence.get("observed_at") != observed_at:
        raise ValueError("scan observation time is invalid")
    return counts, evidence, page_count, observed_at


def _validated_preview_proof(
    data: Mapping[str, object],
    details: Mapping[str, object],
    refs: list[str],
    counts: Mapping[str, int],
    *,
    page_count: int,
    observed_at: str,
    primary_evidence_ref: object,
) -> None:
    preview = data.get("preview_evidence")
    if not isinstance(preview, Mapping):
        raise ValueError("scan preview evidence is missing")
    source_refs = _evidence_ref_list(
        preview.get("source_evidence_refs"),
        "preview source evidence",
    )
    if source_refs != refs or page_count != len(refs):
        raise ValueError("scan preview Host evidence order is invalid")
    if (
        primary_evidence_ref != refs[-1]
        or details.get("phase") != "preview"
        or details.get("pagination_complete") is not True
        or details.get("write_attempted") is not False
        or preview.get("pagination_complete") is not True
        or preview.get("observed_at") != observed_at
        or preview.get("target_date") != data.get("target_date")
        or preview.get("source_page_count") != page_count
        or preview.get("normalized_record_count") != data.get("normalized")
        or preview.get("selection_count") != counts["scheduled_items"]
        or preview.get("batch_count") != counts["batches"]
    ):
        raise ValueError("scan preview proof is invalid")
    items = preview.get("items")
    if not isinstance(items, list) or len(items) != counts["scheduled_items"]:
        raise ValueError("scan preview selection proof is invalid")
    for field in (
        "source_snapshot_sha256",
        "selection_sha256",
        "batch_plan_sha256",
    ):
        _digest(preview.get(field), f"preview {field}")
    expected_details = {
        "source_page_count": preview.get("source_page_count"),
        "normalized_record_count": preview.get("normalized_record_count"),
        "source_snapshot_sha256": preview.get("source_snapshot_sha256"),
        "source_evidence_refs": source_refs,
        "selection_count": preview.get("selection_count"),
        "selection_sha256": preview.get("selection_sha256"),
        "batch_count": preview.get("batch_count"),
        "batch_plan_sha256": preview.get("batch_plan_sha256"),
    }
    if any(details.get(key) != value for key, value in expected_details.items()):
        raise ValueError("scan preview action evidence is inconsistent")


def _validated_preview_revalidation(
    data: Mapping[str, object],
    details: Mapping[str, object],
) -> Mapping[str, object]:
    revalidation = data.get("preview_revalidation")
    if not isinstance(revalidation, Mapping) or revalidation.get("verified") is not True:
        raise ValueError("scan preview revalidation proof is missing")
    for field in ("preview_run_id", "preview_step_id", "verified_at"):
        if not isinstance(revalidation.get(field), str) or not revalidation[field].strip():
            raise ValueError("scan preview revalidation proof is invalid")
    context_sha256 = _digest(
        revalidation.get("context_sha256"),
        "preview context digest",
    )
    snapshot_sha256 = _digest(
        revalidation.get("source_snapshot_sha256"),
        "preview snapshot digest",
    )
    _digest(revalidation.get("selection_sha256"), "preview selection digest")
    _digest(revalidation.get("batch_plan_sha256"), "preview batch digest")
    if (
        revalidation.get("target_date") != data.get("target_date")
        or details.get("preview_revalidation_matched") is not True
        or details.get("preview_context_sha256") != context_sha256
        or details.get("projection_snapshot_sha256") != snapshot_sha256
    ):
        raise ValueError("scan preview revalidation proof is inconsistent")
    return revalidation


def _validated_formal_proof(
    data: Mapping[str, object],
    details: Mapping[str, object],
    refs: list[str],
    counts: Mapping[str, int],
    *,
    page_count: int,
    primary_evidence_ref: object,
) -> None:
    _validated_preview_revalidation(data, details)
    projection_ref = details.get("projection_evidence_ref")
    if not isinstance(projection_ref, str) or not projection_ref.strip():
        raise ValueError("scan projection proof is missing")
    if (
        details.get("phase") != "formal"
        or details.get("projection_record_count") != data.get("normalized")
        or details.get("batch_count") != counts["batches"]
        or details.get("scheduled_items") != counts["scheduled_items"]
        or details.get("scanned") != counts["scanned"]
        or details.get("skipped_signed_count")
        != counts["skipped_signed_count"]
    ):
        raise ValueError("scan formal count proof is invalid")
    _digest(details.get("projection_snapshot_sha256"), "projection snapshot digest")
    submit_refs = _evidence_ref_list(
        details.get("submit_evidence_refs"),
        "submit evidence",
    )
    verification_refs = _evidence_ref_list(
        details.get("verification_evidence_refs"),
        "verification evidence",
    )
    if (
        len(submit_refs) != counts["batches"]
        or len(verification_refs) != counts["batches"]
    ):
        raise ValueError("scan per-batch evidence count is invalid")
    batch_refs = [
        reference
        for pair in zip(submit_refs, verification_refs, strict=True)
        for reference in pair
    ]
    expected_refs = [*refs[:page_count], projection_ref, *batch_refs]
    if refs != expected_refs:
        raise ValueError("scan formal Host evidence order is invalid")
    if counts["batches"] == 0:
        if (
            submit_refs
            or verification_refs
            or details.get("external_write_attempted") is not False
            or primary_evidence_ref != projection_ref
        ):
            raise ValueError("scan zero-candidate proof is invalid")
    elif (
        details.get("external_write_attempted") is not True
        or primary_evidence_ref != verification_refs[-1]
        or refs[-1] != verification_refs[-1]
    ):
        raise ValueError("scan formal external-write proof is invalid")


def _validated_action_proof(
    data: Mapping[str, object],
    meta: Mapping[str, object],
    refs: list[str],
    *,
    operation: str,
) -> tuple[dict[str, object], str]:
    counts, _evidence, page_count, observed_at = _validated_result_context(
        data,
        meta,
        refs,
        operation=operation,
    )
    postconditions = meta.get("postconditions")
    raw_proofs = meta.get("postcondition_evidence")
    if postconditions != {"0": True} or not isinstance(raw_proofs, Mapping):
        raise ValueError("scan action postcondition is invalid")
    proof = raw_proofs.get("0")
    if not isinstance(proof, Mapping):
        raise ValueError("scan action proof is missing")
    expected_condition = (
        _PREVIEW_POSTCONDITION
        if operation == PREVIEW_OPERATION
        else _FORMAL_POSTCONDITION
    )
    details = proof.get("details")
    primary_evidence_ref = proof.get("evidence_ref")
    if (
        proof.get("condition") != expected_condition
        or proof.get("verified") is not True
        or proof.get("observed_at") != observed_at
        or not isinstance(primary_evidence_ref, str)
        or primary_evidence_ref not in refs
        or not isinstance(details, Mapping)
    ):
        raise ValueError("scan action proof is invalid")
    if operation == PREVIEW_OPERATION:
        _validated_preview_proof(
            data,
            details,
            refs,
            counts,
            page_count=page_count,
            observed_at=observed_at,
            primary_evidence_ref=primary_evidence_ref,
        )
    else:
        _validated_formal_proof(
            data,
            details,
            refs,
            counts,
            page_count=page_count,
            primary_evidence_ref=primary_evidence_ref,
        )
    return dict(proof), observed_at


def _service_success_result(
    value: object,
    tracker: _ExecutionTracker,
    *,
    operation: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("status") != "SUCCESS":
        raise ValueError("scan action did not return success")
    data = value.get("data")
    meta = value.get("meta")
    warnings = value.get("warnings")
    if (
        not isinstance(data, Mapping)
        or not isinstance(meta, Mapping)
        or not isinstance(warnings, list)
    ):
        raise ValueError("scan action result is invalid")
    refs = list(tracker.host_refs)
    if not refs or len(refs) != len(set(refs)):
        raise ValueError("scan Host evidence is missing or duplicated")
    action_refs = meta.get("evidence_refs")
    if not isinstance(action_refs, list) or action_refs != refs:
        raise ValueError("scan Host evidence does not match action evidence")
    if operation == PREVIEW_OPERATION and tracker.mutating_started:
        raise ValueError("scan preview crossed a write boundary")
    if operation == EXECUTE_OPERATION and not tracker.mutating_started:
        raise ValueError("scan formal execution has no projection write")
    action_proof, observed_at = _validated_action_proof(
        data,
        meta,
        refs,
        operation=operation,
    )
    action_evidence = data.get("evidence")
    if not isinstance(action_evidence, Mapping):
        raise ValueError("scan source evidence is missing")
    outcome = "READ_ONLY" if operation == PREVIEW_OPERATION else "WRITE_VERIFIED"
    projected_data = dict(data)
    projected_data["evidence"] = {
        **dict(action_evidence),
        "service": SERVICE_NAME,
        "operation": operation,
        "outcome": outcome,
        "observed_at": observed_at,
    }
    result_summary = {
        **projected_data,
        "evidence": dict(projected_data["evidence"]),
    }
    proof = {
        "condition": "plugin_result_contract_valid",
        "verified": True,
        "observed_at": observed_at,
        "evidence_ref": refs[-1],
        "details": {
            "result_summary": result_summary,
            "evidence_refs": list(refs),
            "action_postconditions": {"0": True},
            "action_postcondition_evidence": {"0": action_proof},
        },
    }
    projected_meta = dict(meta)
    projected_meta.update(
        {
            "evidence_refs": refs,
            "postconditions": {"0": True},
            "postcondition_evidence": {"0": proof},
            "write_outcome": (
                "NOT_APPLIED"
                if operation == PREVIEW_OPERATION
                else "WRITE_VERIFIED"
            ),
        }
    )
    return {
        "status": "SUCCESS",
        "data": projected_data,
        "meta": projected_meta,
        "warnings": list(warnings),
        "error": None,
    }


def _failure_result(
    *,
    code: str,
    write_outcome: str,
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "status": "FAILED",
        "data": {"completed_results": []},
        "meta": {
            "source_system": "ronghui+internal_projection",
            "observed_at": _utc_now(),
            "record_count": 0,
            "pagination_complete": False,
            "evidence_refs": list(evidence_refs or []),
            "write_outcome": write_outcome,
        },
        "warnings": [],
        "error": {
            "code": code,
            "message": "The scan operation did not produce a complete result",
            "retryable": False,
        },
    }


def _read_request() -> tuple[str, dict[str, object]]:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("service request schema is invalid")
    if (
        request.get("schema_version") != 2
        or request.get("runtime_model") != "SERVICE_V2"
        or request.get("automation_id") != os.environ.get("BOYI_AUTOMATION_ID", "")
        or request.get("plugin_id") != PLUGIN_ID
        or request.get("plugin_id") != os.environ.get("BOYI_PLUGIN_ID", "")
        or request.get("plugin_version")
        != os.environ.get("BOYI_PLUGIN_VERSION", "")
        or not isinstance(request.get("arguments"), dict)
    ):
        raise ValueError("service request identity is invalid")
    entrypoint = str(request.get("entrypoint") or "")
    if entrypoint in CONTRIBUTION_TARGETS:
        contribution_id, contribution_kind, allowed_operation = (
            CONTRIBUTION_TARGETS[entrypoint]
        )
        expected_identity = (contribution_id, contribution_kind)
        allowed_operations = {allowed_operation}
    elif entrypoint == "service":
        expected_identity = ("host.service.invoke", "service")
        allowed_operations = {PREVIEW_OPERATION, EXECUTE_OPERATION}
    else:
        raise ValueError("service request entrypoint is invalid")
    if (
        not isinstance(expected_identity, tuple)
        or len(expected_identity) != 2
        or not all(isinstance(item, str) and item for item in expected_identity)
    ):
        raise ValueError("service contribution identity is invalid")
    target = request.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("service target is invalid")
    operation = target.get("operation")
    expected_target = {
        "service": SERVICE_NAME,
        "operation": operation,
        "contribution_id": expected_identity[0],
        "contribution_kind": expected_identity[1],
    }
    if (
        operation not in allowed_operations
        or dict(target) != expected_target
    ):
        raise ValueError("service target is invalid")
    selected_operation = str(operation)
    expected_governance = {
        PREVIEW_OPERATION: {
            "effect": "read",
            "operation_type": "read",
            "broker_effect": "read",
            "harness_allowed": True,
        },
        EXECUTE_OPERATION: {
            "effect": "external_write",
            "operation_type": "external_write",
            "broker_effect": "write",
            "harness_allowed": False,
        },
    }[selected_operation]
    governance = request.get("governance")
    if (
        not isinstance(governance, Mapping)
        or type(governance.get("harness_allowed")) is not bool
        or dict(governance) != expected_governance
    ):
        raise ValueError("service governance is invalid")
    arguments = _normalize_operation_arguments(
        selected_operation,
        request["arguments"],
    )
    return selected_operation, arguments


def run_scan_service(
    operation: str,
    arguments: Mapping[str, object],
    *,
    tracker: _ExecutionTracker | None = None,
) -> dict[str, object]:
    """Run the embedded v1 scan action through exact Host Connectors."""

    values = _normalize_operation_arguments(operation, arguments)
    import action

    execution = tracker or _ExecutionTracker()
    preflight_services = _operation_preflight(operation)
    sent_preflight = False

    def broker(
        primitive_operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        nonlocal sent_preflight
        _, connector_operation = _connector_target(
            primitive_operation,
            action,
            role,
            arguments,
        )
        if connector_operation in MUTATING_CONNECTOR_OPERATIONS:
            # Enter the uncertainty boundary before snapshot replacement or
            # scan submission. A lost response after either call is not safe
            # to retry blindly.
            execution.mutating_started = True
        result = service_invoke_adapter(
            broker_call,
            primitive_operation,
            action=action,
            role=role,
            arguments=arguments,
            preflight_services=preflight_services if not sent_preflight else (),
        )
        sent_preflight = True
        return _record_host_evidence(result, execution)

    result = action.run_action(values, broker)
    return _service_success_result(result, execution, operation=operation)


def run_scan_action_offline(
    operation: str | Mapping[str, object],
    arguments: Mapping[str, object] | object,
    host_broker: object | None = None,
) -> dict[str, object]:
    """Run the embedded action with a caller-injected offline Host broker.

    The explicit ``operation, arguments, broker`` form mirrors the package
    runtime. Fixture callers may also pass ``arguments, broker`` and derive
    the immutable phase from ``dry_run``.
    """

    if isinstance(operation, str):
        if host_broker is None or not isinstance(arguments, Mapping):
            raise ValueError("offline scan broker arguments are invalid")
        selected_operation = operation
        selected_arguments = dict(arguments)
        selected_broker = host_broker
    else:
        if host_broker is not None:
            raise ValueError("offline scan broker arguments are invalid")
        selected_arguments = dict(operation)
        selected_broker = arguments
        selected_operation = (
            PREVIEW_OPERATION
            if selected_arguments.get("dry_run") is True
            else EXECUTE_OPERATION
        )
    values = _normalize_operation_arguments(
        selected_operation,
        selected_arguments,
    )
    import action

    preflight_services = _operation_preflight(selected_operation)
    sent_preflight = False

    def broker(
        primitive_operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        nonlocal sent_preflight
        result = service_invoke_adapter(
            selected_broker,
            primitive_operation,
            action=action,
            role=role,
            arguments=arguments,
            preflight_services=preflight_services if not sent_preflight else (),
        )
        sent_preflight = True
        return result

    return action.run_action(values, broker)


def main() -> int:
    tracker = _ExecutionTracker()
    try:
        operation, arguments = _read_request()
        result = run_scan_service(operation, arguments, tracker=tracker)
        _reject_sensitive(result)
    except ValueError as exc:
        result = _failure_result(
            code=_error_code(
                exc,
                (
                    "SERVICE_EXECUTION_FAILED"
                    if tracker.mutating_started
                    else "INVALID_CONFIGURATION"
                ),
            ),
            write_outcome=(
                "WRITE_OUTCOME_UNKNOWN" if tracker.mutating_started else "NOT_APPLIED"
            ),
            evidence_refs=tracker.host_refs,
        )
    except Exception as exc:
        result = _failure_result(
            code=_error_code(exc, "SERVICE_EXECUTION_FAILED"),
            write_outcome=(
                "WRITE_OUTCOME_UNKNOWN" if tracker.mutating_started else "NOT_APPLIED"
            ),
            evidence_refs=tracker.host_refs,
        )
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
