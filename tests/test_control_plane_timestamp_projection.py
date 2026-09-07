"""Expose proven UTC instants without assigning a guessed zone to SQL metadata."""
from datetime import datetime, timedelta, timezone

from agent.orchestration.control_plane_service import _approval_dto, _run_dto, _work_item_dto


def _received(**extra):
    return {
        "run_id": "time-projection", "status": "RECEIVED", "steps": [],
        "created_at": datetime(2026, 9, 8, 1, 36, 4),
        "submitted_at": datetime(2026, 9, 7, 17, 36, 4),
        **extra,
    }


def test_queued_stage_uses_command_utc_instead_of_database_creation_clock():
    result = _run_dto(_received())
    assert result["submitted_at"] == "2026-09-07T17:36:04Z"
    assert result["stage_started_at"] == result["submitted_at"]
    assert result["created_at"] == "2026-09-08T01:36:04"


def test_legacy_queue_without_submission_time_does_not_invent_utc():
    row = _received()
    del row["submitted_at"]
    result = _run_dto(row)
    assert result["stage_started_at"] == "2026-09-08T01:36:04"
    assert "submitted_at" not in result


def test_running_step_uses_utc_start_and_keeps_database_metadata_unzoned():
    result = _run_dto(_received(status="RUNNING", steps=[{
        "step_id": "step", "step_order": 1, "status": "RUNNING",
        "operation_type": "READ", "started_at": datetime(2026, 9, 7, 17, 36, 5),
        "created_at": datetime(2026, 9, 8, 1, 36, 5),
    }]))
    assert result["stage_started_at"] == "2026-09-07T17:36:05Z"
    assert result["steps"][0]["started_at"] == result["stage_started_at"]
    assert result["steps"][0]["created_at"] == "2026-09-08T01:36:05"


def test_terminal_stage_uses_utc_finish_without_changing_submission():
    result = _run_dto(_received(status="CANCELLED", finished_at=datetime(2026, 9, 7, 17, 36, 54)))
    assert result["stage_started_at"] == "2026-09-07T17:36:54Z"
    assert result["submitted_at"] == "2026-09-07T17:36:04Z"


def test_approval_deadline_is_utc_but_old_mixed_decision_time_is_not_guessed():
    result = _approval_dto({
        "expires_at": datetime(2026, 9, 7, 17, 51, 4),
        "decided_at": datetime(2026, 9, 8, 1, 37, 0),
        "created_at": datetime(2026, 9, 8, 1, 36, 4),
    })
    assert result["expires_at"] == "2026-09-07T17:51:04Z"
    assert result["decided_at"] == "2026-09-08T01:37:00"
    assert result["created_at"] == "2026-09-08T01:36:04"


def test_aware_metadata_keeps_its_actual_instant():
    result = _run_dto(_received(created_at=datetime(2026, 9, 8, 1, 36, 4,
                                                   tzinfo=timezone(timedelta(hours=8)))))
    assert result["created_at"] == "2026-09-07T17:36:04Z"


def test_business_local_close_time_does_not_acquire_a_guessed_utc_zone():
    result = _work_item_dto({"closed_at": datetime(2026, 9, 8, 1, 36, 4)})
    assert result["closed_at"] == "2026-09-08T01:36:04"
