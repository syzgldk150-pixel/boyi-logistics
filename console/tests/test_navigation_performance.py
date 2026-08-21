import shutil
import subprocess
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import LocalDocFlowApp


CONSOLE_DIR = Path(__file__).resolve().parents[1]


def _node_host_path(path: Path, node_binary: str) -> str:
    text = str(path)
    if not node_binary.lower().endswith(".exe") or not text.startswith("/"):
        return text
    converted = subprocess.run(
        ["wslpath", "-w", text],
        check=True,
        capture_output=True,
        text=True,
    )
    return converted.stdout.strip()


class _Handler:
    pass


class _AutomationAccountsTemplateEnv:
    def __init__(self):
        self.context = None

    def get_template(self, name):
        if name != "automation_accounts.html":
            raise AssertionError(f"unexpected template: {name}")
        env = self

        class _Template:
            def render(self, **context):
                env.context = context
                return "automation_accounts"

        return _Template()


class NavigationPerformanceTests(unittest.TestCase):
    def test_cal_console_design_tokens_and_project_spec_are_versioned(self):
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
        design = (CONSOLE_DIR.parent / "DESIGN.md").read_text(encoding="utf-8")

        self.assertIn("--primary: #111111", stylesheet)
        self.assertIn("--focus-strong: #3b82f6", stylesheet)
        self.assertIn("--surface: #f8f9fa", stylesheet)
        self.assertIn("Cal.com design-md", design)
        self.assertIn("Source Han Sans SC", design)
        self.assertIn("InterVariable-Latin.woff2", design)
        self.assertIn("新页面交付清单", design)
        self.assertNotIn("border-left: 3px solid transparent", stylesheet)

    def test_console_ui_has_abortable_navigation_and_page_runtime_cleanup(self):
        script = (CONSOLE_DIR / "static" / "console_ui.js").read_text(encoding="utf-8")

        self.assertIn("let navigationController", script)
        self.assertIn("let navigationSeq", script)
        self.assertIn("new AbortController()", script)
        self.assertIn("signal: controller.signal", script)
        self.assertIn("cleanupPageRuntime()", script)
        self.assertIn("runtime.listeners", script)
        self.assertIn("currentPageRuntime = tab.runtime", script)
        self.assertIn("originalWindowClearInterval", script)
        self.assertIn("updateActiveNav(url.pathname)", script)

    def test_console_ui_refreshes_feather_icons_with_root_scope(self):
        script = (CONSOLE_DIR / "static" / "console_ui.js").read_text(encoding="utf-8")

        self.assertIn("function refreshIcons(root = document)", script)
        self.assertIn('root.querySelectorAll("[data-feather]")', script)
        self.assertIn("icon.toSvg(attrs)", script)
        self.assertNotIn("script[src*='chart.js']", script)

    def test_base_template_does_not_load_chart_js_globally(self):
        template = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        login_template = (CONSOLE_DIR / "templates" / "login.html").read_text(encoding="utf-8")

        self.assertNotIn("cdn.jsdelivr.net/npm/chart.js", template)
        self.assertIn("/static/style.css?v=cal-console-20260821-plugin-manager1", template)
        self.assertIn("/static/assets/fonts/InterVariable-Latin.woff2", template)
        self.assertIn("/static/assets/fonts/SourceHanSansCN-UI.woff2", template)
        self.assertIn("/static/vendor/feather-4.29.2.min.js", template)
        self.assertIn("/static/console_ui.js?v=cal-console-20260815-tabs2", template)
        self.assertIn("/static/style.css?v=cal-console-20260815-project-plugin3", login_template)
        self.assertIn("/static/assets/fonts/InterVariable-Latin.woff2", login_template)
        self.assertIn("/static/assets/fonts/SourceHanSansCN-UI.woff2", login_template)
        self.assertIn("/static/vendor/feather-4.29.2.min.js", login_template)
        self.assertIn("/static/console_ui.js?v=cal-console-20260815-tabs2", login_template)
        self.assertNotIn("unpkg.com", template)
        self.assertNotIn("unpkg.com", login_template)
        self.assertNotIn("api.dicebear.com", template)
        self.assertIn("/static/assets/avatar-placeholder.svg", template)
        self.assertNotIn("partial-nav-logo-20260515", login_template)

    def test_console_bundles_the_specified_cjk_and_latin_fonts(self):
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
        login_template = (CONSOLE_DIR / "templates" / "login.html").read_text(encoding="utf-8")

        self.assertIn('font-family: "Source Han Sans SC";', stylesheet)
        self.assertIn('SourceHanSansCN-UI.woff2', stylesheet)
        self.assertIn('SourceHanSansCN-Common.woff2', stylesheet)
        self.assertIn('SourceHanSansCN-VF.ttf.woff2', stylesheet)
        self.assertIn('font-family: "Inter";', stylesheet)
        self.assertIn('InterVariable-Latin.woff2', stylesheet)
        self.assertIn(
            "--font-ui: var(--font-latin), var(--font-cjk), system-ui, -apple-system, sans-serif;",
            stylesheet,
        )
        self.assertNotIn(
            "--font-ui: var(--font-latin), system-ui, -apple-system, sans-serif, var(--font-cjk);",
            stylesheet,
        )
        self.assertNotIn("fonts.googleapis.com", stylesheet)
        self.assertTrue((CONSOLE_DIR / "static" / "assets" / "fonts" / "SourceHanSansCN-VF.ttf.woff2").is_file())
        self.assertTrue((CONSOLE_DIR / "static" / "assets" / "fonts" / "SourceHanSansCN-UI.woff2").is_file())
        self.assertTrue((CONSOLE_DIR / "static" / "assets" / "fonts" / "SourceHanSansCN-Common.woff2").is_file())
        self.assertTrue((CONSOLE_DIR / "static" / "assets" / "fonts" / "InterVariable-Latin.woff2").is_file())
        self.assertTrue((CONSOLE_DIR / "static" / "assets" / "fonts" / "InterVariable.woff2").is_file())
        self.assertFalse((CONSOLE_DIR / "static" / "assets" / "fonts" / "Roboto-Latin-Variable.woff2").exists())
        self.assertIn('data-ui-submit', login_template)
        self.assertIn('role="alert"', login_template)

    def test_public_brand_uses_boyi_logistics_wordmark_with_icon(self):
        template = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        login_template = (CONSOLE_DIR / "templates" / "login.html").read_text(encoding="utf-8")

        brand_start = template.index('<a class="brand"')
        brand_end = template.index("</a>", brand_start)
        sidebar_brand = template[brand_start:brand_end]
        login_brand_start = login_template.index('<div class="login-brand">')
        login_brand_end = login_template.index("</div>", login_brand_start)
        login_brand = login_template[login_brand_start:login_brand_end]

        for brand in (sidebar_brand, login_brand):
            self.assertIn('src="/static/assets/boyi-logistics-logo-7e1f2994.webp"', brand)
            self.assertIn('width="', brand)
            self.assertIn('height="', brand)
            self.assertNotIn("M13 2L3 14h9l-1 8 10-12h-9l1-8z", brand)
            self.assertNotIn("<svg", brand)
            self.assertNotIn("SHIPNOW", brand)

        logo = CONSOLE_DIR / "static" / "assets" / "boyi-logistics-logo-7e1f2994.webp"
        self.assertTrue(logo.is_file())
        self.assertLess(logo.stat().st_size, 50_000)

    def test_base_template_exposes_keep_alive_tab_shell(self):
        template = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("data-console-tabs", template)
        self.assertIn("data-console-page-stack", template)
        self.assertIn("data-console-tab-template", template)
        self.assertIn("data-console-tab-title", template)
        self.assertIn('class="top-header-right"', template)
        tabs_index = template.index("data-console-tabs")
        header_start = template.rindex('<header class="top-header"', 0, tabs_index)
        header_end = template.index("</header>", tabs_index)
        self.assertLess(header_start, tabs_index)
        self.assertLess(tabs_index, header_end)

    def test_console_ui_uses_keep_alive_tab_registry(self):
        script = (CONSOLE_DIR / "static" / "console_ui.js").read_text(encoding="utf-8")

        self.assertIn("const openTabs = new Map()", script)
        self.assertIn("function getTabKey(url)", script)
        self.assertIn("function activateTab(tabKey", script)
        self.assertIn("function closeTab(tabKey", script)
        self.assertIn("function ensureModuleTab(url", script)
        self.assertIn("cleanupPageRuntime(tab.runtime)", script)
        self.assertIn("tab.main.hidden = false", script)
        self.assertNotIn("tab.main.hidden = true", script)
        self.assertIn("item.main.hidden = !active", script)
        self.assertLess(
            script.index('"[data-nav-list] .nav-link[href]"'),
            script.index('"[data-shell-home-link][href]"'),
        )

    def test_keep_alive_tabs_have_layout_styles_for_fullscreen_pages(self):
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn(".console-tab-bar", stylesheet)
        self.assertIn(".console-tab.is-active", stylesheet)
        self.assertIn(".console-page-stack", stylesheet)
        self.assertIn(".ocr-page .console-page-stack", stylesheet)
        self.assertIn(".dispatch-page .console-page-stack", stylesheet)
        self.assertNotIn("--console-tabs-h", stylesheet)
        self.assertNotIn("height: calc(100vh - var(--console-tabs-h))", stylesheet)

    def test_console_tabs_use_stable_header_nav_layout(self):
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn(".top-header { display: flex; align-items: center;", stylesheet)
        self.assertIn("position: static", stylesheet)
        self.assertNotIn(".top-header { display: block; position: fixed;", stylesheet)
        self.assertNotIn("left: var(--sidebar-w)", stylesheet)
        self.assertNotIn(".top-header { display: grid;", stylesheet)
        self.assertNotIn("grid-template-columns: minmax(0, 1fr) minmax(320px, var(--header-search-w)) auto", stylesheet)
        self.assertIn(".console-tab-bar { position: static; flex: 1 1 auto;", stylesheet)
        self.assertIn("max-width: none", stylesheet)
        self.assertIn("justify-content: flex-start", stylesheet)
        self.assertIn(".main-content { grid-column: 2; grid-row: 1; min-width: 0; padding: 0 var(--content-x-pad) 40px;", stylesheet)
        self.assertIn(".top-header-right { display: flex; align-items: center; justify-content: flex-end;", stylesheet)
        self.assertIn("margin-left: auto", stylesheet)
        self.assertIn(".top-header-search { flex: 0 1 var(--header-search-w);", stylesheet)
        self.assertIn("margin-left: 0", stylesheet)
        self.assertIn(".top-header-actions--page { flex: 0 0 auto;", stylesheet)
        self.assertIn(".top-header-actions--page > * { flex: 0 0 auto;", stylesheet)
        self.assertIn(".console-tab.is-active::after", stylesheet)
        self.assertIn(".console-tab.is-pinned .console-tab-close", stylesheet)
        self.assertIn("display: none", stylesheet)
        self.assertIn("border-radius: 0", stylesheet)
        self.assertIn("--header-h: 88px", stylesheet)
        self.assertIn("--header-x-pad", stylesheet)
        self.assertIn("--content-x-pad: var(--header-x-pad)", stylesheet)
        self.assertIn("--tab-gap", stylesheet)
        self.assertIn("--tab-font-size", stylesheet)
        self.assertIn("--header-search-w", stylesheet)
        self.assertIn("--header-actions-w", stylesheet)
        self.assertIn(".console-tab-bar[hidden]", stylesheet)
        self.assertNotIn("padding: 152px var(--content-x-pad) 32px", stylesheet)

    def test_page_templates_do_not_override_shared_tab_header_position(self):
        waybills = (CONSOLE_DIR / "templates" / "waybills.html").read_text(encoding="utf-8")
        document = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        dispatch = (CONSOLE_DIR / "templates" / "dispatch.html").read_text(encoding="utf-8")

        self.assertNotIn(".waybills-page-body .top-header { align-items: stretch; flex-direction: column;", waybills)
        self.assertNotIn(".top-header { flex: 0 0 auto;", document)
        self.assertNotIn("calc(var(--header-h) + 20px) 24px 16px", document)
        self.assertNotIn("calc(var(--header-h) + 20px) 0 0", document)
        self.assertNotIn(".top-header { position: fixed;", dispatch)
        self.assertNotIn(".dispatch-page .main-content { padding: 0 !important;", dispatch)

    def test_fullscreen_pages_reuse_global_tab_sizing(self):
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
        dispatch = (CONSOLE_DIR / "templates" / "dispatch.html").read_text(encoding="utf-8")

        self.assertIn("--header-h: 88px", stylesheet)
        self.assertIn("--tab-gap: 16px", stylesheet)
        self.assertIn("--tab-font-size: 0.875rem", stylesheet)
        self.assertIn(".console-tab-bar { position: static; flex: 1 1 auto;", stylesheet)
        self.assertIn("min-height: 52px", stylesheet)
        self.assertIn(".console-tab-title span", stylesheet)
        self.assertIn("font-size: var(--tab-font-size)", stylesheet)
        self.assertIn(".dispatch-page .top-header { position: relative; z-index: 30;", stylesheet)
        self.assertIn("padding: 0 var(--header-x-pad)", stylesheet)
        self.assertNotIn("body.dispatch-page { --header-h:", stylesheet)
        self.assertNotIn("--tab-gap: 18px", stylesheet)
        self.assertNotIn("--tab-font-size: 0.86rem", stylesheet)
        self.assertNotIn(".dispatch-page .console-tab-bar", stylesheet)
        self.assertNotIn(".dispatch-page .console-tab-title", stylesheet)
        self.assertNotIn(".dispatch-page .console-tab-close", stylesheet)
        self.assertIn(".dispatch-page .nav-search--global { min-height: 42px;", stylesheet)
        self.assertIn(".dispatch-page .main-content { padding: 0 0 40px !important;", dispatch)

    def test_header_actions_keep_content_sized_controls(self):
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn("--radius-sm: 4px", stylesheet)
        self.assertIn("--shadow-hover:", stylesheet)
        self.assertIn(".top-header-actions--page { flex: 0 0 auto;", stylesheet)
        self.assertIn("flex-wrap: nowrap", stylesheet)
        self.assertIn(".top-header-actions--page > * { flex: 0 0 auto;", stylesheet)
        self.assertNotIn(".top-header-actions--page { flex: 0 0 var(--header-actions-w);", stylesheet)
        self.assertNotIn(".top-header-actions--page { flex-basis: auto; min-width: 0; }", stylesheet)
        self.assertIn(".automation-toolbar-field { display: inline-flex;", stylesheet)
        self.assertIn("white-space: nowrap", stylesheet)
        self.assertNotIn(".tms-dot-wrap", stylesheet)
        self.assertIn(".automation-account-add-btn { display: inline-flex;", stylesheet)
        self.assertIn("line-height: 1", stylesheet)

    def test_console_ui_keeps_home_tab_pinned_and_visible(self):
        script = (CONSOLE_DIR / "static" / "console_ui.js").read_text(encoding="utf-8")

        self.assertIn('const homeTab = openTabs.get("/")', script)
        self.assertIn('const tabs = homeTab ? [homeTab, ...otherTabs] : otherTabs', script)
        self.assertIn("tabsRoot.hidden = tabs.length === 0", script)
        self.assertIn('pinned: key === "/"', script)
        self.assertIn('pinned: tabKey === "/"', script)
        self.assertIn("closeButton.hidden = Boolean(tab.pinned)", script)
        self.assertNotIn("hideSingleHomeTab", script)

    def test_direct_refresh_bootstraps_pinned_overview_before_active_module(self):
        script = (CONSOLE_DIR / "static" / "console_ui.js").read_text(encoding="utf-8")
        base = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        helper = script[
            script.index("function ensureOverviewPlaceholder"):
            script.index("function ensureInitialTab")
        ]
        self.assertIn('if (currentKey === "/" || openTabs.has("/"))', helper)
        self.assertIn('openTabs.set("/", {', helper)
        self.assertIn('key: "/"', helper)
        self.assertIn("pinned: true", helper)
        self.assertIn("placeholder: true", helper)
        self.assertIn("main: null", helper)
        for path in ("/modules/finance", "/automations"):
            with self.subTest(path=path):
                # Both direct module URLs take the non-overview branch, so the
                # fixed placeholder is inserted before ensureInitialTab adds
                # and activates the current module.
                self.assertNotEqual("/", path)
                projected_tabs = ["/", path]
                self.assertEqual("/", projected_tabs[0])
                self.assertEqual(path, projected_tabs[1])
                self.assertEqual(path, projected_tabs[-1])

        self.assertIn('const tabs = homeTab ? [homeTab, ...otherTabs] : otherTabs', script)
        self.assertIn("activateTab(key, { pushState: false, skipScroll: true })", script)
        self.assertIn("if (tab.placeholder)", script)
        self.assertIn("void ensureModuleTab(tab.url, { reload: true", script)
        self.assertIn("window.addEventListener(\"popstate\"", script)
        self.assertIn("(tab.pinned && !options.force)", script)
        self.assertIn("closeButton.hidden = Boolean(tab.pinned)", script)
        self.assertIn("data-console-tab-close", base)

    def test_finance_deep_link_executes_with_pinned_overview_and_active_module(self):
        node_binary = shutil.which("node") or shutil.which("node.exe")
        if node_binary is None:
            self.skipTest("Node.js is required for the executable Console DOM regression")
        dom_test = Path(__file__).with_name("console_ui_deeplink_dom.test.cjs")
        console_ui = CONSOLE_DIR / "static" / "console_ui.js"
        completed = subprocess.run(
            [
                node_binary,
                _node_host_path(dom_test, node_binary),
                _node_host_path(console_ui, node_binary),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            0,
            completed.returncode,
            msg=f"{completed.stdout}\n{completed.stderr}",
        )

    def test_start_backend_health_check_uses_home_entry(self):
        script = (CONSOLE_DIR / "start_backend.sh").read_text(encoding="utf-8")

        self.assertIn('health_url="http://127.0.0.1:${PORT}/"', script)
        self.assertNotIn('health_url="http://127.0.0.1:${PORT}/automations"', script)

    def test_automation_accounts_page_uses_cached_initial_load(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(app_title="Console")
        app.template_env = _AutomationAccountsTemplateEnv()
        calls = []

        def fetch_accounts(self, *, force=True, prefer_cached=False):
            calls.append((force, prefer_cached))
            return [], ""

        def send_html(self, handler, body):
            self.sent_body = body

        app._fetch_automation_accounts = types.MethodType(fetch_accounts, app)
        app._send_html = types.MethodType(send_html, app)

        app._render_automation_accounts(_Handler(), {})

        self.assertEqual("automation_accounts", app.sent_body)
        self.assertEqual([(False, True)], calls)

    def test_automation_accounts_template_polls_cached_statuses(self):
        template = (CONSOLE_DIR / "templates" / "automation_accounts.html").read_text(encoding="utf-8")

        self.assertIn('/automation-accounts/statuses?force=1&prefer_cached=1', template)
        self.assertIn("statusPollIntervalMs = 60000", template)


if __name__ == "__main__":
    unittest.main()
