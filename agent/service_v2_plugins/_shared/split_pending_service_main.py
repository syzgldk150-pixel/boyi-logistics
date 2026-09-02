"""Closed service-v2 subprocess entrypoint for split-pending problem upload."""

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
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")
_FORBIDDEN_KEYS = ("password", "cookie", "credential", "secret", "token", "session")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


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
    raise ValueError("split-pending operation is invalid")


def _validate_operation_arguments(
    operation: str,
    arguments: Mapping[str, object],
) -> None:
    if operation == PREVIEW_OPERATION:
        if arguments.get("dry_run") is not True:
            raise ValueError("preview requires dry_run=true")
        selected = arguments.get("selected_bill_codes")
        fingerprint = str(arguments.get("preview_fingerprint") or "").strip()
        if selected not in (None, []):
            raise ValueError("preview cannot include selected_bill_codes")
        if fingerprint:
            raise ValueError("preview cannot include preview_fingerprint")
        return
    if operation != EXECUTE_OPERATION:
        raise ValueError("split-pending operation is invalid")
    if arguments.get("dry_run") is not False:
        raise ValueError("execute requires dry_run=false")
    selected = arguments.get("selected_bill_codes")
    if not isinstance(selected, list) or not selected:
        raise ValueError("execute requires selected_bill_codes")
    fingerprint = arguments.get("preview_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or _FINGERPRINT.fullmatch(fingerprint.strip()) is None
    ):
        raise ValueError("execute requires a valid preview_fingerprint")


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


def _validated_action_proof(
    data: Mapping[str, object],
    meta: Mapping[str, object],
    refs: list[str],
    *,
    operation: str,
    observed_at: str,
) -> dict[str, object]:
    """Retain the action-owned per-ticket proof inside the Host proof."""

    postconditions = meta.get("postconditions")
    raw_proofs = meta.get("postcondition_evidence")
    if postconditions != {"0": True} or not isinstance(raw_proofs, Mapping):
        raise ValueError("split-pending action postcondition is invalid")
    proof = raw_proofs.get("0")
    if not isinstance(proof, Mapping):
        raise ValueError("split-pending action proof is missing")
    details = proof.get("details")
    if (
        proof.get("condition") != "third_party_split_problem_confirmed"
        or proof.get("verified") is not True
        or proof.get("observed_at") != observed_at
        or not isinstance(proof.get("evidence_ref"), str)
        or proof.get("evidence_ref") not in refs
        or not isinstance(details, Mapping)
    ):
        raise ValueError("split-pending action proof is invalid")

    fingerprint = data.get("preview_fingerprint")
    if details.get("preview_fingerprint") != fingerprint:
        raise ValueError("split-pending action fingerprint proof is invalid")
    if operation == PREVIEW_OPERATION:
        if (
            data.get("dry_run") is not True
            or data.get("results") != []
            or data.get("selected_bill_codes") != []
            or details.get("confirmed_count") != 0
            or details.get("dry_run") is not True
            or details.get("write_attempted") is not False
        ):
            raise ValueError("split-pending preview proof is invalid")
        return dict(proof)

    results = data.get("results")
    selected = data.get("selected_bill_codes")
    verification_refs = details.get("verification_evidence_refs")
    if (
        operation != EXECUTE_OPERATION
        or data.get("dry_run") is not False
        or not isinstance(results, list)
        or not isinstance(selected, list)
        or not results
        or len(results) != len(selected)
        or not isinstance(verification_refs, list)
        or len(verification_refs) != len(selected)
        or len(verification_refs) != len(set(verification_refs))
        or any(reference not in refs for reference in verification_refs)
        or details.get("confirmed_count") != len(selected)
        or details.get("selected_bill_codes") != selected
        or proof.get("evidence_ref") != verification_refs[-1]
    ):
        raise ValueError("split-pending per-ticket proof is invalid")
    for bill_code, result in zip(selected, results, strict=True):
        if (
            not isinstance(bill_code, str)
            or not isinstance(result, Mapping)
            or result.get("bill_code") != bill_code
            or result.get("verified") is not True
        ):
            raise ValueError("split-pending result order proof is invalid")
    return dict(proof)


