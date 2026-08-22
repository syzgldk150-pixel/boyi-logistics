"""Package-owned dual clock-in orchestration and write verification."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from boyi_plugin_result import (
    broker_evidence_ref,
    postcondition_proof,
    success_result,
    utc_observed_at,
)


ACTION_ID = "clock_in_dual"
_ROLE = "account_id"


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _required_text(arguments: Mapping[str, object], key: str, maximum: int) -> str:
    value = str(arguments.get(key) or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{key} is invalid")
    return value


def _submit_and_verify(
    *,
    clock_type: str,
    site: Mapping[str, str],
    broker: Callable[..., object],
) -> tuple[dict[str, object], list[str]]:
    submitted = _object(
        broker(
            "browser.invoke",
            action="ronghui.clock.submit",
            role=_ROLE,
            arguments={"clock_type": clock_type, "site": dict(site)},
        ),
        "clock submit result",
    )
    operation_id = str(submitted.get("operation_id") or "").strip()
    submit_ref = broker_evidence_ref(submitted, "clock submit result")
    if submitted.get("accepted") is not True or not operation_id:
        raise ValueError("clock submit was not explicitly accepted")
    verified = _object(
        broker(
            "browser.invoke",
            action="ronghui.clock.verify",
            role=_ROLE,
            arguments={
                "clock_type": clock_type,
                "operation_id": operation_id,
                "site": dict(site),
            },
        ),
        "clock verification result",
    )
    if verified.get("confirmed") is not True:
        raise ValueError("clock write was not confirmed by a fresh read")
    if str(verified.get("clock_type") or "").strip() != clock_type:
        raise ValueError("clock verification returned another operation type")
    verify_ref = broker_evidence_ref(verified, "clock verification result")
    return (
        {
            "clock_type": clock_type,
            "operation_id": operation_id,
            "confirmed": True,
            "observed_at": str(verified.get("observed_at") or ""),
        },
        [submit_ref, verify_ref],
    )


def run_action(arguments: dict[str, object], broker: Callable[..., object]) -> dict[str, object]:
    site = {
        "sitecode": _required_text(arguments, "sitecode", 64),
        "sitefbcode": _required_text(arguments, "sitefbcode", 64),
        "sitename": _required_text(arguments, "sitename", 100),
        "sitefbname": _required_text(arguments, "sitefbname", 100),
    }
    first_type = _required_text(arguments, "first_type", 32)
    second_type = _required_text(arguments, "second_type", 32)
    if first_type == second_type:
        raise ValueError("dual clock types must be distinct")
    delay = arguments.get("delay_seconds", 0)
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
        raise ValueError("delay_seconds is invalid")
    precheck = _object(
        broker(
            "browser.invoke",
            action="ronghui.clock.precheck",
            role=_ROLE,
            arguments={"site": site, "clock_types": [first_type, second_type]},
        ),
        "clock precheck result",
    )
    if precheck.get("ready") is not True:
        raise ValueError("clock precheck did not confirm the exact site")
    evidence_refs = [broker_evidence_ref(precheck, "clock precheck result")]
    first, first_refs = _submit_and_verify(clock_type=first_type, site=site, broker=broker)
    evidence_refs.extend(first_refs)
    if delay:
        time.sleep(float(delay))
    second, second_refs = _submit_and_verify(clock_type=second_type, site=site, broker=broker)
    evidence_refs.extend(second_refs)
    observed_at = utc_observed_at()
    condition = "both_third_party_clock_ins_confirmed"
    return success_result(
        data={
            "results": [first, second],
            "evidence": {
                "source": "signed_first_party_plugin",
                "site": site,
                "observed_at": observed_at,
                "execution_result": "both_confirmed",
                condition: True,
            },
        },
        source_system="ronghui",
        record_count=2,
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={
            "0": postcondition_proof(
                condition=condition,
                observed_at=observed_at,
                evidence_ref=evidence_refs[-1],
                details={
                    "first_operation_id": first["operation_id"],
                    "second_operation_id": second["operation_id"],
                    "both_confirmed": True,
                },
            )
        },
    )
