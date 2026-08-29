import unittest
from unittest.mock import Mock, patch

from agent.tms_runtime.scripts import split_pending_problem_upload as runtime


def item(
    bill_code="R1",
    *,
    problem_type="少货/分批",
    complaint_status=None,
    problem_item_status="pending",
):
    arrived = 5 if problem_type == "少货/分批" else 0
    return {
        "bill_code": bill_code,
        "problem_type": problem_type,
        "problem_owner_type": "交接异常" if arrived else "通知类（不顺延时效）",
        "problem_cause": "应到10件 实际到5件" if arrived else "有发未到",
        "expected_quantity": 10,
        "arrived_quantity": arrived,
        "pending_quantity": 10 - arrived,
        "complaint_status": complaint_status or "not_applicable",
        "problem_item_status": problem_item_status,
    }


def resolved(params):
    return {**params, "account_id": "ronghui_default", "session_profile": "default"}


class SplitPendingProblemUploadRuntimeTests(unittest.TestCase):
    def test_runtime_requires_project_account_binding(self):
        with self.assertRaisesRegex(ValueError, "显式绑定 account_id"):
            runtime._resolve_account({})

    def test_runtime_requires_explicit_step_states(self):
        raw = item()
        raw.pop("complaint_status")
        with self.assertRaisesRegex(ValueError, "complaint_status"):
            runtime._validated_items([raw])

    def test_dry_run_has_no_login_or_external_write(self):
        with patch.object(runtime, "_resolve_account", side_effect=resolved), patch.object(
            runtime, "TMSAuth"
        ) as auth_mock, patch.object(runtime, "upload_problem_item") as problem_mock:
            result = runtime.run_once({"dry_run": True, "items": [item()]})
        self.assertTrue(result["ok"])
        self.assertEqual("dry_run", result["stage"])
        auth_mock.assert_not_called()
        problem_mock.assert_not_called()

    def test_partial_and_zero_arrival_publish_directly_as_problem_items(self):
        session = object()
        auth = Mock()
        auth.login_and_get_session.return_value = session
        problem_result = {"bill_code": "R_ZERO", "saved": True}
        with patch.object(runtime, "_resolve_account", side_effect=resolved), patch.object(
            runtime, "TMSAuth", return_value=auth
        ), patch.object(runtime, "resolve_problem_page_context", return_value="page"), patch.object(
            runtime, "fetch_login_context", return_value="login"
        ), patch.object(runtime, "upload_problem_item", return_value=problem_result) as problem_mock:
            result = runtime.run_once(
                {"items": [item("R_PART"), item("R_ZERO", problem_type="有发未到")]}
            )
        self.assertTrue(result["ok"])
        self.assertEqual([], result["failed_bill_codes"])
        self.assertEqual(
            ["R_PART", "R_ZERO"],
            [call.kwargs["record"]["bill_code"] for call in problem_mock.call_args_list],
        )
        by_code = {row["bill_code"]: row for row in result["results"]}
        self.assertEqual("not_applicable", by_code["R_PART"]["complaint_status"])
        self.assertEqual("success", by_code["R_ZERO"]["problem_item_status"])

    def test_problem_failure_does_not_stop_later_bill(self):
        session = object()
        auth = Mock()
        auth.login_and_get_session.return_value = session

        def upload_problem(_session, *, record, **_kwargs):
            if record["bill_code"] == "R1":
                raise RuntimeError("问题件失败")
            return {"bill_code": record["bill_code"], "saved": True}

        with patch.object(runtime, "_resolve_account", side_effect=resolved), patch.object(
            runtime, "TMSAuth", return_value=auth
        ), patch.object(runtime, "resolve_problem_page_context", return_value="page"), patch.object(
            runtime, "fetch_login_context", return_value="login"
        ), patch.object(runtime, "upload_problem_item", side_effect=upload_problem):
            result = runtime.run_once({"items": [item("R1"), item("R2")]})
        self.assertEqual(["R1"], result["failed_bill_codes"])
        self.assertEqual("failed", result["results"][0]["problem_item_status"])
        self.assertTrue(result["results"][1]["complete"])

    def test_historical_problem_success_skips_external_write(self):
        auth = Mock()
        auth.login_and_get_session.return_value = object()
        with patch.object(runtime, "_resolve_account", side_effect=resolved), patch.object(
            runtime, "TMSAuth", return_value=auth
        ), patch.object(runtime, "upload_problem_item") as problem_mock:
            result = runtime.run_once(
                {"items": [item("R1", problem_item_status="success")]}
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["results"][0]["complete"])
        self.assertIsNone(result["results"][0]["problem_item"])
        problem_mock.assert_not_called()

    def test_failed_problem_item_is_retried_directly(self):
        auth = Mock()
        auth.login_and_get_session.return_value = object()
        with patch.object(runtime, "_resolve_account", side_effect=resolved), patch.object(
            runtime, "TMSAuth", return_value=auth
        ), patch.object(runtime, "resolve_problem_page_context", return_value="page"
        ), patch.object(runtime, "fetch_login_context", return_value="login"), patch.object(
            runtime, "upload_problem_item", return_value={"bill_code": "R1", "saved": True}
        ) as problem_mock:
            result = runtime.run_once(
                {"items": [item("R1", problem_item_status="failed")]}
            )
        self.assertTrue(result["ok"])
        problem_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
