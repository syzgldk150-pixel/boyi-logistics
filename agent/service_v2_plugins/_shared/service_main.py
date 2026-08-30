"""Closed service-v2 subprocess entrypoint for one clock-in package."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping

from boyi_plugin_sdk import broker_call
from clock_runtime import failure_result, run_clock_service
from plugin import CONTRIBUTION_TARGETS, EXPECTED_SITE_NAME, PLUGIN_ID, SERVICE_NAME


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


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if (
                key in {"account_id", "account_ids"}
                or key.endswith(("_account_id", "_account_ids"))
                or any(marker in key for marker in _FORBIDDEN_KEYS)
            ):
                raise ValueError("plugin data contains a broker-owned field")
            _reject_sensitive(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive(child)


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
    governance = request.get("governance")
    expected_target = {
        "service": SERVICE_NAME,
        "operation": "run",
        "contribution_id": contribution_target[0],
        "contribution_kind": contribution_target[1],
    }
    if not isinstance(target, Mapping) or dict(target) != expected_target:
        raise ValueError("service target is invalid")
    if (
        not isinstance(governance, Mapping)
        or governance.get("effect") != "external_write"
        or governance.get("operation_type") != "external_write"
        or governance.get("broker_effect") != "write"
        or governance.get("harness_allowed") is not False
    ):
        raise ValueError("service governance is invalid")
    _reject_sensitive(request["arguments"])
    return request


def main() -> int:
    try:
        request = _read_request()
        result = run_clock_service(
            dict(request["arguments"]),
            broker_call,
            expected_site_name=EXPECTED_SITE_NAME,
            service_name=SERVICE_NAME,
        )
        _reject_sensitive(result)
    except ValueError:
        result = failure_result(code="INVALID_CONFIGURATION", write_outcome="NOT_APPLIED")
    except Exception:
        result = failure_result(code="SERVICE_EXECUTION_FAILED", write_outcome="NOT_APPLIED")
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
