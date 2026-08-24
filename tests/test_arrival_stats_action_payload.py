from __future__ import annotations

import pytest

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
    assert calls.index("scan.snapshot.read") < calls.index("scan.snapshot.replace")


def test_dry_run_counts_fresh_scan_without_mutating_the_snapshot() -> None:
    action_module = load_first_party_action("sync_arrival_stats")
    main = "R12345678901"
    child = f"{main}0001"
    calls: list[str] = []

    def response(action: str, **values: object) -> dict[str, object]:
        calls.append(action)
        return {
            **values,
            "evidence_ref": f"broker-evidence:{len(calls)}:{action}",
        }

    def broker(operation, *, action, role, arguments):
        del operation, role, arguments
        if action == "ronghui.arrive_list.read_page":
            return response(
                action,
                items=[
                    {
                        "tracking_number": main,
                        "goods_name": "goods",
                        "package_type": "carton",
                        "delivery_method": "dispatch",
                        "quantity": 1,
                        "recipient_name": "recipient",
                        "recipient_phone": "13800000000",
                        "recipient_address": "complete recipient address",
                    }
                ],
                pagination_complete=True,
                next_cursor=None,
            )
        if action == "ronghui.scan.read_page":
            return response(
                action,
                items=[
                    {
                        "bill_code": child,
                        "destination": "station",
                        "scan_type": "arrival",
                        "scan_time": "2026-08-24 08:00:00",
                        "scan_site": "station",
                    }
                ],
                pagination_complete=True,
                next_cursor=None,
            )
        if action == "arrival.snapshot.completed_before":
            return response(action, tracking_numbers=[], pagination_complete=True)
        if action == "scan.snapshot.read":
            return response(action, items=[], pagination_complete=True)
        raise AssertionError(f"dry-run attempted unexpected broker action: {action}")

    result = action_module.run_action(
        {"target_date": "2026-08-24", "dry_run": True},
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["count_result"]["arrived_nonzero"] == 1
    assert result["data"]["accumulated_main_trackings"] == 1
    assert calls == [
        "ronghui.arrive_list.read_page",
        "ronghui.scan.read_page",
        "arrival.snapshot.completed_before",
        "scan.snapshot.read",
    ]


def test_broker_budget_fails_before_detail_reads_or_writes() -> None:
    action_module = load_first_party_action("sync_arrival_stats")
    mains = [f"R{index:011d}" for index in range(986)]
    calls: list[str] = []

    def response(action: str, **values: object) -> dict[str, object]:
        calls.append(action)
        return {
            **values,
            "evidence_ref": f"broker-evidence:{len(calls)}:{action}",
        }

    def broker(operation, *, action, role, arguments):
        del operation, role, arguments
        if action == "ronghui.arrive_list.read_page":
            return response(action, items=[], pagination_complete=True, next_cursor=None)
        if action == "ronghui.scan.read_page":
            return response(
                action,
                items=[
                    {
                        "bill_code": main,
                        "destination": "station",
                        "scan_type": "arrival",
                        "scan_time": "2026-08-24 08:00:00",
                        "scan_site": "station",
                    }
                    for main in mains
                ],
                pagination_complete=True,
                next_cursor=None,
            )
        if action == "arrival.snapshot.completed_before":
            return response(action, tracking_numbers=[], pagination_complete=True)
        if action == "scan.snapshot.read":
            return response(action, items=[], pagination_complete=True)
        raise AssertionError(f"budget preflight allowed unexpected broker action: {action}")

    with pytest.raises(ValueError, match="signed broker call budget"):
        action_module.run_action({"target_date": "2026-08-24"}, broker)

    assert calls == [
        "ronghui.arrive_list.read_page",
        "ronghui.scan.read_page",
        "arrival.snapshot.completed_before",
        "scan.snapshot.read",
    ]
