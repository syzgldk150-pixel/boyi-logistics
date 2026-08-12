from __future__ import annotations

import datetime as dt
import types
import unittest

from shared.finance.accounts import resolve_account_binding
from shared.finance.models import SummarySnapshot, TransactionRecord
from shared.finance.money import quantize_storage
from shared.finance.validation import CaptureEvidence, validate_finance_capture

from agent.tms_runtime.scripts.finance_capture_common import CaptureResult, FinanceCaptureError
from tools.finance_sync_service import (
    EARLIEST_DATE_UNCONFIRMED,
    FinanceSyncError,
    FinanceSyncService,
    month_chunks,
    plan_sync_request,
    resolve_finance_accounts,
)


SHARED_API = types.SimpleNamespace(
    TransactionRecord=TransactionRecord,
    SummarySnapshot=SummarySnapshot,
    CaptureEvidence=CaptureEvidence,
    quantize_storage=quantize_storage,
    validate_finance_capture=validate_finance_capture,
    resolve_account_binding=resolve_account_binding,
)


ACCOUNT_SPECS = (
    ("ronghui", "price_default"),
    ("ronghui", "ronghui_daxiang_s"),
    ("ronghui", "ronghui_self_pickup_problem"),
    ("yunda", "yunda_default"),
)


class _AccountManager:
    def __init__(self, *, include_extra: bool = False) -> None:
        self.rows = [
            {
                "account_id": account_id,
                "system": system,
                "name": f"Fixture {account_id}",
                "is_active": True,
                "session_profile": f"fixture_{account_id}",
            }
            for system, account_id in ACCOUNT_SPECS
        ]
        if include_extra:
            self.rows.append(
                {
                    "account_id": "ronghui_unapproved",
                    "system": "ronghui",
                    "name": "Fixture extra",
                    "is_active": True,
                    "session_profile": "fixture_extra",
                }
            )

    def list_accounts(self, *, include_status: bool = False):
        return [dict(row) for row in self.rows]

    def public_credentials(self, account_id: str):
        return {"username": f"fixture-login-{account_id}"}


class _Repository:
    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.commits: list[dict] = []
        self.no_data: list[dict] = []
        self.failures: list[dict] = []
        self.binding_failures: list[dict] = []
        self.missing_dates: list[dt.date] = []
        self.retry_targets: list[dict] = []
        self.initialized = False
        self.seeded = False
        self.batch_kwargs = None

    def initialize_schema(self):
        self.initialized = True

    def seed_fee_mappings(self):
        self.seeded = True
        return 0

    def create_batch(self, **kwargs):
        self.batch_kwargs = kwargs
        return 41

    def start_run(self, **kwargs):
        self.runs.append(kwargs)
        return len(self.runs)

    def start_failed_run(self, **kwargs):
        self.binding_failures.append(kwargs)
        return 1000 + len(self.binding_failures)

    def commit_run_snapshot(self, **kwargs):
        self.commits.append(kwargs)
        return {"no_data": False}

    def mark_run_no_data(self, **kwargs):
        self.no_data.append(kwargs)

    def fail_run(self, **kwargs):
        self.failures.append(kwargs)

    def finalize_batch(self, _batch_id):
        failures = self.failures or self.binding_failures
        if failures and (self.commits or self.no_data):
            return "partial_failed"
        if failures:
            return "failed"
        return "success"

    def list_missing_dates(self, **_kwargs):
        return list(self.missing_dates)

    def list_retry_targets(self, _batch_id):
        return list(self.retry_targets)

    def get_validation_context(self, **_kwargs):
        return {"known_fee_items": set(), "previous_record_payloads": {}}


def _capture(target_date: dt.date, *, platform: str = "ronghui") -> CaptureResult:
    day = target_date.isoformat()
    primary = "结算" if platform == "yunda" else "收操作费"
    secondary = "派送费" if platform == "yunda" else ""
    transaction = {
        "platform": platform,
        "source_id": f"fixture-{platform}-{day}",
        "target_date": day,
        "trade_time": f"{day}T12:00:00+08:00",
        "fee_name": secondary or primary,
        "fee_level_1": primary if platform == "yunda" else "",
        "fee_level_2": secondary,
        "income": "0.0000",
        "expend": "1.2500",
        "old_amount": "80.0000",
        "new_amount": "78.7500",
        "bill_code": "fixture-waybill",
        "logistics_id": "fixture-logistics",
        "source_reference": "000001",
        "source_payload": {},
    }
    summary = {
        "platform": platform,
        "snapshot_date": day,
        "fee_name": secondary or primary,
        "fee_level_1": primary if platform == "yunda" else "",
        "fee_level_2": secondary,
        "income": "0.0000",
        "expend": "1.2500",
    }
    return CaptureResult(
        transactions=[transaction],
        summaries=[summary],
        source_site_code="fixture-site",
        source_site_name="Fixture Site",
        validation={
            "source_total": 1,
            "page_row_counts": [1],
            "page_row_count": 1,
            "unique_count": 1,
        },
    )


