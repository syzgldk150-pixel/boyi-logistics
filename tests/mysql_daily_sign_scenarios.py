from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from tools import daily_sign_store


def _ledger_row(tracking_number: str) -> dict[str, object]:
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
        "goods_name": "集成测试货物",
        "package_type": "纸箱",
        "delivery_method": "派送",
        "recipient_address": "集成测试地址",
        "data_quality_flags": [],
        "calculation_trace": {"rule": "existing_daily_sign_rule"},
    }


def run_test_daily_sign_fresh_readback_rejects_mysql_tamper(case) -> None:
    database = case.daily_sign_readback_database
    suffix = uuid4().hex[:12]
    tracking = f"DS{suffix}"
    problem_external_id = f"problem-{suffix}"
    sign_external_id = f"sign-{suffix}"
    problem_events = [
        {
            "source": "integration",
            "external_id": problem_external_id,
            "tracking_number": tracking,
            "problem_type": "客户要求延迟派送",
            "registered_at": datetime(2026, 8, 15, 16, 0, 0),
            "registered_site": "集成测试网点",
            "upload_complete": True,
            "payload": {"source": "integration"},
        }
    ]
    sign_events = [
        {
            "source": "integration",
            "external_id": sign_external_id,
            "tracking_number": tracking,
            "scan_code": tracking,
            "scan_type": "签收",
            "scanned_at": datetime(2026, 8, 15, 18, 0, 0),
            "scan_site": "集成测试网点",
            "is_main_waybill": True,
            "payload": {"source": "integration"},
        }
    ]
    ledger_rows = [_ledger_row(tracking)]
    verification_rows = [
        {
            "tracking_number": tracking,
            "last_checked_at": datetime(2026, 8, 15, 18, 5, 0),
            "last_result": "not_signed",
            "next_check_at": datetime(2026, 8, 16, 18, 5, 0),
            "consecutive_not_signed": 1,
            "last_error": None,
        }
    ]
    marker = daily_sign_store.build_daily_sign_persistence_marker(
        problem_events=problem_events,
        sign_events=sign_events,
        ledger_rows=ledger_rows,
        sign_verification_states=verification_rows,
        publication_rows=ledger_rows,
    )

    def connect():
        return case.pymysql.connect(
            host=case.host,
            port=case.port,
            user=case.user,
            password=case.password,
            database=database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=case.pymysql.cursors.DictCursor,
        )

    with patch("tools.daily_sign_store._connect", side_effect=connect):
        run_id, _started_at = daily_sign_store.start_sync_run()
        try:
            daily_sign_store.persist_daily_sign_snapshot(
                problem_events=problem_events,
                sign_events=sign_events,
                ledger_rows=ledger_rows,
                sign_verification_states=verification_rows,
                publication_rows=ledger_rows,
                run_id=run_id,
                persistence_marker=marker,
            )
            kwargs = {
                "run_id": run_id,
                "problem_events": problem_events,
                "sign_events": sign_events,
                "ledger_rows": ledger_rows,
                "sign_verification_states": verification_rows,
                "publication_rows": ledger_rows,
                "persistence_marker": marker,
            }
            case.assertTrue(
                daily_sign_store.verify_daily_sign_persistence(**kwargs)["verified"]
            )
            with case._connection(database, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE waybill_problem_events SET problem_type=%s "
                        "WHERE source=%s AND external_id=%s",
                        ("篡改类型", "integration", problem_external_id),
                    )
            with case.assertRaises(daily_sign_store.DailySignPersistenceReadbackError):
                daily_sign_store.verify_daily_sign_persistence(**kwargs)
            with case._connection(database, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE waybill_problem_events SET problem_type=%s "
                        "WHERE source=%s AND external_id=%s",
                        ("客户要求延迟派送", "integration", problem_external_id),
                    )
                    cursor.execute(
                        "UPDATE daily_sign_ledger SET tms_signed=TRUE "
                        "WHERE tracking_number=%s",
                        (tracking,),
                    )
            with case.assertRaisesRegex(
                daily_sign_store.DailySignPersistenceReadbackError,
                "publication rows",
            ):
                daily_sign_store.verify_daily_sign_persistence(**kwargs)
        finally:
            with case._connection(database, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM waybill_sign_verification_state "
                        "WHERE tracking_number=%s",
                        (tracking,),
                    )
                    cursor.execute(
                        "DELETE FROM waybill_sign_events "
                        "WHERE source=%s AND external_id=%s",
                        ("integration", sign_external_id),
                    )
                    cursor.execute(
                        "DELETE FROM waybill_problem_events "
                        "WHERE source=%s AND external_id=%s",
                        ("integration", problem_external_id),
                    )
                    cursor.execute(
                        "DELETE FROM daily_sign_ledger WHERE tracking_number=%s",
                        (tracking,),
                    )
                    cursor.execute(
                        "DELETE FROM daily_sign_sync_runs WHERE run_id=%s",
                        (run_id,),
                    )
