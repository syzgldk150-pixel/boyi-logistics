"""Customer business publication for independent plugin invocations.

The caller owns the transaction. Only current source/account aliases supply
detail requests; no Command, Run, Step or Work Item is created or consulted.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent.automation_plugins.first_party_handler_common import customer_problem_identity
from agent.automation_plugins.models import GenerationVerificationContext
from agent.automation_plugins.host_observation import customer_observed_result
from agent.customer_collection_validation import (
    _mapping_list,
    _opaque_detail_recheck_map,
    _opaque_problem_identity,
    _split_plugin_customer_rows,
    _validated_customer_generation,
)
from agent.customer_source_projection import publish_customer_collection
from shared.data_sources import DataSourceError, required_text, row_dict


def _accounts(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    bindings = metadata.get("account_bindings")
    values = bindings.get("customer_service_source") if isinstance(bindings, Mapping) else None
    values = (values,) if isinstance(values, str) else values
    if not isinstance(values, (list, tuple)) or not values:
        raise DataSourceError("CUSTOMER_ACCOUNT_BINDINGS_MISSING")
    result = tuple(required_text(value, "account_id") for value in values)
    if len(result) != len(set(result)):
        raise DataSourceError("CUSTOMER_ACCOUNT_BINDINGS_AMBIGUOUS")
    return result


def prepare_customer_rechecks(connection: Any, *, automation_id: str,
                              metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    accounts = _accounts(metadata)
    placeholders = ",".join(["%s"] * len(accounts))
    with connection.cursor() as cursor:
        cursor.execute("""SELECT r.external_id,r.source_direction,r.waybill_no,
            s.provider,a.account_id FROM customer_problem_records r
            JOIN module_data_sources s ON s.source_id=r.source_id
            JOIN module_data_source_accounts a ON a.source_id=s.source_id AND a.provider=s.provider
            WHERE s.module='customer_service' AND s.status='active'
            AND s.producer_instance_id=%s AND r.resolved=0
            AND a.account_id IN (""" + placeholders + ") ORDER BY s.source_id,r.external_id,r.source_direction,a.account_id",
            (required_text(automation_id, "automation_id"), *accounts))
        rows = [row_dict(cursor, row) for row in cursor.fetchall()]
    refs: dict[str, dict[str, Any]] = {}
    for row in rows:
        platform = required_text(row["provider"], "provider")
        direction = required_text(row["source_direction"], "source_direction")
        if platform not in {"ronghui", "yunda"} or direction not in {"received", "registered", "query", "published"}:
            raise DataSourceError("CUSTOMER_PERSISTED_IDENTITY_INVALID")
        external = required_text(row["external_id"], "external_id", 128)
        key = customer_problem_identity(account_id=row["account_id"], platform=platform,
            external_id=external, source_direction=direction)
        ref = {"dedupe_key": key, "platform": platform, "external_id": external, "source_direction": direction}
        if row["waybill_no"]:
            ref["waybill_no"] = required_text(row["waybill_no"], "waybill_no", 100)
        if key in refs:
            raise DataSourceError("CUSTOMER_PERSISTED_IDENTITY_AMBIGUOUS")
        refs[key] = ref
    return [refs[key] for key in sorted(refs)]


def publish_verified_customer_collection(connection: Any, *, invocation_id: str,
                                        automation_id: str, metadata: Mapping[str, Any],
                                        outcome: Any,
                                        recheck_items: Sequence[Mapping[str, Any]]) -> None:
    verification = getattr(outcome, "generation_verification", None)
    result = getattr(outcome, "result", None)
    if (outcome.accepted is not True or result is None
            or not isinstance(verification, GenerationVerificationContext)
            or verification.invocation_id != invocation_id
            or verification.automation_id != automation_id
            or set(verification.account_ids) != set(_accounts(metadata))):
        raise DataSourceError("CUSTOMER_INVOCATION_PROOF_INVALID")
    verification = _validated_customer_generation(result, verification)
    records = _mapping_list(result.data.get("records"), "records")
    _split_plugin_customer_rows(records)
    proof = result.data.get("evidence")
    if (not isinstance(proof, Mapping) or proof.get("configured_accounts_queried") is not True
            or proof.get("pagination_complete") is not True):
        raise DataSourceError("CUSTOMER_COLLECTION_PROOF_INVALID")
    seen: set[str] = set()
    for row in records:
        key = _opaque_problem_identity(row, verification)["dedupe_key"]
        if key in seen:
            raise DataSourceError("CUSTOMER_DUPLICATE_IDENTITY")
        seen.add(key)
    snapshot = {str(row["dedupe_key"]): row for row in recheck_items}
    if len(snapshot) != len(recheck_items):
        raise DataSourceError("CUSTOMER_RECHECK_SNAPSHOT_AMBIGUOUS")
    checks = _opaque_detail_recheck_map(result.data.get("rechecks", []), verification,
        existing_aliases=snapshot)
    if set(checks) - (set(snapshot) - seen):
        raise DataSourceError("UNEXPECTED_PROBLEM_DETAIL_RECHECK")
    # ResultVerifier authenticates broker observations independently of JSON.
    # A closure still needs an actual exact-detail call for this same identity.
    detail_keys = {
        result.get("dedupe_key")
        for observation in verification.host_call_observations
        if (result := customer_observed_result(observation, action="detail")) is not None
    }
    for key, check in checks.items():
        if check.get("status") == "RESOLVED" and key not in detail_keys:
            raise DataSourceError("CUSTOMER_DETAIL_EVIDENCE_MISSING")
    publish_customer_collection(connection, verification=verification,
        run_id=required_text(invocation_id, "invocation_id"), records=records,
        rechecks=tuple(checks.values()))
