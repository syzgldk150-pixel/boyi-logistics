import unittest
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class ConsoleUiStylesheetLoadingTests(unittest.TestCase):
    def test_new_module_waits_for_stylesheet_and_cleans_stale_head_nodes(self):
        script = (CONSOLE_DIR / "static" / "console_ui.js").read_text(encoding="utf-8")
        navigation = script[
            script.index("async function ensureModuleTab"):
            script.index("async function navigateContent")
        ]
        fresh_tab_start = navigation.index(
            "const headNodes = await syncHead(nextDocument, tabKey, controller.signal);"
        )

        self.assertIn("function appendStylesheet(linkNode, tabKey, signal)", script)
        self.assertIn('link.addEventListener("load", onLoad, { once: true })', script)
        self.assertIn('link.addEventListener("error", onError, { once: true })', script)
        self.assertIn('signal?.addEventListener("abort", onAbort, { once: true })', script)
        self.assertIn("Stylesheet load timed out:", script)
        self.assertIn("link.remove();", script)
        self.assertIn("headNodes.forEach((node) => node.remove());", navigation)
        self.assertLess(
            fresh_tab_start,
            navigation.index("activateTab(tabKey", fresh_tab_start),
        )

        activate = script[
            script.index("function activateTab"):
            script.index("function pickNextTab")
        ]
        self.assertLess(
            activate.index("syncActiveHead(tab);"),
            activate.index("item.main.hidden = !active"),
        )

    def test_shells_reference_the_new_console_ui_cache_key(self):
        base_template = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        login_template = (CONSOLE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
        cache_key = "/static/console_ui.js?v=mobile-responsive-20260809"

        self.assertIn(cache_key, base_template)
        self.assertIn(cache_key, login_template)


if __name__ == "__main__":
    unittest.main()
