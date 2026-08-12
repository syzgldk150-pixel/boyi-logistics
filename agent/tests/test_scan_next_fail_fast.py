"""Regression tests for fail-fast Ronghui outgoing scans."""

from _tms_runtime_test_support import *  # noqa: F403

from agent.tms_runtime.scripts import scan_next


class ScanNextFailFastTests(unittest.TestCase):
    def test_scan_user_fields_match_original_page_without_display_fallbacks(self):
        script = scan_next.MINI_ADD_BILL_CODE_SCRIPT

        self.assertIn('formData.SCAN_MAN = loginUserName;', script)
        self.assertIn('formData.SCAN_MAN_CODE = loginUserAccount;', script)
        self.assertIn('"loginUserName"', script)
        self.assertIn('"loginUserAccount"', script)
        self.assertNotIn("headerUser", script)
        self.assertNotIn("headerSite", script)
        self.assertNotIn('|| "TMS"', script)
        self.assertNotIn('|| "73901"', script)

    def test_scan_user_code_is_rejected_before_grid_write_when_too_long(self):
        script = scan_next.MINI_ADD_BILL_CODE_SCRIPT

        length_check = script.index("scanManCodeUtf8Bytes > 20")
        grid_write = script.index("grid.addRow(formData)")
        self.assertLess(length_check, grid_write)
        self.assertIn('error: "scan_man_code_too_long"', script)
        self.assertNotIn("slice(0, 20)", script)
        self.assertNotIn("substring(0, 20)", script)

    def test_station_lookup_requires_one_exact_row_and_real_code(self):
        script = scan_next.MINI_SET_STATION_SCRIPT

        self.assertIn("exact.length !== 1", script)
        self.assertIn('error: "station_fields_missing"', script)
        self.assertNotIn(".includes(wanted)", script)
        self.assertNotIn("wanted.includes", script)
        self.assertNotIn("valueFor(row, codeKeys) || name", script)

    def test_invalid_scan_items_are_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "索引: 1"):
            scan_next._coerce_items(  # noqa: SLF001
                [
                    {"station_name": "测试站", "bill_code": "R00010001"},
                    {"station_name": "测试站"},
                ]
            )

    def test_station_api_failure_does_not_fall_back_to_typing(self):
        with (
            patch.object(scan_next, "_wait_xpath_visible"),
            patch.object(
                scan_next,
                "_set_station_by_mini_api",
                return_value={"ok": False, "error": "station_not_found"},
            ),
            patch.object(scan_next, "_fill_xpath") as fill_xpath,
        ):
            with self.assertRaisesRegex(RuntimeError, "station_not_found"):
                scan_next._select_station(object(), "测试站")  # noqa: SLF001

        fill_xpath.assert_not_called()

    def test_scan_flow_has_no_secondary_input_or_upload_path(self):
        source = inspect.getsource(scan_next._run_flow_impl)  # noqa: SLF001

        self.assertNotIn("回退输入法", source)
        self.assertNotIn("回退点击上传", source)
        self.assertNotIn("XPATH_UPLOAD", source)
