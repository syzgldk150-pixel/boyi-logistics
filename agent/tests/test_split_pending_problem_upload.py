import unittest

from agent.direct_tool_router import direct_tool_request_from_text, is_deprecated_split_command


SOURCE_HEADERS = [
    "08.03运单编号",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "回单号",
    "实际重量",
    "体积",
    "备注",
    "目的站点",
    "收件人",
    "收件电话",
    "收件地址",
    "结算重量",
    "体积重",
    "运费",
    "支付类型",
    "到付款",
    "累计到货件数",
]


def source_row(bill_code: str, expected: int, arrived: int) -> list[object]:
    return [
        bill_code,
        "货物",
        "纸箱",
        "送货",
        expected,
        "",
        10,
        1,
        "",
        "邵阳操作场",
        "收件人",
        "13800000000",
        "地址",
        10,
        1,
        20,
        "现付",
        0,
        arrived,
    ]


def source_values(*rows: list[object]) -> list[list[object]]:
    return [SOURCE_HEADERS, *rows]


class SplitPendingProblemUploadTests(unittest.TestCase):
    def test_router_accepts_only_exact_new_trigger(self):
        request = direct_tool_request_from_text("分批")
        self.assertEqual("preview_split_pending_problems", request["tool_name"])
        self.assertEqual({}, request["params"])
        self.assertEqual("automation_preview", request["mode"])
        self.assertEqual(
            "builtin.split_pending_problem_upload",
            request["automation_route_key"],
        )
        self.assertIn("selection_intent", request)
        for old_text in ("分批问题件", "上报分批差错", "分批差错", "上传分批/未到问题件"):
            self.assertTrue(is_deprecated_split_command(old_text))
            self.assertIsNone(direct_tool_request_from_text(old_text))


if __name__ == "__main__":
    unittest.main()
