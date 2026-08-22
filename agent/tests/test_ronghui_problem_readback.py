import unittest
import hashlib
import json

from agent.tms_runtime.scripts import ronghui_problem_upload
from agent.tms_runtime.scripts import ronghui_split_complaint


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        return _Response(self._payloads.pop(0))


def _problem_row(**overrides):
    row = {
        "GUID": "guid-1",
        "BILL_CODE": "R1",
        "TYPE": "有发未到",
        "OWNER_PROBELM_TYPE": "通知类（不顺延时效）",
        "PROBLEM_CAUSE": "有发未到",
        "SEND_SITE_CODE": "notice-code",
        "SEND_SITE": "notice-site",
        "REGISTER_SITE_CODE": "login-code",
        "REGISTER_SITE": "login-site",
        "REGISTER_MAN_CODE": "emp-code",
        "REGISTER_MAN": "emp-name",
        "REGISTER_SAVE_DATE": "2026-08-15T01:00:00Z",
    }
    row.update(overrides)
    return row


def _complaint_row(**overrides):
    row = {
        "BILL_CODE": "R1",
        "EXCEPTION_ID": "exception-1",
        "STATUS": "待处理",
        "EXCEPTION_DATE": "2026-08-15T01:00:00Z",
        "CATEGORY": "违规操作类",
        "EXCEPTION_TYPE": "分批",
        "REMARK": "问题子单：R1-1",
        "EXCEPTIONSITE_SIDE_CODE": "site-code",
        "EXCEPTIONSITE_SIDE": "目标网点",
        "EXCEPTION_SITE": "登记网点",
        "COMPLAINANT_NAME": "登记人",
        "EXCEPTION_DEPT_SIDE": "",
        "EXCEPTION_MAN_SIDE": "",
    }
    row.update(overrides)
    return row


