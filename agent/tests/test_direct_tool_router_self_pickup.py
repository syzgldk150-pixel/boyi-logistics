import unittest

from agent import direct_tool_router


class SelfPickupDirectRouteTest(unittest.TestCase):
    def test_self_pickup_arrival_problem_command_routes_to_preview(self):
        for text in ("自提到货问题件", "“自提到货问题件”"):
            with self.subTest(text=text):
                request = direct_tool_router.direct_tool_request_from_text(text)

                self.assertIsNotNone(request)
                self.assertEqual("self_pickup_problem_upload", request["tool_name"])
                self.assertEqual(
                    {"dry_run": True, "account_id": "ronghui_self_pickup_problem"},
                    request["params"],
                )
                self.assertEqual("reply", request["mode"])
                self.assertEqual(
                    {"dry_run": False, "account_id": "ronghui_self_pickup_problem"},
                    request["confirm_intent"]["execute_params"],
                )


if __name__ == "__main__":
    unittest.main()
