from __future__ import annotations

import unittest

from shared.finance import (
    CaptureEvidence,
    Direction,
    FeeItemKey,
    Platform,
    SummarySnapshot,
    TransactionRecord,
    ValidationStatus,
    validate_finance_capture,
)


def _rows() -> tuple[list[TransactionRecord], list[SummarySnapshot]]:
    transactions = [
        TransactionRecord(
            platform=Platform.YUNDA,
            account_id="fictional-account",
            login_account="fictional-login",
            source_record_key="fictional-id-1",
            business_date="2026-01-02",
            transaction_at="2026-01-02 10:00:00",
            primary_fee_name="测试收入",
            direction=Direction.INCOME,
            income="10",
            expense="0",
            before_balance="100",
            after_balance="110",
            source_reference="0001",
        ),
        TransactionRecord(
            platform=Platform.YUNDA,
            account_id="fictional-account",
            login_account="fictional-login",
            source_record_key="fictional-id-2",
            business_date="2026-01-02",
            transaction_at="2026-01-02 11:00:00",
            primary_fee_name="测试支出",
            direction=Direction.EXPENSE,
            income="0",
            expense="3",
            before_balance="110",
            after_balance="107",
            source_reference="0002",
        ),
    ]
    summaries = [
        SummarySnapshot(
            Platform.YUNDA,
            "fictional-account",
            "2026-01-02",
            "测试收入",
            Direction.INCOME,
            "10",
            "0",
        ),
        SummarySnapshot(
            Platform.YUNDA,
            "fictional-account",
            "2026-01-02",
            "测试支出",
            Direction.EXPENSE,
            "0",
            "3",
        ),
    ]
    return transactions, summaries


class FinanceValidationTests(unittest.TestCase):
    def test_valid_capture_reconciles_counts_fees_totals_and_balance_chain(self) -> None:
        transactions, summaries = _rows()
        report = validate_finance_capture(
            CaptureEvidence(
                remote_total=2,
                page_row_counts=[1, 1],
                transactions=transactions,
                summaries=summaries,
            )
        )
        self.assertIs(report.status, ValidationStatus.PASSED)
        self.assertTrue(report.passed)
        self.assertEqual(report.metrics["detail_net_change"], 7)
        self.assertEqual(report.metrics["minimum_net_amount"], -3)
        self.assertEqual(report.metrics["maximum_net_amount"], 10)

    def test_pagination_overlap_is_deduplicated_but_remains_visible_warning(self) -> None:
        transactions, summaries = _rows()
        report = validate_finance_capture(
            CaptureEvidence(
                remote_total=2,
                page_row_counts=[2, 1],
                transactions=transactions,
                summaries=summaries,
            )
        )
        self.assertIs(report.status, ValidationStatus.WARNING)
        self.assertEqual(report.metrics["duplicate_page_row_count"], 1)
        self.assertIn("PAGINATION_OVERLAP", {issue.code for issue in report.warnings})

    def test_duplicate_stable_key_and_different_content_fail(self) -> None:
        transactions, summaries = _rows()
        conflicting = TransactionRecord(
            platform=Platform.YUNDA,
            account_id="fictional-account",
            login_account="fictional-login",
            source_record_key="fictional-id-1",
            business_date="2026-01-02",
            transaction_at="2026-01-02 12:00:00",
            primary_fee_name="测试收入",
            direction=Direction.INCOME,
            income="1",
            expense="0",
            before_balance="107",
            after_balance="108",
            source_reference="0003",
        )
        report = validate_finance_capture(
            CaptureEvidence(
                remote_total=2,
                page_row_counts=[3],
                transactions=[*transactions, conflicting],
                summaries=summaries,
            )
        )
        self.assertIs(report.status, ValidationStatus.FAILED)
        codes = {issue.code for issue in report.errors}
        self.assertIn("DUPLICATE_SOURCE_KEY", codes)
        self.assertIn("SAME_KEY_CONTENT_CONFLICT", codes)

    def test_fee_summary_mismatch_and_balance_equation_fail(self) -> None:
        transactions, summaries = _rows()
        broken = TransactionRecord(
            platform=Platform.YUNDA,
            account_id="fictional-account",
            login_account="fictional-login",
            source_record_key="fictional-id-2",
            business_date="2026-01-02",
            transaction_at="2026-01-02 11:00:00",
            primary_fee_name="测试支出",
            direction=Direction.EXPENSE,
            income="0",
            expense="4",
            before_balance="110",
            after_balance="107",
            source_reference="0002",
        )
        report = validate_finance_capture(
            CaptureEvidence(
                remote_total=2,
                page_row_counts=[2],
                transactions=[transactions[0], broken],
                summaries=summaries,
            )
        )
        codes = {issue.code for issue in report.errors}
        self.assertIn("FEE_SUMMARY_MISMATCH", codes)
        self.assertIn("TOTAL_AMOUNT_MISMATCH", codes)
        self.assertIn("BALANCE_EQUATION_MISMATCH", codes)

    def test_new_fee_history_revision_and_extreme_are_warnings(self) -> None:
        transactions, summaries = _rows()
        previous = {
            transactions[0].source_record_key: tuple(
                list(transactions[0].comparison_payload())[:-1] + ["changed-remark"]
            )
        }
        known = {
            FeeItemKey(Platform.YUNDA, "测试收入", "", Direction.INCOME)
        }
        report = validate_finance_capture(
            CaptureEvidence(
                remote_total=2,
                page_row_counts=[2],
                transactions=transactions,
                summaries=summaries,
                known_fee_items=known,
                previous_record_payloads=previous,
                extreme_abs_threshold="5",
            )
        )
        codes = {issue.code for issue in report.warnings}
        self.assertIn("NEW_FEE_ITEM", codes)
        self.assertIn("HISTORICAL_REVISION", codes)
        self.assertIn("AMOUNT_EXTREME", codes)

    def test_explicit_remote_zero_is_the_only_no_data_evidence(self) -> None:
        report = validate_finance_capture(
            CaptureEvidence(
                remote_total=0,
                page_row_counts=[0],
                transactions=[],
                summaries=[],
                response_valid=True,
            )
        )
        self.assertIs(report.status, ValidationStatus.PASSED)
        self.assertTrue(report.metrics["eligible_no_data"])
        invalid = validate_finance_capture(
            CaptureEvidence(
                remote_total=0,
                page_row_counts=[],
                transactions=[],
                summaries=[],
                response_valid=False,
            )
        )
        self.assertIs(invalid.status, ValidationStatus.FAILED)
        self.assertFalse(invalid.metrics["eligible_no_data"])


if __name__ == "__main__":
    unittest.main()
