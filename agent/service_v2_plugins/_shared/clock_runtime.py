"""Account-blind two-write clock-in orchestration for service-v2 packages."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone


_CAPABILITY = "browser.session"
_ROLE = "operator"
_PRECHECK = "ronghui.clock.precheck"
_SUBMIT = "ronghui.clock.submit"
_VERIFY = "ronghui.clock.verify"
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")
_ARGUMENT_FIELDS = {
    "sitecode",
    "sitefbcode",
    "sitename",
    "sitefbname",
    "first_type",
    "second_type",
    "delay_seconds",
}
_SITE_FIELDS = ("sitecode", "sitefbcode", "sitename", "sitefbname")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _required_text(value: object, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{label} is invalid")
    return text


def _evidence_ref(value: Mapping[str, object], label: str) -> str:
    reference = _required_text(value.get("evidence_ref"), label, 512)
    return reference


def _safe_error_code(exc: BaseException, fallback: str) -> str:
    candidate = str(exc).strip().upper()
    return candidate if _SAFE_ERROR_CODE.fullmatch(candidate) else fallback


def _blocked_status(code: str) -> str:
    if code in {
        "AUTH_PENDING_CODE",
        "AUTH_REQUIRED",
        "BLOCKED_LOGIN",
        "LOGIN_REQUIRED",
        "SESSION_EXPIRED",
    }:
        return "BLOCKED_LOGIN"
    return "BLOCKED_DATA"


def failure_result(
    *,
    code: str,
    write_outcome: str,
    evidence_refs: list[str] | None = None,
    completed_results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Return a closed, non-retryable failure without echoing source data."""

    observed_at = _utc_now()
    refs = list(evidence_refs or [])
    completed = list(completed_results or [])
    return {
        "status": "FAILED",
        "data": {"completed_results": completed},
        "meta": {
            "source_system": "ronghui",
            "observed_at": observed_at,
            "record_count": len(completed),
            "pagination_complete": False,
            "evidence_refs": refs,
            "blocked_status": _blocked_status(code),
            "write_outcome": write_outcome,
        },
        "warnings": [],
        "error": {
            "code": code,
            "message": "The clock-in operation did not produce complete independent write evidence",
            "retryable": False,
        },
    }


def _success_result(
    *,
    service_name: str,
    results: list[dict[str, object]],
    evidence_refs: list[str],
) -> dict[str, object]:
    observed_at = _utc_now()
    data = {
        "results": results,
        "evidence": {
            "service": service_name,
            "operation": "run",
            "outcome": "WRITE_VERIFIED",
            "both_third_party_clock_ins_confirmed": True,
            "observed_at": observed_at,
        },
    }
    # Do not reuse ``data`` itself here.  The subprocess serializes the
    # payload, but keeping the summary detached also makes in-process callers
    # unable to mutate both the result and its proof through one alias.
    result_summary = {
        "results": [
            {
                **dict(result),
                "site": dict(result["site"]),
            }
            for result in results
        ],
        "evidence": dict(data["evidence"]),
    }
    return {
        "status": "SUCCESS",
        "data": data,
        "meta": {
            "source_system": "ronghui",
            "observed_at": observed_at,
            "record_count": 2,
            "pagination_complete": True,
            "evidence_refs": evidence_refs,
            "postconditions": {"0": True},
            "postcondition_evidence": {
                "0": {
                    "condition": "plugin_result_contract_valid",
                    "verified": True,
                    "observed_at": observed_at,
                    "evidence_ref": evidence_refs[-1],
                    "details": {
                        # The core compares this result summary with the
                        # normalized result instead of trusting the plugin's
                        # boolean success claim.
                        "result_summary": result_summary,
                        "evidence_refs": list(evidence_refs),
                    },
                }
            },
            "write_outcome": "WRITE_VERIFIED",
        },
        "warnings": [],
        "error": None,
    }


def _site(arguments: Mapping[str, object], expected_site_name: str) -> dict[str, str]:
    site = {
        "sitecode": _required_text(arguments.get("sitecode"), "sitecode", 64),
        "sitefbcode": _required_text(arguments.get("sitefbcode"), "sitefbcode", 64),
        "sitename": _required_text(arguments.get("sitename"), "sitename", 100),
        "sitefbname": _required_text(arguments.get("sitefbname"), "sitefbname", 100),
    }
    if site["sitename"] != expected_site_name:
        raise ValueError("sitename does not match this package")
    return site


def _same_site(observed: object, expected: Mapping[str, str]) -> bool:
    if not isinstance(observed, Mapping):
        return False
    return all(str(observed.get(field) or "").strip() == expected[field] for field in _SITE_FIELDS)


