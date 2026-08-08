import unittest
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class ManualPrintPreviewTemplateTests(unittest.TestCase):
    def test_manual_preview_click_has_visible_feedback(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")

        self.assertIn("正在打开 C-Lodop 打印预览", template)
        self.assertIn("已调用 C-Lodop 打印预览", template)

    def test_printer_status_does_not_report_connected_without_printers(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")

        self.assertIn('count > 0 ? "C-Lodop已连接" : "C-Lodop未检测到打印机"', template)
        self.assertNotIn('count > 0 ? "C-Lodop已连接" : "C-Lodop已连接"', template)

    def test_manual_preview_validation_feedback_is_in_card_and_marks_fields(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")

        self.assertIn("data-manual-feedback", template)
        self.assertIn('aria-live="polite"', template)
        self.assertIn("打印预览前请先填写", template)
        self.assertIn('field.classList.add("is-invalid")', template)
        self.assertIn('field.setAttribute("aria-invalid", "true")', template)
        self.assertIn("clearManualFieldInvalid", template)

    def test_clodop_loader_tries_ipv4_loopback_and_waits_for_startup(self):
        document_template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        print_template = (CONSOLE_DIR / "templates" / "waybill_print.html").read_text(encoding="utf-8")

        for template in (document_template, print_template):
            https_url = "https://localhost.lodop.net:8443/CLodopfuncs.js"
            http_url = "http://127.0.0.1:8000/CLodopfuncs.js"
            self.assertIn(https_url, template)
            self.assertLess(template.index(https_url), template.index(http_url))
            self.assertIn("http://localhost.lodop.net:8000/CLodopfuncs.js", template)
            self.assertIn("http://127.0.0.1:8000/CLodopfuncs.js", template)
            self.assertIn("http://127.0.0.1:18000/CLodopfuncs.js", template)
            self.assertIn("waitForCLODOPReady", template)
            self.assertIn("timeoutMs = 8000", template)
            self.assertIn('script.crossOrigin = "anonymous"', template)

    def test_print_preview_reports_distinct_failure_stage(self):
        document_template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        print_template = (CONSOLE_DIR / "templates" / "waybill_print.html").read_text(encoding="utf-8")

        self.assertIn("formatManualPrintError", document_template)
        self.assertIn("面单底版加载失败", document_template)
        self.assertIn("面单打印模板未加载", document_template)
        self.assertIn("lastCLODOPLoadError", document_template)
        self.assertIn("formatManualPrintError(error)", document_template)

        self.assertIn("formatPrintError", print_template)
        self.assertIn("面单底版加载失败", print_template)
        self.assertIn("面单打印模板未加载", print_template)
        self.assertIn("lastCLODOPLoadError", print_template)
        self.assertIn("formatPrintError(error)", print_template)

    def test_clodop_loader_has_fetch_injection_fallback(self):
        document_template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        print_template = (CONSOLE_DIR / "templates" / "waybill_print.html").read_text(encoding="utf-8")

        for template in (document_template, print_template):
            self.assertIn("fetchCLODOPScript", template)
            self.assertIn("injectCLODOPScriptText", template)
            self.assertIn('mode: "cors"', template)
            self.assertIn('credentials: "omit"', template)
            self.assertIn("response.text()", template)

    def test_https_pages_only_try_secure_clodop_urls_and_keep_error_history(self):
        document_template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        print_template = (CONSOLE_DIR / "templates" / "waybill_print.html").read_text(encoding="utf-8")

        for template in (document_template, print_template):
            self.assertIn("CLODOP_LOAD_ERRORS", template)
            self.assertIn("getCandidateCLODOPUrls", template)
            self.assertIn('window.location.protocol === "https:"', template)
            self.assertIn('url.startsWith("https://")', template)
            self.assertIn("formatCLODOPLoadErrors", template)
            self.assertIn("公网 HTTP 地址打开后台", template)
            self.assertNotIn('throw new Error("C-Lodop blocked by public HTTP origin")', template)


if __name__ == "__main__":
    unittest.main()
