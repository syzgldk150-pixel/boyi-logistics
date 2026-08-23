from __future__ import annotations

from tests.first_party_action_payload_support import load_first_party_action


def test_empty_source_reports_verified_snapshot_and_clears_all_sinks() -> None:
    action_module = load_first_party_action("sync_arrive_list")
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        del operation
        calls.append(action)
        if action == "ronghui.arrive_list.read_page":
            assert arguments["target_date"] == "2026-08-24"
            return {
                "items": [],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "broker-evidence:arrive-empty-source",
            }
        if action in {
            "waybill.snapshot.replace",
            "arrival.forecast_snapshot.replace",
        }:
            assert arguments["records"] == []
        elif action == "feishu.sheet.replace":
            assert arguments["values"] == []
            assert arguments["target_date"] == "2026-08-24"
        else:
            raise AssertionError(action)
        return {
            "committed": True,
            "record_count": 0,
            "evidence_ref": f"broker-evidence:{action}:{role}",
        }

    result = action_module.run_action({"target_date": "2026-08-24"}, broker)

    assert result["status"] == "SUCCESS"
    assert result["meta"]["record_count"] == 0
    assert result["data"]["evidence"]["execution_result"] == "no_data_cleared"
    assert calls == [
        "ronghui.arrive_list.read_page",
        "waybill.snapshot.replace",
        "feishu.sheet.replace",
        "feishu.sheet.replace",
        "arrival.forecast_snapshot.replace",
    ]
