"""Regression tests for fail-fast Ronghui outgoing scans."""

from _tms_runtime_test_support import *  # noqa: F403

from agent.tms_runtime.scripts import scan_next


class ScanNextFailFastTests(unittest.TestCase):
    def test_run_once_forwards_selected_session_profile(self):
        with patch.object(scan_next, "run_flow", return_value={"ok": True}) as run_flow:
            scan_next.run_once(
                {
                    "session_profile": "ronghui_account_a",
                    "items": [
                        {"station_name": "测试站", "bill_code": "R00010001"},
                    ],
                }
            )

        self.assertEqual(run_flow.call_args.kwargs["session_profile"], "ronghui_account_a")

    def test_blank_session_profile_does_not_fall_back_to_default(self):
        with self.assertRaisesRegex(ValueError, "session_profile 不能为空"):
            scan_next.run_once(
                {
                    "session_profile": " ",
                    "items": [
                        {"station_name": "测试站", "bill_code": "R00010001"},
                    ],
                }
            )

    def test_selected_session_profile_reaches_browser_and_auth(self):
        source = inspect.getsource(scan_next._run_flow_impl)  # noqa: SLF001

        self.assertIn("profile=session_profile", source)
        self.assertRegex(
            source,
            r"(?s)launch_browser\(.*?profile=session_profile,.*?\)",
        )
        self.assertRegex(
            source,
            r"(?s)TMSBrowserAuth\(.*?profile=session_profile,.*?\)",
        )

    def test_login_context_waits_for_real_frame_user_info(self):
        frame = Mock()
        frame.evaluate.side_effect = [
            {
                "ok": False,
                "error": "login_context_unavailable",
                "context_status": "z_missing",
            },
            {"ok": True, "context_status": "ready"},
        ]

        result = scan_next._wait_login_context(frame, timeout_ms=1_000)  # noqa: SLF001

        self.assertTrue(result["ok"])
        self.assertEqual(frame.evaluate.call_count, 2)

    def test_login_context_timeout_keeps_precise_failure_state(self):
        frame = Mock()
        frame.evaluate.return_value = {
            "ok": False,
            "error": "missing_login_context_fields",
            "context_status": "required_fields_missing",
            "missing_fields": ["loginSiteCode"],
        }
        with (
            patch.object(scan_next.time, "time", side_effect=[0.0, 0.0, 1.0]),
            self.assertRaisesRegex(
                RuntimeError,
                "missing_login_context_fields.*missing_fields=loginSiteCode.*context_status=required_fields_missing",
            ),
        ):
            scan_next._wait_login_context(frame, timeout_ms=10)  # noqa: SLF001

    def test_login_context_is_checked_before_any_station_or_bill_write(self):
        source = inspect.getsource(scan_next._run_flow_impl)  # noqa: SLF001

        preflight = source.index('stage = "wait_login_context"')
        station_write = source.index('stage = "select_station"')
        bill_write = source.index('stage = "input_bill_code"')
        self.assertLess(preflight, station_write)
        self.assertLess(preflight, bill_write)

    def test_scan_user_fields_match_original_page_without_display_fallbacks(self):
        script = scan_next.MINI_ADD_BILL_CODE_SCRIPT

        self.assertIn('typeof $Z !== "undefined"', script)
        self.assertNotIn("window.$Z", script)
        self.assertIn("userInfo = $Z.user.getUserInfo();", script)
        self.assertIn('{ name: "iframe", scope: window }', script)
        self.assertIn('{ name: "top", scope: window.top }', script)
        self.assertIn('{ name: "parent", scope: window.parent }', script)
        self.assertIn('error: "ambiguous_login_context"', script)
        self.assertIn("signatures.size !== 1", script)
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

    def test_signed_items_are_skipped_without_stopping_later_items(self):
        source = inspect.getsource(scan_next._run_flow_impl)  # noqa: SLF001

        self.assertRegex(
            source,
            r"(?s)elif add_result\.get\(\"signed\"\):.*?skipped_signed_codes\.append\(bill\).*?continue",
        )
        self.assertRegex(
            source,
            r"(?s)if skipped:.*?skipped_signed_codes\.append\(bill\).*?continue",
        )
