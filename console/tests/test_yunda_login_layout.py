"""Render the observed sandboxed three-frame login layout without real QR data."""

from pathlib import Path
import unittest


EXTENSION = Path(__file__).resolve().parents[1] / "static/browser_extensions/ronghui"


class YundaLoginLayoutTests(unittest.TestCase):
    def test_complete_qr_fits_in_sandboxed_login_at_multiple_viewports(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is required for the QR layout regression")
        css = (EXTENSION / "yunda-login.css").read_text(encoding="utf-8")
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Chromium is required for the QR layout regression")
            with playwright.chromium.launch() as browser:
                for width, height in ((1410, 599), (1920, 900)):
                    with self.subTest(viewport=(width, height)):
                        page = browser.new_page(viewport={"width": width, "height": height})
                        pages = {
                            "https://host.test/": '<iframe id="login" sandbox="allow-scripts allow-same-origin" src="https://sso.test/login"></iframe>',
                            "https://sso.test/login": '<iframe id="third_qrcode_container" scrolling="no" style="width:250px;min-height:250px;border:0" src="/public/feishu/login"></iframe>',
                            "https://sso.test/public/feishu/login": '<style>body{margin:0}</style><div class="qrcode-body"><p>飞书登录</p><div id="feishu_qrcode"><iframe style="width:250px;height:250px;border:0" scrolling="no" src="https://qr.test/"></iframe></div></div>',
                            "https://qr.test/": '<div id="qr" style="width:234px;height:234px;background:black"></div>',
                        }
                        page.route("**/*", lambda route: route.fulfill(body=pages[route.request.url], content_type="text/html"))
                        page.goto("https://host.test/")
                        owner = page.frames[1]
                        wrapper = next(f for f in page.frames if f.url.endswith("/public/feishu/login"))
                        qr = next(f for f in page.frames if f.url == "https://qr.test/")
                        wrapper.add_style_tag(content=css)
                        # 0.4.0 styled only this wrapper: 36px of the QR window is clipped.
                        self.assertEqual(wrapper.evaluate("innerHeight"), 250)
                        self.assertGreater(wrapper.locator("iframe").bounding_box()["height"], 200)
                        self.assertGreater(wrapper.evaluate("document.querySelector('iframe').getBoundingClientRect().bottom"), 250)

                        owner.add_style_tag(content=css)

                        self.assertEqual(wrapper.evaluate("innerHeight"), 294)
                        self.assertLessEqual(wrapper.evaluate("document.documentElement.scrollHeight"), 294)
                        self.assertLessEqual(wrapper.evaluate("document.querySelector('iframe').getBoundingClientRect().bottom"), 294)
                        self.assertLessEqual(qr.evaluate("document.querySelector('#qr').getBoundingClientRect().bottom"), qr.evaluate("innerHeight"))
                        page.close()
