import unittest
from unittest.mock import patch

from tools import split_pending_snapshot as snapshot


def arrival_row(bill_code: str, expected: int, arrived: int) -> list[object]:
    return [
        bill_code,
        "测试货物",
        "纸箱",
        "派送",
        expected,
        "",
        10,
        1,
        "",
        "邵阳大祥S站",
        "收件人",
        "13800000000",
        "测试地址",
        10,
        1,
        20,
        "现付",
        0,
        arrived,
    ]


class SplitPendingSnapshotTests(unittest.TestCase):
    def test_refresh_traces_complete_partial_and_not_arrived_rows(self):
        database_calls = []
        sheet_calls = []
        values = [
            snapshot.TARGET_HEADERS,
            arrival_row("R_COMPLETE", 2, 2),
            arrival_row("R_PARTIAL", 5, 3),
            arrival_row("R_NOT_ARRIVED", 4, 0),
        ]

        result = snapshot.refresh_snapshot(
            values,
            database_writer=lambda rows: database_calls.append(rows) or {"ok": True, "rows": len(rows)},
            sheet_writer=lambda rows: sheet_calls.append(rows) or {"ok": True, "rows": len(rows)},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(3, result["source_rows"])
        self.assertEqual(1, result["complete_rows"])
        self.assertEqual(2, result["rows"])
        self.assertEqual({"少货/分批": 1, "有发未到": 1}, result["type_counts"])
        self.assertEqual(["R_PARTIAL", "R_NOT_ARRIVED"], [row["bill_code"] for row in database_calls[0]])
        self.assertEqual(["R_PARTIAL", "R_NOT_ARRIVED"], [row["bill_code"] for row in sheet_calls[0]])
        self.assertEqual(
            {
                "expected_min": 4,
                "expected_max": 5,
                "arrived_min": 0,
                "arrived_max": 3,
                "pending_min": 2,
                "pending_max": 4,
            },
            result["quantity_summary"],
        )

    def test_all_complete_replaces_database_and_sheet_with_empty_snapshot(self):
        database_calls = []
        sheet_calls = []

        result = snapshot.refresh_snapshot(
            [snapshot.TARGET_HEADERS, arrival_row("R_COMPLETE", 2, 2)],
            database_writer=lambda rows: database_calls.append(rows) or {"ok": True, "rows": len(rows)},
            sheet_writer=lambda rows: sheet_calls.append(rows) or {"ok": True, "rows": len(rows)},
        )

        self.assertEqual(0, result["rows"])
        self.assertEqual(1, result["complete_rows"])
        self.assertEqual([[]], database_calls)
        self.assertEqual([[]], sheet_calls)

    def test_dry_run_validates_but_does_not_write(self):
        def unexpected_writer(_rows):
            raise AssertionError("dry-run must not write")

        result = snapshot.refresh_snapshot(
            [snapshot.TARGET_HEADERS, arrival_row("R_PARTIAL", 5, 3)],
            dry_run=True,
            database_writer=unexpected_writer,
            sheet_writer=unexpected_writer,
        )

        self.assertTrue(result["skipped"])
        self.assertEqual("dry_run", result["reason"])
        self.assertEqual(1, result["rows"])

    def test_duplicate_bill_fails_before_any_write(self):
        values = [
            snapshot.TARGET_HEADERS,
            arrival_row("R_DUPLICATE", 5, 3),
            arrival_row("R_DUPLICATE", 5, 4),
        ]
        with self.assertRaisesRegex(ValueError, "重复运单号"):
            snapshot.refresh_snapshot(values)

    def test_zero_candidates_clears_old_rows_and_writes_header(self):
        operations = []

        def fake_feishu_operation(action, params):
            operations.append((action, params))
            return {"ok": True}

        resource = {
            "spreadsheet_token": snapshot.EXPECTED_SPREADSHEET_TOKEN,
            "sheet_id": snapshot.EXPECTED_TARGET_SHEET_ID,
            "range": f"{snapshot.EXPECTED_TARGET_SHEET_ID}!A1:S1",
            "clear_range": f"{snapshot.EXPECTED_TARGET_SHEET_ID}!A2:S5000",
        }
        with (
            patch.object(snapshot, "get_required_resource", return_value=resource),
            patch.object(snapshot, "feishu_operation", side_effect=fake_feishu_operation),
        ):
            result = snapshot.sync_target_sheet([])

        self.assertEqual(0, result["rows"])
        self.assertEqual(["clear_sheet", "write_sheet"], [action for action, _ in operations])
        self.assertEqual(
            f"{snapshot.EXPECTED_TARGET_SHEET_ID}!A1:S1",
            operations[1][1]["range"],
        )
        self.assertEqual([snapshot.TARGET_HEADERS], operations[1][1]["values"])


if __name__ == "__main__":
    unittest.main()