def _empty_capture(*, with_site: bool = True) -> CaptureResult:
    return CaptureResult(
        transactions=[],
        summaries=[],
        source_site_code="fixture-site" if with_site else "",
        source_site_name="Fixture Site" if with_site else "",
        validation={
            "source_total": 0,
            "page_row_counts": [0],
            "page_row_count": 0,
            "unique_count": 0,
        },
    )


class _Adapter:
    def __init__(self, binding, *, empty: bool = False, discover_error: bool = False, empty_site: bool = False):
        self.binding = binding
        self.empty = empty
        self.discover_error = discover_error
        self.empty_site = empty_site

    def discover(self):
        if self.discover_error:
            raise FinanceCaptureError(
                "AUTH_REQUIRED",
                "fixture authentication unavailable",
                stage="page_discovery",
            )
        if self.empty_site:
            return {"source_site_code": "", "source_site_name": ""}
        return {"source_site_code": "fixture-site", "source_site_name": "Fixture Site"}

    def fetch_day(self, target_date):
        return (
            _empty_capture(with_site=not self.empty_site)
            if self.empty
            else _capture(target_date, platform=self.binding.system)
        )


class FinanceSyncServiceTests(unittest.TestCase):
    def _service(self, repository, factory, manager=None):
        return FinanceSyncService(
            repository=repository,
            account_manager=manager or _AccountManager(),
            adapter_factory=factory,
            shared_api=SHARED_API,
            now=lambda: dt.datetime(2026, 7, 12, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        )

    def test_success_maps_adapter_rows_to_shared_contract(self):
        repository = _Repository()
        service = self._service(repository, lambda binding: _Adapter(binding))
        result = service.run(
            {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1, "account_id": "price_default"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["successful_runs"])
        record = repository.commits[0]["transactions"][0]
        self.assertEqual("fixture-ronghui-2026-07-11", record.source_record_key)
        self.assertEqual(dt.date(2026, 7, 11), record.business_date)
        self.assertEqual("expense", record.direction.value)
        self.assertEqual("fixture-waybill", record.waybill_no)
        self.assertEqual("000001", record.source_reference)

    def test_explicit_total_zero_marks_no_data(self):
        repository = _Repository()
        service = self._service(repository, lambda binding: _Adapter(binding, empty=True))
        result = service.run(
            {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1, "account_id": "price_default"}
        )
        self.assertEqual(1, result["successful_runs"])
        self.assertEqual(1, len(repository.no_data))
        self.assertFalse(repository.commits)

    def test_yunda_zero_day_allows_absent_site_without_fabrication(self):
        repository = _Repository()
        service = self._service(
            repository,
            lambda binding: _Adapter(binding, empty=True, empty_site=True),
        )
        result = service.run(
            {
                "mode": "sync",
                "target_date": "2026-07-11",
                "rescan_days": 1,
                "account_id": "yunda_default",
            }
        )

        self.assertEqual(1, result["successful_runs"])
        self.assertEqual(1, len(repository.no_data))
        self.assertEqual("", repository.runs[0]["source_site_code"])

    def test_partial_account_failure_preserves_successful_snapshots(self):
        repository = _Repository()

        def factory(binding):
            return _Adapter(binding, discover_error=binding.account_id == "ronghui_daxiang_s")

        service = self._service(repository, factory)
        result = service.run(
            {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1, "platform": "ronghui"}
        )
        self.assertTrue(result["partial_success"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["success"])
        self.assertEqual("partial_failed", result["status"])
        self.assertEqual("FINANCE_SYNC_PARTIAL_FAILED", result["error_code"])
        self.assertEqual(2, result["successful_runs"])
        self.assertEqual(1, result["failed_runs"])
        self.assertEqual(2, len(repository.commits))

    def test_all_failures_surface_safe_first_capture_stage(self):
        repository = _Repository()
        service = self._service(repository, lambda binding: _Adapter(binding, discover_error=True))

        with self.assertRaises(FinanceSyncError) as caught:
            service.run(
                {
                    "mode": "sync",
                    "target_date": "2026-07-11",
                    "rescan_days": 1,
                    "account_id": "price_default",
                }
            )

        self.assertEqual("AUTH_REQUIRED", caught.exception.code)
        self.assertIn("fixture authentication unavailable", str(caught.exception))
        self.assertIn("stage=page_discovery", str(caught.exception))

    def test_account_binding_failure_is_recorded_without_blocking_other_roles(self):
        repository = _Repository()
        manager = _AccountManager()
        manager.rows = [
            row
            for row in manager.rows
            if row["account_id"] != "ronghui_daxiang_s"
        ]
        service = self._service(
            repository,
            lambda binding: _Adapter(binding),
            manager=manager,
        )

        result = service.run(
            {
                "mode": "sync",
                "target_date": "2026-07-11",
                "rescan_days": 1,
                "platform": "ronghui",
            }
        )

        self.assertTrue(result["partial_success"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["success"])
        self.assertEqual("partial_failed", result["status"])
        self.assertEqual("FINANCE_SYNC_PARTIAL_FAILED", result["error_code"])
        self.assertEqual(2, result["successful_runs"])
        self.assertEqual(1, result["failed_runs"])
        self.assertEqual(2, len(repository.commits))
        self.assertEqual(1, len(repository.binding_failures))
        failure = repository.binding_failures[0]
        self.assertEqual("ronghui", failure["platform"])
        self.assertEqual("ronghui_daxiang_s", failure["account_id"])
        self.assertEqual(dt.date(2026, 7, 11), failure["target_date"])

    def test_startup_catchup_only_runs_missing_dates(self):
        repository = _Repository()
        repository.missing_dates = [dt.date(2026, 7, 9)]
        service = self._service(repository, lambda binding: _Adapter(binding))
        result = service.run(
            {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 3, "account_id": "price_default", "_startup_catchup": True}
        )
        self.assertEqual(1, result["successful_runs"])
        self.assertEqual(dt.date(2026, 7, 9), repository.runs[0]["target_date"])
        self.assertEqual("startup", repository.batch_kwargs["trigger_type"])

    def test_retry_rejects_target_filters_and_uses_failed_targets(self):
        with self.assertRaises(FinanceSyncError) as caught:
            plan_sync_request({"mode": "retry", "batch_id": 8, "platform": "ronghui"})
        self.assertEqual("INVALID_PARAMS", caught.exception.code)

        repository = _Repository()
        repository.retry_targets = [
            {"platform": "ronghui", "account_id": "price_default", "target_date": "2026-07-10"}
        ]
        service = self._service(repository, lambda binding: _Adapter(binding))
        result = service.run({"mode": "retry", "batch_id": 8})
        self.assertEqual(1, result["successful_runs"])
        self.assertEqual(dt.date(2026, 7, 10), repository.runs[0]["target_date"])

    def test_non_whitelisted_account_is_rejected(self):
        with self.assertRaises(FinanceSyncError) as caught:
            resolve_finance_accounts(
                _AccountManager(include_extra=True),
                shared_api=SHARED_API,
                account_id="ronghui_unapproved",
            )
        self.assertEqual("ACCOUNT_NOT_ALLOWED", caught.exception.code)

    def test_platform_resolution_does_not_read_other_platform_login_metadata(self):
        manager = _AccountManager()
        original = manager.public_credentials

        def public_credentials(account_id):
            if account_id == "price_default":
                raise RuntimeError("fixture unrelated account unavailable")
            return original(account_id)

        manager.public_credentials = public_credentials
        bindings = resolve_finance_accounts(
            manager,
            shared_api=SHARED_API,
            platform="yunda",
            account_id="yunda_default",
        )

        self.assertEqual(1, len(bindings))
        self.assertEqual("yunda_default", bindings[0].account_id)

    def test_backfill_plan_is_month_chunked_and_warns_earliest_unconfirmed(self):
        plan = plan_sync_request(
            {"mode": "backfill", "start_date": "2026-01-30", "end_date": "2026-03-02"}
        )
        self.assertEqual(
            [
                (dt.date(2026, 1, 30), dt.date(2026, 1, 31)),
                (dt.date(2026, 2, 1), dt.date(2026, 2, 28)),
                (dt.date(2026, 3, 1), dt.date(2026, 3, 2)),
            ],
            month_chunks(plan["start_date"], plan["end_date"]),
        )
        self.assertEqual(EARLIEST_DATE_UNCONFIRMED, plan["earliest_date_status"])

    def test_delayed_run_uses_scheduled_date_not_actual_start_date(self):
        plan = plan_sync_request(
            {
                "mode": "sync",
                "rescan_days": 1,
                "_scheduled_task": {
                    "scheduled_for": "2026-07-12T23:59:00+08:00",
                },
            },
            now=dt.datetime(
                2026,
                7,
                13,
                0,
                5,
                tzinfo=dt.timezone(dt.timedelta(hours=8)),
            ),
        )
        self.assertEqual(dt.date(2026, 7, 11), plan["end_date"])


if __name__ == "__main__":
    unittest.main()
