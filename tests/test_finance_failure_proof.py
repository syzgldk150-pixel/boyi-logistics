from __future__ import annotations

import copy

import pytest

from agent.automation_plugins.finance_failure_proof import is_verified_finance_failure
from tests.test_sync_finance_bills_action_payload import _FinanceBroker, _load_action


def _failed_batch(*, fail_all=False):
    broker = _FinanceBroker(invalid_kind="balance")
    observations = []

    def observed(operation, *, action, role, arguments):
        if fail_all and action == "ronghui.finance.capture_page":
            raise RuntimeError("isolated supplier unavailable")
        result = broker(operation, action=action, role=role, arguments=arguments)
        observations.append({"operation": operation, "action": action, "role": role,
            "write_started": operation == "ledger.invoke", "result": copy.deepcopy(result),
            "evidence_ref": None})
        return result

    result = _load_action().run_action(
        {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1}, observed)
    return dict(plugin_id="sync_finance_bills", result=result,
        started_mutating_call_count=sum(row["write_started"] for row in observations),
        host_call_observations=observations)


def test_failed_finance_batch_matches_host_commits():
    assert is_verified_finance_failure(**_failed_batch())


def test_all_sources_failed_with_read_back_failure_records_is_known_failure():
    args = _failed_batch(fail_all=True)
    assert args["result"]["error"]["code"] == "FINANCE_SYNC_FAILED"
    assert is_verified_finance_failure(**args)


@pytest.mark.parametrize("tamper", ["plugin", "missing", "started", "projection", "batch",
    "amount_count", "summary_count", "status", "evidence", "extra_write", "snapshot", "role"])
def test_unknown_or_forged_finance_failure_is_not_verified(tamper):
    args = _failed_batch()
    rows = args["host_call_observations"]
    if tamper == "plugin":
        args["plugin_id"] = "other"
    elif tamper == "missing":
        args["host_call_observations"] = []
    elif tamper == "started":
        args["started_mutating_call_count"] += 1
    elif tamper == "projection":
        rows[-1]["result"]["committed"] = False
    elif tamper == "batch":
        rows[-1]["result"]["batch_id"] += 1
    elif tamper == "amount_count":
        args["result"]["data"]["written_transactions"] += 1
    elif tamper == "summary_count":
        args["result"]["data"]["summary_rows"] += 1
    elif tamper == "status":
        args["result"]["error"]["code"] = "WRITE_OUTCOME_UNKNOWN"
    elif tamper == "evidence":
        args["result"]["meta"]["evidence_refs"] = ["plugin-invented"]
    elif tamper == "extra_write":
        rows[-2]["action"] = "arbitrary.write"
    elif tamper == "snapshot":
        rows[-2]["result"]["committed"] = False
    elif tamper == "role":
        rows[-2]["role"] = "another_source"
    assert not is_verified_finance_failure(**args)
