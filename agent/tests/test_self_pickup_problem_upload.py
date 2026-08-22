import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "agent" / "tms_runtime" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import self_pickup_problem_upload
from agent.tms_runtime.scripts import ronghui_problem_upload


class SelfPickupProblemUploadTests(unittest.TestCase):
    def _base_bill_info(self):
        return {
            "DESTINATION": "destination",
            "REGISTER_SITE_CODE": "notice_code",
            "REGISTER_SITE": "notice_site",
            "SEND_SITE_CODE": "send_code",
            "SEND_SITE": "send_site",
        }

    def _login_context(self):
        return {
            "site_code": "login_code",
            "site_name": "login_site",
            "emp_code": "emp_code",
            "emp_name": "emp_name",
            "user_id": "user_id",
            "user_name": "user_name",
            "dept_name": "dept",
        }

    def test_dry_run_collects_self_pickup_and_daxiang_s_self_pickup_sources(self):
        values = [
            ["运单编号", "目的站点", "派送方式", "累计到货件数", "货物件数"],
            ["R_SELF", "邵阳自提部", "派送", "2", "2"],
            ["R_DX_PICK", "邵阳大祥S站", "自提", "1", "1"],
            ["R_DX_SEND", "邵阳大祥S站", "派送", "1", "1"],
        ]

        with patch.object(self_pickup_problem_upload, "_tenant_access_token", return_value="token"), \
            patch.object(self_pickup_problem_upload, "_resolve_sheet_id", return_value=("sheet1", "每日到货表")), \
            patch.object(self_pickup_problem_upload, "_read_sheet_values", return_value=values):
            result = self_pickup_problem_upload.run_once({"dry_run": True})

        self.assertEqual(2, result["candidate_count"])
        source_summaries = {item["source_id"]: item for item in result["source_summaries"]}
        self.assertEqual(
            ["R_SELF"],
            [item["bill_code"] for item in source_summaries["self_pickup_department"]["candidates"]],
        )
        self.assertEqual(
            ["R_DX_PICK"],
            [item["bill_code"] for item in source_summaries["daxiang_s_self_pickup"]["candidates"]],
        )
        self.assertEqual("ronghui_self_pickup_problem", source_summaries["self_pickup_department"]["account_id"])
        self.assertEqual("self_pickup_problem_upload", source_summaries["self_pickup_department"]["session_profile"])
        self.assertEqual("ronghui_daxiang_s", source_summaries["daxiang_s_self_pickup"]["account_id"])
        self.assertEqual("daxiang_s", source_summaries["daxiang_s_self_pickup"]["session_profile"])

    def test_dry_run_resolves_bound_role_accounts_to_session_profiles(self):
        values = [
            ["运单编号", "目的站点", "派送方式", "累计到货件数", "货物件数"],
            ["R_SELF", "邵阳自提部", "派送", "2", "2"],
            ["R_DX_PICK", "邵阳大祥S站", "自提", "1", "1"],
        ]

        class FakeAccountManager:
            def resolve_role_account_params(self, params, *, account_field, output_session_profile_field, **kwargs):
                result = dict(params)
                if account_field == "account_id":
                    result[output_session_profile_field] = "custom_self_profile"
                elif account_field == "daxiang_s_account_id":
                    result[output_session_profile_field] = "custom_daxiang_profile"
                return result

        with patch.object(self_pickup_problem_upload, "_tenant_access_token", return_value="token"), \
            patch.object(self_pickup_problem_upload, "_resolve_sheet_id", return_value=("sheet1", "每日到货表")), \
            patch.object(self_pickup_problem_upload, "_read_sheet_values", return_value=values), \
            patch.object(self_pickup_problem_upload, "get_account_manager", return_value=FakeAccountManager()):
            result = self_pickup_problem_upload.run_once(
                {
                    "dry_run": True,
                    "account_id": "custom_self",
                    "daxiang_s_account_id": "custom_daxiang",
                }
            )

        source_summaries = {item["source_id"]: item for item in result["source_summaries"]}
        self.assertEqual("custom_self", source_summaries["self_pickup_department"]["account_id"])
        self.assertEqual("custom_self_profile", source_summaries["self_pickup_department"]["session_profile"])
        self.assertEqual("custom_daxiang", source_summaries["daxiang_s_self_pickup"]["account_id"])
        self.assertEqual("custom_daxiang_profile", source_summaries["daxiang_s_self_pickup"]["session_profile"])

    def test_real_run_uses_separate_tms_profiles_by_source(self):
        values = [
            ["运单编号", "目的站点", "派送方式", "累计到货件数", "货物件数"],
            ["R_SELF", "邵阳自提部", "派送", "2", "2"],
            ["R_DX_PICK", "邵阳大祥S站", "自提", "1", "1"],
        ]
        profiles = []

        class FakeAuth:
            def __init__(self, profile):
                profiles.append(profile)

            def login_and_get_session(self, max_attempts):
                return object()

        with patch.object(self_pickup_problem_upload, "_tenant_access_token", return_value="token"), \
            patch.object(self_pickup_problem_upload, "_resolve_sheet_id", return_value=("sheet1", "每日到货表")), \
            patch.object(self_pickup_problem_upload, "_read_sheet_values", return_value=values), \
            patch.object(self_pickup_problem_upload, "TMSAuth", side_effect=FakeAuth), \
            patch.object(self_pickup_problem_upload, "_resolve_problem_page_context", return_value={"url": "http://example.invalid"}), \
            patch.object(self_pickup_problem_upload, "_fetch_login_context", return_value=self._login_context()), \
            patch.object(self_pickup_problem_upload, "_fetch_bill_info", return_value=self._base_bill_info()), \
            patch.object(self_pickup_problem_upload, "_fetch_existing_problem_rows", return_value=[], create=True), \
            patch.object(self_pickup_problem_upload, "_fetch_guid", return_value="guid-1"), \
            patch.object(self_pickup_problem_upload, "_save_tables", return_value={"success": True, "message": "ok"}), \
            patch.object(
                ronghui_problem_upload,
                "verify_registered_problem_item",
                side_effect=lambda _session, *, expected, **_kwargs: {
                    "source": "FIND_PROBLEM_REGISTER_LIST",
                    "external_id": expected["GUID"],
                },
            ):
            result = self_pickup_problem_upload.run_once({"dry_run": False, "update_postpone_days": False})

        self.assertEqual(["self_pickup_problem_upload", "daxiang_s"], profiles)
        self.assertEqual(2, result["saved_bills"])
        self.assertEqual(0, result["failed_bills"])

    def test_process_bill_does_not_query_existing_problems_before_saving(self):
        saved_operations = []

        def fake_save_tables(_session, operations, _page_context):
            saved_operations.extend(operations)
            return {"success": True, "message": "ok"}

        with patch.object(self_pickup_problem_upload, "_fetch_bill_info", return_value=self._base_bill_info()), \
            patch.object(self_pickup_problem_upload, "_fetch_existing_problem_rows", side_effect=AssertionError("unexpected existing problem lookup"), create=True), \
            patch.object(self_pickup_problem_upload, "_fetch_guid", return_value="guid-1"), \
            patch.object(self_pickup_problem_upload, "_save_tables", side_effect=fake_save_tables), \
            patch.object(
                ronghui_problem_upload,
                "verify_registered_problem_item",
                side_effect=lambda _session, *, expected, **_kwargs: {
                    "source": "FIND_PROBLEM_REGISTER_LIST",
                    "external_id": expected["GUID"],
                },
            ):
            result = self_pickup_problem_upload._process_bill(
                object(),
                record={"bill_code": "R0001"},
                page_context={"url": "http://example.invalid"},
                login_context=self._login_context(),
                params={"update_postpone_days": False},
                upload_cache={},
            )

        self.assertEqual("R0001", result["bill_code"])
        self.assertEqual(True, result["saved"])
        self.assertEqual(True, result["verified"])
        self.assertEqual(["TAB_PROBLEM_ADD"], [item["operationKey"] for item in saved_operations])

    def test_process_bill_saves_even_when_skip_existing_problem_param_is_true(self):
        saved_operations = []

        def fake_save_tables(_session, operations, _page_context):
            saved_operations.extend(operations)
            return {"success": True, "message": "ok"}

        with patch.object(self_pickup_problem_upload, "_fetch_bill_info", return_value=self._base_bill_info()), \
            patch.object(self_pickup_problem_upload, "_fetch_existing_problem_rows", side_effect=AssertionError("unexpected existing problem lookup"), create=True), \
            patch.object(self_pickup_problem_upload, "_fetch_guid", return_value="guid-1"), \
            patch.object(self_pickup_problem_upload, "_save_tables", side_effect=fake_save_tables), \
            patch.object(
                ronghui_problem_upload,
                "verify_registered_problem_item",
                side_effect=lambda _session, *, expected, **_kwargs: {
                    "source": "FIND_PROBLEM_REGISTER_LIST",
                    "external_id": expected["GUID"],
                },
            ):
            result = self_pickup_problem_upload._process_bill(
                object(),
                record={"bill_code": "R0001"},
                page_context={"url": "http://example.invalid"},
                login_context=self._login_context(),
                params={"update_postpone_days": False, "skip_existing_problem": True},
                upload_cache={},
            )

        self.assertEqual("R0001", result["bill_code"])
        self.assertEqual(True, result["saved"])
        self.assertEqual(["TAB_PROBLEM_ADD"], [item["operationKey"] for item in saved_operations])

    def test_process_bill_without_screenshot_saves_problem_only(self):
        saved_operations = []

        def fake_save_tables(_session, operations, _page_context):
            saved_operations.extend(operations)
            return {"success": True, "message": "ok"}

        with patch.object(self_pickup_problem_upload, "_fetch_bill_info", return_value=self._base_bill_info()), \
            patch.object(self_pickup_problem_upload, "_fetch_existing_problem_rows", return_value=[], create=True), \
            patch.object(self_pickup_problem_upload, "_fetch_guid", return_value="guid-1"), \
            patch.object(self_pickup_problem_upload, "_save_tables", side_effect=fake_save_tables), \
            patch.object(
                ronghui_problem_upload,
                "verify_registered_problem_item",
                side_effect=lambda _session, *, expected, **_kwargs: {
                    "source": "FIND_PROBLEM_REGISTER_LIST",
                    "external_id": expected["GUID"],
                },
            ), \
            patch.object(self_pickup_problem_upload, "_validate_image_path", side_effect=AssertionError("unexpected image validation")), \
            patch.object(self_pickup_problem_upload, "_upload_image", side_effect=AssertionError("unexpected image upload")):
            result = self_pickup_problem_upload._process_bill(
                object(),
                record={"bill_code": "R0001"},
                page_context={"url": "http://example.invalid"},
                login_context=self._login_context(),
                params={"update_postpone_days": False},
                upload_cache={},
            )

        self.assertEqual(True, result["saved"])
        self.assertEqual("", result["image_path"])
        self.assertEqual("", result["uploaded_file"])
        self.assertEqual(["TAB_PROBLEM_ADD"], [item["operationKey"] for item in saved_operations])

    def test_process_bill_with_screenshot_param_uploads_pic_first(self):
        saved_operations = []

        def fake_save_tables(_session, operations, _page_context):
            saved_operations.extend(operations)
            return {"success": True, "message": "ok"}

        with patch.object(self_pickup_problem_upload, "_fetch_bill_info", return_value=self._base_bill_info()), \
            patch.object(self_pickup_problem_upload, "_fetch_existing_problem_rows", return_value=[], create=True), \
            patch.object(self_pickup_problem_upload, "_fetch_guid", return_value="guid-1"), \
            patch.object(self_pickup_problem_upload, "_save_tables", side_effect=fake_save_tables), \
            patch.object(self_pickup_problem_upload, "_validate_image_path", return_value="/tmp/order.png"), \
            patch.object(
                self_pickup_problem_upload,
                "_upload_image",
                return_value={"file_path": "/unauth/download/order.png", "file_name": "order.png"},
            ):
            result = self_pickup_problem_upload._process_bill(
                object(),
                record={"bill_code": "R0001"},
                page_context={"url": "http://example.invalid"},
                login_context=self._login_context(),
                params={"update_postpone_days": False, "screenshot_path": "/tmp/order.png"},
                upload_cache={},
            )

        self.assertEqual("/tmp/order.png", result["image_path"])
        self.assertEqual("order.png", result["uploaded_file"])
        self.assertEqual(
            ["TAB_PIC_SCAN_ADD", "TAB_PROBLEM_ADD"],
            [item["operationKey"] for item in saved_operations],
        )


if __name__ == "__main__":
    unittest.main()
