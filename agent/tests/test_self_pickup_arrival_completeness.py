import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_script_module():
    stub_names = (
        "httpx",
        "dotenv",
        "login_manager",
        "agent.tms_runtime",
        "agent.tms_runtime.account_manager",
    )
    previous_modules = {name: sys.modules.get(name) for name in stub_names}
    sys.modules["httpx"] = types.SimpleNamespace()
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None)
    sys.modules["login_manager"] = types.SimpleNamespace(TMSAuth=object)
    runtime_pkg = types.ModuleType("agent.tms_runtime")
    runtime_pkg.__path__ = []
    account_manager = types.ModuleType("agent.tms_runtime.account_manager")
    account_manager.get_account_manager = lambda: None
    sys.modules["agent.tms_runtime"] = runtime_pkg
    sys.modules["agent.tms_runtime.account_manager"] = account_manager
    try:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "agent"
            / "tms_runtime"
            / "scripts"
            / "self_pickup_problem_upload.py"
        )
        spec = importlib.util.spec_from_file_location("self_pickup_problem_upload_under_test", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def bound_source_params():
    return {
        "account_id": "bound-self",
        "session_profile": "bound-self-profile",
        "daxiang_s_account_id": "bound-daxiang",
        "daxiang_s_session_profile": "bound-daxiang-profile",
    }


class SelfPickupArrivalCompletenessTest(unittest.TestCase):
    def test_problem_causes_match_current_pickup_notice_text(self):
        script = load_script_module()

        self.assertEqual(
            "货已到，尽快安排提货，自提部免费仓储只有1天，尽快提走，"
            "超时产生仓储费0.03元/KG/天10元票/天；自提电话：0739-5186128 "
            "地址：双清区建设南路白马田伟业物流城内融辉物流(导航：勇胜物流)；"
            "托盘类、少量件数类货物提货时间:9:00-20:00；"
            "件数多的需要装卸工操作的货物提货时间10:00-20:00；",
            script.DEFAULT_PROBLEM_CAUSE,
        )
        self.assertEqual(
            "货已到，尽快安排提货，网点免费仓储只有3天，尽快提走，"
            "超时产生仓储费0.03元/KG/天10元票/天；自提电话：0739-5186128 "
            "地址：双清区建设南路白马田伟业物流城内融辉物流(导航：勇胜物流)；"
            "托盘类、少量件数类货物提货时间:9:00-20:00；"
            "件数多的需要装卸工操作的货物提货时间10:00-20:00",
            script.DAXIANG_S_PROBLEM_CAUSE,
        )

    def test_collects_only_rows_where_arrived_count_equals_goods_count(self):
        script = load_script_module()
        values = [
            ["运单编号", "目的站点", "派送方式", "累计到货件数", "货物件数"],
            ["R_READY", "邵阳自提部", "派送", "3", "3"],
            ["R_PARTIAL", "邵阳自提部", "派送", "2", "3"],
            ["R_DX_READY", "邵阳大祥S站", "自提", "1", "1"],
            ["R_DX_PARTIAL", "邵阳大祥S站", "自提", "1", "2"],
        ]

        records = script._collect_waybills_from_values(
            values,
            source_rules=script._source_rules(bound_source_params()),
            source_sheet_id="sheet1",
            source_sheet_title="每日到货表",
        )

        self.assertEqual(["R_READY", "R_DX_READY"], [item["bill_code"] for item in records])
        self.assertEqual("3", records[0]["arrival_count"])
        self.assertEqual("3", records[0]["goods_count"])

    def test_collects_completed_rows_from_stats_sheet_headers(self):
        script = load_script_module()
        values = [
            ["0601运单编号", "货物名称", "包装类型", "派送方式", "件数", "目的站点", "累计到货件数"],
            ["R_READY", "配件", "纸箱", "派送", "3", "邵阳自提部", "3"],
            ["R_PARTIAL", "配件", "纸箱", "派送", "3", "邵阳自提部", "2"],
            ["R_DX_READY", "配件", "纸箱", "自提", "1", "邵阳大祥S站", "1"],
        ]

        records = script._collect_waybills_from_values(
            values,
            source_rules=script._source_rules(bound_source_params()),
            source_sheet_id="sheet1",
            source_sheet_title="每日到货表",
        )

        self.assertEqual(["R_READY", "R_DX_READY"], [item["bill_code"] for item in records])

    def test_missing_arrival_count_columns_fails_closed(self):
        script = load_script_module()
        values = [
            ["运单编号", "目的站点", "派送方式"],
            ["R_UNVERIFIED", "邵阳自提部", "派送"],
        ]

        with self.assertRaisesRegex(RuntimeError, "累计到货件数.*货物件数"):
            script._collect_waybills_from_values(
                values,
                source_rules=script._source_rules(bound_source_params()),
                source_sheet_id="sheet1",
                source_sheet_title="每日到货表",
            )


if __name__ == "__main__":
    unittest.main()
