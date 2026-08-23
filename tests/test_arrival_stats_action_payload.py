from __future__ import annotations

from tests.first_party_action_payload_support import load_first_party_action


def test_empty_source_clears_snapshots_without_creating_archive() -> None:
    action_module = load_first_party_action("sync_arrival_stats")
    calls: list[str] = []
    evidence_index = 0

    def broker(operation, *, action, role, arguments):
        nonlocal evidence_index
        del operation, role
        calls.append(action)
        evidence_index += 1
        if action in {"ronghui.arrive_list.read_page", "ronghui.scan.read_page"}:
            assert arguments["target_date"] == "2026-08-24"
            return {
                "items": [],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": f"broker-evidence:{evidence_index}:{action}",
            }
        if action == "arrival.snapshot.completed_before":
            return {
                "tracking_numbers": [],
                "pagination_complete": True,
                "evidence_ref": f"broker-evidence:{evidence_index}:completed-before",
            }
        if action == "scan.snapshot.read":
            return {
                "items": [],
                "pagination_complete": True,
                "evidence_ref": f"broker-evidence:{evidence_index}:scan-read",
            }
        if action == "waybill.pending.read":
            return {
                "items": [],
                "pagination_complete": True,
                "evidence_ref": f"broker-evidence:{evidence_index}:pending-read",
            }
        if action == "feishu.sheet.add":
            raise AssertionError("empty snapshots must not create an archive sheet")
        if action == "feishu.sheet.replace":
            assert arguments["records"] == []
            assert arguments["target_date"] == "2026-08-24"
        elif action in {
            "scan.snapshot.replace",
            "waybill.snapshot.replace",
            "split_pending.snapshot.refresh",
            "arrival.snapshot.replace",
        }:
            assert arguments["records"] == []
            assert arguments["target_date"] == "2026-08-24"
        elif action != "scan.snapshot.cleanup":
            raise AssertionError(action)
        return {
            "committed": True,
            "record_count": 0,
            "evidence_ref": f"broker-evidence:{evidence_index}:{action}",
        }

    result = action_module.run_action({"target_date": "2026-08-24"}, broker)

    assert result["status"] == "SUCCESS"
    assert result["meta"]["record_count"] == 0
    assert result["data"]["evidence"]["execution_result"] == "no_data_cleared"
    assert "feishu.sheet.add" not in calls
    assert calls.index("scan.snapshot.cleanup") < calls.index("scan.snapshot.read")