class RonghuiProblemReadbackTests(unittest.TestCase):
    def test_page_query_reads_every_declared_row_with_exact_protocol(self):
        session = _Session(
            [
                {"data": [_problem_row(GUID="guid-1")], "total": 2},
                {"data": [_problem_row(GUID="guid-2")], "total": 2},
            ]
        )

        rows = ronghui_problem_upload.query_page_rows(
            session,
            call_id="FIND_PROBLEM_REGISTER_LIST",
            data={"BILL_CODE": "R1", "LOGIN_SITE_CODE": "login-code"},
            page_context={"url": "https://tms.ronghuiwl.com/widget/home"},
            page_size=1,
            max_rows=10,
        )

        self.assertEqual(["guid-1", "guid-2"], [row["GUID"] for row in rows])
        self.assertEqual([0, 1], [call["kwargs"]["data"]["pageIndex"] for call in session.calls])
        self.assertTrue(
            all(call["kwargs"]["data"]["pageSize"] == 1 for call in session.calls)
        )
        self.assertTrue(
            all(call["kwargs"]["data"]["BILL_CODE"] == "R1" for call in session.calls)
        )
        self.assertTrue(
            session.calls[0]["url"].endswith(
                "/dataQuery/findPageByCallId?id=FIND_PROBLEM_REGISTER_LIST"
            )
        )

    def test_page_query_rejects_missing_total(self):
        session = _Session([{"data": []}])

        with self.assertRaisesRegex(RuntimeError, "分页总数缺失"):
            ronghui_problem_upload.query_page_rows(
                session,
                call_id="FIND_PROBLEM_REGISTER_LIST",
                data={"BILL_CODE": "R1"},
                page_context={"url": "https://tms.ronghuiwl.com/widget/home"},
            )

    def test_problem_match_requires_one_exact_complete_row(self):
        proof = ronghui_problem_upload.match_unique_registered_problem_item(
            [_problem_row()],
            expected=_problem_row(REGISTER_SAVE_DATE="write-time-not-compared"),
        )

        self.assertEqual("guid-1", proof["external_id"])
        self.assertEqual("FIND_PROBLEM_REGISTER_LIST", proof["source"])

    def test_problem_match_fails_for_missing_multiple_and_incomplete_rows(self):
        with self.assertRaisesRegex(RuntimeError, "未找到目标运单"):
            ronghui_problem_upload.match_unique_registered_problem_item(
                [],
                expected=_problem_row(),
            )
        with self.assertRaisesRegex(RuntimeError, "2 条完全一致记录"):
            ronghui_problem_upload.match_unique_registered_problem_item(
                [_problem_row(), _problem_row()],
                expected=_problem_row(),
            )
        incomplete = _problem_row()
        incomplete.pop("REGISTER_SAVE_DATE")
        with self.assertRaisesRegex(RuntimeError, "缺少关键字段"):
            ronghui_problem_upload.match_unique_registered_problem_item(
                [incomplete],
                expected=_problem_row(),
            )

    def test_problem_fingerprint_requires_exact_external_identity_and_fields(self):
        cause_hash = hashlib.sha256("有发未到".encode("utf-8")).hexdigest()
        proof = ronghui_problem_upload.match_unique_registered_problem_fingerprint(
            [_problem_row()],
            bill_code="R1",
            external_id="guid-1",
            problem_type="有发未到",
            problem_owner_type="通知类（不顺延时效）",
            problem_cause_sha256=cause_hash,
        )
        self.assertEqual(cause_hash, proof["problem_cause_sha256"])
        self.assertEqual("guid-1", proof["external_id"])
        self.assertIsNone(
            ronghui_problem_upload.find_unique_registered_problem_fingerprint(
                [_problem_row()],
                bill_code="R1",
                problem_type="少货/分批",
                problem_owner_type="交接异常",
                problem_cause_sha256=cause_hash,
            )
        )

    def test_upload_success_requires_authoritative_readback(self):
        observed = {}

        def verify(_session, *, expected, **_kwargs):
            observed.update(expected)
            return {
                "source": "FIND_PROBLEM_REGISTER_LIST",
                "external_id": expected["GUID"],
            }

        result = ronghui_problem_upload.upload_problem_item(
            object(),
            record={
                "bill_code": "R1",
                "problem_type": "有发未到",
                "problem_owner_type": "通知类（不顺延时效）",
                "problem_cause": "有发未到",
            },
            page_context={"url": "https://tms.ronghuiwl.com/widget/home"},
            login_context={
                "site_code": "login-code",
                "site_name": "login-site",
                "emp_code": "emp-code",
                "emp_name": "emp-name",
                "dept_name": "dept",
            },
            helpers={
                "fetch_bill_info": lambda *_args: {"DESTINATION": "destination"},
                "resolve_notice_site": lambda *_args: ("notice-code", "notice-site"),
                "fetch_guid": lambda *_args: "guid-1",
                "save_tables": lambda *_args: {"success": True, "message": "ok"},
                "verify_registered_problem_item": verify,
            },
        )

        self.assertTrue(result["saved"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["save_acknowledged"])
        self.assertEqual("guid-1", observed["GUID"])

    def test_upload_reconciles_uncertain_ack_only_with_exact_readback(self):
        def failed_save(*_args):
            raise TimeoutError("response unavailable")

        result = ronghui_problem_upload.upload_problem_item(
            object(),
            record={
                "bill_code": "R1",
                "problem_type": "有发未到",
                "problem_owner_type": "通知类（不顺延时效）",
                "problem_cause": "有发未到",
            },
            page_context={"url": "https://tms.ronghuiwl.com/widget/home"},
            login_context={
                "site_code": "login-code",
                "site_name": "login-site",
                "emp_code": "emp-code",
                "emp_name": "emp-name",
                "dept_name": "dept",
            },
            helpers={
                "fetch_bill_info": lambda *_args: {"DESTINATION": "destination"},
                "resolve_notice_site": lambda *_args: ("notice-code", "notice-site"),
                "fetch_guid": lambda *_args: "guid-1",
                "save_tables": failed_save,
                "verify_registered_problem_item": lambda _session, **_kwargs: {
                    "source": "FIND_PROBLEM_REGISTER_LIST",
                    "external_id": "guid-1",
                },
            },
        )

        self.assertTrue(result["verified"])
        self.assertFalse(result["save_acknowledged"])

    def test_upload_fails_when_ack_is_not_backed_by_readback(self):
        with self.assertRaisesRegex(RuntimeError, "权威列表未找到"):
            ronghui_problem_upload.upload_problem_item(
                object(),
                record={
                    "bill_code": "R1",
                    "problem_type": "有发未到",
                    "problem_owner_type": "通知类（不顺延时效）",
                    "problem_cause": "有发未到",
                },
                page_context={"url": "https://tms.ronghuiwl.com/widget/home"},
                login_context={
                    "site_code": "login-code",
                    "site_name": "login-site",
                    "emp_code": "emp-code",
                    "emp_name": "emp-name",
                    "dept_name": "dept",
                },
                helpers={
                    "fetch_bill_info": lambda *_args: {"DESTINATION": "destination"},
                    "resolve_notice_site": lambda *_args: ("notice-code", "notice-site"),
                    "fetch_guid": lambda *_args: "guid-1",
                    "save_tables": lambda *_args: {"success": True},
                    "verify_registered_problem_item": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        RuntimeError("not found")
                    ),
                },
            )


