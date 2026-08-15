from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from console.services.automation import (
    AutomationServiceMixin,
    group_scheduled_rows_by_automation_id,
)


class AutomationProjectGroupingTests(unittest.TestCase):
    def test_same_project_groups_arbitrary_scheduled_row_ids(self):
        rows = [
            {"id": "opaque-slot-alpha", "automation_id": "instance-one"},
            {"id": "another-unrelated-id", "automation_id": "instance-one"},
        ]

        groups = group_scheduled_rows_by_automation_id(rows)

        self.assertEqual(1, len(groups))
        self.assertEqual("instance-one", groups[0]["task_id"])
        self.assertFalse(groups[0]["missing_automation_id"])
        self.assertEqual(rows, groups[0]["rows"])

    def test_suffix_lookalikes_with_different_projects_never_merge(self):
        rows = [
            {"id": "shared_0500", "automation_id": "instance-one"},
            {"id": "shared_0700", "automation_id": "instance-two"},
        ]

        groups = group_scheduled_rows_by_automation_id(rows)

        self.assertEqual(["instance-one", "instance-two"], [row["task_id"] for row in groups])
        self.assertTrue(all(len(row["rows"]) == 1 for row in groups))

    def test_missing_or_invalid_project_identity_is_individually_fail_closed(self):
        rows = [
            {"id": "legacy_0500", "automation_id": None},
            {"id": "legacy_0700", "automation_id": ""},
            {"id": "legacy_0900", "automation_id": "invalid/id"},
        ]

        groups = group_scheduled_rows_by_automation_id(rows)

        self.assertEqual(3, len(groups))
        self.assertEqual(3, len({row["storage_key"] for row in groups}))
        self.assertTrue(all(row["missing_automation_id"] for row in groups))
        self.assertTrue(all(len(row["rows"]) == 1 for row in groups))

    def test_page_rendering_does_not_use_legacy_suffix_grouping(self):
        source = inspect.getsource(AutomationServiceMixin._render_automations)

        self.assertIn("group_scheduled_rows_by_automation_id", source)
        self.assertNotIn("normalize_task_group_id", source)
        self.assertNotIn("task_group_slot_index", source)

    def test_unlinked_row_never_derives_project_policy_from_task_id(self):
        service = AutomationServiceMixin.__new__(AutomationServiceMixin)
        service._mysql_console_principal = lambda _user: None
        task = {
            "task_id": "instance-one",
            "automation_link_missing": True,
            "plugin": None,
        }

        service._load_automation_project_policies(
            SimpleNamespace(current_admin_user=None),
            [task],
        )

        self.assertIsNone(task["approval_policy"])


if __name__ == "__main__":
    unittest.main()
