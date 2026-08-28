from __future__ import annotations

from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from tools.daily_sign_rules import (
    build_ledger_row,
    calculate_system_sign_due,
    ledger_row_should_publish,
)
from tools.daily_sign_pipeline import DailySignSyncError
from tools import (
    daily_sign_backfill_tool,
    daily_sign_readback,
    daily_sign_store,
    daily_sign_sync_tool,
)
from agent.execution_boundary import current_execution_capability, execution_capability_scope
from agent.automation_plugins.first_party_handler_common import _encode_daily_sign_result
from agent.tms_runtime.scripts import get_qianshou, get_scan, get_sign_records


def arrival(day: str, expected: int, arrived: int, destination: str = "邵阳大祥S站") -> dict:
    return {
        "business_date": day,
        "expected_quantity": expected,
        "arrived_quantity": arrived,
        "destination_station": destination,
    }


def problem(problem_type: str, registered_at: str, *, complete: bool = True) -> dict:
    return {
        "external_id": f"{problem_type}-{registered_at}",
        "problem_type": problem_type,
        "registered_at": registered_at,
        "upload_complete": complete,
    }


def persisted_snapshot_proof(**kwargs) -> dict:
    marker = kwargs["persistence_marker"]
    return {
        "ok": True,
        "ledger_rows": len(kwargs["ledger_rows"]),
        "publication_rows": len(kwargs["publication_rows"]),
        "persistence_marker": marker,
    }


def persistence_readback_proof(**kwargs) -> dict:
    marker = kwargs["persistence_marker"]

    def row_set(name: str) -> dict:
        return {
            "verified": True,
            "record_count": marker[name]["count"],
            "sha256": marker[name]["sha256"],
        }

    return {
        "verified": True,
        "record_count": len(kwargs["ledger_rows"]),
        "problem_events": row_set("problem_events"),
        "sign_events": row_set("sign_events"),
        "sign_verification_states": row_set("sign_verification_states"),
        "ledger_rows": row_set("ledger_rows"),
        "publication_rows": row_set("publication_rows"),
        "ledger_sha256": marker["ledger_rows"]["sha256"],
        "publication_sha256": marker["publication_rows"]["sha256"],
        "persistence_sha256": marker["marker_sha256"],
    }


def projection_readback_proof(rows: list[dict], *, digest_char: str) -> dict:
    return {
        "verified": True,
        "record_count": len(rows),
        "snapshot_sha256": digest_char * 64,
    }


def completed_run_readback_proof(*, expected_values, **_kwargs) -> dict:
    diagnostics = expected_values.get("diagnostics_json")
    marker = (
        diagnostics.get("persistence_commit")
        if isinstance(diagnostics, dict)
        else None
    )
    return {
        "verified": True,
        "record_count": expected_values.get("published_rows", 0),
        "publication_sha256": expected_values.get("fingerprint") or "",
        "persistence_sha256": (
            marker.get("marker_sha256") if isinstance(marker, dict) else ""
        ),
    }


def failed_run_values(_run_id, diagnostics, *, message: str) -> dict:
    return {
        "status": "failed",
        "published_rows": 0,
        "fingerprint": diagnostics.get("fingerprint"),
        "diagnostics_json": diagnostics,
        "error_summary": message,
    }


