from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

from agent.tms_runtime.scripts.finance_capture_common import (
    FinanceCaptureError,
    paginate_by_source_key,
    response_json,
    validate_page_identity,
)
from agent.tms_runtime.scripts.finance_live_capture import (
    _format_identity_evidence,
    _ronghui_schema_evidence,
)
from agent.tms_runtime.scripts.ronghui_finance_adapter import capture_ronghui_day
from agent.tms_runtime.scripts.yunda_finance_adapter import capture_yunda_day


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "finance"
YUNDA_BINDINGS = {
    "trade_time": "trade_time",
    "fee_level_1": "first_fee_item",
    "fee_level_2": "second_fee_item",
    "income": "income",
    "expend": "expend",
    "old_amount": "old_amount",
    "new_amount": "new_amount",
    "logistics_id": "logistics_Id",
    "source_reference": "serial_no",
    "business_code": "business_code",
    "remark": "remark",
}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class _Response:
    def __init__(self, *, text: str, payload=None, status_code: int = 200, content_type: str = "application/json"):
        self.text = text
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.url = "https://fixture.invalid/finance"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


class FinanceCaptureAdapterTests(unittest.TestCase):
    def test_ronghui_identity_evidence_never_contains_values(self):
        evidence = _format_identity_evidence(
            {
                "identityEvidence": {
                    "expectedLength": 8,
                    "rawType": "string",
                    "rawLength": 120,
                    "jsonParsed": True,
                    "infoType": "object",
                    "infoKeys": ["loginAccount", "unsafe key=value"],
                    "candidates": [
                        {
                            "path": "profile.loginAccount",
                            "length": 8,
                            "exact": False,
                            "casefold": True,
                            "contains": False,
                            "containedBy": False,
                            "value": "secret-account",
                        }
                    ],
                }
            }
        )

        self.assertIn("loginAccount", evidence)
        self.assertIn("raw_type=string", evidence)
        self.assertIn("json_parsed=True", evidence)
        self.assertIn("casefold=True", evidence)
        self.assertNotIn("secret-account", evidence)
        self.assertNotIn("unsafe key=value", evidence)

    def test_ronghui_schema_evidence_contains_only_structural_tokens(self):
        evidence = _ronghui_schema_evidence(
            """
            callId: 'FIND_BALANCE_QRY_RENAMED'
            columns: ['BALANCE_DATE', 'SETTLEMENT_FEE', 'AFTER_AMOUNT']
            account: 'private-sample-account'
            """,
            expected_markers={"FIND_BALANCE_QRY_OLD", "BALANCE_DATE", "BEFORE_AMOUNT"},
        )

        self.assertIn("call_ids=FIND_BALANCE_QRY_RENAMED", evidence)
        self.assertIn("BALANCE_DATE", evidence)
        self.assertIn("missing=BEFORE_AMOUNT,FIND_BALANCE_QRY_OLD", evidence)
        self.assertNotIn("private-sample-account", evidence)

    def test_pagination_overlap_deduplicates_by_stable_key(self):
        pages = {
            1: {"total": 3, "rows": [{"GUID": "g1"}, {"GUID": "g2"}]},
            2: {"total": 3, "rows": [{"GUID": "g2"}, {"GUID": "g3"}]},
        }
        batch = paginate_by_source_key(
            lambda page, _size: pages[page],
            source_key="GUID",
            page_size=2,
            max_pages=3,
            stage="fixture",
        )
        self.assertEqual(3, len(batch.rows))
        self.assertEqual(1, batch.duplicate_rows)
        self.assertEqual(4, batch.page_row_count)
        self.assertEqual((2, 2), batch.page_row_counts)

    def test_pagination_missing_stable_key_fails_explicitly(self):
        with self.assertRaises(FinanceCaptureError) as caught:
            paginate_by_source_key(
                lambda _page, _size: {"total": 1, "rows": [{"not_guid": "x"}]},
                source_key="GUID",
                page_size=100,
                max_pages=1,
                stage="fixture",
            )
        self.assertEqual("STABLE_KEY_MISSING", caught.exception.code)

    def test_empty_body_non_json_and_login_page_fail_explicitly(self):
        cases = [
            (_Response(text="", payload={}), "EMPTY_RESPONSE"),
            (_Response(text="not-json", payload=None), "NON_JSON_RESPONSE"),
            (
                _Response(text='<form class="login-form"><input type="password"></form>', payload=None, content_type="text/html"),
                "AUTH_REQUIRED",
            ),
        ]
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(FinanceCaptureError) as caught:
                response_json(response, platform="fixture", stage="fixture")
            self.assertEqual(code, caught.exception.code)

    def test_empty_rows_without_total_is_unverified(self):
        with self.assertRaises(FinanceCaptureError) as caught:
            paginate_by_source_key(
                lambda _page, _size: {"rows": []},
                source_key="GUID",
                page_size=100,
                max_pages=2,
                stage="fixture",
            )
        self.assertEqual("UNVERIFIED_TOTAL", caught.exception.code)

        with self.assertRaises(FinanceCaptureError) as list_caught:
            paginate_by_source_key(
                lambda _page, _size: [],
                source_key="GUID",
                page_size=100,
                max_pages=2,
                stage="fixture",
            )
        self.assertEqual("UNVERIFIED_TOTAL", list_caught.exception.code)

    def test_ronghui_fixture_normalizes_whitelisted_fields(self):
        payload = _fixture("ronghui_detail_page.json")
        result = capture_ronghui_day(
            account_id="fixture-ronghui",
            target_date=dt.date(2026, 7, 11),
            field_bindings={
                "trade_time": "BALANCE_DATE",
                "fee_name": "SETTLEMENT_TYPE",
                "amount": "SETTLEMENT_AMOUNT",
                "bill_time": "BILL_DATE",
                "waybill_no": "BILL_CODE",
                "old_amount": "BEFORE_AMOUNT",
                "new_amount": "AFTER_AMOUNT",
                "balance_order": "BALANCE_ORDER",
                "bill_code": "BILL_CODE",
            },
            source_site_code="fixture-site",
            source_site_name="Fixture Site",
            login_site_code="fixture-site",
            account_match=True,
            fetch_detail_page=lambda _page, _size: payload,
        )
        self.assertEqual(2, len(result.transactions))
        self.assertEqual("fixture-rh-001", result.transactions[0]["source_id"])
        self.assertEqual("12.3000", result.transactions[0]["expend"])
        self.assertEqual("fixture-balance-001", result.transactions[0]["source_reference"])
        self.assertNotIn("GUID", result.transactions[0])

    def test_ronghui_field_drift_reports_only_response_keys(self):
        payload = _fixture("ronghui_detail_page.json")
        payload["rows"][0]["private_value"] = "do-not-log-this-value"
        del payload["rows"][0]["SETTLEMENT_AMOUNT"]

        with self.assertRaises(FinanceCaptureError) as caught:
            capture_ronghui_day(
                account_id="fixture-ronghui",
                target_date=dt.date(2026, 7, 11),
                field_bindings={
                    "trade_time": "BALANCE_DATE",
                    "fee_name": "SETTLEMENT_TYPE",
                    "amount": "SETTLEMENT_AMOUNT",
                    "old_amount": "BEFORE_AMOUNT",
                    "new_amount": "AFTER_AMOUNT",
                    "balance_order": "BALANCE_ORDER",
                    "bill_code": "BILL_CODE",
                },
                source_site_code="fixture-site",
                source_site_name="Fixture Site",
                login_site_code="fixture-site",
                account_match=True,
                fetch_detail_page=lambda _page, _size: payload,
            )

        self.assertEqual("FIELD_DRIFT", caught.exception.code)
        self.assertIn("SETTLEMENT_AMOUNT", str(caught.exception))
        self.assertIn("private_value", str(caught.exception))
        self.assertNotIn("do-not-log-this-value", str(caught.exception))

    def test_yunda_fixture_filters_cross_midnight_and_preserves_required_fields(self):
        payload = _fixture("yunda_detail_page.json")
        result = capture_yunda_day(
            account_id="fixture-yunda",
            target_date=dt.date(2026, 7, 11),
            context={
                "dynamic_endpoints": {
                    "selectDynamicFileds": "/fixture/selectDynamicFileds",
                    "selectFiledsData": "/fixture/selectFiledsData",
                    "selectInterface": "/fixture/selectInterface",
                },
                "source_site_code": "fixture-site",
                "source_site_name": "Fixture Site",
                "login_site_code": "fixture-site",
                "account_match": True,
                "source_site_verified": True,
            },
            field_bindings=YUNDA_BINDINGS,
            fetch_detail_page=lambda _page, _size: payload,
        )
        self.assertEqual(1, len(result.transactions))
        self.assertEqual(1, result.validation["excluded_other_dates"])
        record = result.transactions[0]
        self.assertEqual("fixture-logistics-001", record["logistics_id"])
        self.assertEqual("0.0000", record["income"])
        self.assertEqual("1.2500", record["expend"])

    def test_yunda_rejects_both_amount_directions_nonzero(self):
        payload = _fixture("yunda_detail_page.json")
        payload["rows"] = [dict(payload["rows"][0], income="1.00", expend="1.00")]
        payload["total"] = 1
        with self.assertRaises(FinanceCaptureError) as caught:
            capture_yunda_day(
                account_id="fixture-yunda",
                target_date=dt.date(2026, 7, 11),
                context={
                    "dynamic_endpoints": {name: f"/fixture/{name}" for name in ("selectDynamicFileds", "selectFiledsData", "selectInterface")},
                    "source_site_code": "fixture-site",
                    "source_site_name": "Fixture Site",
                    "login_site_code": "fixture-site",
                    "account_match": True,
                    "source_site_verified": True,
                },
                field_bindings=YUNDA_BINDINGS,
                fetch_detail_page=lambda _page, _size: payload,
            )
        self.assertEqual("AMOUNT_DIRECTION_INVALID", caught.exception.code)

    def test_yunda_explicit_zero_day_does_not_fabricate_a_site(self):
        result = capture_yunda_day(
            account_id="fixture-yunda",
            target_date=dt.date(2026, 7, 11),
            context={
                "dynamic_endpoints": {
                    name: f"/fixture/{name}"
                    for name in ("selectDynamicFileds", "selectFiledsData", "selectInterface")
                },
                "source_site_code": "",
                "source_site_name": "",
                "account_match": True,
                "source_site_verified": False,
            },
            field_bindings={},
            fetch_detail_page=lambda _page, _size: {"total": 0, "rows": []},
        )

        self.assertEqual([], result.transactions)
        self.assertEqual([], result.summaries)
        self.assertEqual("", result.source_site_code)
        self.assertEqual(0, result.validation["source_total"])

    def test_yunda_field_drift_is_not_defaulted(self):
        payload = _fixture("yunda_detail_page.json")
        del payload["rows"][0]["logistics_Id"]
        with self.assertRaises(FinanceCaptureError) as caught:
            capture_yunda_day(
                account_id="fixture-yunda",
                target_date=dt.date(2026, 7, 11),
                context={
                    "dynamic_endpoints": {name: f"/fixture/{name}" for name in ("selectDynamicFileds", "selectFiledsData", "selectInterface")},
                    "source_site_code": "fixture-site",
                    "source_site_name": "Fixture Site",
                    "login_site_code": "fixture-site",
                    "account_match": True,
                    "source_site_verified": True,
                },
                field_bindings=YUNDA_BINDINGS,
                fetch_detail_page=lambda _page, _size: payload,
            )
        self.assertEqual("FIELD_DRIFT", caught.exception.code)

    def test_yunda_missing_dynamic_amount_binding_fails(self):
        payload = _fixture("yunda_detail_page.json")
        bindings = dict(YUNDA_BINDINGS)
        del bindings["old_amount"]
        with self.assertRaises(FinanceCaptureError) as caught:
            capture_yunda_day(
                account_id="fixture-yunda",
                target_date=dt.date(2026, 7, 11),
                context={
                    "dynamic_endpoints": {name: f"/fixture/{name}" for name in ("selectDynamicFileds", "selectFiledsData", "selectInterface")},
                    "source_site_code": "fixture-site",
                    "source_site_name": "Fixture Site",
                    "login_site_code": "fixture-site",
                    "account_match": True,
                    "source_site_verified": True,
                },
                field_bindings=bindings,
                fetch_detail_page=lambda _page, _size: payload,
            )
        self.assertEqual("FIELD_DRIFT", caught.exception.code)

    def test_page_site_mismatch_fails(self):
        with self.assertRaises(FinanceCaptureError) as caught:
            validate_page_identity(
                platform="fixture",
                account_match=True,
                login_site_code="fixture-login-site",
                source_site_code="fixture-query-site",
            )
        self.assertEqual("SOURCE_SITE_MISMATCH", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
