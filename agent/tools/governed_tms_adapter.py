"""Narrow HTTP adapters for governed TMS tool capabilities.

The registry validates the public arguments before these helpers run.  Each
wrapper binds one fixed TMS target (and, for customer-service tools, one fixed
action) so an execution capability cannot be reused for a broader operation.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


class GovernedAdapterError(ValueError):
    """Raised when a narrow adapter receives an internally inconsistent input."""


def call_http_service(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """Load the existing HTTP client only when an executor actually runs."""

    from tools.tms_tool import call_http_service as call

    return call(endpoint, params)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _valid_observed_at(value: Any) -> bool:
    text = _clean(value)
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _selected(source: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: source[name] for name in names if name in source}


def build_receipts_sync_params(params: dict[str, Any]) -> dict[str, Any]:
    return _selected(
        params,
        (
            "platform",
            "direction",
            "date_from",
            "date_to",
            "q",
            "receipt_status",
            "date_type",
            "code_type",
            "page_size",
            "max_pages",
            "timeout_sec",
            "source_workers",
        ),
    )


def build_receipts_audit_params(params: dict[str, Any]) -> dict[str, Any]:
    return _selected(
        params,
        (
            "receipt_id",
            "platform",
            "direction",
            "result",
            "reason",
            "waybill_no",
            "receipt_no",
            "return_waybill_no",
        ),
    )


def build_clock_in_params(params: dict[str, Any]) -> dict[str, Any]:
    selected = _selected(
        params,
        (
            "account_id",
            "sitecode",
            "sitefbcode",
            "sitename",
            "sitefbname",
            "first_type",
            "second_type",
            "delay_seconds",
        ),
    )
    selected["mode"] = "api"
    # Keep the legacy server-side timeout envelope.  The outer governed tool
    # has a larger timeout so cancellation and final evidence can still be
    # recorded cleanly.
    return {"params": selected, "timeout_sec": 60, "client_timeout_sec": 75}


def _customer_base(params: dict[str, Any], action: str) -> dict[str, Any]:
    result = {
        "platform": params["platform"],
        "account_id": params["account_id"],
        "action": action,
    }
    account_label = _clean(params.get("account_label"))
    if account_label:
        result["account_label"] = account_label
    return result


def build_customer_action_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build the runtime payload for one fixed customer-service action."""

    result = _customer_base(params, action)
    if action == "query":
        filters = _selected(
            params,
            ("direction", "q", "waybill_no", "date_from", "date_to", "page", "rows", "page_size"),
        )
        result["direction"] = params["direction"]
        result["filters"] = filters
        return result

    if action == "fetch_attachment":
        result["payload"] = {"source_url": params["source_url"]}
        return result

    if action in {"detail", "mark_read", "reply"}:
        item = _selected(params, ("external_id", "source_direction", "waybill_no", "status"))
        result["item"] = item
        if action == "detail":
            return result
        if action == "mark_read":
            # Ronghui intentionally remains capture-required until the real
            # update fields are known.  This adapter never accepts arbitrary
            # update_fields from callers.
            result["payload"] = {}
            return result
        result["payload"] = _selected(params, ("reply_text", "prob_status", "old_prob_status"))
        return result

    if action == "publish":
        payload = dict(params["payload"])
        platform = _clean(params["platform"]).lower()
        payload_fields = set(payload)
        ronghui_fields = {
            "bill_code",
            "problem_type",
            "owner_problem_type",
            "notice_site_code",
            "notice_site",
            "problem_cause",
        }
        yunda_fields = {"ship_no", "classes_type", "prob_text", "site_id"}
        expected = ronghui_fields if platform == "ronghui" else yunda_fields
        if payload_fields != expected:
            raise GovernedAdapterError(
                f"publish payload fields do not match platform {platform!r}"
            )
        result["payload"] = payload
        return result

    if action == "upload_attachment":
        result["payload"] = {
            "file_path": params["file_path"],
            # The caller owns the file lifecycle.  Never let this adapter
            # delete an arbitrary path after upload.
            "delete_after_upload": False,
        }
        return result

    raise GovernedAdapterError(f"unsupported governed customer-service action: {action}")