class DailySignLedgerRulesTest(unittest.TestCase):
 def test_missing_publication_fields_are_enriched_from_real_waybill_detail(self):
    rows = [
        {
            "tracking_number": "R001",
            "goods_name": None,
            "package_type": None,
            "expected_quantity": None,
            "delivery_method": None,
            "recipient_address": "湖南省******",
        }
    ]
    detail = {
        "data": [
            {
                "tracking_number": "R001",
                "goods_name": "医疗器械",
                "package_type": "木箱",
                "quantity": 3,
                "delivery_method": "派送",
                "recipient_address": "湖南省邵阳市大祥区测试路1号",
            }
        ]
    }

    with patch(
        "tools.daily_sign_sync_tool.call_http_service",
        return_value=detail,
    ) as call_detail:
        enriched, result = daily_sign_sync_tool._enrich_missing_addresses(
            rows,
            {"account_id": "ronghui_daxiang_s"},
        )

    self.assertTrue(result["ok"])
    self.assertEqual(1, result["updated"])
    self.assertEqual("医疗器械", enriched[0]["goods_name"])
    self.assertEqual("木箱", enriched[0]["package_type"])
    self.assertEqual(3, enriched[0]["expected_quantity"])
    self.assertEqual("派送", enriched[0]["delivery_method"])
    self.assertEqual("湖南省邵阳市大祥区测试路1号", enriched[0]["recipient_address"])
    self.assertEqual("/query_waybill_detail", call_detail.call_args.args[0])

 def test_broker_accepts_catalog_daily_sign_postcondition_name(self):
    digest = "a" * 64
    result = _encode_daily_sign_result(
        {
            "status": "SUCCESS",
            "data": {},
            "meta": {
                "account_id": "multi_account",
                "pagination_complete": True,
                "evidence_refs": ["proof"],
                "postconditions": {"0": True},
                "postcondition_evidence": {
                    "0": {
                        "verified": True,
                        "condition": "authoritative_snapshot_committed",
                        "details": {
                            "persistence_sha256": digest,
                            "bitable_snapshot_sha256": digest,
                            "sheet_snapshot_sha256": digest,
                        },
                    }
                },
            },
            "warnings": [],
            "error": None,
        }
    )

    self.assertEqual("multi_account", result["meta"]["account_scope"])

 def test_broker_relies_on_verified_daily_sign_proof_instead_of_digest_shape(self):
    result = _encode_daily_sign_result(
        {
            "status": "SUCCESS",
            "data": {},
            "meta": {
                "account_id": "multi_account",
                "pagination_complete": True,
                "evidence_refs": ["proof"],
                "postconditions": {"0": True},
                "postcondition_evidence": {
                    "0": {
                        "verified": True,
                        "condition": "authoritative_snapshot_committed",
                        "evidence_ref": "proof",
                        "details": {"source_run_id": "run"},
                    }
                },
            },
            "warnings": [],
            "error": None,
        }
    )

    self.assertTrue(result["meta"]["postconditions"]["0"])

 def test_bitable_readback_treats_omitted_empty_cells_as_blank(self):
    expected = [
        {
            "fields": {
                "运单编号": "A",
                "货物品名": "",
                "货物件数": None,
                "到货件数": 0,
            }
        }
    ]
    observed = {
        "items": [
            {
                "record_id": "rec-a",
                "fields": {"运单编号": "A", "到货件数": 0},
            }
        ]
    }

    proof = daily_sign_readback.verify_bitable_snapshot(
        expected,
        observed,
        identity_field="运单编号",
    )

    self.assertTrue(proof["verified"])
    self.assertEqual(1, proof["record_count"])

 def test_verification_state_uses_database_second_precision(self):
    rows = daily_sign_store._normalize_sign_verification_states(
        [
            {
                "tracking_number": "A",
                "last_checked_at": datetime(2026, 8, 26, 23, 49, 9, 586445),
                "last_result": "not_signed",
                "next_check_at": datetime(2026, 8, 27, 23, 49, 9, 586445),
                "consecutive_not_signed": 1,
                "last_error": None,
            }
        ]
    )

    self.assertEqual(datetime(2026, 8, 26, 23, 49, 9), rows[0]["last_checked_at"])
    self.assertEqual(datetime(2026, 8, 27, 23, 49, 9), rows[0]["next_check_at"])

 def test_ledger_fingerprint_uses_database_second_precision(self):
    row = {
        field: None
        for field in daily_sign_store.LEDGER_FIELDS
    }
    row.update(
        {
            "tracking_number": "A",
            "last_seen_r13_at": datetime(2026, 8, 27, 0, 1, 47, 549921),
            "r13_current": True,
            "tms_signed": False,
            "data_quality_flags": [],
            "calculation_trace": {},
        }
    )

    canonical = daily_sign_store._canonical_ledger_row(row)

    self.assertEqual("2026-08-27 00:01:47", canonical["last_seen_r13_at"])

 def test_persistence_readback_message_exposes_only_safe_row_set(self):
    exc = daily_sign_store.DailySignPersistenceReadbackError(
        "daily-sign ledger rows fresh readback changed"
    )

    message = daily_sign_sync_tool._persistence_readback_failure_message(exc)

    self.assertEqual(
        "每日应签权威持久化回读不匹配：daily-sign ledger rows fresh readback changed",
        message,
    )

 def test_publication_readback_uses_due_subset_of_full_ledger(self):
    due = {"tracking_number": "DUE", "tms_signed": False}
    future = {"tracking_number": "FUTURE", "tms_signed": False}

    observed = daily_sign_store._select_publication_readback_rows(
        [due],
        [due, future],
    )

    self.assertEqual([due], observed)

 def test_authoritative_ledger_snapshot_prunes_rows_outside_current_candidates(self):
    class Cursor:
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append((" ".join(sql.split()), params))

        def executemany(self, sql, params):
            self.executed.append((" ".join(sql.split()), params))

    row = {field: None for field in daily_sign_store.LEDGER_FIELDS}
    row.update(
        {
            "tracking_number": "A",
            "r13_current": False,
            "tms_signed": False,
            "data_quality_flags": [],
            "calculation_trace": {},
        }
    )
    cursor = Cursor()

    daily_sign_store._upsert_ledger_rows(cursor, [row], prune_missing=True)

    delete_sql, delete_params = cursor.executed[-1]
    self.assertEqual(
        "DELETE FROM daily_sign_ledger WHERE tracking_number NOT IN (%s)",
        delete_sql,
    )
    self.assertEqual(("A",), delete_params)

 def test_exact_tracking_workers_inherit_execution_capability(self):
    observed_capabilities = []

    def fake_query(code, _params):
        observed_capabilities.append(current_execution_capability())
        return {"tracking_number": code, "sign_event": None}

    with (
        execution_capability_scope("sync_daily_should_sign", ttl_seconds=30),
        patch("tools.daily_sign_sync_tool._query_exact_main_sign", side_effect=fake_query),
    ):
        expected = current_execution_capability()
        results = daily_sign_sync_tool._query_exact_sign_results(["A", "B"], {})

    self.assertEqual({"A", "B"}, {row["tracking_number"] for row in results})
    self.assertTrue(expected)
    self.assertEqual([expected, expected], sorted(observed_capabilities))

 def test_normal_complete_is_due_next_day_end_of_day(self):
    due, state = calculate_system_sign_due([arrival("2026-08-12", 10, 10)], [])
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)
    self.assertEqual("complete_on_first_arrival", state["trace"]["reason"])


 def test_partial_without_valid_problem_is_due_next_day(self):
    due, _ = calculate_system_sign_due([arrival("2026-08-12", 10, 5)], [])
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)


 def test_partial_with_successful_split_problem_before_cutoff_has_blank_due(self):
    due, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5)],
        [problem("少货/分批", "2026-08-12 16:59:59")],
    )
    self.assertIsNone(due)


 def test_split_problem_at_cutoff_or_failed_does_not_postpone(self):
    due_at_cutoff, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5)],
        [problem("少货/分批", "2026-08-12 17:00:00")],
    )
    due_failed, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5)],
        [problem("少货/分批", "2026-08-12 16:00:00", complete=False)],
    )
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due_at_cutoff)
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due_failed)


 def test_partial_completion_is_due_on_completion_day(self):
    due, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5), arrival("2026-08-13", 10, 5)],
        [problem("少货/分批", "2026-08-12 16:00:00")],
    )
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)


 def test_daily_arrival_rows_are_accumulated_until_expected_quantity_is_reached(self):
    due, state = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5), arrival("2026-08-13", 10, 5)],
        [],
    )
    self.assertEqual(10, state["arrived_quantity"])
    self.assertEqual("completed", state["arrival_status"])
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)


 def test_exact_manual_problem_before_cutoff_only_moves_due_later(self):
    due, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5), arrival("2026-08-13", 10, 5)],
        [
            problem("少货/分批", "2026-08-12 16:00:00"),
            problem("客户要求延迟派送", "2026-08-13 16:59:59"),
            problem("联系不上收件人", "2026-08-14 16:00:00"),
        ],
    )
    self.assertEqual(datetime(2026, 8, 15, 23, 59, 59), due)


 def test_inexact_manual_type_and_after_cutoff_are_invalid(self):
    due, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 10)],
        [
            problem("客户要求延迟派送（其他）", "2026-08-13 16:00:00"),
            problem("联系不上收件人", "2026-08-13 17:00:00"),
        ],
    )
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)


 def test_r13_only_candidate_keeps_r13_due_and_blank_system_due(self):
    row = build_ledger_row(
        "R1",
        r13_row={"billNumberMain": "R1", "planSignTime": "2026-08-13 23:59:59"},
        previous_row=None,
        arrival_history=[],
        problem_events=[],
        sign_event=None,
        observed_at=datetime(2026, 8, 12, 12, 0, 0),
    )
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), row["r13_plan_sign_at"])
    self.assertIsNone(row["system_sign_due_at"])
    self.assertIn("r13_without_arrival_history", row["data_quality_flags"])


 def test_r13_signed_without_tms_scan_stays_open_but_tms_main_sign_closes(self):
    open_row = build_ledger_row(
        "R1",
        r13_row={"billNumberMain": "R1", "signTime": "2026-08-13 10:00:00"},
        previous_row=None,
        arrival_history=[arrival("2026-08-12", 1, 1)],
        problem_events=[],
        sign_event=None,
        observed_at=datetime(2026, 8, 13, 12, 0, 0),
    )
    closed_row = build_ledger_row(
        "R1",
        r13_row={"billNumberMain": "R1", "isSigns": "未签"},
        previous_row=None,
        arrival_history=[arrival("2026-08-12", 1, 1)],
        problem_events=[],
        sign_event={"scanned_at": "2026-08-13 11:00:00", "scan_type": "签收"},
        observed_at=datetime(2026, 8, 13, 12, 0, 0),
    )
    self.assertFalse(open_row["tms_signed"])
    self.assertIn("r13_signed_without_tms_scan", open_row["data_quality_flags"])
    self.assertTrue(closed_row["tms_signed"])


 def test_stale_same_code_sign_before_current_dispatch_does_not_close(self):
    row = build_ledger_row(
        "R1",
        r13_row={
            "billNumberMain": "R1",
            "isSigns": "未签",
            "dispTime": "2026-08-27 08:00:00",
            "planSignTime": "2026-08-27 23:59:59",
        },
        previous_row=None,
        arrival_history=[arrival("2026-08-27", 1, 1)],
        problem_events=[],
        sign_event={
            "scanned_at": "2026-01-01 10:00:00",
            "scan_type": "签收",
            "external_id": "old-cycle-sign",
        },
        observed_at=datetime(2026, 8, 27, 12, 0, 0),
    )

    self.assertFalse(row["tms_signed"])
    self.assertIsNone(row["tms_signed_at"])
    self.assertIn("stale_sign_before_current_lifecycle", row["data_quality_flags"])
    self.assertTrue(ledger_row_should_publish(row, datetime(2026, 8, 27).date()))


 def test_historical_due_row_requires_authoritative_r13_plan_before_publication(self):
    observed_at = datetime(2026, 8, 13, 9, 0, 0)
    missing_plan = build_ledger_row(
        "R-MISSING",
        r13_row=None,
        previous_row={"first_seen_r13_at": "2026-08-11 09:00:00"},
        arrival_history=[arrival("2026-08-12", 1, 1)],
        problem_events=[],
        sign_event=None,
        observed_at=observed_at,
    )
    valid_plan = build_ledger_row(
        "R-VALID",
        r13_row=None,
        previous_row={
            "first_seen_r13_at": "2026-08-11 09:00:00",
            "r13_plan_sign_at": "2026-08-13 23:59:59",
        },
        arrival_history=[arrival("2026-08-12", 1, 1)],
        problem_events=[],
        sign_event=None,
        observed_at=observed_at,
    )

    self.assertFalse(ledger_row_should_publish(missing_plan, observed_at.date()))
    self.assertTrue(ledger_row_should_publish(valid_plan, observed_at.date()))


