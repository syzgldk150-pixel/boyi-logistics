from pathlib import Path
import sys
import unittest


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class DispatchModuleNameTests(unittest.TestCase):
    def test_dispatch_navigation_uses_huolala_name(self):
        base_template = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("<span>货拉拉调度</span>", base_template)
        self.assertNotIn("<span>车辆调度</span>", base_template)

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
        base_template = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("<span>自动化</span>", base_template)
        self.assertNotIn("<span>Agent 自动化</span>", base_template)

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
        base_template = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("<span>寄件运单查询</span>", base_template)
        self.assertNotIn("<span>运单查询</span>", base_template)

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
        base_template = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")

        waybill_pos = base_template.index('href="/waybills"')
        tracking_pos = base_template.index('<a class="nav-link" href="/tracking"')
        receipts_pos = base_template.index('href="/receipts"')
        waybill_block_end = base_template.index("</a>", waybill_pos) + len("</a>")
        next_anchor_pos = base_template.find('<a class="nav-link"', waybill_block_end)

        self.assertLess(waybill_pos, tracking_pos)
        self.assertLess(tracking_pos, receipts_pos)
        self.assertEqual(tracking_pos, next_anchor_pos)


if __name__ == "__main__":
    unittest.main()
