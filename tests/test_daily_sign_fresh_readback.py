from __future__ import annotations

import copy
import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from tools.daily_sign_readback import (
    DailySignReadbackError,
    verify_bitable_snapshot,
    verify_sheet_snapshot,
)
from tools.daily_sign_store import (
    DailySignPersistenceReadbackError,
    build_daily_sign_persistence_marker,
    verify_daily_sign_persistence,
)


def _ledger_row(*, tracking_number: str = "R1") -> dict[str, object]:
    return {
        "tracking_number": tracking_number,
        "r13_plan_sign_at": datetime(2026, 8, 15, 23, 59, 59),
        "r13_sign_status": "待签收",
        "r13_sign_at": None,
        "first_seen_r13_at": datetime(2026, 8, 15, 5, 0, 0),
        "last_seen_r13_at": datetime(2026, 8, 15, 5, 0, 0),
        "r13_current": True,
        "first_arrival_date": date(2026, 8, 14),
        "completion_date": None,
        "expected_quantity": 2,
        "arrived_quantity": 1,
        "arrival_status": "partial",
        "system_sign_due_at": datetime(2026, 8, 16, 23, 59, 59),
        "tms_signed": False,
        "tms_signed_at": None,
        "goods_name": "测试货物",
        "package_type": "纸箱",
        "delivery_method": "派送",
        "recipient_address": "测试地址",
        "data_quality_flags": [],
        "calculation_trace": {"rule": "existing_daily_sign_rule"},
    }


def _row_sets() -> dict[str, list[dict[str, object]]]:
    ledger = _ledger_row()
    return {
        "problem_events": [
            {
                "source": "test_problem",
                "external_id": "problem-1",
                "tracking_number": "R1",
                "problem_type": "客户要求延迟派送",
                "registered_at": datetime(2026, 8, 15, 16, 0, 0),
                "registered_site": "测试网点",
                "upload_complete": True,
                "payload": {"source": "test_problem"},
            }
        ],
        "sign_events": [
            {
                "source": "test_sign",
                "external_id": "sign-1",
                "tracking_number": "R1",
                "scan_code": "R1",
                "scan_type": "签收",
                "scanned_at": datetime(2026, 8, 15, 18, 0, 0),
                "scan_site": "测试网点",
                "is_main_waybill": True,
                "payload": {"source": "test_sign"},
            }
        ],
        "ledger_rows": [ledger],
        "sign_verification_states": [
            {
                "tracking_number": "R1",
                "last_checked_at": datetime(2026, 8, 15, 18, 5, 0),
                "last_result": "not_signed",
                "next_check_at": datetime(2026, 8, 16, 18, 5, 0),
                "consecutive_not_signed": 1,
                "last_error": None,
            }
        ],
        "publication_rows": [ledger],
    }


def _database_rows(row_sets, marker):
    problem = row_sets["problem_events"][0]
    sign = row_sets["sign_events"][0]
    verification = row_sets["sign_verification_states"][0]
    ledger = row_sets["ledger_rows"][0]
    return {
        "run": [
            {
                "status": "running",
                "diagnostics_json": json.dumps(
                    {"persistence_commit": marker},
                    ensure_ascii=False,
                    default=str,
                ),
            }
        ],
        "problems": [
            {
                **problem,
                "before_cutoff": True,
                "postpones_sign": True,
                "upload_complete": 1,
                "payload_json": json.dumps(problem["payload"], ensure_ascii=False),
            }
        ],
        "signs": [
            {
                **sign,
                "is_main_waybill": 1,
                "payload_json": json.dumps(sign["payload"], ensure_ascii=False),
            }
        ],
        "verifications": [{**verification}],
        "ledger": [
            {
                **ledger,
                "r13_current": 1,
                "tms_signed": 0,
                "data_quality_flags": json.dumps(ledger["data_quality_flags"]),
                "calculation_trace": json.dumps(
                    ledger["calculation_trace"],
                    ensure_ascii=False,
                ),
            }
        ],
    }


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self._current = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=None):
        compact = " ".join(str(sql).split())
        if "FROM daily_sign_sync_runs" in compact:
            self._current = self._rows["run"]
        elif "FROM waybill_problem_events" in compact:
            self._current = self._rows["problems"]
        elif "FROM waybill_sign_events" in compact:
            self._current = self._rows["signs"]
        elif "FROM waybill_sign_verification_state" in compact:
            self._current = self._rows["verifications"]
        elif "FROM daily_sign_ledger" in compact:
            self._current = self._rows["ledger"]
        else:  # pragma: no cover - a new unreviewed read is a test failure
            raise AssertionError(compact)

    def fetchall(self):
        return copy.deepcopy(self._current)


