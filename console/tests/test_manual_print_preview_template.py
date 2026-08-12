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

    def test_clodop_loader_uses_shared_websocket_loader(self):
        document_template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        print_template = (CONSOLE_DIR / "templates" / "waybill_print.html").read_text(encoding="utf-8")
        loader = (CONSOLE_DIR / "static" / "js" / "clodop_loader.js").read_text(encoding="utf-8")

        for template in (document_template, print_template):
            self.assertIn('/static/js/clodop_loader.js?v=20260812', template)
            self.assertIn("window.BoyiCLodop.getInstance()", template)
            self.assertNotIn("CLodopfuncs.js?priority=", template)

        self.assertIn("ws://localhost:8000/CLodopfuncs.js", loader)
        self.assertIn("ws://localhost:18000/CLodopfuncs.js", loader)
        self.assertIn("https://localhost.lodop.net:8443/CLodopfuncs.js", loader)
        self.assertIn("loadViaWebSocket", loader)
        self.assertIn("waitForObject", loader)
        self.assertIn("DEFAULT_TIMEOUT_MS = 8000", loader)

    def test_print_preview_reports_distinct_failure_stage(self):
        document_template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        print_template = (CONSOLE_DIR / "templates" / "waybill_print.html").read_text(encoding="utf-8")

        self.assertIn("formatManualPrintError", document_template)
        self.assertIn("面单底版加载失败", document_template)
        self.assertIn("面单打印模板未加载", document_template)
        self.assertIn("window.BoyiCLodop?.getLoadErrors()", document_template)
        self.assertIn("formatManualPrintError(error)", document_template)

        self.assertIn("formatPrintError", print_template)
        self.assertIn("面单底版加载失败", print_template)
        self.assertIn("面单打印模板未加载", print_template)
        self.assertIn("window.BoyiCLodop?.getLoadErrors()", print_template)
        self.assertIn("formatPrintError(error)", print_template)

    def test_clodop_loader_injects_websocket_payload_and_can_retry(self):
        loader = (CONSOLE_DIR / "static" / "js" / "clodop_loader.js").read_text(encoding="utf-8")

        self.assertIn("injectServiceScript(url, event.data)", loader)
        self.assertIn("loadPromise = null", loader)
        self.assertIn("C-Lodop WebSocket load timeout", loader)
        self.assertIn("getLoadErrors: () => loadErrors.slice()", loader)

    def test_https_pages_try_websocket_before_https_fallback(self):
        loader = (CONSOLE_DIR / "static" / "js" / "clodop_loader.js").read_text(encoding="utf-8")

        self.assertLess(loader.index("for (const url of WEBSOCKET_URLS)"), loader.index("const scriptUrls"))
        self.assertIn('global.location.protocol === "https:" ? HTTPS_URLS : HTTP_URLS', loader)
        self.assertIn("recordLoadError", loader)


if __name__ == "__main__":
    unittest.main()
