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


if __name__ == "__main__":
    unittest.main()
