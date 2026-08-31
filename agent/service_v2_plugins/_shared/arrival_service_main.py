"""Closed service-v2 subprocess entrypoint for the arrival-statistics package."""

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
    MUTATING_CONNECTOR_OPERATIONS,
    PLUGIN_ID,
    PROJECTION_CONNECTOR,
    SERVICE_NAME,
    SHEET_CONNECTORS,
    TMS_CONNECTOR,
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
_FORBIDDEN_KEYS = ("password", "cookie", "credential", "secret", "token", "session")
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if (
                key in {"account_id", "account_ids", "resource_id", "resource_ids"}
                or key.endswith(
                    ("_account_id", "_account_ids", "_resource_id", "_resource_ids")
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
    """Keep only outer Host receipts and whether a mutation was entered."""

    def __init__(self) -> None:
        self.host_refs: list[str] = []
        self.mutating_started = False


def _preflight_connector_services(arguments: Mapping[str, object]) -> tuple[str, ...]:
    """List every Connector this invocation can use before its first write."""

    connectors = [TMS_CONNECTOR, PROJECTION_CONNECTOR]
    if arguments.get("dry_run") is not True:
        connectors.extend(
            (
                SHEET_CONNECTORS["arrival_stats_primary_sheet"],
                SHEET_CONNECTORS["arrival_stats_secondary_sheet"],
                SHEET_CONNECTORS["arrival_stats_split_pending_sheet"],
            )
        )
        if arguments.get("pending_sheet_disabled") is not True:
            connectors.append(SHEET_CONNECTORS["arrival_stats_pending_sheet"])
        if arguments.get("archive_snapshot", True) is True:
            connectors.append(SHEET_CONNECTORS["arrival_stats_archive_sheet"])
    return tuple(connectors)


def _service_success_result(
    value: object,
    tracker: _ExecutionTracker,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("status") != "SUCCESS":
        raise ValueError("arrival statistics action did not return success")
    data = value.get("data")
    meta = value.get("meta")
    if not isinstance(data, Mapping) or not isinstance(meta, Mapping):
        raise ValueError("arrival statistics action result is invalid")
    refs = list(tracker.host_refs)
    if not refs or len(refs) != len(set(refs)):
        raise ValueError("arrival statistics Host evidence is missing or duplicated")
    observed_at = str(meta.get("observed_at") or "").strip()
    if not observed_at:
        raise ValueError("arrival statistics observation time is missing")
    legacy_evidence = data.get("evidence")
    if not isinstance(legacy_evidence, Mapping):
        raise ValueError("arrival statistics source evidence is missing")
    projected_data = dict(data)
    projected_data["evidence"] = {
        **dict(legacy_evidence),
        "service": SERVICE_NAME,
        "operation": "run",
        "outcome": "WRITE_VERIFIED",
        "observed_at": observed_at,
    }
    proof = {
        "condition": "plugin_result_contract_valid",
        "verified": True,
        "observed_at": observed_at,
        "evidence_ref": refs[-1],
        "details": {
            "result_summary": dict(projected_data),
            "evidence_refs": list(refs),
        },
    }
    projected_meta = dict(meta)
    projected_meta.update(
        {
            "evidence_refs": refs,
            "write_outcome": "WRITE_VERIFIED",
            "postconditions": {"0": True},
            "postcondition_evidence": {"0": proof},
        }
    )
    return {
        "status": "SUCCESS",
        "data": projected_data,
        "meta": projected_meta,
        "warnings": list(value.get("warnings") or []),
        "error": None,
    }


def _failure_result(*, code: str, write_outcome: str) -> dict[str, object]:
    observed_at = _utc_now()
    blocked_status = "BLOCKED_LOGIN" if code in {
        "AUTH_PENDING_CODE",
        "AUTH_REQUIRED",
        "BLOCKED_LOGIN",
        "LOGIN_REQUIRED",
        "SESSION_EXPIRED",
    } else "BLOCKED_DATA"
    return {
        "status": "FAILED",
        "data": {"completed_results": []},
        "meta": {
            "source_system": "ronghui+feishu+mysql",
            "observed_at": observed_at,
            "record_count": 0,
            "pagination_complete": False,
            "evidence_refs": [],
            "blocked_status": blocked_status,
            "write_outcome": write_outcome,
        },
        "warnings": [],
        "error": {
            "code": code,
            "message": "The arrival-statistics operation did not produce a complete result",
            "retryable": False,
        },
    }


def _read_request() -> dict[str, object]:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("service request schema is invalid")
    if (
        request.get("schema_version") != 2
        or request.get("runtime_model") != "SERVICE_V2"
        or request.get("automation_id") != os.environ.get("BOYI_AUTOMATION_ID", "")
        or request.get("plugin_id") != PLUGIN_ID
        or request.get("plugin_id") != os.environ.get("BOYI_PLUGIN_ID", "")
        or request.get("plugin_version") != os.environ.get("BOYI_PLUGIN_VERSION", "")
        or not isinstance(request.get("arguments"), dict)
    ):
        raise ValueError("service request identity is invalid")

    entrypoint = str(request.get("entrypoint") or "")
    contribution_target = CONTRIBUTION_TARGETS.get(entrypoint)
    if (
        not isinstance(contribution_target, tuple)
        or len(contribution_target) != 2
        or not all(isinstance(item, str) and item for item in contribution_target)
    ):
        raise ValueError("service request entrypoint is invalid")
    target = request.get("target")
    expected_target = {
        "service": SERVICE_NAME,
        "operation": "run",
        "contribution_id": contribution_target[0],
        "contribution_kind": contribution_target[1],
    }
    if not isinstance(target, Mapping) or dict(target) != expected_target:
        raise ValueError("service target is invalid")
    governance = request.get("governance")
    if (
        not isinstance(governance, Mapping)
        or governance.get("effect") != "external_write"
        or governance.get("operation_type") != "external_write"
        or governance.get("broker_effect") != "write"
        or governance.get("harness_allowed") is not False
    ):
        raise ValueError("service governance is invalid")
    arguments = request["arguments"]
    if (
        "pending_sheet_disabled" not in arguments
        or not isinstance(arguments.get("pending_sheet_disabled"), bool)
    ):
        raise ValueError("pending_sheet_disabled must be explicitly configured")
    _reject_sensitive(arguments)
    return request


def main() -> int:
    tracker = _ExecutionTracker()
    try:
        request = _read_request()
        result = run_arrival_service(
            dict(request["arguments"]),
            tracker=tracker,
        )
        _reject_sensitive(result)
    except ValueError as exc:
        result = _failure_result(
            code=_error_code(
                exc,
                "SERVICE_EXECUTION_FAILED" if tracker.mutating_started else "INVALID_CONFIGURATION",
            ),
            write_outcome=(
                "WRITE_OUTCOME_UNKNOWN" if tracker.mutating_started else "NOT_APPLIED"
            ),
        )
    except Exception as exc:
        result = _failure_result(
            code=_error_code(exc, "SERVICE_EXECUTION_FAILED"),
            write_outcome=(
                "WRITE_OUTCOME_UNKNOWN" if tracker.mutating_started else "NOT_APPLIED"
            ),
        )
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return 0


def run_arrival_service(
    arguments: dict[str, object],
    *,
    tracker: _ExecutionTracker | None = None,
) -> dict[str, object]:
    """Run the first-party algorithm through the closed Connector adapter."""

    import action

    execution = tracker or _ExecutionTracker()
    preflight_services = _preflight_connector_services(arguments)
    sent_preflight = False

    def broker(operation: str, *, action: str, role: str, arguments: dict[str, object]) -> object:
        nonlocal sent_preflight
        _, connector_operation = _connector_target(operation, action, role, arguments)
        if connector_operation in MUTATING_CONNECTOR_OPERATIONS:
            execution.mutating_started = True
        result = service_invoke_adapter(
            broker_call,
            operation,
            action=action,
            role=role,
            arguments=arguments,
            preflight_services=preflight_services if not sent_preflight else (),
        )
        sent_preflight = True
        if isinstance(result, Mapping):
            reference = str(result.get("evidence_ref") or "").strip()
            if not reference or len(reference) > 512:
                raise ValueError("arrival statistics Host evidence is invalid")
            execution.host_refs.append(reference)
        return result

    result = action.run_action(arguments, broker)
    if arguments.get("dry_run") is True:
        return _failure_result(code="DRY_RUN_NOT_APPLIED", write_outcome="NOT_APPLIED")
    if not execution.mutating_started:
        raise RuntimeError("WRITE_OUTCOME_UNKNOWN")
    return _service_success_result(result, execution)


def run_arrival_action_offline(
    arguments: dict[str, object],
    host_broker: object,
) -> dict[str, object]:
    """Run the embedded action with an injected offline Host broker.

    This helper exists for fixture parity only.  Production entrypoints use
    ``run_arrival_service`` so an external-write result cannot be reported
    successful without Host mutation receipts.
    """

    import action

    def broker(operation: str, *, action: str, role: str, arguments: dict[str, object]) -> object:
        return service_invoke_adapter(
            host_broker,
            operation,
            action=action,
            role=role,
            arguments=arguments,
        )

    return action.run_action(arguments, broker)


if __name__ == "__main__":
    raise SystemExit(main())