class DailySignSyncPipelineTest(unittest.TestCase):
    @staticmethod
    def _params(**overrides):
        return {
            "r13_account_id": "r13-test",
            "account_id": "ronghui-test",
            "days": 1,
            "enrich_addresses": False,
            **overrides,
        }

    def _state(self, *, signs=None):
        return {
            "ledger": {
                "OLD": {
                    "tracking_number": "OLD",
                    "r13_plan_sign_at": "2026-08-11 23:59:59",
                    "tms_signed": False,
                    "goods_name": "历史货",
                }
            },
            "arrivals": {
                "R1": [arrival("2026-08-12", 2, 2)],
                "R2": [arrival("2026-08-12", 1, 1, destination="旧网点")],
            },
            "target_station_codes": {"R1"},
            "problems": {},
            "signs": signs or {},
            "sign_verifications": {},
            "source_refs": ["arrival_stat:arrival-run:arrival-hash"],
            "arrival_source_proof": {
                "complete": True,
                "active_stat_runs": 1,
                "latest_forecast_runs": 0,
                "run_ids": ["arrival-run"],
            },
        }

    def test_candidate_union_publishes_current_r13_rows_and_only_due_history(self):
        state = self._state()
        captured = []
        observed_at = datetime(2026, 8, 12, 12, 0, 0)
        r13_rows = [
            {
                "billNumberMain": "R2",
                "planSignTime": "2026-08-13 23:59:59",
                "signTime": "2026-08-12 09:00:00",
                "goodsName": "测试货物",
                "packTypeDesc": "纸箱",
                "pcs": 1,
                "dispatchMode": "派送",
                "dispAddress": "测试地址",
            },
            {
                "billNumberMain": "R3",
                "planSignTime": "2026-08-13 23:59:59",
                "goodsName": "测试货物",
                "packTypeDesc": "纸箱",
                "pcs": 1,
                "dispatchMode": "派送",
                "dispAddress": "测试地址",
            },
        ]
        old_sign = {
            "source": "ronghui_sign:test",
            "external_id": "old-sign",
            "tracking_number": "OLD",
            "scan_code": "OLD",
            "scan_type": "签收",
            "scanned_at": "2026-08-12 10:00:00",
            "scan_site": "邵阳大祥S站",
            "is_main_waybill": True,
        }

        def sync_bitable(rows, _params):
            return {
                "ok": True,
                "written": len(rows),
                "readback": projection_readback_proof(rows, digest_char="b"),
            }

        def sync_sheet(rows, _params):
            captured.extend(rows)
            return {
                "ok": True,
                "rows": len(rows),
                "readback": projection_readback_proof(rows, digest_char="s"),
            }

        with (
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                return_value=r13_rows,
            ),
            patch(
                "tools.daily_sign_sync_tool.start_sync_run",
                return_value=("run", observed_at),
            ),
            patch("tools.daily_sign_sync_tool.finish_sync_run"),
            patch("tools.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch(
                "tools.daily_sign_pipeline._source_query_window",
                return_value=(observed_at, observed_at),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_problem_events",
                return_value=([], {"rows": 0, "declared_total": 0, "complete": True}),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_sign_events",
                return_value=([old_sign], {"source_rows": 1, "complete": True}),
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_r13_sign_conflicts",
                return_value=([], {"ok": True, "complete": True}),
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_historical_sign_verifications",
                return_value=(
                    [],
                    {"ok": True, "complete": True, "verification_rows": []},
                ),
            ),
            patch(
                "tools.daily_sign_sync_tool.persist_daily_sign_snapshot",
                side_effect=persisted_snapshot_proof,
            ) as persist,
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_persistence",
                side_effect=persistence_readback_proof,
            ) as verify_persistence,
            patch(
                "tools.daily_sign_sync_tool._sync_bitable",
                side_effect=sync_bitable,
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_sheet",
                side_effect=sync_sheet,
            ),
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_completed_run",
                side_effect=completed_run_readback_proof,
            ) as verify_completed,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(self._params())

        self.assertEqual("SUCCESS", result["status"])
        self.assertIsNone(result["error"])
        self.assertEqual("run", result["data"]["source_run_id"])
        self.assertTrue(result["meta"]["pagination_complete"])
        self.assertEqual(
            "authoritative_snapshot_committed",
            result["meta"]["postcondition_evidence"]["0"]["condition"],
        )
        self.assertEqual(
            ["R2", "R3"],
            sorted(row["tracking_number"] for row in captured),
        )
        persisted_rows = persist.call_args.kwargs["ledger_rows"]
        old = next(row for row in persisted_rows if row["tracking_number"] == "OLD")
        r2 = next(row for row in persisted_rows if row["tracking_number"] == "R2")
        r3 = next(row for row in persisted_rows if row["tracking_number"] == "R3")
        self.assertTrue(old["tms_signed"])
        self.assertFalse(r2["tms_signed"])
        self.assertIn("r13_signed_without_tms_scan", r2["data_quality_flags"])
        self.assertIsNone(r3["system_sign_due_at"])
        self.assertIsNone(r3["arrived_quantity"])
        marker = persist.call_args.kwargs["persistence_marker"]
        self.assertEqual(0, marker["problem_events"]["count"])
        self.assertEqual(1, marker["sign_events"]["count"])
        self.assertEqual(0, marker["sign_verification_states"]["count"])
        self.assertEqual(4, marker["ledger_rows"]["count"])
        self.assertEqual(2, marker["publication_rows"]["count"])
        self.assertEqual(64, len(marker["marker_sha256"]))
        verify_persistence.assert_called_once()
        verify_completed.assert_called_once()

    def test_missing_projection_readback_proof_fails_closed(self):
        observed_at = datetime(2026, 8, 12, 12, 0, 0)
        state = {
            "ledger": {},
            "arrivals": {},
            "target_station_codes": set(),
            "problems": {},
            "signs": {},
            "sign_verifications": {},
            "source_refs": [],
            "arrival_source_proof": {
                "complete": True,
                "active_stat_runs": 1,
                "latest_forecast_runs": 0,
                "run_ids": ["arrival-run"],
            },
        }
        with (
            patch("tools.daily_sign_sync_tool.call_http_service", return_value=[]),
            patch(
                "tools.daily_sign_sync_tool.start_sync_run",
                return_value=("run", observed_at),
            ),
            patch(
                "tools.daily_sign_sync_tool.load_daily_sign_state",
                return_value=state,
            ),
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch(
                "tools.daily_sign_pipeline._source_query_window",
                return_value=(observed_at, observed_at),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_problem_events",
                return_value=(
                    [],
                    {"rows": 0, "declared_total": 0, "complete": True},
                ),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_sign_events",
                return_value=([], {"source_rows": 0, "complete": True}),
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_r13_sign_conflicts",
                return_value=([], {"ok": True, "complete": True}),
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_historical_sign_verifications",
                return_value=(
                    [],
                    {"ok": True, "complete": True, "verification_rows": []},
                ),
            ),
            patch(
                "tools.daily_sign_sync_tool.persist_daily_sign_snapshot",
                side_effect=persisted_snapshot_proof,
            ),
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_persistence",
                side_effect=persistence_readback_proof,
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_bitable",
                return_value={"ok": True},
            ),
            patch("tools.daily_sign_sync_tool._sync_sheet") as sheet,
            patch("tools.daily_sign_sync_tool.finish_sync_run") as finish,
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_completed_run"
            ) as verify_completed,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(self._params())

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("WRITE_OUTCOME_UNKNOWN", result["error"]["code"])
        self.assertFalse(result["error"]["retryable"])
        sheet.assert_not_called()
        finish.assert_not_called()
        verify_completed.assert_not_called()

    def test_sign_query_failure_is_blocked_and_never_publishes(self):
        state = self._state()
        observed_at = datetime(2026, 8, 12, 12, 0, 0)
        with (
            patch("tools.daily_sign_sync_tool.call_http_service", return_value=[]),
            patch(
                "tools.daily_sign_sync_tool.start_sync_run",
                return_value=("run", observed_at),
            ),
            patch("tools.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch(
                "tools.daily_sign_pipeline._source_query_window",
                return_value=(observed_at, observed_at),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_problem_events",
                return_value=([], {"rows": 0, "declared_total": 0, "complete": True}),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_sign_events",
                side_effect=DailySignSyncError(
                    "INCOMPLETE_SOURCE_EVIDENCE",
                    "主单签收来源不完整。",
                    retryable=True,
                ),
            ),
            patch(
                "tools.daily_sign_pipeline._finish_failed_run",
                side_effect=failed_run_values,
            ) as failed_run,
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_completed_run",
                side_effect=completed_run_readback_proof,
            ) as verify_completed,
            patch("tools.daily_sign_sync_tool.persist_daily_sign_snapshot") as persist,
            patch("tools.daily_sign_sync_tool._sync_bitable") as bitable,
            patch("tools.daily_sign_sync_tool._sync_sheet") as sheet,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(self._params())

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("INCOMPLETE_SOURCE_EVIDENCE", result["error"]["code"])
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual("run", result["data"]["source_run_id"])
        self.assertFalse(result["meta"]["pagination_complete"])
        failed_run.assert_called_once()
        verify_completed.assert_called_once()
        persist.assert_not_called()
        bitable.assert_not_called()
        sheet.assert_not_called()

    def test_problem_query_incomplete_stops_before_publish(self):
        state = self._state()
        observed_at = datetime(2026, 8, 12, 12, 0, 0)
        with (
            patch("tools.daily_sign_sync_tool.call_http_service", return_value=[]),
            patch(
                "tools.daily_sign_sync_tool.start_sync_run",
                return_value=("run", observed_at),
            ),
            patch("tools.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch(
                "tools.daily_sign_pipeline._source_query_window",
                return_value=(observed_at, observed_at),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_problem_events",
                side_effect=DailySignSyncError(
                    "PAGINATION_INCOMPLETE",
                    "问题件分页不完整。",
                    retryable=True,
                ),
            ),
            patch(
                "tools.daily_sign_pipeline._finish_failed_run",
                side_effect=failed_run_values,
            ) as failed_run,
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_completed_run",
                side_effect=completed_run_readback_proof,
            ) as verify_completed,
            patch("tools.daily_sign_sync_tool.persist_daily_sign_snapshot") as persist,
            patch("tools.daily_sign_sync_tool._sync_bitable") as bitable,
            patch("tools.daily_sign_sync_tool._sync_sheet") as sheet,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(self._params())

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("PAGINATION_INCOMPLETE", result["error"]["code"])
        self.assertTrue(result["error"]["retryable"])
        self.assertFalse(result["meta"]["pagination_complete"])
        failed_run.assert_called_once()
        verify_completed.assert_called_once()
        persist.assert_not_called()
        bitable.assert_not_called()
        sheet.assert_not_called()

    def test_problem_query_retries_transient_source_failure_without_guessing(self):
        problem_row = {
            "external_id": "P1",
            "registered_at": "2026-08-12 16:00:00",
            "problem_type": "联系不上收件人",
            "waybill_no": "R1",
            "registered_site": "邵阳大祥S站",
        }
        with (
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                side_effect=[
                    {
                        "ok": False,
                        "data": {
                            "ok": False,
                            "error_code": "SOURCE_QUERY_FAILED",
                            "message": "temporary source failure",
                        },
                    },
                    {
                        "ok": True,
                        "data": {
                            "ok": True,
                            "rows": [problem_row],
                            "stats": {"total": 1},
                        },
                    },
                ],
            ) as query,
            patch("tools.daily_sign_sync_tool.time.sleep") as sleep,
            patch(
                "tools.daily_sign_sync_tool.upsert_problem_events",
                return_value={"ok": True, "upserted": 1},
            ),
        ):
            events, result = daily_sign_sync_tool._sync_manual_problem_events(
                {
                    "account_id": "ronghui-test",
                    "problem_start_date": "2026-08-12",
                    "problem_end_date": "2026-08-12",
                    "problem_page_retries": 2,
                }
            )

        self.assertTrue(result["complete"])
        self.assertEqual("R1", events[0]["tracking_number"])
        self.assertEqual(2, query.call_count)
        sleep.assert_called_once_with(1.0)

    def test_problem_query_reports_persistent_source_error_without_raw_rows(self):
        failure = {
            "ok": False,
            "data": {
                "ok": False,
                "error_code": "SOURCE_QUERY_FAILED",
                "message": "source unavailable",
            },
        }
        with (
            patch("tools.daily_sign_sync_tool.call_http_service", return_value=failure),
            patch("tools.daily_sign_sync_tool.time.sleep"),
        ):
            events, result = daily_sign_sync_tool._sync_manual_problem_events(
                {
                    "account_id": "ronghui-test",
                    "problem_start_date": "2026-08-12",
                    "problem_end_date": "2026-08-12",
                    "problem_page_retries": 2,
                }
            )

        self.assertIsNone(events)
        self.assertFalse(result["complete"])
        self.assertEqual("SOURCE_QUERY_FAILED", result["error_code"])
        self.assertEqual("source unavailable", result["error"])
        self.assertNotIn("raw", result)

    def test_r13_signed_conflict_only_closes_on_exact_main_sign_route(self):
        state = {"signs": {}}
        responses = {
            "R1": {
                "data": {
                    "ok": True,
                    "type": "ronghui_tms",
                    "route_rows": [
                        {"scan_type": "签收", "scan_time": "2026-08-12 10:00:00", "scan_station": "邵阳大祥S站"}
                    ],
                }
            },
            "R2": {
                "data": {
                    "ok": True,
                    "type": "ronghui_tms",
                    "route_rows": [
                        {"scan_type": "到件", "scan_time": "2026-08-12 09:00:00", "scan_station": "邵阳大祥S站"}
                    ],
                }
            },
        }

        def fake_call(_endpoint, request):
            return responses[request["params"]["tracking_number"]]

        with (
            patch("tools.daily_sign_sync_tool.call_http_service", side_effect=fake_call),
            patch("tools.daily_sign_sync_tool.upsert_sign_events", return_value={"ok": True, "upserted": 1}) as store,
        ):
            events, result = daily_sign_sync_tool._sync_r13_sign_conflicts(
                {"exact_sign_workers": 2, "account_id": "ronghui-test"},
                {"R1": {"isSigns": "已签"}, "R2": {"signTime": "2026-08-12 11:00:00"}},
                state,
            )

        self.assertTrue(result["complete"])
        self.assertEqual(["R1"], [event["tracking_number"] for event in events])
        self.assertEqual("tms_tracking_exact", events[0]["source"])
        store.assert_called_once()

    def test_candidate_union_excludes_child_codes_and_zero_arrival_ghosts(self):
        ledger = {
            "R00018175062": {
                "tracking_number": "R00018175062",
                "last_seen_r13_at": "2026-07-20 10:00:00",
                "arrived_quantity": 6,
                "tms_signed": False,
            },
            "20030301080003": {
                "tracking_number": "20030301080003",
                "arrived_quantity": None,
                "tms_signed": False,
            },
            "ZERO_ONLY": {
                "tracking_number": "ZERO_ONLY",
                "arrived_quantity": None,
                "tms_signed": False,
            },
        }

        candidates, excluded = daily_sign_sync_tool._daily_sign_candidate_codes(
            {},
            ledger,
            {"20030301080003"},
        )

        self.assertEqual({"R00018175062"}, candidates)
        self.assertEqual({"20030301080003"}, excluded)
        self.assertNotIn("ZERO_ONLY", candidates)

    def test_historical_reroute_sign_is_closed_by_exact_main_route_and_cached(self):
        observed_at = datetime(2026, 8, 13, 12, 0, 0)
        state = {
            "ledger": {
                "R00018175062": {
                    "tracking_number": "R00018175062",
                    "r13_current": False,
                    "last_seen_r13_at": "2026-07-20 10:00:00",
                    "arrived_quantity": 6,
                    "tms_signed": False,
                },
                "20030301080003": {
                    "tracking_number": "20030301080003",
                    "arrived_quantity": None,
                    "tms_signed": False,
                },
            },
            "target_station_codes": {"20030301080003"},
            "signs": {},
            "sign_verifications": {},
        }
        result = {
            "tracking_number": "R00018175062",
            "sign_event": {
                "source": "tms_tracking_exact",
                "external_id": "sign-1",
                "tracking_number": "R00018175062",
                "scan_code": "R00018175062",
                "scan_type": "签收",
                "scanned_at": "2026-07-21 15:53:54",
                "scan_site": "邵阳洞口站",
                "is_main_waybill": True,
            },
        }

        with (
            patch("tools.daily_sign_sync_tool._query_exact_sign_results", return_value=[result]) as query,
            patch("tools.daily_sign_sync_tool.upsert_sign_events", return_value={"ok": True, "upserted": 1}) as sign_store,
            patch("tools.daily_sign_sync_tool.upsert_sign_verification_states", return_value={"ok": True, "upserted": 1}) as verification_store,
        ):
            events, audit = daily_sign_sync_tool._sync_historical_sign_verifications(
                {},
                {},
                state,
                observed_at=observed_at,
            )

        query.assert_called_once_with(["R00018175062"], {})
        self.assertEqual(["R00018175062"], [event["tracking_number"] for event in events])
        self.assertEqual(1, audit["confirmed"])
        self.assertEqual(["20030301080003"], audit["excluded_child_codes"])
        sign_store.assert_called_once()
        verification = verification_store.call_args.args[0][0]
        self.assertEqual("signed", verification["last_result"])
        self.assertIsNone(verification["next_check_at"])

    def test_historical_unsigned_verification_uses_backoff_and_skips_until_due(self):
        observed_at = datetime(2026, 8, 13, 12, 0, 0)
        state = {
            "ledger": {
                "OLD": {
                    "tracking_number": "OLD",
                    "r13_current": False,
                    "last_seen_r13_at": "2026-07-20 10:00:00",
                    "tms_signed": False,
                }
            },
            "target_station_codes": set(),
            "signs": {},
            "sign_verifications": {
                "OLD": {
                    "tracking_number": "OLD",
                    "last_result": "not_signed",
                    "next_check_at": "2026-08-14 12:00:00",
                    "consecutive_not_signed": 1,
                }
            },
        }

        with (
            patch("tools.daily_sign_sync_tool._query_exact_sign_results") as query,
            patch("tools.daily_sign_sync_tool.upsert_sign_events", return_value={"ok": True}),
            patch("tools.daily_sign_sync_tool.upsert_sign_verification_states", return_value={"ok": True}),
        ):
            events, audit = daily_sign_sync_tool._sync_historical_sign_verifications(
                {},
                {},
                state,
                observed_at=observed_at,
            )

        query.assert_called_once_with([], {})
        self.assertEqual([], events)
        self.assertEqual(0, audit["queried"])

    def test_historical_exact_error_defers_all_confirmed_closures(self):
        state = {
            "ledger": {
                code: {
                    "tracking_number": code,
                    "last_seen_r13_at": "2026-08-12 10:00:00",
                    "tms_signed": False,
                }
                for code in ("SIGNED", "ERROR")
            },
            "target_station_codes": set(),
            "signs": {},
            "sign_verifications": {},
        }
        results = [
            {
                "tracking_number": "SIGNED",
                "sign_event": {
                    "tracking_number": "SIGNED",
                    "scan_type": "签收",
                    "scanned_at": "2026-08-13 10:00:00",
                    "scan_site": "其他网点",
                },
            },
            {"tracking_number": "ERROR", "error": "source unavailable"},
        ]

        with (
            patch("tools.daily_sign_sync_tool._query_exact_sign_results", return_value=results),
            patch("tools.daily_sign_sync_tool.upsert_sign_events", return_value={"ok": True}) as sign_store,
            patch("tools.daily_sign_sync_tool.upsert_sign_verification_states", return_value={"ok": True}) as verification_store,
        ):
            events, audit = daily_sign_sync_tool._sync_historical_sign_verifications(
                {},
                {},
                state,
                observed_at=datetime(2026, 8, 13, 12, 0, 0),
            )

        self.assertEqual([], events)
        self.assertFalse(audit["complete"])
        self.assertEqual(1, audit["confirmed_but_deferred_on_error"])
        self.assertEqual([], sign_store.call_args.args[0])
        verification_by_code = {
            row["tracking_number"]: row for row in verification_store.call_args.args[0]
        }
        self.assertEqual("error", verification_by_code["SIGNED"]["last_result"])
        self.assertEqual("batch_deferred_after_peer_query_error", verification_by_code["SIGNED"]["last_error"])

    def test_sheet_validates_nine_headers_and_writes_before_clearing_tail(self):
        actions = []
        rows = [
            {
                "tracking_number": "R1",
                "r13_plan_sign_at": "2026-08-13 23:59:59",
            }
        ]
        expected_values = daily_sign_sync_tool._build_ledger_sheet_values(rows)

        def fake_operation(action, params):
            actions.append(action)
            if action == "read_sheet":
                values = (
                    [daily_sign_sync_tool.SHEET_HEADERS]
                    if params["range"] == "Sheet1!A1:I1"
                    else expected_values
                )
                return {
                    "ok": True,
                    "data": {"valueRange": {"values": values}},
                }
            return {"ok": True}

        with (
            patch("tools.daily_sign_sync_tool.resolve_sheet_target", return_value=("token", "Sheet1!A2:I200")),
            patch("tools.daily_sign_sync_tool.feishu_operation", side_effect=fake_operation),
        ):
            result = daily_sign_sync_tool._sync_sheet(rows, {})

        self.assertTrue(result["ok"])
        self.assertEqual(
            ["read_sheet", "write_sheet", "clear_sheet", "read_sheet"],
            actions,
        )
        self.assertTrue(result["readback"]["verified"])
        self.assertEqual(1, result["readback"]["record_count"])
        self.assertEqual(64, len(result["readback"]["snapshot_sha256"]))

    def test_sheet_header_mismatch_fails_without_write(self):
        with (
            patch("tools.daily_sign_sync_tool.resolve_sheet_target", return_value=("token", "Sheet1!A2:I200")),
            patch(
                "tools.daily_sign_sync_tool.feishu_operation",
                return_value={"ok": True, "data": {"valueRange": {"values": [["运单编号", "旧应签收时间"]]}}},
            ) as operation,
        ):
            result = daily_sign_sync_tool._sync_sheet([], {})

        self.assertIn("表头不一致", result["error"])
        self.assertEqual(1, operation.call_count)

    def test_sheet_migrates_exact_legacy_eight_column_header(self):
        actions = []
        header_reads = 0

        def fake_operation(action, params):
            nonlocal header_reads
            actions.append(action)
            if action == "read_sheet" and params["range"] == "Sheet1!A1:I1":
                header_reads += 1
                headers = (
                    daily_sign_sync_tool.SHEET_HEADERS[:8]
                    if header_reads == 1
                    else daily_sign_sync_tool.SHEET_HEADERS
                )
                return {"ok": True, "data": {"valueRange": {"values": [headers]}}}
            if action == "read_sheet":
                return {"ok": True, "data": {"valueRange": {"values": []}}}
            return {"ok": True}

        with (
            patch(
                "tools.daily_sign_sync_tool.resolve_sheet_target",
                return_value=("token", "Sheet1!A2:I200"),
            ),
            patch(
                "tools.daily_sign_sync_tool.feishu_operation",
                side_effect=fake_operation,
            ),
        ):
            result = daily_sign_sync_tool._sync_sheet([], {})

        self.assertTrue(result["ok"])
        self.assertEqual(
            ["read_sheet", "write_sheet", "read_sheet", "clear_sheet", "read_sheet"],
            actions,
        )

    def test_sheet_migrates_confirmed_verbose_legacy_header(self):
        header_reads = 0

        def fake_operation(action, params):
            nonlocal header_reads
            if action == "read_sheet" and params["range"] == "Sheet1!A1:I1":
                header_reads += 1
                headers = (
                    daily_sign_sync_tool.LEGACY_VERBOSE_SHEET_HEADERS
                    if header_reads == 1
                    else daily_sign_sync_tool.SHEET_HEADERS
                )
                return {"ok": True, "data": {"valueRange": {"values": [headers]}}}
            if action == "read_sheet":
                return {"ok": True, "data": {"valueRange": {"values": []}}}
            if action == "write_sheet" and params["range"] == "Sheet1!A1:I1":
                self.assertEqual([daily_sign_sync_tool.SHEET_HEADERS], params["values"])
            return {"ok": True}

        with (
            patch(
                "tools.daily_sign_sync_tool.resolve_sheet_target",
                return_value=("token", "Sheet1!A2:I200"),
            ),
            patch(
                "tools.daily_sign_sync_tool.feishu_operation",
                side_effect=fake_operation,
            ),
        ):
            result = daily_sign_sync_tool._sync_sheet([], {})

        self.assertTrue(result["ok"])
        self.assertEqual(2, header_reads)

    def test_bitable_uses_delta_write_then_delete(self):
        actions = []
        rows = [{"tracking_number": "A"}, {"tracking_number": "B"}]
        target_records = daily_sign_sync_tool._build_ledger_records(rows)
        list_calls = 0

        def fake_operation(action, params):
            nonlocal list_calls
            actions.append(action)
            if action == "list_records":
                list_calls += 1
                if list_calls == 2:
                    return {
                        "ok": True,
                        "items": [
                            {"record_id": f"rec-{index}", **record}
                            for index, record in enumerate(target_records, 1)
                        ],
                    }
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-a", "fields": {"运单编号": "A"}},
                        {"record_id": "rec-old", "fields": {"运单编号": "OLD"}},
                    ],
                }
            return {"ok": True, "written": len(params.get("records", [])), "deleted": len(params.get("record_ids", []))}

        with (
            patch("tools.daily_sign_sync_tool.resolve_bitable_target", return_value=("base", "table")),
            patch("tools.daily_sign_sync_tool._ensure_bitable_schema", return_value={"ok": True, "fields": {"R13应签收时间": 1}}),
            patch("tools.daily_sign_sync_tool.feishu_operation", side_effect=fake_operation),
        ):
            result = daily_sign_sync_tool._sync_bitable(rows, {})

        self.assertTrue(result["ok"])
        self.assertEqual(
            ["list_records", "write_records", "delete_records", "list_records"],
            actions,
        )
        self.assertTrue(result["readback"]["verified"])
        self.assertEqual(2, result["readback"]["record_count"])
        self.assertEqual(64, len(result["readback"]["snapshot_sha256"]))


