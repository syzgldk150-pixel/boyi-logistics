from __future__ import annotations

import pytest

from agent.automation_plugins.management import (
    classify_arrival_stats_recovery_readback,
)


def test_empty_arrival_stats_readback_is_not_applied() -> None:
    assert classify_arrival_stats_recovery_readback(
        {
            "arrival_stat_runs": 0,
            "arrival_stat_items": 0,
            "feishu_rows_created": 0,
        }
    ) == "NOT_APPLIED"


@pytest.mark.parametrize(
    "field",
    ("arrival_stat_runs", "arrival_stat_items", "feishu_rows_created"),
)
def test_any_arrival_stats_readback_evidence_stays_unknown(field: str) -> None:
    evidence = {
        "arrival_stat_runs": 0,
        "arrival_stat_items": 0,
        "feishu_rows_created": 0,
    }
    evidence[field] = 1
    assert classify_arrival_stats_recovery_readback(evidence) == "WRITE_OUTCOME_UNKNOWN"


def test_arrival_stats_readback_contract_is_closed() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        classify_arrival_stats_recovery_readback(
            {"arrival_stat_runs": 0, "arrival_stat_items": 0}
        )
    with pytest.raises(ValueError, match="invalid"):
        classify_arrival_stats_recovery_readback(
            {
                "arrival_stat_runs": -1,
                "arrival_stat_items": 0,
                "feishu_rows_created": 0,
            }
        )
