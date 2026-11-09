"""One round of authorized automatic customer-service replies.

The package only walks the Host's candidate list. For each problem the Host
drafts advice and says whether it qualifies; only then the package asks the
Host to reply with that advice's id. The Host re-checks authorization, refuses
a second automatic reply for the same problem, reads the platform before and
after sending, and raises ``WRITE_OUTCOME_UNKNOWN`` when the reply cannot be
confirmed. Any such error stops the round; nothing is retried.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from boyi_plugin_result import postcondition_proof, success_result, utc_observed_at

ACTION_ID = "customer_problem_auto_reply"
_ROLE = "customer_service_source"
_DEFAULT_MAX = 5
_KEY_FIELDS = ("source_id", "external_id", "source_direction")


def _limit(arguments: Mapping[str, object]) -> int:
    value = arguments.get("max_per_run", _DEFAULT_MAX)
    if value is None:
        return _DEFAULT_MAX
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise ValueError("max_per_run is invalid")
    return value


def run_action(arguments: dict[str, object], broker: Callable[..., object]) -> dict[str, object]:
    limit = _limit(arguments)
    refs: list[str] = []
    listing = broker("projection.invoke", action="customer_auto_reply.candidates", role=_ROLE,
                     arguments={"max_items": limit})
    refs.append(str(listing["evidence_ref"]))
    items = list(listing.get("items") or [])[:limit]
    outcomes = {"sent": 0, "already_replied": 0, "not_eligible": 0}
    reasons: dict[str, int] = {}
    for item in items:
        key = {field: str(item[field]) for field in _KEY_FIELDS}
        draft = broker("projection.invoke", action="customer_auto_reply.draft", role=_ROLE, arguments=key)
        refs.append(str(draft["evidence_ref"]))
        if not draft.get("eligible"):
            outcomes["not_eligible"] += 1
            reason = str(draft.get("reason") or "不符合自动处理条件")[:60]
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        result = broker("browser.invoke", action="customer_auto_reply.reply", role=_ROLE,
                        arguments={"suggestion_id": str(draft["suggestion_id"])})
        refs.append(str(result["evidence_ref"]))
        if result.get("confirmed") is True:
            outcomes["sent"] += 1
        elif result.get("skipped") == "already_replied":
            outcomes["already_replied"] += 1
        else:
            raise ValueError("WRITE_OUTCOME_UNKNOWN")
    observed_at = utc_observed_at()
    return success_result(
        data={
            "checked": len(items),
            **outcomes,
            "not_eligible_reasons": [{"reason": reason, "count": count} for reason, count in sorted(reasons.items())],
            "evidence": {"source": "service_v2_plugin", "observed_at": observed_at, "checked": len(items)},
        },
        source_system="customer_auto_reply",
        record_count=len(items),
        pagination_complete=True,
        evidence_refs=list(dict.fromkeys(refs)),
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": postcondition_proof(
            condition="authorized_replies_confirmed", observed_at=observed_at, evidence_ref=refs[-1],
            details={"checked": len(items), **outcomes})},
    )