def _error_from_response(response: dict[str, Any]) -> dict[str, Any]:
    nested = response.get("error")
    if isinstance(nested, dict):
        message = _clean(nested.get("message") or nested.get("error"))
        code = _clean(nested.get("error_code") or nested.get("code"))
    else:
        message = _clean(nested)
        code = ""
    data = response.get("data")
    if isinstance(data, dict):
        nested_data = data.get("data") if isinstance(data.get("data"), dict) else data
        message = message or _clean(nested_data.get("message") or nested_data.get("error"))
        code = code or _clean(nested_data.get("error_code"))
    return {
        "error": message or "governed TMS target reported failure",
        "error_code": code or _clean(response.get("error_code")) or "TMS_TARGET_FAILED",
    }


def execute_fixed_target(target: str, params: dict[str, Any]) -> dict[str, Any]:
    response = call_http_service(f"/{target}", params)
    if not isinstance(response, dict):
        return {"error": "governed TMS target returned a non-object response", "error_code": "INVALID_RESPONSE"}
    if response.get("ok") is False or response.get("error"):
        return _error_from_response(response)
    return response


def _business_payload(response: dict[str, Any]) -> dict[str, Any]:
    payload = response
    envelope_keys = {
        "ok",
        "data",
        "error",
        "error_code",
        "evidence",
        "cost_sec",
        "status",
        "deprecated",
        "postcondition_evidence",
    }
    for _ in range(5):
        nested = payload.get("data")
        if not isinstance(nested, dict) or not set(payload).issubset(envelope_keys):
            break
        payload = nested
    return payload


def _record_count(payload: dict[str, Any]) -> int:
    for key in ("rows", "records", "items"):
        if isinstance(payload.get(key), list):
            return len(payload[key])
    stats = payload.get("stats")
    if isinstance(stats, dict):
        for key in ("deduped", "returned", "fetched"):
            value = stats.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
    return 1


def _pagination_complete(payload: dict[str, Any], response: dict[str, Any]) -> bool:
    evidence = response.get("evidence")
    if isinstance(evidence, dict) and evidence.get("pagination_complete") is True:
        return True
    stats = payload.get("stats")
    if isinstance(stats, dict):
        returned = stats.get("returned")
        total = stats.get("total")
        if (
            isinstance(returned, int)
            and not isinstance(returned, bool)
            and isinstance(total, int)
            and not isinstance(total, bool)
        ):
            return returned >= total
    return True


def _unified_result(
    *,
    target: str,
    original_params: dict[str, Any],
    response: dict[str, Any],
    write: bool,
) -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    platform = _clean(original_params.get("platform"))
    source_system = platform if platform and platform != "all" else (
        "ronghui,yunda" if target == "receipts_sync" else "ronghui"
    )
    meta = {
        "source_system": source_system,
        "account_id": _clean(original_params.get("account_id")),
        "observed_at": observed_at,
        "record_count": 0,
        "pagination_complete": False,
        "evidence_refs": [],
    }
    if response.get("error"):
        code = _clean(response.get("error_code")) or "TMS_TARGET_FAILED"
        if code in {"AUTH_REQUIRED", "AUTH_PENDING_CODE"}:
            code = "LOGIN_REQUIRED"
        return {
            "status": "FAILED",
            "data": {},
            "meta": meta,
            "warnings": [],
            # ToolExecutor treats any non-null ``error`` as a failed process
            # envelope.  Keep the classified code at the top level as well
            # so the orchestration adapter can preserve LOGIN_REQUIRED and
            # other governed failure classifications.
            "error_code": code,
            "error": {
                "code": code,
                "message": _clean(response.get("error")) or "governed TMS target failed",
                "retryable": False,
            },
        }

    payload = _business_payload(response)
    meta["record_count"] = _record_count(payload)
    meta["pagination_complete"] = _pagination_complete(payload, response)
    identifier = _clean(
        original_params.get("external_id")
        or original_params.get("waybill_no")
        or original_params.get("receipt_id")
        or original_params.get("direction")
        or "result"
    )
    meta["evidence_refs"] = [
        f"{target}:{source_system}:{meta['account_id'] or 'unscoped'}:{identifier}:{observed_at}"
    ]
    proof_items = response.get("postcondition_evidence")
    if write and isinstance(proof_items, list):
        verified_proofs: dict[str, dict[str, Any]] = {}
        reported: dict[str, bool] = {}
        evidence_ref = str(meta["evidence_refs"][0])
        for index, item in enumerate(proof_items):
            if not isinstance(item, dict):
                continue
            key = str(index)
            proof = {
                "condition": _clean(item.get("condition")),
                "verified": item.get("verified") is True,
                "observed_at": observed_at,
                "evidence_ref": evidence_ref,
            }
            details = item.get("details")
            if isinstance(details, dict):
                proof["details"] = details
            verified_proofs[key] = proof
            reported[key] = proof["verified"]
        if verified_proofs:
            meta["postconditions"] = reported
            meta["postcondition_evidence"] = verified_proofs
    warnings = payload.get("warnings")
    return {
        "status": "SUCCESS",
        "data": payload,
        "meta": meta,
        "warnings": warnings if isinstance(warnings, list) else [],
        "error": None,
    }


