from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from console.services.automation import (
    AutomationServiceMixin,
    group_scheduled_rows_by_automation_id,
)


class AutomationProjectGroupingTests(unittest.TestCase):
    @staticmethod
    def _rendered_tasks(
        *,
        scheduled_rows=None,
        plugin_instances=None,
        hidden_automation_ids=frozenset(),
        plugin_warning="",
    ):
        captured = {}

        class _Template:
            def render(self, **context):
                captured.update(context)
                return "rendered"

        service = AutomationServiceMixin.__new__(AutomationServiceMixin)
        service.repository = SimpleNamespace(
            list_scheduled_tasks=lambda: list(scheduled_rows or []),
            list_workflow_resources=lambda: [],
        )
        service.settings = SimpleNamespace(app_title="Console", agent_base_url="http://agent")
        service.automation_virtual_task_state = {}
        service.template_env = SimpleNamespace(get_template=lambda _name: _Template())
        service._load_automation_plugin_catalog = lambda _handler: (
            [],
            list(plugin_instances or []),
            [],
            [],
            hidden_automation_ids,
            plugin_warning,
            False,
        )
        service._load_automation_project_policies = lambda _handler, _tasks: ("", False)
        service._fetch_automation_accounts = lambda **_kwargs: ([], "")
        service._enrich_automation_tasks_with_accounts = lambda _tasks, _accounts: None
        service._send_html = lambda _handler, _body: None

        service._render_automations(SimpleNamespace(), {})
        return captured["scheduled_tasks"]

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

    def test_catalog_unavailable_never_synthesizes_static_workflow_cards(self):
        tasks = self._rendered_tasks(plugin_warning="catalog unavailable")

        self.assertEqual([], tasks)

    def test_every_catalog_instance_renders_once_without_a_schedule_row(self):
        def plugin(automation_id):
            return {
                "automation_id": automation_id,
                "plugin_id": "integration_action",
                "instance_name": automation_id,
                "version": "1.0.0",
                "execution_platform": "server",
                "can_schedule": False,
                "scheduling": {
                    "supported": False,
                    "allowed_kinds": [],
                    "max_daily_times": 0,
                },
                "entrypoints": ["console"],
                "enabled_entrypoints": ["console"],
                "enabled": True,
                "configured": True,
                "blocked": False,
                "state": "ENABLED",
                "account_roles": [],
                "resource_roles": [],
                "config_fields": [],
            }

        tasks = self._rendered_tasks(
            plugin_instances=[plugin("instance-one"), plugin("instance-two")]
        )

        self.assertEqual(
            ["instance-one", "instance-two"],
            sorted(task["task_id"] for task in tasks),
        )

    def test_release_excluded_schedule_row_stays_hidden(self):
        tasks = self._rendered_tasks(
            scheduled_rows=[{"id": "slot-0100", "automation_id": "hidden-project"}],
            hidden_automation_ids=frozenset({"hidden-project"}),
        )

        self.assertEqual([], tasks)

    def test_hidden_id_collision_never_hides_an_unlinked_schedule_row(self):
        tasks = self._rendered_tasks(
            scheduled_rows=[{"id": "hidden-project", "automation_id": None}],
            hidden_automation_ids=frozenset({"hidden-project"}),
        )

        self.assertEqual(1, len(tasks))
        self.assertTrue(tasks[0]["automation_link_missing"])
        self.assertFalse(tasks[0]["can_save"])
        self.assertFalse(tasks[0]["can_run_now"])
        self.assertTrue(tasks[0]["plugin_blocked"])

    def test_catalog_failure_does_not_hide_r7_row_by_local_static_identity(self):
        tasks = self._rendered_tasks(
            scheduled_rows=[
                {"id": "slot-0100", "automation_id": "r7_arrival_checkin"}
            ],
            plugin_warning="catalog unavailable",
        )

        self.assertEqual(1, len(tasks))
        self.assertEqual("r7_arrival_checkin", tasks[0]["task_id"])
        self.assertFalse(tasks[0]["can_save"])
        self.assertFalse(tasks[0]["can_run_now"])
        self.assertTrue(tasks[0]["plugin_blocked"])

    def test_unlinked_schedule_row_remains_visible_but_fail_closed(self):
        tasks = self._rendered_tasks(
            scheduled_rows=[{"id": "legacy-slot-0100", "automation_id": None}],
        )

        self.assertEqual(1, len(tasks))
        self.assertTrue(tasks[0]["automation_link_missing"])
        self.assertFalse(tasks[0]["can_save"])
        self.assertFalse(tasks[0]["can_run_now"])
        self.assertTrue(tasks[0]["plugin_blocked"])

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
