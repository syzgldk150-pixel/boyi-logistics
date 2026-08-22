import unittest
import sys
import json
import types
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "agent" / "tms_runtime" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import receipts_audit


class ReceiptAuditTests(unittest.TestCase):
    def test_target_is_registered_for_runtime_routing(self):
        dispatch_source = (Path(__file__).resolve().parents[1] / "agent" / "tms_runtime" / "dispatch.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"receipts_audit"', dispatch_source)
        self.assertIn("agent.tms_runtime.scripts.receipts_audit", dispatch_source)

    def test_ronghui_audit_posts_captured_save_tables_payload(self):
        class Response:
            text = '{"success": true, "message": "数据保存成功"}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True, "message": "数据保存成功"}

        class Session:
            def __init__(self):
                self.cookies = {
                    "userInfo": json.dumps(
                        {
                            "loginSiteCode": "7390004",
                            "loginSiteName": "邵阳大祥站",
                            "loginEmpCode": "73900040001",
                            "loginEmpName": "邵阳大祥站(管理员)",
                        },
                        ensure_ascii=False,
                    )
                }
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append({"url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        readback_rows = [
            {
                "GUID": "5445D4B0062D7152E0630100007F6598",
                "BILL_CODE": "2606000040",
                "R_BILLCODE": "R001",
                "AUDIT_STATUS": "2",
            }
        ]

        with (
            patch.object(receipts_audit, "get_session_broker", return_value=broker),
            patch.object(
                receipts_audit,
                "resolve_ronghui_entry_url",
                return_value="https://tms.ronghuiwl.com/widget/home?authenticationKey=auth-1&pageId=page-1",
            ),
            patch.object(receipts_audit, "fetch_ronghui_process_rows", return_value=readback_rows),
        ):
            result = receipts_audit.run_once(
                {
                    "platform": "ronghui",
                    "result": "passed",
                    "waybill_no": "2606000040",
                    "receipt_no": "R001",
                    "reason": "",
                    "raw_payload": {
                        "GUID": "5445D4B0062D7152E0630100007F6598",
                        "BILL_CODE": "2606000040",
                        "REPLY_CONTENT": "回单签名生成",
                        "token": "must-not-echo",
                    },
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual("ronghui", result["platform"])
        self.assertEqual("direct_api_executed", result["result_status"])
        self.assertEqual("审核通过", result["audit_status"])
        self.assertEqual(
            result["verification"],
            {
                "verified": True,
                "external_id": "5445D4B0062D7152E0630100007F6598",
                "waybill_no": "2606000040",
                "audit_status": "2",
                "observed_at": result["verification"]["observed_at"],
            },
        )
        self.assertNotIn("token", repr(result).lower())
        self.assertEqual("https://tms.ronghuiwl.com/dataOperation/saveTables", session.calls[0]["url"])
        headers = session.calls[0]["kwargs"]["headers"]
        self.assertEqual("auth-1", headers["authenticationKey"])
        self.assertEqual("page-1", headers["pageId"])
        self.assertNotIn("auth-1", repr(result))
        self.assertNotIn("page-1", repr(result))
        files = session.calls[0]["kwargs"]["files"]
        payload = json.loads(files["params"][1])
        self.assertEqual("TAB_PROCESS_RECORD_UPT", payload[0]["operationKey"])
        row = payload[0]["data"][0]
        self.assertEqual("5445D4B0062D7152E0630100007F6598", row["GUID"])
        self.assertEqual("2606000040", row["BILL_CODE"])
        self.assertEqual("2", row["AUDIT_STATUS"])
        self.assertEqual("7390004", row["AUDIT_SITE_CODE"])
        self.assertEqual("邵阳大祥站(管理员)", row["AUDIT_MAN"])
        self.assertNotIn("token", json.dumps(payload, ensure_ascii=False))

    def test_ronghui_audit_fetches_process_guid_before_save(self):
        class Response:
            text = '{"success": true}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True}

        class Session:
            def __init__(self):
                self.cookies = {
                    "userInfo": json.dumps(
                        {
                            "loginSiteCode": "7390004",
                            "loginSiteName": "邵阳大祥站",
                            "loginEmpCode": "73900040001",
                            "loginEmpName": "邵阳大祥站(管理员)",
                        },
                        ensure_ascii=False,
                    )
                }
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append({"url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        process_rows = [
            {
                "GUID": "5445D4B0062D7152E0630100007F6598",
                "BILL_CODE": "2606000040",
                "REPLY_CONTENT": "回单签名生成",
                "AUDIT_STATUS": "1",
            }
        ]

        with (
            patch.object(receipts_audit, "get_session_broker", return_value=broker),
            patch.object(
                receipts_audit,
                "resolve_ronghui_entry_url",
                return_value="https://tms.ronghuiwl.com/widget/home?authenticationKey=auth-1&pageId=page-1",
            ),
            patch.object(
                receipts_audit,
                "fetch_ronghui_process_rows",
                side_effect=[
                    process_rows,
                    [{**process_rows[0], "AUDIT_STATUS": "2"}],
                ],
            ) as fetch_rows,
        ):
            result = receipts_audit.run_once({"platform": "ronghui", "result": "passed", "waybill_no": "2606000040"})

        self.assertTrue(result["ok"])
        self.assertEqual(fetch_rows.call_count, 2)
        headers = session.calls[0]["kwargs"]["headers"]
        self.assertEqual("auth-1", headers["authenticationKey"])
        self.assertEqual("page-1", headers["pageId"])
        payload = json.loads(session.calls[0]["kwargs"]["files"]["params"][1])
        row = payload[0]["data"][0]
        self.assertEqual("5445D4B0062D7152E0630100007F6598", row["GUID"])
        self.assertEqual("回单签名生成", row["REPLY_CONTENT"])
        self.assertEqual("2", row["AUDIT_STATUS"])

    def test_ronghui_audit_fails_when_saved_row_cannot_be_read_back(self):
        class Response:
            text = '{"success": true}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True}

        class Session:
            def __init__(self):
                self.cookies = {
                    "userInfo": json.dumps(
                        {
                            "loginSiteCode": "7390004",
                            "loginSiteName": "邵阳大祥站",
                            "loginEmpCode": "73900040001",
                            "loginEmpName": "邵阳大祥站(管理员)",
                        },
                        ensure_ascii=False,
                    )
                }

            def post(self, url, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with (
            patch.object(receipts_audit, "get_session_broker", return_value=broker),
            patch.object(
                receipts_audit,
                "resolve_ronghui_entry_url",
                return_value="https://tms.ronghuiwl.com/widget/home?authenticationKey=auth-1&pageId=page-1",
            ),
            patch.object(receipts_audit, "fetch_ronghui_process_rows", return_value=[]),
        ):
            result = receipts_audit.run_once(
                {
                    "platform": "ronghui",
                    "result": "passed",
                    "waybill_no": "2606000040",
                    "raw_payload": {
                        "GUID": "5445D4B0062D7152E0630100007F6598",
                        "BILL_CODE": "2606000040",
                    },
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "POSTCONDITION_UNVERIFIED")

    def test_ronghui_audit_fails_without_guid_instead_of_guessing(self):
        class Session:
            cookies = {
                "userInfo": json.dumps(
                    {
                        "loginSiteCode": "7390004",
                        "loginSiteName": "邵阳大祥站",
                        "loginEmpCode": "73900040001",
                        "loginEmpName": "邵阳大祥站(管理员)",
                    },
                    ensure_ascii=False,
                )
            }

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())

        with (
            patch.object(receipts_audit, "get_session_broker", return_value=broker),
            patch.object(
                receipts_audit,
                "resolve_ronghui_entry_url",
                return_value="https://tms.ronghuiwl.com/widget/home?authenticationKey=auth-1&pageId=page-1",
            ),
            patch.object(receipts_audit, "fetch_ronghui_process_rows", return_value=[]),
        ):
            result = receipts_audit.run_once({"platform": "ronghui", "result": "passed", "waybill_no": "2606000040"})

        self.assertFalse(result["ok"])
        self.assertEqual("MISSING_RONGHUI_AUDIT_FIELDS", result["error_code"])

    def test_yunda_still_requires_captured_adapter(self):
        result = receipts_audit.run_once({"platform": "yunda", "result": "passed", "waybill_no": "Y001"})

        self.assertFalse(result["ok"])
        self.assertEqual("capture_required", result["result_status"])
        self.assertEqual("审核通过", result["audit_status"])
        self.assertEqual("AUDIT_CAPTURE_REQUIRED", result["error_code"])

    def test_validates_result_and_platform(self):
        bad_result = receipts_audit.run_once({"platform": "yunda", "result": "approved", "waybill_no": "Y001"})
        self.assertFalse(bad_result["ok"])
        self.assertEqual("INVALID_AUDIT_RESULT", bad_result["error_code"])

        bad_platform = receipts_audit.run_once({"platform": "other", "result": "failed", "waybill_no": "Y001"})
        self.assertFalse(bad_platform["ok"])
        self.assertEqual("UNSUPPORTED_PLATFORM", bad_platform["error_code"])


if __name__ == "__main__":
    unittest.main()
