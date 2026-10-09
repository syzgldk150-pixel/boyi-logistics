"""Exercise native JS cookie refresh in a real, isolated MV3 browser."""

from pathlib import Path
import json
import tempfile
import unittest
from urllib.parse import urlsplit


CONSOLE = Path(__file__).resolve().parents[1]
EXTENSION = CONSOLE / "static/browser_extensions/ronghui"
BOYI = "https://boyi.homes"
RONGHUI = "https://tms.ronghuiwl.com"

HOST_HTML = f'''<!doctype html><meta charset="utf-8">
<input id="unsaved" value="keep this draft">
<iframe data-ronghui-live-frame data-entry-frame-bound="1"
  data-entry-pending-src="{RONGHUI}/module/index?mv=index"
  referrerpolicy="no-referrer"
  sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals"></iframe>'''

NATIVE_HOME = '''<!doctype html><meta charset="utf-8">
<script>window.mini={get:()=>null};</script>
<button onclick="document.querySelector('iframe').src='/widget/home?next=1'">Open entry</button>
<iframe src="/widget/home"></iframe>'''

NATIVE_ENTRY = '''<!doctype html><meta charset="utf-8">
<output id="site"></output><output id="map"></output><output id="address"></output>
<script>
window.mini={get:()=>null,decode:JSON.parse,Cookie:{get(name){
  const pair=document.cookie.split('; ').find(item=>item.startsWith(name+'='));
  return pair?decodeURIComponent(pair.slice(name.length+1)):null;
}}};
window.$Z={user:{getUserInfo:()=>mini.decode(mini.Cookie.get('userInfo'))}};
window.crud={
  setSendData(){document.querySelector('#site').textContent=$Z.user.getUserInfo().loginSiteName;},
  init(){
    this.setSendData();
    document.querySelector('#map').textContent='synthetic map initialized';
    document.querySelector('#address').textContent='synthetic address initialized';
  }
};
crud.init();
</script>'''


class RonghuiCookieRefreshTests(unittest.TestCase):
    def test_native_js_refresh_remains_readable_in_nested_cross_site_entry(self):
        from playwright.sync_api import expect, sync_playwright

        version = json.loads((EXTENSION / "manifest.json").read_text())["version"]
        with sync_playwright() as playwright, tempfile.TemporaryDirectory() as profile:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Chromium is required for the extension regression")
            context = playwright.chromium.launch_persistent_context(
                profile, channel="chromium", headless=True,
                args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
            )
            reads = {"home": 0, "entry": 0}
            try:
                def respond(route):
                    url = urlsplit(route.request.url)
                    if url.netloc == "boyi.homes":
                        body = HOST_HTML
                    elif url.netloc == "tms.ronghuiwl.com" and url.path == "/synthetic-login":
                        body = '<!doctype html><meta charset="utf-8"><p>Synthetic source</p>'
                    elif url.netloc == "tms.ronghuiwl.com" and url.path == "/module/index":
                        reads["home"] += 1
                        body = NATIVE_HOME
                    elif url.netloc == "tms.ronghuiwl.com" and url.path == "/widget/home":
                        reads["entry"] += 1
                        body = NATIVE_ENTRY
                    else:
                        route.fulfill(status=404, body="")
                        return
                    route.fulfill(body=body, content_type="text/html; charset=utf-8")

                context.route("**/*", respond)
                source = context.new_page()
                source.goto(RONGHUI + "/synthetic-login")
                write_profile = """site => {
                  const profile=encodeURIComponent(JSON.stringify({loginSiteName:site}));
                  // Host-only, non-Secure Lax matches the observed native refresh.
                  document.cookie='userInfo='+profile+'; Path=/; SameSite=Lax';
                }"""
                source.evaluate(write_profile, "synthetic-site-1")
                host = context.new_page()
                host.goto(BOYI + "/ocr")
                outer = host.locator('[data-ronghui-live-frame]')
                expect(outer).to_have_attribute("data-ronghui-extension", version)
                native = host.frame_locator('[data-ronghui-live-frame]')
                entry = native.frame_locator('iframe')
                expect(entry.locator('#site')).to_have_text("synthetic-site-1")
                expect(entry.locator('#map')).to_have_text("synthetic map initialized")

                # This write occurs after initial preparation. Only onChanged can
                # restore cross-site access; the outer frame is never re-prepared.
                source.evaluate(write_profile, "synthetic-site-2")
                live_entry = next(frame for frame in host.frames if urlsplit(frame.url).path == "/widget/home")
                live_entry.wait_for_function(
                    "window.$Z.user.getUserInfo()?.loginSiteName === 'synthetic-site-2'", timeout=5000,
                )
                self.assertTrue(live_entry.evaluate("document.hasStorageAccess()"))
                native.get_by_role("button", name="Open entry").click()
                expect(entry.locator('#site')).to_have_text("synthetic-site-2")
                expect(entry.locator('#map')).to_have_text("synthetic map initialized")
                expect(entry.locator('#address')).to_have_text("synthetic address initialized")
                expect(host.locator('#unsaved')).to_have_value("keep this draft")
                self.assertEqual(reads, {"home": 1, "entry": 2})
            finally:
                context.close()