def _with_postcondition(
    response: dict[str, Any],
    *,
    condition: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    result = dict(response)
    result["postcondition_evidence"] = [
        {
            "condition": condition,
            "verified": True,
            "details": details,
        }
    ]
    return result


def _source_confirmed(value: Any) -> bool:
    return isinstance(value, dict) and (
        value.get("success") is True or value.get("ok") is True
    )


def _write_failure(message: str, code: str = "POSTCONDITION_UNVERIFIED") -> dict[str, Any]:
    return {"error": message, "error_code": code}


def validate_receipts_sync_response(
    response: dict[str, Any],
    _params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require the source response itself to prove complete pagination."""

    if response.get("error"):
        return response
    payload: Any = response
    for _ in range(4):
        if not isinstance(payload, dict) or isinstance(payload.get("stats"), dict):
            break
        nested = payload.get("data")
        if not isinstance(nested, dict):
            break
        payload = nested
    if not isinstance(payload, dict):
        return {"error": "receipt sync returned no auditable payload", "error_code": "INVALID_RESPONSE"}
    warnings = payload.get("warnings")
    stats = payload.get("stats")
    sources = stats.get("sources") if isinstance(stats, dict) else None
    if warnings not in (None, []) or not isinstance(sources, list) or not sources:
        return {
            "error": "receipt sync did not prove all requested sources completed",
            "error_code": "INCOMPLETE_SOURCE_EVIDENCE",
        }
    for source in sources:
        if not isinstance(source, dict):
            return {"error": "receipt source evidence is invalid", "error_code": "INVALID_SOURCE_EVIDENCE"}
        fetched = source.get("fetched")
        total = source.get("total")
        attachment_errors = source.get("attachment_errors", 0)
        if (
            isinstance(fetched, bool)
            or not isinstance(fetched, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or fetched < total
            or bool(source.get("truncated"))
            or attachment_errors != 0
        ):
            return {
                "error": "receipt source pagination or attachment evidence is incomplete",
                "error_code": "INCOMPLETE_SOURCE_EVIDENCE",
            }
    response = dict(response)
    response["evidence"] = {
        "pagination_complete": True,
        "source": [
            {
                "platform": source.get("platform"),
                "direction": source.get("direction"),
                "fetched": source.get("fetched"),
                "total": source.get("total"),
            }
            for source in sources
        ],
    }
    return _with_postcondition(
        response,
        condition="internal_receipt_projection_returned",
        details={
            "sources": len(sources),
            "pagination_complete": True,
        },
    )


def validate_receipts_audit_response(
    response: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    if response.get("error"):
        return response
    payload = _business_payload(response)
    verification = payload.get("verification")
    expected_status = "2" if _clean(params.get("result")).lower() == "passed" else "3"
    if (
        not isinstance(verification, dict)
        or verification.get("verified") is not True
        or _clean(verification.get("audit_status")) != expected_status
        or _clean(verification.get("waybill_no")) != _clean(params.get("waybill_no"))
        or not _clean(verification.get("external_id"))
        or not _valid_observed_at(verification.get("observed_at"))
    ):
        return _write_failure("receipt audit read-back did not match the approved entity and result")
    return _with_postcondition(
        response,
        condition="third_party_receipt_audit_confirmed",
        details={
            "platform": _clean(params.get("platform")),
            "external_id": _clean(verification.get("external_id")),
            "waybill_no": _clean(verification.get("waybill_no")),
            "audit_status": expected_status,
            "source_observed_at": _clean(verification.get("observed_at")),
        },
    )


def validate_clock_in_response(
    response: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    if response.get("error"):
        return response
    payload = _business_payload(response)
    if not (
        payload.get("first_success") is True
        and payload.get("second_success") is True
        and isinstance(payload.get("first_response"), dict)
        and payload["first_response"].get("success") is True
        and isinstance(payload.get("second_response"), dict)
        and payload["second_response"].get("success") is True
    ):
        return _write_failure("both clock-in source responses were not explicitly successful")
    return _with_postcondition(
        response,
        condition="both_third_party_clock_ins_confirmed",
        details={
            "account_id": _clean(params.get("account_id")),
            "sitecode": _clean(params.get("sitecode")),
            "sitefbcode": _clean(params.get("sitefbcode")),
            "sitename": _clean(params.get("sitename")),
            "sitefbname": _clean(params.get("sitefbname")),
            "first_type": _clean(params.get("first_type")),
            "second_type": _clean(params.get("second_type")),
        },
    )


def _external_upload_ref(value: Any, *, local_file_path: str) -> str:
    if not isinstance(value, dict):
        return ""
    for key in (
        "save_pos",
        "SAVE_POS",
        "url",
        "path",
        "file_id",
        "id",
    ):
        candidate = _clean(value.get(key))
        if candidate and candidate != local_file_path:
            return candidate
    nested = value.get("data")
    return (
        _external_upload_ref(nested, local_file_path=local_file_path)
        if isinstance(nested, dict)
        else ""
    )


def validate_customer_write_response(
    action: str,
    response: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    if response.get("error"):
        return response
    payload = _business_payload(response)
    source_result = payload.get("result")
    if not _source_confirmed(source_result):
        return _write_failure(f"customer-service {action} source response did not confirm success")

    conditions = {
        "mark_read": "third_party_read_state_confirmed",
        "reply": "third_party_reply_confirmed",
        "publish": "third_party_problem_publish_confirmed",
        "upload_attachment": "third_party_attachment_upload_confirmed",
    }
    condition = conditions.get(action)
    if condition is None:
        return _write_failure("unsupported customer-service write postcondition")

    details: dict[str, Any] = {
        "platform": _clean(params.get("platform")),
        "account_id": _clean(params.get("account_id")),
    }
    if action in {"mark_read", "reply"}:
        expected_id = _clean(params.get("external_id"))
        if not expected_id or _clean(payload.get("external_id")) != expected_id:
            return _write_failure("customer-service response did not confirm the approved external ID")
        details["external_id"] = expected_id
    elif action == "publish":
        external_id = _clean(payload.get("external_id"))
        if not external_id:
            return _write_failure("customer-service publish returned no external ID")
        details["external_id"] = external_id
    else:
        upload_ref = _external_upload_ref(
            source_result,
            local_file_path=_clean(params.get("file_path")),
        )
        if not upload_ref:
            return _write_failure("attachment upload returned no server-side file reference")
        details["upload_ref_sha256"] = hashlib.sha256(upload_ref.encode("utf-8")).hexdigest()
    return _with_postcondition(response, condition=condition, details=details)


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