def _service_success_result(
    value: object,
    tracker: _ExecutionTracker,
    *,
    operation: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("status") != "SUCCESS":
        raise ValueError("split-pending action did not return success")
    data = value.get("data")
    meta = value.get("meta")
    if not isinstance(data, Mapping) or not isinstance(meta, Mapping):
        raise ValueError("split-pending action result is invalid")
    refs = list(tracker.host_refs)
    if not refs or len(refs) != len(set(refs)):
        raise ValueError("split-pending Host evidence is missing or duplicated")
    legacy_refs = meta.get("evidence_refs")
    if not isinstance(legacy_refs, list) or legacy_refs != refs:
        raise ValueError("split-pending Host evidence does not match action evidence")
    observed_at = str(meta.get("observed_at") or "").strip()
    if not observed_at:
        raise ValueError("split-pending observation time is missing")
    action_proof = _validated_action_proof(
        data,
        meta,
        refs,
        operation=operation,
        observed_at=observed_at,
    )
    legacy_evidence = data.get("evidence")
    if not isinstance(legacy_evidence, Mapping):
        raise ValueError("split-pending source evidence is missing")
    outcome = "READ_ONLY" if operation == PREVIEW_OPERATION else "WRITE_VERIFIED"
    projected_data = dict(data)
    projected_data["evidence"] = {
        **dict(legacy_evidence),
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
        "warnings": list(value.get("warnings") or []),
        "error": None,
    }


def _failure_result(
    *,
    code: str,
    write_outcome: str,
    evidence_refs: list[str] | None = None,
    completed_results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    completed = list(completed_results or [])
    return {
        "status": "FAILED",
        "data": {"completed_results": completed},
        "meta": {
            "source_system": "feishu+mysql+ronghui",
            "observed_at": _utc_now(),
            "record_count": len(completed),
            "pagination_complete": False,
            "evidence_refs": list(evidence_refs or []),
            "write_outcome": write_outcome,
        },
        "warnings": [],
        "error": {
            "code": code,
            "message": (
                "The split-pending problem operation did not produce a complete result"
            ),
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
    target = request.get("target")
    if entrypoint in {"console", "feishu", "harness"}:
        expected_identity = CONTRIBUTION_TARGETS[entrypoint]
    elif entrypoint == "service":
        expected_identity = ("host.service.invoke", "service")
    else:
        raise ValueError("service request entrypoint is invalid")
    if (
        not isinstance(target, Mapping)
        or set(target)
        != {"service", "operation", "contribution_id", "contribution_kind"}
        or target.get("service") != SERVICE_NAME
        or target.get("operation") not in {PREVIEW_OPERATION, EXECUTE_OPERATION}
        or target.get("contribution_id") != expected_identity[0]
        or target.get("contribution_kind") != expected_identity[1]
    ):
        raise ValueError("service target is invalid")
    operation = str(target["operation"])
    if entrypoint == "harness" and operation != PREVIEW_OPERATION:
        raise ValueError("Harness may invoke only the split-pending preview")
    governance = request.get("governance")
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
    }[operation]
    if not isinstance(governance, Mapping) or any(
        governance.get(key) != expected
        for key, expected in expected_governance.items()
    ):
        raise ValueError("service governance is invalid")
    arguments = dict(request["arguments"])
    if entrypoint == "harness":
        arguments["dry_run"] = True
    _reject_sensitive(arguments)
    _validate_operation_arguments(operation, arguments)
    return operation, arguments


def run_split_pending_service(
    operation: str,
    arguments: dict[str, object],
    *,
    tracker: _ExecutionTracker | None = None,
) -> dict[str, object]:
    """Run the embedded v1 action through exact Host Connector identities."""

    _validate_operation_arguments(operation, arguments)
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
            # The marker begins before snapshot/Sheet/problem/event/result
            # writes. Any later exception is an unknown write outcome.
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

    result = action.run_action(arguments, broker)
    return _service_success_result(result, execution, operation=operation)


def run_split_pending_action_offline(
    operation: str | Mapping[str, object],
    arguments: Mapping[str, object] | object,
    host_broker: object | None = None,
) -> dict[str, object]:
    """Run the embedded action with a caller-injected offline Host broker.

    The explicit ``operation, arguments, broker`` form is used by the package
    runtime. For fixture callers, ``arguments, broker`` is also accepted and
    derives the immutable operation from the required ``dry_run`` flag.
    """

    if isinstance(operation, str):
        if host_broker is None or not isinstance(arguments, Mapping):
            raise ValueError("offline split-pending broker arguments are invalid")
        selected_operation = operation
        selected_arguments = dict(arguments)
        selected_broker = host_broker
    else:
        if host_broker is not None:
            raise ValueError("offline split-pending broker arguments are invalid")
        selected_arguments = dict(operation)
        selected_broker = arguments
        selected_operation = (
            PREVIEW_OPERATION
            if selected_arguments.get("dry_run") is True
            else EXECUTE_OPERATION
        )

    _validate_operation_arguments(selected_operation, selected_arguments)
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

    return action.run_action(selected_arguments, broker)


def main() -> int:
    tracker = _ExecutionTracker()
    try:
        operation, arguments = _read_request()
        result = run_split_pending_service(operation, arguments, tracker=tracker)
        _reject_sensitive(result)
    except ValueError as exc:
        result = _failure_result(
            code=_error_code(exc, "INVALID_CONFIGURATION"),
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
