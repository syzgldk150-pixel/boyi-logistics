"""Ronghui finance statement capture Service v2 entrypoint."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone


PLUGIN_ID = "sync_ronghui_finance_statement_v2"
PLUGIN_VERSION = "1.0.0"
SERVICE_NAME = "plugin.sync_ronghui_finance_statement_v2.capture@1"
_CONTRIBUTIONS = {"console": "monthly_capture"}
_REQUEST_FIELDS = {
    "schema_version", "runtime_model", "automation_id", "plugin_id", "plugin_version",
    "entrypoint", "target", "governance", "arguments",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_request() -> dict[str, object]:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("service request schema is invalid")
    automation_id = os.environ.get("BOYI_AUTOMATION_ID", "")
    arguments = request.get("arguments")
    if (
        request.get("schema_version") != 2
        or request.get("runtime_model") != "SERVICE_V2"
        or not automation_id
        or request.get("automation_id") != automation_id
        or request.get("plugin_id") != PLUGIN_ID
        or request.get("plugin_id") != os.environ.get("BOYI_PLUGIN_ID", "")
        or request.get("plugin_version") != PLUGIN_VERSION
        or request.get("plugin_version") != os.environ.get("BOYI_PLUGIN_VERSION", "")
        or request.get("entrypoint") not in _CONTRIBUTIONS
        or not isinstance(arguments, dict)
        or set(arguments) != {"month"}
    ):
        raise ValueError("service request identity is invalid")
    expected_target = {
        "service": SERVICE_NAME,
        "operation": "run",
        "contribution_id": _CONTRIBUTIONS[str(request["entrypoint"])],
        "contribution_kind": str(request["entrypoint"]),
    }
    target = request.get("target")
    governance = request.get("governance")
    if not isinstance(target, Mapping) or dict(target) != expected_target:
        raise ValueError("service target is invalid")
    if (not isinstance(governance, Mapping) or governance.get("effect") != "internal_write"
            or governance.get("broker_effect") != "write"):
        raise ValueError("service governance is invalid")
    return request


def _finalize(result: dict[str, object], write_started: bool) -> dict[str, object]:
    """Attach the Host-verifiable outcome and plugin-contract postcondition."""

    meta = dict(result["meta"])
    data = dict(result["data"])
    refs = list(meta.get("evidence_refs") or [])
    success = result["status"] == "SUCCESS"
    outcome = ("WRITE_VERIFIED" if success else "WRITE_OUTCOME_UNKNOWN") if write_started else "NOT_APPLIED"
    observed = str(meta["observed_at"])
    meta["write_outcome"] = outcome
    if success:
        data["evidence"] = {"service": SERVICE_NAME, "operation": "run", "outcome": outcome, "observed_at": observed}
        meta["postconditions"] = {"0": True}
        meta["postcondition_evidence"] = {"0": {
            "condition": "plugin_result_contract_valid", "verified": True, "observed_at": observed,
            "evidence_ref": refs[-1], "details": {"result_summary": data, "evidence_refs": refs},
        }}
    return {**result, "data": data, "meta": meta}


def main() -> int:
    try:
        request = _read_request()
        from boyi_plugin_sdk import broker_call
        import action

        started = []

        def broker(operation, *, action, role, arguments):
            if operation == "service.invoke" and action == "write_platform_capture":
                started.append(True)
            return broker_call(operation, action=action, role=role, arguments=arguments)

        result = _finalize(action.run(broker, dict(request["arguments"])), bool(started))
    except (ValueError, json.JSONDecodeError):
        result = {
            "status": "FAILED",
            "data": {},
            "meta": {"source_system": "ronghui", "observed_at": _now(), "record_count": 0,
                     "pagination_complete": False, "evidence_refs": [], "write_outcome": "NOT_APPLIED"},
            "warnings": [],
            "error": {"code": "INVALID_REQUEST", "message": "request rejected", "retryable": False},
        }
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
