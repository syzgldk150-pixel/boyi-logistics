"""Publish verified customer collection using Host-observed source identities."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any, Mapping, Sequence

from agent.automation_plugins.first_party_handler_common import customer_problem_identity
from agent.automation_plugins.models import GenerationVerificationContext
from shared.customer_service_repository import CustomerServiceRepository
from shared.data_sources import DataSourceError, DataSourceRepository, SourceIdentity, row_dict


def publish_customer_collection(connection: Any, *, verification: GenerationVerificationContext,
                                run_id: str, records: Sequence[Mapping[str, Any]],
                                rechecks: Sequence[Mapping[str, Any]] = (),
                                require_runtime_provenance: bool = True) -> None:
    source_contexts: dict[str, dict[str, Any]] = {}
    completed: set[tuple[str, str]] = set()
    for observation in verification.host_call_observations:
        if observation.get("action") != "customer_problem.list_page":
            continue
        result = observation.get("result")
        context = result.get("source_context") if isinstance(result, Mapping) else None
        if not isinstance(context, Mapping):
            raise DataSourceError("SOURCE_ORGANIZATION_UNVERIFIED")
        provider = context.get("provider")
        matches = [account for account in verification.account_ids
            if customer_problem_identity(account_id=account, platform=provider,
                external_id="source-identity") == context.get("account_ref")]
        if len(matches) != 1:
            raise DataSourceError("SOURCE_ACCOUNT_PROOF_INVALID")
        account = matches[0]
        previous = source_contexts.get(account)
        if previous and (previous["provider"], previous["organization_key"]) != (provider, context.get("organization_key")):
            raise DataSourceError("SOURCE_IDENTITY_CHANGED_DURING_CAPTURE")
        source_contexts[account] = dict(context)
        if context.get("source_complete") is True:
            completed.add((account, str(context.get("direction"))))
    if set(source_contexts) != set(verification.account_ids):
        raise DataSourceError("SOURCE_ACCOUNT_PROOF_INCOMPLETE")
    for account, context in source_contexts.items():
        required = context.get("required_directions")
        if (not isinstance(required, list) or not required
                or any(value not in {"received", "registered", "query", "published"} for value in required)
                or any((account, value) not in completed for value in required)):
            raise DataSourceError("SOURCE_PAGINATION_INCOMPLETE")
    sources = DataSourceRepository(connection)
    source_by_account: dict[str, dict[str, Any]] = {}
    grouped: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for account, context in source_contexts.items():
        identity = SourceIdentity(module="customer_service", provider=context["provider"],
            organization_key=context["organization_key"], dataset="customer_service.problems",
            contract_version="1", dedup_contract="provider-external-id-direction-v1")
        source = sources.register_source(identity, display_name=context["organization_key"],
            producer_instance_id=verification.automation_id, producer_generation=verification.generation,
            request_id=f"customer-run:{run_id}")
        sources.bind_account_alias(source["source_id"], provider=context["provider"], account_id=account)
        source_by_account[account] = source
        grouped[source["source_id"]]
    for raw in records:
        matches = [account for account in verification.account_ids
            if customer_problem_identity(account_id=account, platform=raw["platform"],
                external_id=raw["external_id"], source_direction=raw["source_direction"]) == raw.get("dedupe_key")]
        if len(matches) != 1:
            raise DataSourceError("CUSTOMER_RECORD_ACCOUNT_PROOF_INVALID")
        source = source_by_account[matches[0]]
        row = {key: value for key, value in raw.items() if key != "dedupe_key"}
        key = (str(raw["external_id"]), str(raw["source_direction"]))
        old = grouped[source["source_id"]].get(key)
        if old is not None and old != row:
            raise DataSourceError("CUSTOMER_EQUIVALENT_SOURCE_CONFLICT")
        grouped[source["source_id"]][key] = row
    # These rows have already passed the projection's exact existing-item and
    # detail-evidence checks. Retain all business/manual fields when closing.
    for check in rechecks:
        if (check.get("status") != "RESOLVED" or check.get("source_returned") is not True
                or check.get("resolution_reason") not in {"explicit_reply", "explicit_terminal_status"}):
            continue
        account = check.get("account_id")
        if account not in source_by_account:
            raise DataSourceError("CUSTOMER_DETAIL_ACCOUNT_PROOF_INVALID")
        source = source_by_account[account]
        key = (str(check["external_id"]), str(check["source_direction"]))
        if key in grouped[source["source_id"]]:
            raise DataSourceError("CUSTOMER_DETAIL_NOT_DISAPPEARED")
        with connection.cursor() as cursor:
            cursor.execute("SELECT source_json FROM customer_problem_records WHERE source_id=%s AND external_id=%s AND source_direction=%s FOR UPDATE",
                (source["source_id"], *key))
            previous = row_dict(cursor, cursor.fetchone())
        if previous is None:
            # A legacy Work Item may predate module data publication. There is
            # no business row to invent; its verified Work Item still closes.
            continue
        row = json.loads(previous["source_json"]) if isinstance(previous["source_json"], str) else dict(previous["source_json"])
        row.update(resolved=True, status="已解决", resolution_reason=check["resolution_reason"])
        grouped[source["source_id"]][key] = row
    repository = CustomerServiceRepository(connection)
    unique_sources = {source["source_id"]: source for source in source_by_account.values()}
    for source_id, source in unique_sources.items():
        repository.publish(source_id=source_id, producer_instance_id=verification.automation_id,
            producer_generation=verification.generation, source_revision=int(source["revision"]),
            run_id=run_id, records=list(grouped[source_id].values()), pagination_complete=True,
            require_runtime_provenance=require_runtime_provenance)
