"""Recognize a failed daily-sign read whose only writes were verified run records."""
from collections.abc import Mapping

from agent.automation_plugins.daily_sign_connectors_v2 import DAILY_SIGN_PORTS


def is_verified_daily_sign_failure(*, plugin_id, result, started_mutating_call_count, host_call_observations):
    if (plugin_id != "sync_daily_should_sign_v2" or result.get("status") != "FAILED"
            or type(started_mutating_call_count) is not int or started_mutating_call_count != 2):
        return False
    data, error, meta = (result.get(key) for key in ("data", "error", "meta"))
    if (not all(isinstance(value, Mapping) for value in (data, error, meta))
            or not error.get("code") or error["code"] == "WRITE_OUTCOME_UNKNOWN"):
        return False
    run_id = data.get("source_run_id")
    if not isinstance(run_id, str) or not run_id or not host_call_observations:
        return False
    calls = []
    for row in host_call_observations:
        if not isinstance(row, Mapping):
            return False
        target, output = row.get("service_target"), row.get("result")
        if (not isinstance(target, Mapping) or not isinstance(output, Mapping)
                or set(target) != {"service", "operation", "effect"}
                or row.get("operation") != "service.invoke" or row.get("role") != "__system__"
                or row.get("action") != target.get("operation")
                or type(row.get("write_started")) is not bool
                or not isinstance(row.get("evidence_ref"), str) or not row["evidence_ref"]):
            return False
        role = next((role for role in DAILY_SIGN_PORTS if target["service"] == f"connector.boyi.{role}@1"), None)
        effect = DAILY_SIGN_PORTS[role][2].get(target["operation"]) if role else None
        if effect is None or effect != target["effect"] or row["write_started"] != (effect != "read"):
            return False
        calls.append((role, target["operation"], output.get("value"), row["evidence_ref"], row["write_started"]))
    references = [call[3] for call in calls]
    public_refs = meta.get("evidence_refs")
    if (len(set(references)) != len(references) or not isinstance(public_refs, list)
            or any(not isinstance(ref, str) for ref in public_refs)
            or len(set(public_refs)) != len(public_refs) or not set(public_refs) <= set(references)):
        return False
    writes = [call for call in calls if call[4]]
    if (len(writes) != 2 or [call[:2] for call in writes] != [
            ("daily_sign_store", "start_run"), ("daily_sign_store", "finish_run")]
            or any(not isinstance(call[2], Mapping) or call[3] not in public_refs for call in writes)
            or writes[0][2].get("run_id") != run_id or writes[1][2].get("committed") is not True):
        return False
    verification = calls[-1]
    value = verification[2]
    return (verification[:2] == ("daily_sign_store", "verify_run")
            and calls.index(writes[-1]) == len(calls) - 2
            and verification[3] in public_refs and isinstance(value, Mapping)
            and value.get("verified") is True and value.get("run_id") == run_id
            and value.get("status") == "failed" and type(value.get("record_count")) is int
            and value["record_count"] == 0)
