"""Account-bound, read-only three-platform quote Service V2 entrypoint."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone


PLUGIN_ID = 'quote_prices_v2'
PLUGIN_VERSION = '1.0.1'
SERVICE_NAME = 'plugin.quote_prices_v2.quote@1'
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
_EXPECTED_GOVERNANCE = {
    "effect": "read",
    "operation_type": "read",
    "risk_level": "low",
    "lock_class": "none",
    "evidence": {"required": False, "required_fields": []},
    "postconditions": [],
    "retry": {"safe": True, "max_attempts": 3},
    "harness_allowed": True,
    "broker_effect": "read",
    "approval": {"mode": "project_policy"},
    "idempotency": {"mode": "parameters", "key_fields": []},
    "project_full_auto_allowed": True,
}


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _meta(*, record_count: int) -> dict[str, object]:
    return {
        "source_system": "ronghui,yunda,best",
        "observed_at": _observed_at(),
        "record_count": record_count,
        "pagination_complete": True,
        "evidence_refs": [],
    }


def _read_request() -> dict[str, object]:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("service request schema is invalid")
    automation_id = os.environ.get("BOYI_AUTOMATION_ID", "")
    if (
        request.get("schema_version") != 2
        or request.get("runtime_model") != "SERVICE_V2"
        or not automation_id
        or request.get("automation_id") != automation_id
        or request.get("plugin_id") != PLUGIN_ID
        or request.get("plugin_id") != os.environ.get("BOYI_PLUGIN_ID", "")
        or request.get("plugin_version") != PLUGIN_VERSION
        or request.get("plugin_version") != os.environ.get("BOYI_PLUGIN_VERSION", "")
        or request.get("entrypoint") not in {"console", "feishu"}
    ):
        raise ValueError("service request identity is invalid")
    contribution_id = "quote" if request.get("entrypoint") == "console" else "command"
    expected_target = {
        "service": SERVICE_NAME,
        "operation": "quote",
        "contribution_id": contribution_id,
        "contribution_kind": str(request.get("entrypoint")),
    }
    target = request.get("target")
    governance = request.get("governance")
    if not isinstance(target, Mapping) or dict(target) != expected_target:
        raise ValueError("service target is invalid")
    if not isinstance(governance, Mapping) or dict(governance) != _EXPECTED_GOVERNANCE:
        raise ValueError("service governance is invalid")
    if __package__:
        from .common import quote_inputs
    else:
        from common import quote_inputs
    quote_inputs(request.get("arguments"))
    return request


def main() -> int:
    try:
        request = _read_request()
        from boyi_plugin_sdk import broker_call
        from action import run
        result = run(broker_call, request["arguments"])
    except (ValueError, json.JSONDecodeError):
        result = {
            "status": "FAILED",
            "data": {},
            "meta": _meta(record_count=0),
            "warnings": [],
            "error": {"code": "INVALID_REQUEST", "message": "报价请求无效，请提供完整地址、重量（公斤）和体积（立方米）"},
        }
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