class RonghuiComplaintReadbackTests(unittest.TestCase):
    def test_complaint_query_uses_exact_bill_and_login_site(self):
        session = _Session([{"rows": [_complaint_row()], "total": 1}])

        rows = ronghui_split_complaint.query_registered_complaints(
            session,
            bill_code="R1",
            complaint_list_url=(
                "https://tms.ronghuiwl.com/widget/home?authenticationKey=auth&pageId=page"
            ),
            login_site_code="login-code",
        )

        self.assertEqual("exception-1", rows[0]["EXCEPTION_ID"])
        request = session.calls[0]["kwargs"]
        self.assertEqual("R1", request["data"]["BILL_CODE"])
        self.assertEqual("login-code", request["data"]["LOGIN_SITE_CODE"])
        self.assertEqual("auth", request["headers"]["authenticationKey"])
        self.assertEqual("page", request["headers"]["pageId"])

    def test_complaint_match_requires_unique_complete_exact_row(self):
        expected = {
            key: _complaint_row()[key]
            for key in (
                "BILL_CODE",
                "CATEGORY",
                "EXCEPTION_TYPE",
                "REMARK",
                "EXCEPTIONSITE_SIDE_CODE",
                "EXCEPTIONSITE_SIDE",
            )
        }
        proof = ronghui_split_complaint.match_unique_complaint_registration(
            [_complaint_row()],
            expected=expected,
        )
        self.assertEqual("exception-1", proof["external_id"])
        expected_hash = hashlib.sha256(
            json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected_hash, proof["plan_sha256"])
        fingerprint_proof = (
            ronghui_split_complaint.match_unique_complaint_fingerprint(
                [_complaint_row()],
                bill_code="R1",
                external_id="exception-1",
                plan_sha256=expected_hash,
            )
        )
        self.assertEqual(expected_hash, fingerprint_proof["plan_sha256"])

        with self.assertRaisesRegex(RuntimeError, "2 exact matches"):
            ronghui_split_complaint.match_unique_complaint_registration(
                [_complaint_row(), _complaint_row()],
                expected=expected,
            )
        incomplete = _complaint_row()
        incomplete.pop("STATUS")
        with self.assertRaisesRegex(RuntimeError, "missing critical fields"):
            ronghui_split_complaint.match_unique_complaint_registration(
                [incomplete],
                expected=expected,
            )


if __name__ == "__main__":
    unittest.main()