class _Connection:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cursor(self._rows)

    def rollback(self):
        return None

    def close(self):
        return None


def _verify(row_sets, database_rows, marker):
    with (
        patch("tools.daily_sign_store.ensure_daily_sign_tables"),
        patch(
            "tools.daily_sign_store._daily_sign_connect",
            return_value=_Connection(database_rows),
        ),
    ):
        return verify_daily_sign_persistence(
            run_id="run-1",
            persistence_marker=marker,
            **row_sets,
        )


def test_persistence_fresh_readback_binds_every_exact_row_set() -> None:
    row_sets = _row_sets()
    marker = build_daily_sign_persistence_marker(**row_sets)
    result = _verify(row_sets, _database_rows(row_sets, marker), marker)

    assert result["verified"] is True
    assert result["problem_events"]["record_count"] == 1
    assert result["sign_events"]["record_count"] == 1
    assert result["sign_verification_states"]["record_count"] == 1
    assert result["ledger_rows"]["record_count"] == 1
    assert result["publication_rows"]["record_count"] == 1
    assert result["persistence_sha256"] == marker["marker_sha256"]


def test_persistence_fresh_readback_rejects_event_field_tamper() -> None:
    row_sets = _row_sets()
    marker = build_daily_sign_persistence_marker(**row_sets)
    database_rows = _database_rows(row_sets, marker)
    database_rows["problems"][0]["problem_type"] = "被篡改类型"

    with pytest.raises(DailySignPersistenceReadbackError, match="problem events"):
        _verify(row_sets, database_rows, marker)


def test_persistence_fresh_readback_rejects_publication_set_tamper() -> None:
    row_sets = _row_sets()
    marker = build_daily_sign_persistence_marker(**row_sets)
    row_sets["publication_rows"] = []

    with pytest.raises(DailySignPersistenceReadbackError, match="marker"):
        _verify(row_sets, _database_rows(row_sets, marker), marker)


def test_persistence_fresh_readback_accepts_empty_publication_when_all_signed() -> None:
    row_sets = _row_sets()
    ledger = row_sets["ledger_rows"][0]
    ledger["tms_signed"] = True
    ledger["tms_signed_at"] = datetime(2026, 8, 15, 18, 0, 0)
    row_sets["publication_rows"] = []
    marker = build_daily_sign_persistence_marker(**row_sets)
    database_rows = _database_rows(row_sets, marker)
    database_rows["ledger"][0]["tms_signed"] = 1

    result = _verify(row_sets, database_rows, marker)

    assert result["verified"] is True
    assert result["publication_rows"]["record_count"] == 0


def test_sheet_readback_rejects_target_or_cleared_tail_drift() -> None:
    expected = [["R1", "2026-08-15 23:59:59"]]
    result = verify_sheet_snapshot(
        expected,
        [expected[0], []],
        observed_row_capacity=2,
        columns=2,
    )
    assert result["verified"] is True

    with pytest.raises(DailySignReadbackError):
        verify_sheet_snapshot(
            expected,
            [expected[0], ["unexpected"]],
            observed_row_capacity=2,
            columns=2,
        )


def test_bitable_readback_rejects_extra_or_paginated_records() -> None:
    expected = [{"fields": {"运单编号": "R1", "到货件数": 1}}]
    with pytest.raises(DailySignReadbackError):
        verify_bitable_snapshot(
            expected,
            {
                "items": [
                    {"fields": {"运单编号": "R1", "到货件数": 1}},
                    {"fields": {"运单编号": "R2", "到货件数": 1}},
                ]
            },
            identity_field="运单编号",
        )
    with pytest.raises(DailySignReadbackError, match="incomplete"):
        verify_bitable_snapshot(
            expected,
            {
                "items": [{"fields": {"运单编号": "R1", "到货件数": 1}}],
                "has_more": True,
            },
            identity_field="运单编号",
        )
