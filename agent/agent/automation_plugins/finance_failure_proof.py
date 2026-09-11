"""Recognize a failed finance batch whose local writes were independently read back.

Only the executor's trusted plugin identity and Broker observations may be passed
here. This does not promote a failed collection to success or authorize a retry.
The finance Host adapter returns ``committed`` only after its database readback.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _finance_service_observations(rows):
    """Translate Host-attested V2 calls to the same finance verification facts."""
    from agent.automation_plugins.finance_connectors_v2 import FINANCE_OPERATIONS, ROLES
    services = {f"connector.boyi.{role}@1": role for role in ROLES}
    normalized = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        target = row.get("service_target")
        result = row.get("result")
        if not isinstance(target, Mapping) or not isinstance(result, Mapping):
            return None
        operation = FINANCE_OPERATIONS.get(target.get("operation"))
        role = services.get(target.get("service"))
        reference = row.get("evidence_ref")
        if (operation is None or role is None or set(target) != {"service", "operation", "effect"}
                or row.get("operation") != "service.invoke" or row.get("action") != target["operation"]
                or row.get("role") != "__system__" or target.get("effect") != operation[2]
                or not isinstance(reference, str) or not reference):
            return None
        normalized.append({**row, "operation": operation[0], "action": operation[1], "role": role,
            "result": {**result, "evidence_ref": reference}})
    return normalized


def is_verified_finance_failure(
    *,
    plugin_id: str,
    result: Mapping[str, Any],
    started_mutating_call_count: int | None,
    host_call_observations: Sequence[Mapping[str, Any]],
) -> bool:
    if plugin_id not in {"sync_finance_bills", "sync_finance_bills_v2"} or result.get("status") != "FAILED":
        return False
    if plugin_id == "sync_finance_bills_v2":
        host_call_observations = _finance_service_observations(host_call_observations)
        if host_call_observations is None:
            return False
    data, error, meta = result.get("data"), result.get("error"), result.get("meta")
    if not all(isinstance(value, Mapping) for value in (data, error, meta)):
        return False
    expected_code = {
        "partial_failed": "FINANCE_SYNC_PARTIAL_FAILED",
        "failed": "FINANCE_SYNC_FAILED",
    }.get(data.get("status"))
    if expected_code is None or error.get("code") != expected_code:
        return False
    if type(started_mutating_call_count) is not int or started_mutating_call_count < 3:
        return False
    if not host_call_observations or any(
        not isinstance(row, Mapping) or type(row.get("write_started")) is not bool
        or not isinstance(row.get("result"), Mapping)
        or not isinstance(row["result"].get("evidence_ref"), str) or not row["result"]["evidence_ref"]
        or (row.get("operation"), row.get("action")) not in {
            ("ledger.invoke", "finance.batch.acquire"),
            ("ledger.invoke", "finance.source_snapshot.write"),
            ("ledger.invoke", "finance.projection.commit"),
            ("browser.invoke", "ronghui.finance.capture_page"),
            ("browser.invoke", "ronghui.finance.verify_source_totals"),
        }
        for row in host_call_observations
    ):
        return False
    # Finance uses the legacy closed primitives. Their independently recorded
    # Host result contains the adapter-issued reference; the separate v2-only
    # observation.evidence_ref is null for these primitives.
    references = [row["result"]["evidence_ref"] for row in host_call_observations]
    public_refs = meta.get("evidence_refs")
    if (len(set(references)) != len(references) or not isinstance(public_refs, list)
            or any(not isinstance(ref, str) for ref in public_refs)
            or len(set(public_refs)) != len(public_refs) or not set(public_refs) <= set(references)):
        return False
    writes = [row for row in host_call_observations if row["write_started"]]
    if len(writes) != started_mutating_call_count or any(
        row.get("operation") != "ledger.invoke" or row["result"]["evidence_ref"] not in public_refs for row in writes
    ):
        return False
    if (writes[0].get("action") != "finance.batch.acquire"
            or writes[-1].get("action") != "finance.projection.commit"
            or host_call_observations[-1] is not writes[-1]
            or any(row.get("action") != "finance.source_snapshot.write" for row in writes[1:-1])
            or writes[0].get("role") != "finance_quote_source"
            or writes[-1].get("role") != "finance_quote_source"):
        return False
    batch, projection = writes[0]["result"], writes[-1]["result"]
    batch_id = data.get("batch_id")
    if (type(batch_id) is not int or batch_id < 1 or batch.get("acquired") is not True
            or projection.get("status") != data["status"]
            or not isinstance(batch.get("contract_sha256"), str)
            or projection.get("contract_sha256") != batch["contract_sha256"]
            or any(row["result"].get("schema_version") != 1
                   or row["result"].get("batch_id") != batch_id for row in writes)
            or any(row["result"].get("committed") is not True for row in writes[1:])):
        return False
    snapshots = [row["result"] for row in writes[1:-1]]
    targets, public_runs = batch.get("targets"), data.get("runs")
    if (not isinstance(targets, list) or not isinstance(public_runs, list)
            or len(targets) != len(snapshots) or len(public_runs) != len(snapshots)):
        return False
    for target, run, observation in zip(targets, public_runs, writes[1:-1]):
        snapshot = observation["result"]
        if (not isinstance(target, Mapping) or not isinstance(run, Mapping)
                or target.get("source_role") != observation.get("role")
                or any(run.get(field) != target.get(field) for field in ("source_role", "target_date"))
                or snapshot.get("outcome") not in {"success", "no_data", "failed"}
                or run.get("status") != snapshot["outcome"]):
            return False
        for public_field, snapshot_field in (("transactions", "record_count"), ("summaries", "summary_count")):
            count = snapshot.get(snapshot_field)
            if type(count) is not int or count < 0 or type(run.get(public_field)) is not int or run[public_field] != count:
                return False
        if snapshot.get("written_row_count") != snapshot["record_count"]:
            return False
    successful = [row for row in snapshots if row["outcome"] != "failed"]
    failed = len(snapshots) - len(successful)
    if not failed or data["status"] != ("partial_failed" if successful else "failed"):
        return False
    counts = {
        "successful_runs": len(successful),
        "no_data_runs": sum(row["outcome"] == "no_data" for row in snapshots),
        "failed_runs": failed,
        "written_record_count": sum(row["record_count"] for row in successful),
    }
    for field, count in counts.items():
        public_field = "written_transactions" if field == "written_record_count" else field
        if (type(projection.get(field)) is not int or projection[field] != count
                or type(data.get(public_field)) is not int or data[public_field] != count):
            return False
    return (type(data.get("summary_rows")) is int
            and data["summary_rows"] == sum(row["summary_count"] for row in snapshots)
            and meta.get("record_count") == counts["written_record_count"])
