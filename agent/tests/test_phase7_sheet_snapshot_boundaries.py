"""Boundary tests for Phase 7 spreadsheet snapshot projection helpers."""

from _tms_runtime_test_support import *  # noqa: F403


class Phase7SheetSnapshotBoundaryTests(unittest.TestCase):
    def test_feishu_clear_sheet_caps_end_row_to_sheet_row_count(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "abc123",
                                "title": "Sheet1",
                                "grid_properties": {"row_count": 200},
                            },
                        ],
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:Y5000",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("abc123!A2:Y200", result["range"])
        self.assertEqual("DELETE", calls[1][0])
        self.assertEqual(2, calls[1][2]["dimension"]["startIndex"])
        self.assertEqual(200, calls[1][2]["dimension"]["endIndex"])
        self.assertEqual(2, len(calls))

    def test_feishu_clear_sheet_caps_end_row_from_camel_case_grid_properties(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "Sheet1",
                                "title": "Sheet1",
                                "gridProperties": {"rowCount": 10},
                            },
                        ],
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:Y5000",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("Sheet1!A2:Y10", result["range"])
        self.assertEqual(10, calls[1][2]["dimension"]["endIndex"])
        self.assertEqual(2, len(calls))

    def test_sheet_snapshot_deletes_then_adds_rows_before_writing(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:Y10",
            "clear_range": "Sheet1!A2:Y5000",
        }
        calls = []
        query_count = 0

        def fake_call_open_api(method, path, payload=None, timeout=30):
            nonlocal query_count
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                query_count += 1
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "4103ec",
                                "title": "Sheet1",
                                "gridProperties": {"rowCount": 5 if query_count == 1 else 1},
                            }
                        ]
                    },
                }
            return {"code": 0, "data": {}}

        values = [[f"r{row}c{col}" for col in range(25)] for row in range(9)]
        with (
            patch("tools.phase7_sync_common.get_workflow_resource", return_value=resource),
            patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api),
        ):
            result = phase7_sync_common.sync_sheet_snapshot("phase7.yunda_send_waybills_sheet", values, {})

        self.assertTrue(result["ok"])
        self.assertEqual(("DELETE", "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range"), calls[1][:2])
        self.assertEqual(("POST", "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range"), calls[3][:2])
        self.assertEqual({"sheetId": "4103ec", "majorDimension": "ROWS", "length": 9}, calls[3][2]["dimension"])
        self.assertEqual(("PUT", "/open-apis/sheets/v2/spreadsheets/sheet-token/values"), calls[4][:2])

    def test_feishu_clear_sheet_skips_when_range_starts_after_row_count(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "abc123",
                                "title": "Sheet1",
                                "grid_properties": {"row_count": 1},
                            },
                        ],
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:Y5000",
                },
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual("abc123!A2:Y5000", result["range"])
        self.assertEqual(1, len(calls))
