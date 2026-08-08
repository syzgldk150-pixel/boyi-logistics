import threading
import time
import unittest
from unittest.mock import patch

from agent.tms_runtime.scripts import receipts_sync


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeRonghuiAttachmentSession:
    def __init__(self, process_rows, attachment_rows_by_call_id):
        self.process_rows = process_rows
        self.attachment_rows_by_call_id = attachment_rows_by_call_id
        self.get_calls = []
        self.post_calls = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.get_calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout})
        return _FakeResponse(self.process_rows)

    def post(self, url, *, params=None, data=None, headers=None, timeout=None):
        self.post_calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "data": dict(data or {}),
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        call_id = str((params or {}).get("id") or "")
        return _FakeResponse(self.attachment_rows_by_call_id.get(call_id, []))


class ReceiptSyncTests(unittest.TestCase):
    def test_ronghui_process_call_id_uses_direction_specific_page_contract(self):
        self.assertEqual("FIND_SEND_RETURN_PROCESS", receipts_sync._ronghui_process_call_id("send"))
        self.assertEqual("FIND_DISP_RETURN_PROCESS", receipts_sync._ronghui_process_call_id("receive"))

    def test_ronghui_payload_uses_process_page_date_field(self):
        payload = receipts_sync._ronghui_payload(
            {"date_from": "2026-06-01", "date_to": "2026-06-03"},
            direction="send",
            page_index=0,
            page_size=100,
            login_site_code="7390004",
        )

        self.assertIn("SEARCH_DATE_RANGE", payload)
        self.assertNotIn("SEARCH_DATE_RANGE1", payload)
        self.assertEqual("7390004", payload["LOGIN_SITE_CODE"])
        self.assertEqual("FIND_SEND_RETURN_PROCESS", receipts_sync._ronghui_process_call_id("send"))

    def test_ronghui_record_attachments_fetches_system_pic_scan_files(self):
        session = _FakeRonghuiAttachmentSession(
            [
                {
                    "GUID": "process-guid",
                    "BILL_CODE": "2003441429",
                    "PROCESS_TYPE": "派方登记",
                    "DATA_FROM": "系统",
                    "REPLY_TIME": "2026-06-03 14:32:37",
                }
            ],
            {
                "FIND_TAB_PIC_SCAN_ALL": [
                    {
                        "FILE_PATH": "/unauth/download/group1/M00/C2/43/demo.jpg",
                        "FILE_NAME": "",
                        "CREATE_DATE": "2026-06-03 14:32:37",
                    }
                ]
            },
        )

        attachments = receipts_sync._fetch_ronghui_record_attachments(
            session,
            {"BILL_CODE": "2003441429", "R_BILLCODE": "hd6497221"},
            headers={},
            timeout_sec=30,
        )

        self.assertEqual(1, len(attachments))
        self.assertEqual("FIND_TAB_PIC_SCAN_ALL", session.post_calls[0]["params"]["id"])
        self.assertEqual("2003441429", session.post_calls[0]["data"]["BILL_CODE"])
        self.assertEqual("6", session.post_calls[0]["data"]["PIC_TYPE"])
        self.assertEqual("demo.jpg", attachments[0]["display_name"])
        self.assertEqual("https://tms.ronghuiwl.com/unauth/download/group1/M00/C2/43/demo.jpg", attachments[0]["source_url"])

    def test_ronghui_record_attachments_fetches_manual_process_files(self):
        session = _FakeRonghuiAttachmentSession(
            [
                {
                    "GUID": "manual-process-guid",
                    "BILL_CODE": "2003441429",
                    "PROCESS_TYPE": "寄方登记",
                    "DATA_FROM": "人工",
                    "REPLY_TIME": "2026-06-03 14:32:37",
                }
            ],
            {
                "FIND_TAB_PROCESS_RECORD_PATH": [
                    {
                        "FILE_PATH": "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/demo.jpg",
                        "FILE_NAME": "签收回单.jpg",
                    }
                ]
            },
        )

        attachments = receipts_sync._fetch_ronghui_record_attachments(
            session,
            {"BILL_CODE": "2003441429"},
            headers={},
            timeout_sec=30,
        )

        self.assertEqual(1, len(attachments))
        self.assertEqual("FIND_TAB_PROCESS_RECORD_PATH", session.post_calls[0]["params"]["id"])
        self.assertEqual("manual-process-guid", session.post_calls[0]["data"]["PROCESS_RECORD_ID"])
        self.assertEqual("签收回单.jpg", attachments[0]["display_name"])
        self.assertEqual("https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/demo.jpg", attachments[0]["source_url"])

    def test_ronghui_normalize_uses_returnbill_status_as_audit_status(self):
        record = receipts_sync._normalize_ronghui_row(
            {
                "BILL_CODE": "2003441427",
                "R_BILLCODE": "hd6495660",
                "RETURNBILL_STATUS": "待客方审核",
                "AUDIT_TIME": "2026-06-05 10:00:01",
            },
            direction="send",
        )

        self.assertEqual("待客方审核", record["audit_status"])
        self.assertEqual("2026-06-05 10:00:01", record["updated_at"])

    def test_ronghui_normalize_uses_returnbill_status_before_explicit_audit_status(self):
        record = receipts_sync._normalize_ronghui_row(
            {
                "BILL_CODE": "2003441427",
                "R_BILLCODE": "hd6495660",
                "RETURNBILL_STATUS": "已返单",
                "AUDIT_STATUS_TEXT": "审核通过",
                "AUDIT_TIME": "2026-06-05 10:00:01",
            },
            direction="send",
        )

        self.assertEqual("已返单", record["audit_status"])

    def test_ronghui_normalize_keeps_missing_returnbill_status_empty(self):
        record = receipts_sync._normalize_ronghui_row(
            {
                "BILL_CODE": "2003441427",
                "R_BILLCODE": "hd6495660",
            },
            direction="send",
        )

        self.assertEqual("", record["audit_status"])

    def test_yunda_datagrid_url_is_selected_from_receipt_grid_region(self):
        html = """
        <script>
        $.ajax({url: "/ky_inms/public/index.php/printer/getTemplate/.html"});
        $("#dg").datagrid({
          url: "/ky_inms/public/index.php/business/waybill/mailing/getList.html"
        });
        </script>
        """

        result = receipts_sync._select_yunda_datagrid_url(
            html,
            "https://kyinms.yunda56.com/ky_inms/public/index.php/business/waybill/mailing/index.html",
            direction="send",
        )

        self.assertEqual(
            "https://kyinms.yunda56.com/ky_inms/public/index.php/business/waybill/mailing/getList.html",
            result,
        )

    def test_yunda_datagrid_url_prefers_real_list_endpoint_over_detail_actions(self):
        html = """
        <table id="dg"></table>
        <script>
        $("#dg_table").on("click", ".row-detail", function () {
          $.ajax({url: "/ky_inms/public/index.php/business/waybill/mailing/detail.html"});
        });
        form.on('submit(formInfo)', function(data){
          $('#dg').datagrid({
            url: "/ky_inms/public/index.php/business/waybill/mailing/list.html",
            queryParams: data.field
          });
        });
        </script>
        """

        result = receipts_sync._select_yunda_datagrid_url(
            html,
            "https://kyinms.yunda56.com/ky_inms/public/index.php/business/waybill/mailing/index.html",
            direction="send",
        )

        self.assertEqual(
            "https://kyinms.yunda56.com/ky_inms/public/index.php/business/waybill/mailing/list.html",
            result,
        )

    def test_yunda_payload_uses_original_single_waybill_query_field(self):
        payload = receipts_sync._yunda_payload(
            {"q": "980703396", "date_from": "2026-06-01", "date_to": "2026-06-04"},
            page=1,
            rows=10,
        )

        self.assertEqual(["980703396"], payload["LogisticsId"])
        self.assertNotIn("SearchValue", payload)
        self.assertNotIn("start_date", payload)

    def test_yunda_payload_uses_original_date_form_fields_without_waybill(self):
        payload = receipts_sync._yunda_payload(
            {"date_from": "2026-06-01", "date_to": "2026-06-04"},
            page=1,
            rows=10,
        )

        self.assertEqual("0", payload["timeType"])
        self.assertEqual("2026-06-01", payload["start_date"])
        self.assertEqual("00:00:00", payload["start_time"])
        self.assertEqual("2026-06-04", payload["end_date"])
        self.assertEqual("23:59:59", payload["end_time"])
        self.assertEqual("3", payload["Return_Logistics_Status"])
        self.assertEqual("all", payload["Return_Adjunct_Addr"])
        self.assertEqual("all", payload["Return_Sign_Adjunct_Addr"])
        self.assertEqual("all", payload["Is_Replace"])

    def test_yunda_normalize_extracts_signed_receipt_attachment_addresses(self):
        record = receipts_sync._normalize_yunda_row(
            {
                "Id": 380812386,
                "Logistics_Id": "980703396",
                "Return_Logistics_Id": "1000980703396",
                "Return_Express_Id": "",
                "Return_Status": "目的分拨已发",
                "Audit_Status_Name": "审核通过",
                "Sign_Status_Name": "否",
                "Mail_Date": "2026-06-02 14:02:06",
                "Return_Adjunct_Count": 0,
                "Return_Sign_Adjunct_Count": 1,
                "Return_Sign_Adjunct_Addr1": "https://kyinms.yunda56.com//ky_inms/public/download/appSignReturnImg/logistics/20260604/13/demo.jpg",
            },
            direction="send",
        )

        self.assertEqual("980703396", record["waybill_no"])
        self.assertEqual("1000980703396", record["receipt_no"])
        self.assertEqual("", record["return_waybill_no"])
        self.assertEqual("目的分拨已发", record["receipt_status"])
        self.assertEqual("审核通过", record["audit_status"])
        self.assertEqual("已上传", record["photo_status"])
        self.assertEqual(1, record["photo_count"])
        self.assertEqual("已签电子回单", record["attachments"][0]["display_name"])
        self.assertEqual(
            "https://kyinms.yunda56.com/ky_inms/public/download/appSignReturnImg/logistics/20260604/13/demo.jpg",
            record["attachments"][0]["source_url"],
        )

    def test_ronghui_attachment_rows_normalize_bare_obs_host(self):
        attachments = receipts_sync._ronghui_attachment_rows_to_attachments(
            [
                {
                    "FILE_PATH": "rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg",
                    "FILE_NAME": "",
                }
            ],
            attachment_type=receipts_sync.RONGHUI_RECORD_ATTACHMENT_CALL_ID,
        )

        self.assertEqual(1, len(attachments))
        self.assertEqual(
            "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg",
            attachments[0]["source_url"],
        )

    def test_ronghui_urls_from_value_extracts_bare_obs_host(self):
        urls = receipts_sync._urls_from_value(
            "renderReplyFiles('rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg')",
            origin=receipts_sync.RONGHUI_ORIGIN,
        )

        self.assertEqual(
            ["https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg"],
            urls,
        )

    def test_yunda_normalize_accepts_underscore_waybill_fields(self):
        record = receipts_sync._normalize_yunda_row(
            {
                "Logistics_Id": "980972387",
                "Return_Logistics_Id": "HD123",
                "Return_Express_Id": "EXP123",
                "Sign_State": "未签收",
                "Scan_State": "录单",
                "Sign_Time": "2026-06-03 10:00:00",
            },
            direction="receive",
        )

        self.assertEqual("980972387", record["waybill_no"])
        self.assertEqual("HD123", record["receipt_no"])
        self.assertEqual("EXP123", record["return_waybill_no"])
        self.assertEqual("未签收", record["receipt_status"])
        self.assertEqual("2026-06-03 10:00:00", record["remote_updated_at"])

    def test_run_once_skips_records_without_waybill_or_receipt_key(self):
        with patch.object(
            receipts_sync,
            "_fetch_source",
            return_value=("yunda", "send", [{"platform": "yunda", "direction": "send", "waybill_no": "", "receipt_no": ""}], {"fetched": 1}, []),
        ):
            result = receipts_sync.run_once({"platform": "yunda", "direction": "send"})

        self.assertEqual([], result["records"])

    def test_run_once_fetches_all_sources_concurrently(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_fetch_source(params, *, platform, direction):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return (
                    platform,
                    direction,
                    [
                        {
                            "platform": platform,
                            "direction": direction,
                            "waybill_no": f"{platform}-{direction}",
                            "receipt_no": "HD",
                        }
                    ],
                    {"fetched": 1, "total": 1, "max_pages": 5, "truncated": False},
                    [],
                )
            finally:
                with lock:
                    active -= 1

        with patch.object(receipts_sync, "_fetch_source", side_effect=fake_fetch_source):
            result = receipts_sync.run_once({"platform": "all", "direction": "all", "source_workers": 4})

        self.assertTrue(result["ok"])
        self.assertEqual(3, len(result["records"]))
        self.assertNotIn(
            ("yunda", "receive"),
            {(record["platform"], record["direction"]) for record in result["records"]},
        )
        self.assertGreater(max_active, 1)

    def test_run_once_returns_warning_when_source_hits_page_cap(self):
        def fake_fetch_source(params, *, platform, direction):
            return (
                platform,
                direction,
                [],
                {"fetched": 500, "total": 800, "max_pages": 5, "truncated": True},
                [],
            )

        with patch.object(receipts_sync, "_fetch_source", side_effect=fake_fetch_source):
            result = receipts_sync.run_once({"platform": "ronghui", "direction": "send"})

        self.assertTrue(result["ok"])
        self.assertIn("ronghui/send reached max_pages=5", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
