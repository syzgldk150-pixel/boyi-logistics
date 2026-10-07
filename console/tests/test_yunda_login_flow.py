"""Exercise the actual MV3 helper across sandbox login, popup and entry return."""

from pathlib import Path
import tempfile
import unittest

EXTENSION = Path(__file__).resolve().parents[1] / "static/browser_extensions/ronghui"
BOYI = "https://boyi.homes"
SSO = "https://ky-sso.yunda56.com"
YUNDA = "https://kyinms.yunda56.com"
ENTRY = YUNDA + "/ky_inms/public/index.php/business/waybill/entry/indexNew.html"
HOME = YUNDA + "/ky_inms/public/index.php/index/index.html"
SANDBOX = "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"


class YundaLoginFlowTests(unittest.TestCase):
    def test_real_extension_uses_popup_and_restores_only_its_entry(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("Playwright is required for the login flow regression")
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Chromium is required for the login flow regression")
            with tempfile.TemporaryDirectory() as profile:
                context = playwright.chromium.launch_persistent_context(
                    profile, channel="chromium", headless=True,
                    args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
                )
                try:
                    signed_in = False
                    entry_reads = 0

                    def respond(route):
                        nonlocal signed_in, entry_reads
                        url = route.request.url
                        if url.startswith(BOYI + "/ocr"):
                            body = f"""<!doctype html><style>
                            body{{font-family:Arial;margin:24px}}
                            .entry-instance-frame{{display:block;width:900px;height:500px;border:0}}
                            </style>
                            <section data-entry-frame-panel data-entry-provider="yunda" data-entry-id="entry-1">
                            <div class="entry-origin-notice"><p>请在新窗口扫码登录韵达</p>
                            <a href="{ENTRY}?page=tab&p=nil" data-entry-login="yunda" target="_blank">在新窗口登录韵达</a>
                            <button data-entry-reload>重新加载</button></div>
                            <div><iframe class="entry-instance-frame" data-yunda-live-frame sandbox="{SANDBOX}" src="{ENTRY}?page=tab&p=nil"></iframe></div>
                            </section>
                            <section data-entry-frame-panel data-entry-provider="boyi" data-entry-id="entry-2">
                            <input id="unsaved" value="keep this draft"></section>
                            <script>
                            document.querySelector('[data-entry-reload]').onclick=()=>{{
                              const frame=document.querySelector('[data-yunda-live-frame]');
                              frame.dispatchEvent(new Event('console:original-page-reload'));
                              frame.src='{ENTRY}?page=tab&p=nil';
                            }};
                            </script>"""
                        elif url.startswith(SSO + "/login"):
                            body = f"""<!doctype html><title>韵达登录</title>
                            <script>if(window.top!==window.self)window.top.location=window.self.location;</script>
                            <p>模拟扫码确认</p><button onclick="location.href='{SSO}/complete-test'">完成测试登录</button>"""
                        elif url == SSO + "/complete-test":
                            signed_in = True
                            route.fulfill(body=f"<script>location.replace({HOME!r})</script>", content_type="text/html; charset=utf-8")
                            return
                        elif url == HOME:
                            body = '<nav id="admin-navbar-side">已登录</nav>'
                        elif url.startswith(ENTRY):
                            entry_reads += 1
                            if not signed_in:
                                route.fulfill(body=f"<script>location.replace({(SSO + '/login')!r})</script>", content_type="text/html; charset=utf-8")
                                return
                            body = '<label>运单号<input name="LogisticsId"></label>'
                        else:
                            route.fulfill(status=404, body="")
                            return
                        route.fulfill(body=body, content_type="text/html; charset=utf-8")

                    context.route("**/*", respond)
                    page = context.new_page()
                    page.goto(BOYI + "/ocr")
                    prompt = page.locator("[data-yunda-login-required]")
                    expect(prompt).to_be_visible()
                    expect(page.locator("[data-yunda-live-frame]")).to_be_hidden()
                    self.assertEqual(page.url, BOYI + "/ocr")

                    # A duplicate notification must not create a second action.
                    login = next(frame for frame in page.frames if frame.url == SSO + "/login")
                    login.evaluate("(origin)=>parent.postMessage({type:'boyi-yunda-login-required'},origin)", BOYI)
                    expect(prompt).to_have_count(1)

                    # Standalone SSO pages retain their native UI and do not return.
                    standalone = context.new_page()
                    standalone.goto(SSO + "/login")
                    expect(standalone.get_by_role("button", name="完成测试登录")).to_be_visible()
                    expect(standalone.locator("[data-yunda-login-required]")).to_have_count(0)
                    standalone.close()

                    with context.expect_page() as popup_event:
                        page.get_by_role("button", name="打开韵达扫码登录").click()
                    popup = popup_event.value
                    expect(popup.get_by_role("button", name="完成测试登录")).to_be_visible()
                    with popup.expect_event("close"):
                        popup.get_by_role("button", name="完成测试登录").click()
                    expect(prompt).to_have_count(0)
                    expect(page.locator("[data-yunda-live-frame]")).to_be_visible()
                    expect(page.frame_locator("[data-yunda-live-frame]").locator('[name="LogisticsId"]')).to_be_visible()
                    expect(page.locator("#unsaved")).to_have_value("keep this draft")
                    self.assertEqual(entry_reads, 3)  # original, login helper, return
                    self.assertEqual(page.url, BOYI + "/ocr")

                    # Correct message type from a wrong origin is ignored.
                    entry = next(frame for frame in page.frames if frame.url.startswith(ENTRY))
                    entry.evaluate("(origin)=>parent.postMessage({type:'boyi-yunda-login-required'},origin)", BOYI)
                    page.wait_for_timeout(50)
                    expect(prompt).to_have_count(0)
                    # An unrelated SSO frame cannot hide the entry.
                    page.evaluate("(url)=>{const f=document.createElement('iframe');f.id='unrelated';f.src=url;document.body.append(f)}", SSO + "/login")
                    expect(page.frame_locator("#unrelated").get_by_role("button", name="完成测试登录")).to_be_visible()
                    expect(prompt).to_have_count(0)
                    expect(page.locator("[data-yunda-live-frame]")).to_be_visible()
                finally:
                    context.close()


if __name__ == "__main__":
    unittest.main()
