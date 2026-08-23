import sys
import unittest
from pathlib import Path
from unittest.mock import patch


AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from tools import feishu_cli_tool  # noqa: E402


class FeishuSearchRecordsToolTests(unittest.TestCase):
    def test_get_record_reads_one_exact_bitable_identity(self):
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append({"method": method, "path": path, "payload": payload, "timeout": timeout})
            return {
                "ok": True,
                "data": {
                    "record": {
                        "record_id": "rec-1",
                        "fields": {"运单编号": "WB-1", "签收状态": "未签收"},
                    }
                },
            }

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "get_record",
                {
                    "base_token": "base",
                    "table_id": "table",
                    "record_id": "rec-1",
                },
            )

        self.assertEqual("rec-1", result["data"]["record"]["record_id"])
        self.assertEqual(
            [
                {
                    "method": "GET",
                    "path": "/open-apis/bitable/v1/apps/base/tables/table/records/rec-1",
                    "payload": None,
                    "timeout": 30,
                }
            ],
            calls,
        )

    def test_search_records_uses_bitable_record_search_filter_without_full_scan(self):
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append({"method": method, "path": path, "payload": payload, "timeout": timeout})
            return {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "record_id": "rec-1",
                            "fields": {"运单编号": "981296115", "收货人": "飞书收货人"},
                        }
                    ],
                    "has_more": False,
                },
            }

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "search_records",
                {
                    "base_token": "base",
                    "table_id": "table",
                    "view_id": "view",
                    "field_names": ["运单编号", "收货人"],
                    "filter": {
                        "conjunction": "and",
                        "conditions": [{"field_name": "运单编号", "operator": "is", "value": ["981296115"]}],
                    },
                    "page_size": 1,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("rec-1", result["items"][0]["record_id"])
        self.assertEqual("POST", calls[0]["method"])
        self.assertIn("/open-apis/bitable/v1/apps/base/tables/table/records/search?", calls[0]["path"])
        self.assertIn("page_size=1", calls[0]["path"])
        self.assertEqual(["运单编号", "收货人"], calls[0]["payload"]["field_names"])
        self.assertEqual(
            {"conjunction": "and", "conditions": [{"field_name": "运单编号", "operator": "is", "value": ["981296115"]}]},
            calls[0]["payload"]["filter"],
        )


if __name__ == "__main__":
    unittest.main()
