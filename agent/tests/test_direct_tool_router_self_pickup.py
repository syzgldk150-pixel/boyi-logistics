import unittest

from agent import direct_tool_router


class SelfPickupDirectRouteTest(unittest.TestCase):
    def test_self_pickup_arrival_problem_command_routes_to_preview(self):
        for text in ("自提到货问题件", "“自提到货问题件”"):
            with self.subTest(text=text):
                request = direct_tool_router.direct_tool_request_from_text(text)

                self.assertIsNotNone(request)
                self.assertEqual("preview_self_pickup_problems", request["tool_name"])
                self.assertEqual({}, request["params"])
                self.assertEqual("automation_preview", request["mode"])
                self.assertEqual(
                    "builtin.self_pickup_problem_upload",
                    request["automation_route_key"],
                )
                self.assertEqual(
                    {"dry_run": False},
                    request["confirm_intent"]["dynamic_inputs"],
                )


if __name__ == "__main__":
    unittest.main()