class DailySignSourceCompletenessTest(unittest.TestCase):
    def test_get_scan_uses_resolved_session_profile_and_login_site_cookie(self):
        session = object()
        profiles = []

        class Auth:
            def __init__(self, *, profile):
                profiles.append(profile)

            def login_and_get_session(self):
                return session

        with (
            patch("agent.tms_runtime.scripts.get_scan.TMSAuth", Auth),
            patch("agent.tms_runtime.scripts.get_scan._read_user_info_cookie", return_value={"SITE_CODE": "7390004"}),
            patch("agent.tms_runtime.scripts.get_scan._resolve_login_site_code_from_user_info", return_value="7390004"),
            patch("agent.tms_runtime.scripts.get_scan.collect_scan_rows", return_value=[]) as collect,
        ):
            result = get_scan.run_once(
                {
                    "date": "2026/08/12",
                    "output_format": "json",
                    "session_profile": "account-ronghui-daxiang-s",
                    "use_login_site_code": True,
                }
            )

        self.assertEqual([], result)
        self.assertEqual(["account-ronghui-daxiang-s"], profiles)
        self.assertEqual("7390004", collect.call_args.kwargs["site_code"])

    def test_r13_keeps_signed_fields_instead_of_filtering_them(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "records": [
                            {
                                "billNumberMain": "R1",
                                "planSignTime": "2026-08-13 23:59:59",
                                "isSigns": "已签",
                                "signSiteName": "邵阳大祥S站",
                                "signTime": "2026-08-12 10:00:00",
                                "dispTime": "2026-08-12 09:00:00",
                            }
                        ],
                        "total": 1,
                    }
                }

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

        class Auth:
            last_token = "token"

            def __init__(self, **_kwargs):
                pass

            def login_and_get_session(self, **_kwargs):
                return Session()

        with patch("agent.tms_runtime.scripts.get_qianshou.R13SSOAuth", Auth):
            rows = get_qianshou.fetch_qianshou(
                config_path=None,
                username=None,
                password=None,
                disp_site_code="7390004",
                start="2026-08-12 00:00:00",
                end="2026-08-12 23:59:59",
                days=1,
                page_size=100,
                page=1,
                account_id="r13_default",
            )

        self.assertEqual(1, len(rows))
        self.assertEqual("已签", rows[0]["isSigns"])
        self.assertEqual("2026-08-12 10:00:00", rows[0]["signTime"])
        self.assertEqual("2026-08-12 09:00:00", rows[0]["dispTime"])

    def test_r13_page_limit_is_incomplete_failure(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "records": [
                            {
                                "billNumberMain": "R1",
                                "planSignTime": "2026-08-12 23:59:59",
                            }
                        ],
                        "total": 2,
                    }
                }

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

        class Auth:
            last_token = "token"

            def __init__(self, **_kwargs):
                pass

            def login_and_get_session(self, **_kwargs):
                return Session()

        with patch("agent.tms_runtime.scripts.get_qianshou.R13SSOAuth", Auth):
            with self.assertRaisesRegex(RuntimeError, "max_pages"):
                get_qianshou.fetch_qianshou(
                    config_path=None,
                    username=None,
                    password=None,
                    disp_site_code="7390004",
                    start="2026-08-12 00:00:00",
                    end="2026-08-12 23:59:59",
                    days=1,
                    page_size=1,
                    page=1,
                    max_pages=1,
                    account_id="r13_default",
                )

    def test_tms_scan_keeps_type_time_and_site_and_fails_at_page_limit(self):
        raw_page = {
            "data": [
                {
                    "BILL_CODE": "R1",
                    "DESTINATION": "邵阳大祥S站",
                    "SCAN_TYPE": "签收",
                    "SCAN_DATE": "2026-08-12 10:00:00",
                    "SCAN_SITE": "邵阳大祥S站",
                }
            ]
        }
        with patch("agent.tms_runtime.scripts.get_scan.fetch_page", side_effect=[raw_page, {"data": []}]):
            rows = get_scan.collect_scan_rows(
                object(),
                {"start": "2026/08/12 00:00:00", "end": "2026/08/12 23:59:59"},
                "7390004",
                "签收",
                100,
                20,
                max_pages=2,
            )
        self.assertEqual("签收", rows[0]["scan_type"])
        self.assertEqual("2026-08-12 10:00:00", rows[0]["scan_time"])
        self.assertEqual("邵阳大祥S站", rows[0]["scan_site"])

        with patch("agent.tms_runtime.scripts.get_scan.fetch_page", return_value=raw_page):
            with self.assertRaisesRegex(RuntimeError, "max_pages"):
                get_scan.collect_scan_rows(
                    object(),
                    {"start": "2026/08/12 00:00:00", "end": "2026/08/12 23:59:59"},
                    "7390004",
                    "签收",
                    100,
                    20,
                    max_pages=1,
                )

    def test_tms_scan_rejects_missing_bill_and_conflicting_duplicates(self):
        with patch(
            "agent.tms_runtime.scripts.get_scan.fetch_page",
            return_value={"data": [{"SCAN_TYPE": "签收"}]},
        ):
            with self.assertRaisesRegex(RuntimeError, "without BILL_CODE"):
                get_scan.collect_scan_rows(
                    object(),
                    {"start": "2026/08/12 00:00:00", "end": "2026/08/12 23:59:59"},
                    "7390004",
                    "签收",
                    100,
                    20,
                )

    def test_tms_sign_query_uses_real_page_contract_and_paginates_completely(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "FIND_SIGNED_TOTAL" in url:
                    return Response(
                        {
                            "data": [
                                {
                                    "AREA_NAME": "虚拟湖南省区",
                                    "SIGN_SITE_CODE": "7390004",
                                    "TOTAL_NUM": 2,
                                }
                            ],
                            "total": 1,
                        }
                    )
                page = int(kwargs["data"]["pageIndex"])
                if page == 0:
                    rows = [
                        {
                            "BILL_CODE": "R1",
                            "SIGN_DATE": "2026-08-12 10:00:00",
                            "SIGN_SITE": "邵阳大祥S站",
                            "RECORD_DATE": "2026-08-12 10:00:01",
                            "RECORD_SITE": "邵阳大祥S站",
                        }
                    ]
                else:
                    rows = [
                        {
                            "BILL_CODE": "R2",
                            "SIGN_DATE": "2026-08-12 11:00:00",
                            "SIGN_SITE": "邵阳大祥S站",
                        }
                    ]
                return Response({"data": rows, "total": 2})

        session = Session()
        with (
            patch("agent.tms_runtime.scripts.get_sign_records.page_support._ronghui_headers", return_value={}),
            patch("agent.tms_runtime.scripts.get_sign_records.page_support._raise_if_source_failed"),
        ):
            rows = get_sign_records.collect_sign_rows(
                session,
                {},
                start=datetime(2026, 8, 12, 0, 0, 0),
                end=datetime(2026, 8, 12, 23, 59, 59),
                login_site_code="7390004",
                page_size=1,
                max_pages=2,
            )

        self.assertEqual(["R1", "R2"], [row["扫描单号"] for row in rows])
        self.assertTrue(all(row["扫描类型"] == "签收" for row in rows))
        self.assertIn("FIND_SIGNED_TOTAL", session.calls[0][0])
        self.assertIn("FIND_SIGNED_DETAIL_ALL_EXCEL", session.calls[1][0])
        first_payload = session.calls[0][1]["data"]
        self.assertEqual("SIGN_DATE", first_payload["searchDateType"])
        self.assertEqual("7390004", first_payload["LOGIN_SITE_CODE"])
        self.assertEqual(0, first_payload["pageIndex"])
        detail_payload = session.calls[1][1]["data"]
        self.assertEqual("7390004", detail_payload["SIGN_SITE_CODE"])
        self.assertEqual("虚拟湖南省区", detail_payload["AREA_NAME"])
        self.assertEqual(1, session.calls[2][1]["data"]["pageIndex"])

    def test_tms_sign_query_rejects_missing_real_fields_and_incomplete_paging(self):
        with self.assertRaisesRegex(RuntimeError, "BILL_CODE"):
            get_sign_records.normalize_sign_row({"SIGN_DATE": "2026-08-12 10:00:00"})

        with self.assertRaisesRegex(ValueError, "1 到 200"):
            get_sign_records.collect_sign_rows(
                object(),
                {},
                start=datetime(2026, 8, 12, 0, 0, 0),
                end=datetime(2026, 8, 12, 23, 59, 59),
                login_site_code="7390004",
                page_size=201,
            )

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Session:
            def post(self, url, **_kwargs):
                if "FIND_SIGNED_TOTAL" in url:
                    return Response(
                        {
                            "data": [
                                {
                                    "AREA_NAME": "虚拟湖南省区",
                                    "SIGN_SITE_CODE": "7390004",
                                    "TOTAL_NUM": 2,
                                }
                            ],
                            "total": 1,
                        }
                    )
                return Response(
                    {
                        "data": [
                            {
                                "BILL_CODE": "R1",
                                "SIGN_DATE": "2026-08-12 10:00:00",
                                "SIGN_SITE": "邵阳大祥S站",
                            }
                        ],
                        "total": 2,
                    }
                )

        with (
            patch("agent.tms_runtime.scripts.get_sign_records.page_support._ronghui_headers", return_value={}),
            patch("agent.tms_runtime.scripts.get_sign_records.page_support._raise_if_source_failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "提前结束"):
                get_sign_records.collect_sign_rows(
                    Session(),
                    {},
                    start=datetime(2026, 8, 12, 0, 0, 0),
                    end=datetime(2026, 8, 12, 23, 59, 59),
                    login_site_code="7390004",
                    page_size=2,
                    max_pages=2,
                )

        conflicting = {
            "data": [
                {
                    "BILL_CODE": "R1",
                    "SCAN_TYPE": "签收",
                    "SCAN_DATE": "2026-08-12 10:00:00",
                    "SCAN_SITE": "站点一",
                },
                {
                    "BILL_CODE": "R1",
                    "SCAN_TYPE": "签收",
                    "SCAN_DATE": "2026-08-12 10:00:00",
                    "SCAN_SITE": "站点二",
                },
            ]
        }
        with patch("agent.tms_runtime.scripts.get_scan.fetch_page", return_value=conflicting):
            with self.assertRaisesRegex(RuntimeError, "conflicting data"):
                get_scan.collect_scan_rows(
                    object(),
                    {"start": "2026/08/12 00:00:00", "end": "2026/08/12 23:59:59"},
                    "7390004",
                    "签收",
                    100,
                    20,
                )

    def test_tms_sign_query_chunks_long_ranges_without_overlap(self):
        calls = []

        def fake_collect(_session, _context, **kwargs):
            calls.append((kwargs["start"], kwargs["end"]))
            return [
                {
                    "扫描单号": f"R{len(calls)}",
                    "扫描类型": "签收",
                    "扫描时间": kwargs["start"].strftime("%Y-%m-%d %H:%M:%S"),
                    "扫描网点": "邵阳大祥S站",
                }
            ]

        with patch(
            "agent.tms_runtime.scripts.get_sign_records.collect_sign_rows",
            side_effect=fake_collect,
        ):
            rows = get_sign_records.collect_sign_rows_in_chunks(
                object(),
                {},
                start=datetime(2026, 1, 1, 0, 0, 0),
                end=datetime(2026, 3, 5, 12, 0, 0),
                login_site_code="7390004",
                chunk_days=31,
            )

        self.assertEqual(3, len(rows))
        self.assertEqual(datetime(2026, 1, 31, 23, 59, 59), calls[0][1])
        self.assertEqual(calls[0][1] + timedelta(seconds=1), calls[1][0])
        self.assertEqual(datetime(2026, 3, 5, 12, 0, 0), calls[-1][1])

    def test_r13_backfill_merge_preserves_sheet_fields_and_refreshes_r13_fields(self):
        seed = daily_sign_backfill_tool._seed_row_from_sheet(
            [
                "R1",
                "2026-08-12 23:59:59",
                "2026-08-13 23:59:59",
                "旧品名",
                "纸箱",
                2,
                "旧地址",
                "送货",
                2,
            ]
        )
        merged = daily_sign_backfill_tool._merge_r13_seed(
            seed,
            {
                "billNumberMain": "R1",
                "planSignTime": "2026-08-14 23:59:59",
                "isSigns": "已签",
            },
            observed_at=datetime(2026, 8, 12, 12, 0, 0),
        )

        self.assertEqual("2026-08-14 23:59:59", merged["r13_plan_sign_at"])
        self.assertEqual("2026-08-13 23:59:59", merged["system_sign_due_at"])
        self.assertEqual("旧地址", merged["recipient_address"])
        self.assertFalse(merged["tms_signed"])


class DailySignBackfillTest(unittest.TestCase):
    def test_shadow_backfill_rebuilds_union_and_only_removes_tms_signed_rows(self):
        archive_values = [
            [f"列{index}" for index in range(19)],
            ["R1", "货物", "纸箱", "送货", 1, "", "", "", "", "邵阳大祥S站", "", "", "地址", "", "", "", "", "", 1],
        ]
        current_values = [
            daily_sign_sync_tool.SHEET_HEADERS,
            ["OLD", "2026-08-11 23:59:59", "", "旧货", "纸箱", 1, "旧地址", "送货", 1],
        ]

        def fake_feishu(_action, params):
            values = current_values if params["spreadsheet_token"] == "current-token" else archive_values
            return {"ok": True, "data": {"valueRange": {"values": values}}}

        with (
            patch("tools.daily_sign_backfill_tool.get_required_resource", return_value={"spreadsheet_token": "archive-token"}),
            patch("tools.daily_sign_backfill_tool._spreadsheet_sheet_ref_map", return_value={"2026-08-12": "archive-id"}),
            patch("tools.daily_sign_backfill_tool._spreadsheet_sheet_info", return_value={"row_count": 2}),
            patch("tools.daily_sign_backfill_tool.get_workflow_resource", return_value={"spreadsheet_token": "current-token", "read_range": "sheet!A1:I200"}),
            patch("tools.daily_sign_backfill_tool.feishu_operation", side_effect=fake_feishu),
            patch("tools.daily_sign_backfill_tool.save_arrival_stat_snapshot", return_value={"ok": True, "skipped": True}),
            patch(
                "tools.daily_sign_backfill_tool._read_r13_history",
                return_value=(
                    [
                        {"billNumberMain": "R1", "planSignTime": "2026-08-13 23:59:59"},
                        {"billNumberMain": "R2", "planSignTime": "2026-08-13 23:59:59"},
                    ],
                    {"ok": True, "rows": 2},
                ),
            ),
            patch("tools.daily_sign_backfill_tool._sync_manual_problem_events", return_value=([], {"ok": True, "complete": True})),
            patch(
                "tools.daily_sign_backfill_tool._sync_sign_events",
                return_value=(
                    [
                        {
                            "tracking_number": "OLD",
                            "scanned_at": "2026-08-12 10:00:00",
                            "scan_type": "签收",
                        }
                    ],
                    {"ok": True, "complete": True},
                ),
            ),
            patch("tools.daily_sign_backfill_tool._sync_r13_sign_conflicts", return_value=([], {"ok": True, "complete": True})),
            patch("tools.daily_sign_backfill_tool.upsert_ledger_rows") as upsert,
        ):
            result = daily_sign_backfill_tool.run_daily_sign_backfill({"apply": False})

        self.assertTrue(result["ok"])
        self.assertEqual("shadow", result["mode"])
        self.assertEqual(2, result["rebuilt_open_rows"])
        self.assertEqual(1, result["removed_open_rows_with_tms_sign"])
        self.assertFalse(result["published"])
        upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