def _valid_observed_at(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _verify_result(
    value: object,
    *,
    operation_id: str,
    clock_type: str,
    site: Mapping[str, str],
) -> dict[str, object]:
    verified = _object(value, "clock verification result")
    match_count = verified.get("match_count")
    if (
        verified.get("confirmed") is not True
        or str(verified.get("operation_id") or "").strip() != operation_id
        or str(verified.get("clock_type") or "").strip() != clock_type
        or isinstance(match_count, bool)
        or match_count != 1
        or not _same_site(verified.get("site"), site)
        or not _required_text(verified.get("outcome_category"), "outcome category", 64)
        or not _valid_observed_at(verified.get("observed_at"))
    ):
        raise ValueError("clock verification is incomplete or ambiguous")
    evidence_ref = _evidence_ref(verified, "clock verification evidence")
    return {
        "clock_type": clock_type,
        "operation_id": operation_id,
        "confirmed": True,
        "outcome_category": str(verified["outcome_category"]),
        "observed_at": str(verified["observed_at"]),
        "evidence_ref": evidence_ref,
        # Preserve the Host-confirmed site for the core ResultVerifier.  A
        # cross-site readback must be rejected instead of being summarized
        # away before verification.
        "site": dict(site),
    }


def _submit_and_verify(
    *,
    clock_type: str,
    site: Mapping[str, str],
    broker: Callable[..., object],
) -> tuple[dict[str, object] | None, list[str], str | None]:
    refs: list[str] = []
    try:
        submitted = _object(
            broker(
                _CAPABILITY,
                action=_SUBMIT,
                role=_ROLE,
                arguments={"clock_type": clock_type, "site": dict(site)},
            ),
            "clock submit result",
        )
    except Exception:
        return None, refs, "WRITE_OUTCOME_UNKNOWN"
    try:
        refs.append(_evidence_ref(submitted, "clock submit evidence"))
        operation_id = _required_text(submitted.get("operation_id"), "operation_id", 256)
        if submitted.get("accepted") is not True:
            return None, refs, "CLOCK_SUBMIT_REJECTED"
    except ValueError:
        return None, refs, "WRITE_OUTCOME_UNKNOWN"
    try:
        verified = broker(
            _CAPABILITY,
            action=_VERIFY,
            role=_ROLE,
            arguments={
                "clock_type": clock_type,
                "operation_id": operation_id,
                "site": dict(site),
            },
        )
        result = _verify_result(
            verified,
            operation_id=operation_id,
            clock_type=clock_type,
            site=site,
        )
    except Exception:
        return None, refs, "WRITE_OUTCOME_UNKNOWN"
    # Keep the verification receipt on the returned result as well as in the
    # aggregate Host evidence list.  ResultVerifier uses this binding to
    # reject a result whose summary points at a different site or operation.
    refs.append(str(result["evidence_ref"]))
    return result, refs, None


def run_clock_service(
    arguments: dict[str, object],
    broker: Callable[..., object],
    *,
    expected_site_name: str,
    service_name: str,
) -> dict[str, object]:
    """Submit two writes in order and require a fresh unique readback for each."""

    if set(arguments) - _ARGUMENT_FIELDS:
        raise ValueError("plugin arguments contain unsupported fields")
    site = _site(arguments, expected_site_name)
    first_type = _required_text(arguments.get("first_type"), "first_type", 32)
    second_type = _required_text(arguments.get("second_type"), "second_type", 32)
    if first_type == second_type:
        raise ValueError("dual clock types must be distinct")
    delay = arguments.get("delay_seconds", 0)
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
        raise ValueError("delay_seconds is invalid")

    try:
        precheck = _object(
            broker(
                _CAPABILITY,
                action=_PRECHECK,
                role=_ROLE,
                arguments={"clock_types": [first_type, second_type], "site": site},
            ),
            "clock precheck result",
        )
    except Exception as exc:
        code = _safe_error_code(exc, "CLOCK_PRECHECK_FAILED")
        return failure_result(code=code, write_outcome="NOT_APPLIED")
    if (
        precheck.get("ready") is not True
        or not _same_site(precheck.get("site"), site)
        or list(precheck.get("clock_types") or []) != [first_type, second_type]
    ):
        return failure_result(code="CLOCK_PRECHECK_NOT_READY", write_outcome="NOT_APPLIED")
    try:
        evidence_refs = [_evidence_ref(precheck, "clock precheck evidence")]
    except ValueError:
        return failure_result(code="CLOCK_PRECHECK_EVIDENCE_MISSING", write_outcome="NOT_APPLIED")

    completed: list[dict[str, object]] = []
    for index, clock_type in enumerate((first_type, second_type)):
        result, refs, error_code = _submit_and_verify(
            clock_type=clock_type,
            site=site,
            broker=broker,
        )
        combined_refs = [*evidence_refs, *refs]
        duplicate_evidence = len(combined_refs) != len(set(combined_refs))
        evidence_refs.extend(refs)
        duplicate_operation = result is not None and any(
            item.get("operation_id") == result.get("operation_id") for item in completed
        )
        if error_code is not None or result is None or duplicate_evidence or duplicate_operation:
            return failure_result(
                code=(error_code if not duplicate_evidence and not duplicate_operation else None)
                or "WRITE_OUTCOME_UNKNOWN",
                write_outcome=(
                    "NOT_APPLIED"
                    if (
                        error_code == "CLOCK_SUBMIT_REJECTED"
                        and not completed
                        and not duplicate_evidence
                        and not duplicate_operation
                    )
                    else "WRITE_OUTCOME_UNKNOWN"
                ),
                evidence_refs=evidence_refs,
                completed_results=completed,
            )
        completed.append(result)
        if index == 0 and delay:
            time.sleep(float(delay))
    return _success_result(
        service_name=service_name,
        results=completed,
        evidence_refs=evidence_refs,
    )
