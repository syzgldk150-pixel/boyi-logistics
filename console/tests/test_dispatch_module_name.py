from pathlib import Path
import sys
import unittest


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


CONSOLE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CONSOLE_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from console.navigation import CONSOLE_NAVIGATION


class DispatchModuleNameTests(unittest.TestCase):
    def test_dispatch_navigation_uses_huolala_name(self):
        dispatch_item = next(item for item in CONSOLE_NAVIGATION if item["route"] == "/dispatch")

        self.assertEqual("货拉拉调度", dispatch_item["label"])
        self.assertNotEqual("车辆调度", dispatch_item["label"])

    def test_dispatch_page_title_uses_huolala_name(self):
        dispatch_template = (CONSOLE_DIR / "templates" / "dispatch.html").read_text(encoding="utf-8")

        self.assertIn("{% block title %}货拉拉调度与比价{% endblock %}", dispatch_template)
        self.assertNotIn("车辆调度与比价", dispatch_template)

    def test_project_module_registry_uses_huolala_name(self):
        app_source = (CONSOLE_DIR / "services" / "documents.py").read_text(encoding="utf-8")

        self.assertIn('name="货拉拉调度"', app_source)
        self.assertNotIn('name="车辆调度"', app_source)


class AutomationModuleNameTests(unittest.TestCase):
    def test_automation_navigation_uses_short_name(self):
        automation_item = next(item for item in CONSOLE_NAVIGATION if item["route"] == "/automations")

        self.assertEqual("自动化", automation_item["label"])
        self.assertNotEqual("Agent 自动化", automation_item["label"])

    def test_automation_page_title_and_breadcrumb_use_short_name(self):
        automation_template = (CONSOLE_DIR / "templates" / "automation.html").read_text(encoding="utf-8")

        self.assertIn("{% block title %}自动化 | {{ app_title }}{% endblock %}", automation_template)
        self.assertIn("{% block breadcrumb %}自动化{% endblock %}", automation_template)
        self.assertNotIn("Agent 自动化", automation_template)

    def test_yunda_login_alert_uses_short_automation_name(self):
        script = (CONSOLE_DIR / "static" / "js" / "yunda_entry_mode.js").read_text(encoding="utf-8")

        self.assertIn('href="/automations">自动化</a>', script)
        self.assertNotIn('href="/automations">Agent 自动化</a>', script)


class WaybillQueryModuleNameTests(unittest.TestCase):
    def test_waybill_navigation_uses_sender_query_name(self):
        waybill_item = next(item for item in CONSOLE_NAVIGATION if item["route"] == "/waybills")

        self.assertEqual("寄件运单查询", waybill_item["label"])
        self.assertNotEqual("运单查询", waybill_item["label"])

    def test_waybill_page_title_breadcrumb_and_subtitle_use_sender_query_name(self):
        waybills_template = (CONSOLE_DIR / "templates" / "waybills.html").read_text(encoding="utf-8")

        self.assertIn("{% block title %}寄件运单查询 | {{ app_title }}{% endblock %}", waybills_template)
        self.assertIn("{% block breadcrumb %}寄件运单查询{% endblock %}", waybills_template)
        self.assertIn("{% block subtitle %}运单管理 / 寄件运单查询{% endblock %}", waybills_template)
        self.assertNotIn("{% block title %}运单查询 | {{ app_title }}{% endblock %}", waybills_template)
        self.assertNotIn("{% block breadcrumb %}运单查询{% endblock %}", waybills_template)
        self.assertNotIn("{% block subtitle %}运单管理 / 运单查询{% endblock %}", waybills_template)


class SidebarNavigationOrderTests(unittest.TestCase):
    def test_tracking_link_is_directly_below_sender_waybill_query(self):
        routes = [item["route"] for item in CONSOLE_NAVIGATION]
        waybill_index = routes.index("/waybills")
        tracking_index = routes.index("/tracking")
        receipts_index = routes.index("/receipts")

        self.assertEqual(waybill_index + 1, tracking_index)
        self.assertEqual(tracking_index + 1, receipts_index)


if __name__ == "__main__":
    unittest.main()
