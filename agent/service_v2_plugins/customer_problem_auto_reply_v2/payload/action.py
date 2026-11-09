"""One round of authorized automatic customer-service replies, following the instance's flow.

The flow (``arguments["flow"]``, or the manifest default) only arranges the
declared blocks: find candidates, optional condition filters, AI draft, reply.
It is validated here with the same ``plugin_flow`` rules the Host enforced when
it was saved. For each problem the Host drafts advice and says whether it
qualifies; only then the package asks the Host to reply with that advice's id.
The Host re-checks authorization, refuses a second automatic reply for the
same problem, reads the platform before and after sending, and raises
``WRITE_OUTCOME_UNKNOWN`` when the reply cannot be confirmed. Any such error
stops the round; nothing is retried.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

from boyi_plugin_result import postcondition_proof, success_result, utc_observed_at
from plugin_flow import evaluate_condition, flow_declaration, validate_flow

ACTION_ID = "customer_problem_auto_reply"
_ROLE = "customer_service_source"
_KEY_FIELDS = ("source_id", "external_id", "source_direction")


def _declaration() -> dict[str, object]:
    manifest = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text(encoding="utf-8"))
    declaration = flow_declaration(manifest["config_schema"])
    if declaration is None:
        raise ValueError("FLOW_DECLARATION_MISSING")
    return declaration


def _flow(arguments: Mapping[str, object]) -> dict[str, object]:
    declaration = _declaration()
    raw = arguments.get("flow")
    if raw is None:
        return declaration["default"]
    return validate_flow(raw, declaration)


def run_action(arguments: dict[str, object], broker: Callable[..., object]) -> dict[str, object]:
    if "max_per_run" in arguments and arguments["max_per_run"] is not None:
        value = arguments["max_per_run"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
            raise ValueError("max_per_run is invalid")
    flow = _flow(arguments)
    steps = flow["steps"]
    first = steps[0]
    if first["block"] != "candidates":
        raise ValueError("FLOW_MUST_START_WITH_CANDIDATES")
    limit = int(first.get("params", {}).get("max_items") or arguments.get("max_per_run") or 5)
    refs: list[str] = []
    listing = broker("projection.invoke", action="customer_auto_reply.candidates", role=_ROLE,
                     arguments={"max_items": limit})
    refs.append(str(listing["evidence_ref"]))
    items = list(listing.get("items") or [])[:limit]
    outcomes = {"sent": 0, "already_replied": 0, "not_eligible": 0, "filtered": 0}
    reasons: dict[str, int] = {}
    for item in items:
        record: dict[str, object] = {"platform": str(item.get("platform") or "")}
        key = {field: str(item[field]) for field in _KEY_FIELDS}
        suggestion_id = ""
        for step in steps[1:]:
            block = step["block"]
            if block == "filter":
                if not evaluate_condition(step["when"], record):
                    outcomes["filtered"] += 1
                    break
            elif block == "draft":
                draft = broker("projection.invoke", action="customer_auto_reply.draft", role=_ROLE, arguments=key)
                refs.append(str(draft["evidence_ref"]))
                record["scenario"] = draft.get("scenario_id") or None
                try:
                    record["confidence"] = float(draft.get("confidence") or 0)
                except ValueError:
                    record["confidence"] = 0.0
                if not draft.get("eligible"):
                    outcomes["not_eligible"] += 1
                    reason = str(draft.get("reason") or "不符合自动处理条件")[:60]
                    reasons[reason] = reasons.get(reason, 0) + 1
                    break
                suggestion_id = str(draft["suggestion_id"])
            elif block == "reply":
                result = broker("browser.invoke", action="customer_auto_reply.reply", role=_ROLE,
                                arguments={"suggestion_id": suggestion_id})
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
