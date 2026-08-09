from pathlib import Path
import unittest


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class MobileDensityUITests(unittest.TestCase):
    def test_automation_cards_keep_compact_accessible_mobile_actions(self):
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
        template = (CONSOLE_DIR / "templates" / "automation.html").read_text(encoding="utf-8")

        self.assertIn(".auto-card:not(.is-mobile-expanded) .auto-mode-badge", stylesheet)
        self.assertIn(".auto-card:not(.is-mobile-expanded) .auto-terminal-btn", stylesheet)
        self.assertIn("justify-content: flex-end", stylesheet)
        self.assertIn("min-width: 80px; min-height: 44px", stylesheet)
        self.assertIn(".auto-mobile-only-expand", stylesheet)
        self.assertIn("data-mobile-card-toggle", template)
        self.assertIn("is-mobile-expanded", template)
        self.assertIn('aria-label="展开任务详情"', template)
        self.assertIn('expanded ? "收起任务详情" : "展开任务详情"', template)

    def test_home_refresh_is_low_emphasis_but_retains_a_touch_target(self):
        template = (CONSOLE_DIR / "templates" / "portal.html").read_text(encoding="utf-8")

        self.assertIn('data-monitoring-refresh aria-label="刷新首页数据"', template)
        self.assertIn(".monitoring-refresh {", template)
        self.assertIn("width: 44px;", template)
        self.assertIn("min-height: 44px;", template)
        self.assertIn(".monitoring-refresh-label", template)

    def test_tracking_search_uses_compact_mobile_controls(self):
        template = (CONSOLE_DIR / "templates" / "tracking.html").read_text(encoding="utf-8")

        self.assertIn('placeholder="输入运单号"', template)
        self.assertIn(".tracking-input-wrap { height: 44px;", template)
        self.assertIn(".tracking-search-btn { width: auto; min-width: 88px; height: 44px;", template)


if __name__ == "__main__":
    unittest.main()
