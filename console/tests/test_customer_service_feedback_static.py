import unittest
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class CustomerServiceFeedbackStaticTests(unittest.TestCase):
    def test_query_feedback_uses_button_busy_state_without_status_bar(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertNotIn("data-cs-query-feedback", template)
        self.assertNotIn("customer-service-query-feedback", template)
        self.assertNotIn('const queryFeedback = $("[data-cs-query-feedback]")', script)
        self.assertNotIn(".customer-service-query-feedback", css)
        self.assertIn('const queryButton = document.querySelector("[data-cs-query]")', script)
        self.assertIn("function setQueryBusy(isBusy)", script)
        self.assertIn("queryButton.disabled = isBusy", script)
        self.assertIn('queryButton.setAttribute("aria-busy", String(isBusy))', script)
        self.assertIn('queryButtonLabel.textContent = isBusy ? "查询中" : queryButtonLabel.dataset.originalText', script)
        self.assertIn('icon.setAttribute("data-feather", isBusy ? "loader" : "refresh-cw")', script)
        self.assertIn('.customer-service-icon-btn[aria-busy="true"]', css)
        self.assertIn("animation: customer-service-spin .9s linear infinite;", css)


if __name__ == "__main__":
    unittest.main()
