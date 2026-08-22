import unittest
from unittest.mock import patch

from agent.tms_runtime.scripts import ronghui_split_complaint


class RonghuiSplitComplaintTests(unittest.TestCase):
    def test_exception_site_lookup_uses_real_ajax_headers(self):
        class Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append({"url": url, "kwargs": kwargs})
                headers = kwargs.get("headers") or {}
                if headers.get("X-Requested-With") != "XMLHttpRequest":
                    return Response({"success": False, "message": "非法的请求\r\n"})
                return Response(
                    {
                        "data": [
                            {"SITE_CODE": "7310009", "SITE_NAME": "长沙分拨直派部", "TYPE": "一级网点"},
                            {"SITE_CODE": "73101", "SITE_NAME": "长沙分拨", "TYPE": "分拨中心"},
                        ]
                    }
                )

        session = Session()
        row = ronghui_split_complaint._find_exception_site_row(
            session,
            "/dataQuery/findPageByCallId?id=FIND_SITE_INFO_ON_EXCEPTION_COMBOX6",
            "长沙分拨",
        )

        self.assertEqual("73101", row["SITE_CODE"])
        headers = session.calls[0]["kwargs"]["headers"]
        self.assertEqual("XMLHttpRequest", headers["X-Requested-With"])
        self.assertIn("widget/home", headers["Referer"])

    def test_duplicate_result_maps_to_duplicate_status(self):
        class Page:
            def close(self):
                return None

        class Browser:
            def __init__(self, *_args, **_kwargs):
                self.page = Page()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def new_page(self):
                return self.page

        with patch.object(
            ronghui_split_complaint, "_resolve_page_context", return_value=object()
        ), patch.object(
            ronghui_split_complaint, "HeadlessTMSBrowser", Browser
        ), patch.object(
            ronghui_split_complaint,
            "_run_single_bill",
            return_value={"bill_code": "R1", "saved": False, "skipped": True, "ok": True},
        ), patch.object(ronghui_split_complaint, "_cleanup_dir"):
            results = ronghui_split_complaint.upload_split_complaints(object(), ["R1"])
        self.assertEqual("duplicate", results[0]["status"])

    def test_submit_requires_authoritative_readback_after_save_ack(self):
        class Locator:
            @property
            def first(self):
                return self

            def wait_for(self, **_kwargs):
                return None

        class FormFrame:
            def locator(self, _selector):
                return Locator()

        class Response:
            status = 200

            @staticmethod
            def json():
                return {"success": True}

        expected = {
            "BILL_CODE": "R1",
            "CATEGORY": "违规操作类",
            "EXCEPTION_TYPE": "分批",
            "REMARK": "问题子单：R1-1",
            "EXCEPTIONSITE_SIDE_CODE": "site-code",
            "EXCEPTIONSITE_SIDE": "目标网点",
        }
        proof = {
            "source": "FIND_TAB_EXCEPTION_REGISTER_CS",
            "external_id": "exception-1",
        }
        with patch.object(
            ronghui_split_complaint,
            "_open_complaint_form_frame",
            return_value=FormFrame(),
        ), patch.object(
            ronghui_split_complaint,
            "_fill_complaint_form",
            return_value=expected,
        ), patch.object(
            ronghui_split_complaint,
            "_upload_grid_attachment",
            side_effect=["page1.png", "page2.png", "page2.png"],
        ), patch.object(
            ronghui_split_complaint,
            "_click_save_and_wait",
            return_value=("saved", Response()),
        ), patch.object(
            ronghui_split_complaint,
            "verify_complaint_registration",
            return_value=proof,
        ) as verify, patch.object(
            ronghui_split_complaint,
            "_dismiss_first_visible_text",
        ):
            result = ronghui_split_complaint._submit_complaint(
                object(),
                object(),
                bill_code="R1",
                accused_site="目标网点",
                problem_bills=["R1-1"],
                page1_path="page1.png",
                page2_path="page2.png",
                complaint_list_url="https://tms.ronghuiwl.com/widget/home",
            )

        self.assertTrue(result.saved)
        self.assertTrue(result.verified)
        self.assertEqual("exception-1", result.external_id)
        self.assertEqual(expected, verify.call_args.kwargs["expected"])

    def test_duplicate_warning_is_not_success_without_readback(self):
        class Locator:
            @property
            def first(self):
                return self

            def wait_for(self, **_kwargs):
                return None

        class FormFrame:
            def locator(self, _selector):
                return Locator()

        expected = {
            "BILL_CODE": "R1",
            "CATEGORY": "违规操作类",
            "EXCEPTION_TYPE": "分批",
            "REMARK": "问题子单：R1-1",
            "EXCEPTIONSITE_SIDE_CODE": "site-code",
            "EXCEPTIONSITE_SIDE": "目标网点",
        }
        with patch.object(
            ronghui_split_complaint,
            "_open_complaint_form_frame",
            return_value=FormFrame(),
        ), patch.object(
            ronghui_split_complaint,
            "_fill_complaint_form",
            return_value=expected,
        ), patch.object(
            ronghui_split_complaint,
            "_upload_grid_attachment",
            side_effect=["page1.png", "page2.png", "page2.png"],
        ), patch.object(
            ronghui_split_complaint,
            "_click_save_and_wait",
            return_value=("duplicate", None),
        ), patch.object(
            ronghui_split_complaint,
            "verify_complaint_registration",
            side_effect=RuntimeError("not found"),
        ), patch.object(
            ronghui_split_complaint,
            "_dismiss_first_visible_text",
        ):
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                ronghui_split_complaint._submit_complaint(
                    object(),
                    object(),
                    bill_code="R1",
                    accused_site="目标网点",
                    problem_bills=["R1-1"],
                    page1_path="page1.png",
                    page2_path="page2.png",
                )


if __name__ == "__main__":
    unittest.main()
