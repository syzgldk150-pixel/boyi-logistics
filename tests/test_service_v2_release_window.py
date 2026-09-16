"""Service V2 release windows use the scheduler's named contribution."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

_spec = importlib.util.spec_from_file_location("boyi_window_test_helpers", Path(__file__).with_name("test_migration_runner.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
_WindowCursor = _helpers._WindowCursor
_WindowConnection = _helpers._WindowConnection


class ServiceV2ReleaseWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = _helpers._load_runner()

    def test_scheduled_write_window_resolves_service_v2_scheduler_contribution(self):
        snapshot = {
            "automation_id": "clockin_daxiang", "generation": 3,
            "runtime_model": "SERVICE_V2", "plugin_api": "2.0.0",
            "enabled_entrypoints": ["daily_clockin", "manual_run"],
            "execution_metadata": {
                "contributions": {"scheduler": [{
                    "id": "daily_clockin", "default_enabled": True,
                    "schedule": {"timezone": "Asia/Shanghai"},
                }]},
                "compiled_invocations": {"daily_clockin": {"arguments": {}}},
                "governance_anchor": {"operation_type": "external_write"},
            },
        }
        row = {
            "task_id": "clockin_daxiang_1830", "automation_id": "clockin_daxiang",
            "automation_generation": 3, "tool_name": "automation.clockin_daxiang.run",
            "cron_expression": "30 18 * * *", "enabled": 1,
            "committed_generation": 3, "project_enabled": 1,
            "project_state": "ENABLED", "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "COMMITTED", "snapshot_json": snapshot,
        }
        for hour, expected in ((12, 0), (18, 1)):
            with self.subTest(hour=hour):
                cursor = _WindowCursor([], policy_exists=True,
                    project_schema_exists=True, project_rows=[row],
                    candidate_rows=[dict(row, id=row["task_id"], tool_params={})])
                with patch.object(self.runner, "_connect", return_value=_WindowConnection(cursor)):
                    result = self.runner.check_scheduled_write_window(
                        now=datetime(2026, 8, 14, hour, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
                self.assertEqual(expected, result)
                self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))
        for invalid in ("missing_contract", "disabled", "ambiguous"):
            with self.subTest(invalid=invalid):
                changed = json.loads(json.dumps(snapshot))
                if invalid == "missing_contract":
                    changed["execution_metadata"]["compiled_invocations"] = {"manual_run": {}}
                elif invalid == "disabled":
                    changed["enabled_entrypoints"] = ["manual_run"]
                else:
                    changed["enabled_entrypoints"].append("second_clockin")
                    changed["execution_metadata"]["contributions"]["scheduler"].append({
                        "id": "second_clockin", "default_enabled": True,
                        "schedule": {"timezone": "Asia/Shanghai"},
                    })
                cursor = _WindowCursor([], policy_exists=True,
                    project_rows=[dict(row, snapshot_json=changed)])
                with self.assertRaisesRegex(Exception, "PROJECT_SCHEDULE_CONTRACT_INVALID"):
                    self.runner._typed_project_scheduled_write_crons(cursor)
