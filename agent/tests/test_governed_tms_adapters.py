from __future__ import annotations

import unittest
from unittest.mock import patch

from tools import governed_tms_adapter as adapter


class GovernedTmsAdapterTests(unittest.TestCase):
    def test_receipts_sync_never_forwards_caller_supplied_data_url(self):
        result = adapter.build_receipts_sync_params(
            {
                "platform": "yunda",
                "direction": "send",
                "date_from": "2026-08-13",
                "date_to": "2026-08-13",
                "datagrid_url": "https://example.invalid/unsafe",
            }
        )

        self.assertNotIn("datagrid_url", result)
        self.assertEqual(result["platform"], "yunda")

    def test_receipts_audit_never_forwards_untrusted_raw_payload(self):
        result = adapter.build_receipts_audit_params(
            {
                "platform": "ronghui",
                "direction": "send",
                "result": "passed",
                "waybill_no": "R001",
                "raw_payload": {"GUID": "caller-controlled"},
            }
        )

        self.assertNotIn("raw_payload", result)
        self.assertEqual(result["waybill_no"], "R001")

    def test_customer_query_action_is_fixed_and_unknown_fields_are_dropped(self):
        result = adapter.build_customer_action_params(
            "query",
            {
                "platform": "ronghui",
                "account_id": "ronghui-a",
                "direction": "published_to_me",
                "action": "reply",
                "raw": {"unsafe": True},
            },
        )

        self.assertEqual(result["action"], "query")
        self.assertNotIn("raw", result)
        self.assertEqual(result["filters"], {"direction": "published_to_me"})

    def test_customer_mark_read_does_not_accept_ronghui_update_fields(self):
        result = adapter.build_customer_action_params(
            "mark_read",
            {
                "platform": "ronghui",
                "account_id": "ronghui-a",
                "external_id": "guid-1",
                "update_fields": {"BL_SEE": "guessed"},
            },
        )

        self.assertEqual(result["action"], "mark_read")
        self.assertEqual(result["payload"], {})
        self.assertNotIn("update_fields", result["item"])

    def test_customer_publish_rejects_payload_for_the_other_platform(self):
        with self.assertRaisesRegex(adapter.GovernedAdapterError, "do not match platform"):
            adapter.build_customer_action_params(
                "publish",
                {
                    "platform": "ronghui",
                    "account_id": "ronghui-a",
                    "payload": {
                        "ship_no": "Y001",
                        "classes_type": "delay",
                        "prob_text": "test",
                        "site_id": ["site-a"],
                    },
                },
            )

    def test_upload_adapter_never_deletes_the_callers_file(self):
        result = adapter.build_customer_action_params(
            "upload_attachment",
            {
                "platform": "yunda",
                "account_id": "yunda-a",
                "file_path": "/tmp/upload.png",
                "delete_after_upload": True,
            },
        )

        self.assertEqual(result["payload"]["delete_after_upload"], False)

    def test_clock_in_adapter_carries_account_and_uses_fixed_inner_timeouts(self):
        result = adapter.build_clock_in_params(
            {
                "account_id": "ronghui_daxiang_s",
                "sitecode": "site-code",
                "sitefbcode": "operation-code",
                "sitename": "site",
                "sitefbname": "operation",
                "first_type": "交件到港",
                "second_type": "接件离港",
                "mode": "browser",
            }
        )

        self.assertEqual(result["timeout_sec"], 60)
        self.assertEqual(result["client_timeout_sec"], 75)
        self.assertEqual(
            result["params"],
            {
                "account_id": "ronghui_daxiang_s",
                "sitecode": "site-code",
                "sitefbcode": "operation-code",
                "sitename": "site",
                "sitefbname": "operation",
                "first_type": "交件到港",
                "second_type": "接件离港",
                "mode": "api",
            },
        )

    def test_fixed_target_returns_compact_failure_without_echoing_payload(self):
        with patch.object(
            adapter,
            "call_http_service",
            return_value={
                "ok": False,
                "data": {"data": {"error_code": "AUTH_REQUIRED", "message": "login required"}},
                "request_payload": {"must_not": "echo"},
            },
        ):
            result = adapter.execute_fixed_target("receipts_sync", {"platform": "all"})

        self.assertEqual(result, {"error": "login required", "error_code": "AUTH_REQUIRED"})

    def test_receipts_sync_requires_complete_numeric_pagination_evidence(self):
        complete = {
            "ok": True,
            "data": {
                "ok": True,
                "stats": {
                    "sources": [
                        {
                            "platform": "ronghui",
                            "direction": "send",
                            "fetched": 2,
                            "total": 2,
                            "truncated": False,
                            "attachment_errors": 0,
                        }
                    ]
                },
                "warnings": [],
            },
        }
        result = adapter.validate_receipts_sync_response(complete)
        self.assertTrue(result["evidence"]["pagination_complete"])

        incomplete = {
            "ok": True,
            "data": {
                "stats": {
                    "sources": [
                        {
                            "platform": "yunda",
                            "direction": "send",
                            "fetched": 200,
                            "total": None,
                            "truncated": False,
                        }
                    ]
                },
                "warnings": [],
            },
        }
        failure = adapter.validate_receipts_sync_response(incomplete)
        self.assertEqual(failure["error_code"], "INCOMPLETE_SOURCE_EVIDENCE")

    def test_unified_write_result_never_invents_a_postcondition(self):
        result = adapter._unified_result(
            target="customer_service_problem",
            original_params={
                "platform": "ronghui",
                "account_id": "ronghui-a",
                "external_id": "problem-1",
            },
            response={"ok": True, "data": {"ok": True, "external_id": "problem-1"}},
            write=True,
        )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["data"], {"ok": True, "external_id": "problem-1"})
        self.assertEqual(result["meta"]["source_system"], "ronghui")
        self.assertEqual(result["meta"]["account_id"], "ronghui-a")
        self.assertEqual(result["meta"]["record_count"], 1)
        self.assertTrue(result["meta"]["pagination_complete"])
        self.assertTrue(result["meta"]["evidence_refs"])
        self.assertNotIn("postconditions", result["meta"])
        self.assertNotIn("postcondition_evidence", result["meta"])

    def test_unified_failure_preserves_login_required_classification(self):
        result = adapter._unified_result(
            target="customer_service_problem",
            original_params={"platform": "ronghui", "account_id": "ronghui-a"},
            response={"error": "session expired", "error_code": "AUTH_REQUIRED"},
            write=False,
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "LOGIN_REQUIRED")
        self.assertEqual(result["error"]["code"], "LOGIN_REQUIRED")

    def test_receipts_sync_evidence_does_not_block_payload_unwrapping(self):
        response = {
            "ok": True,
            "data": {
                "ok": True,
                "stats": {
                    "sources": [
                        {
                            "platform": "ronghui",
                            "direction": "send",
                            "fetched": 1,
                            "total": 1,
                            "truncated": False,
                            "attachment_errors": 0,
                        }
                    ]
                },
                "warnings": [],
            },
        }
        validated = adapter.validate_receipts_sync_response(response)
        result = adapter._unified_result(
            target="receipts_sync",
            original_params={"platform": "all", "direction": "both"},
            response=validated,
            write=True,
        )

        self.assertIn("stats", result["data"])
        self.assertNotIn("data", result["data"])
        self.assertTrue(result["meta"]["pagination_complete"])
        proof = result["meta"]["postcondition_evidence"]["0"]
        self.assertEqual(proof["condition"], "internal_receipt_projection_returned")
        self.assertEqual(proof["observed_at"], result["meta"]["observed_at"])
        self.assertIn(proof["evidence_ref"], result["meta"]["evidence_refs"])

    def test_receipts_audit_requires_matching_readback(self):
        params = {
            "platform": "ronghui",
            "result": "passed",
            "waybill_no": "R001",
        }
        response = {
            "ok": True,
            "data": {
                "ok": True,
                "verification": {
                    "verified": True,
                    "external_id": "guid-1",
                    "waybill_no": "R001",
                    "audit_status": "2",
                    "observed_at": "2026-08-13T00:00:00Z",
                },
            },
        }

        validated = adapter.validate_receipts_audit_response(response, params)
        result = adapter._unified_result(
            target="receipts_audit",
            original_params=params,
            response=validated,
            write=True,
        )

        proof = result["meta"]["postcondition_evidence"]["0"]
        self.assertEqual(proof["condition"], "third_party_receipt_audit_confirmed")
        mismatch = adapter.validate_receipts_audit_response(
            {
                "ok": True,
                "data": {
                    "ok": True,
                    "verification": {
                        "verified": True,
                        "external_id": "guid-1",
                        "waybill_no": "OTHER",
                        "audit_status": "2",
                        "observed_at": "2026-08-13T00:00:00Z",
                    },
                },
            },
            params,
        )
        self.assertEqual(mismatch["error_code"], "POSTCONDITION_UNVERIFIED")

    def test_clock_in_requires_explicit_success_from_both_source_responses(self):
        params = {
            "account_id": "ronghui_daxiang_s",
            "sitecode": "site-code",
            "sitefbcode": "operation-code",
            "sitename": "site",
            "sitefbname": "operation",
            "first_type": "first",
            "second_type": "second",
        }
        response = {
            "ok": True,
            "data": {
                "first_success": True,
                "second_success": True,
                "first_response": {"success": True},
                "second_response": {"success": True},
            },
        }

        validated = adapter.validate_clock_in_response(response, params)
        result = adapter._unified_result(
            target="clock_in_dual",
            original_params=params,
            response=validated,
            write=True,
        )
        self.assertEqual(
            result["meta"]["postcondition_evidence"]["0"]["condition"],
            "both_third_party_clock_ins_confirmed",
        )
        proof = result["meta"]["postcondition_evidence"]["0"]["details"]
        self.assertEqual(proof["account_id"], "ronghui_daxiang_s")
        self.assertEqual(proof["sitecode"], "site-code")
        self.assertEqual(proof["sitefbcode"], "operation-code")
        self.assertEqual(proof["sitename"], "site")
        self.assertEqual(proof["sitefbname"], "operation")

        not_explicit = dict(response)
        not_explicit["data"] = dict(response["data"])
        not_explicit["data"]["first_response"] = {"ok": True}
        failure = adapter.validate_clock_in_response(not_explicit, params)
        self.assertEqual(failure["error_code"], "POSTCONDITION_UNVERIFIED")

    def test_customer_write_conditions_are_action_specific_and_fail_closed(self):
        cases = {
            "mark_read": (
                "third_party_read_state_confirmed",
                {"external_id": "problem-1", "result": {"success": True}},
            ),
            "reply": (
                "third_party_reply_confirmed",
                {"external_id": "problem-1", "result": {"ok": True}},
            ),
            "publish": (
                "third_party_problem_publish_confirmed",
                {"external_id": "problem-new", "result": {"success": True}},
            ),
            "upload_attachment": (
                "third_party_attachment_upload_confirmed",
                {"result": {"success": True, "data": {"file_id": "file-1"}}},
            ),
        }
        for action, (condition, payload) in cases.items():
            with self.subTest(action=action):
                params = {
                    "platform": "yunda",
                    "account_id": "yunda-a",
                    "external_id": "problem-1",
                }
                response = {"ok": True, "data": {"ok": True, **payload}}
                validated = adapter.validate_customer_write_response(action, response, params)
                result = adapter._unified_result(
                    target="customer_service_problem",
                    original_params=params,
                    response=validated,
                    write=True,
                )
                self.assertEqual(
                    result["meta"]["postcondition_evidence"]["0"]["condition"],
                    condition,
                )

        failure = adapter.validate_customer_write_response(
            "reply",
            {
                "ok": True,
                "data": {
                    "ok": True,
                    "external_id": "problem-1",
                    "result": {"message": "accepted"},
                },
            },
            {"platform": "yunda", "account_id": "yunda-a", "external_id": "problem-1"},
        )
        self.assertEqual(failure["error_code"], "POSTCONDITION_UNVERIFIED")

        local_path_only = adapter.validate_customer_write_response(
            "upload_attachment",
            {
                "ok": True,
                "data": {
                    "ok": True,
                    "result": {"success": True, "file_path": "/tmp/upload.png"},
                },
            },
            {
                "platform": "yunda",
                "account_id": "yunda-a",
                "file_path": "/tmp/upload.png",
            },
        )
        self.assertEqual(local_path_only["error_code"], "POSTCONDITION_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
