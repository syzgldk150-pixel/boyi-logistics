"""A re-routed parcel's target-day scan replaces the persisted destination."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "agent" / "service_v2_plugins"


@pytest.fixture()
def action():
    modules = {
        "boyi_plugin_result": ROOT / "_shared" / "result.py",
        "split_rules": ROOT / "split_pending_problem_upload_v2" / "payload" / "split_rules.py",
        "arrival_stats_action_under_test": ROOT / "sync_arrival_stats_v2" / "payload" / "action.py",
    }
    previous = {name: sys.modules.get(name) for name in modules}
    try:
        for name, path in modules.items():
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        yield sys.modules["arrival_stats_action_under_test"]
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_target_day_scan_destination_wins_over_persisted_snapshot(action):
    persisted = {"raw_code": "R000226964500005", "destination": "邵阳大祥S站", "code_type": "child"}
    current = {"bill_code": "R000226964500005", "destination": "邵阳自提部"}
    rows = action._normalized_scan_snapshot([persisted, current])
    assert rows == [{"raw_code": "R000226964500005", "destination": "邵阳自提部",
                     "code_type": "child", "main_tracking": "R00022696450"}]


def test_unchanged_rows_merge_and_children_keep_their_main_waybill(action):
    rows = action._normalized_scan_snapshot([
        {"raw_code": "R00022696450", "destination": "邵阳大祥S站", "code_type": "main"},
        {"bill_code": "R00022696450", "destination": "邵阳大祥S站"},
        {"bill_code": "R000226964500001", "destination": "邵阳大祥S站"},
    ])
    assert [(r["raw_code"], r["code_type"], r["main_tracking"]) for r in rows] == [
        ("R00022696450", "main", "R00022696450"), ("R000226964500001", "child", "R00022696450")]
