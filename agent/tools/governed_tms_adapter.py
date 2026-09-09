"""CLI transport for the shared, closed TMS business contracts."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from agent.tms_runtime.business_contracts import (
    GovernedAdapterError,  # noqa: F401 - retained public CLI adapter contract
    _business_payload,  # noqa: F401 - retained public CLI adapter contract
    _clean,  # noqa: F401 - retained public CLI adapter contract
    _customer_base,  # noqa: F401 - retained public CLI adapter contract
    _error_from_response,  # noqa: F401 - retained public CLI adapter contract
    _external_upload_ref,  # noqa: F401 - retained public CLI adapter contract
    _pagination_complete,  # noqa: F401 - retained public CLI adapter contract
    _record_count,  # noqa: F401 - retained public CLI adapter contract
    _selected,  # noqa: F401 - retained public CLI adapter contract
    _source_confirmed,  # noqa: F401 - retained public CLI adapter contract
    _unified_result,  # noqa: F401 - retained public CLI adapter contract
    _valid_observed_at,  # noqa: F401 - retained public CLI adapter contract
    _with_postcondition,  # noqa: F401 - retained public CLI adapter contract
    _write_failure,  # noqa: F401 - retained public CLI adapter contract
    build_clock_in_params,  # noqa: F401 - retained public CLI adapter contract
    build_customer_action_params,  # noqa: F401 - retained public CLI adapter contract
    build_receipts_audit_params,  # noqa: F401 - retained public CLI adapter contract
    build_receipts_sync_params,  # noqa: F401 - retained public CLI adapter contract
    validate_clock_in_response,  # noqa: F401 - retained public CLI adapter contract
    validate_customer_write_response,  # noqa: F401 - retained public CLI adapter contract
    validate_receipts_audit_response,  # noqa: F401 - retained public CLI adapter contract
    validate_receipts_sync_response,  # noqa: F401 - retained public CLI adapter contract
)


def call_http_service(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """Load the existing HTTP client only when an executor actually runs."""

    from tools.tms_tool import call_http_service as call

    return call(endpoint, params)


def execute_fixed_target(target: str, params: dict[str, Any]) -> dict[str, Any]:
    response = call_http_service(f"/{target}", params)
    if not isinstance(response, dict):
        return {"error": "governed TMS target returned a non-object response", "error_code": "INVALID_RESPONSE"}
    if response.get("ok") is False or response.get("error"):
        return _error_from_response(response)
    return response


def run_cli(
    target: str,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    response_validator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    write: bool = False,
) -> None:
    try:
        raw = json.loads(sys.stdin.read() or "{}")
        if not isinstance(raw, dict):
            raise GovernedAdapterError("tool input must be a JSON object")
        result = execute_fixed_target(target, transform(raw))
        if response_validator is not None:
            result = response_validator(result, raw)
        result = _unified_result(
            target=target,
            original_params=raw,
            response=result,
            write=write,
        )
    except (GovernedAdapterError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result = {"error": str(exc), "error_code": "INVALID_GOVERNED_INPUT"}
    print(json.dumps(result, ensure_ascii=False, default=str))


def run_customer_cli(action: str, *, write: bool = False) -> None:
    run_cli(
        "customer_service_problem",
        lambda params: build_customer_action_params(action, params),
        response_validator=(
            (lambda response, params: validate_customer_write_response(action, response, params))
            if write
            else None
        ),
        write=write,
    )
