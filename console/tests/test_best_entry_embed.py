"""Run the shipped MV3 extension and entry-tab code against native-shaped pages."""

from pathlib import Path
import json
import tempfile
import unittest

from jinja2 import Environment


CONSOLE = Path(__file__).resolve().parents[1]
EXTENSION = CONSOLE / "static/browser_extensions/ronghui"
BOYI = "https://boyi.homes"
BEST = "https://v5.800best.com"
ENTRY = BEST + "/baseService/transOrder/networkProductOrder"
OLD_ENTRY = BEST + "/baseService/transOrder/createOrder"
MENU = BEST + "/ltlv5-war/web/menu/getUserMenuVos"


def host_html():
    template = (CONSOLE / "templates/document.html").read_text(encoding="utf-8")
    start = template.index("{% macro entry_origin_notice(provider) %}")
    end = template.index("{% endmacro %}", start) + len("{% endmacro %}")
    notice = Environment(autoescape=True).from_string(
        template[start:end] + "{{ entry_origin_notice('best') }}"
    ).render(entry_best_src=ENTRY)
    start = template.index('    const entryTabsRoot =')
    end = template.index('    bindInitialEntryTabs();', start) + len('    bindInitialEntryTabs();')
    return f'''<!doctype html><meta charset="utf-8">
    <style>iframe{{width:100%;height:450px}}[hidden]{{display:none!important}}</style>
    <div data-entry-tabs-root data-entry-src-best="{ENTRY}">
      <div data-entry-tab-list><div data-entry-tab data-entry-provider="boyi" data-entry-id="entry-1"></div></div>
      <button data-entry-add-provider="best">百世原页</button><div data-entry-tab-limit hidden></div>
      <template data-entry-origin-template="best">{notice}</template>
      <div data-entry-frame-stack><section data-entry-frame-panel data-entry-provider="boyi" data-entry-id="entry-1">
        <input id="unsaved" value="keep this draft"></section></div>
    </div><script>{template[start:end]}</script>'''


NATIVE_ENTRY = '''<!doctype html><meta charset="utf-8">
<style>
body{margin:0}.g-header{position:fixed;top:0;height:64px;width:100%;background:blue}
.g-main{padding-top:101px;min-height:100%}
.m-header-menu{position:fixed;top:64px;height:37px;width:100%;background:white;transition:top .3s ease-out}
.g-container{position:relative;padding:8px 15px 5px;min-height:540px}
.p-networkCreateOrder .order-btns{position:fixed;top:97px;height:32px;width:100%}
.p-networkCreateOrder .s-table-toolbar{position:fixed;top:106px;height:21px}
.tab-create-order{position:relative;margin-top:33px}.order-container{min-height:900px}
.coupon-window-container{position:fixed;top:0}.specialArea-container{position:fixed;top:100px}
</style>
<div class="g-page"><header class="g-header"><nav>Native logo and main navigation</nav></header>
<main class="g-main"><div class="m-header-menu">首页 首页(旧) 运单录入</div>
<div class="g-container p-networkCreateOrder">
<div role="tablist">
  <div role="tab" aria-selected="true" onclick="selectTab(false)">草稿箱</div>
  <div role="tab" aria-selected="false" onclick="selectTab(true)">运单录入</div>
</div>
<div role="tabpanel" aria-hidden="false">草稿列表
  <div class="order-btns"><button>Draft toolbar</button></div>
  <div class="s-table-toolbar">Draft filters</div>
</div>
<div role="tabpanel" aria-hidden="true" hidden></div>
<div id="native-map">Native map</div>
<div class="coupon-window-container" hidden>Coupon dialog</div>
<div class="specialArea-container" hidden>Special area dialog</div>
</div><footer class="g-footer">Native footer</footer></main></div>
<script>
function selectTab(entry) {
  document.querySelectorAll('[role=tab]').forEach((tab,i)=>tab.setAttribute('aria-selected',String(!!i===entry)));
  document.querySelectorAll('[role=tabpanel]').forEach((panel,i)=>{
    panel.setAttribute('aria-hidden',String(!!i!==entry)); panel.hidden=!!i!==entry;
    if(i && entry && !panel.querySelector('input')) {
      panel.innerHTML='<article class="tab-create-order"><section class="order-container">'+
        '<input id="code"><input id="sendPhone"></section></article>';
      setTimeout(()=>{
        const toolbar=document.createElement('section');toolbar.className='order-btns';
        toolbar.innerHTML='<button>Native toolbar</button>';
        panel.querySelector('article').prepend(toolbar);
      },20);
    }
  });
}
</script>'''


class BestEntryEmbedTests(unittest.TestCase):
    def test_hidden_initial_frame_stops_measuring_and_preserves_draft_when_shown(self):
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright, tempfile.TemporaryDirectory() as profile:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Chromium is required for the extension regression")
            context = playwright.chromium.launch_persistent_context(
                profile, channel="chromium", headless=True,
                args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
            )
            try:
                context.route("**/*", lambda route: route.fulfill(
                    body=(host_html() + '<style id="hide-entry">[data-best-live-frame]{display:none}</style>')
                    if route.request.url.startswith(BOYI) else NATIVE_ENTRY + '''<script>
                      selectTab(true);
                      window.keptInput = document.querySelector('#code');
                      window.keptInput.value = 'keep hidden native draft';
                      document.querySelector('.g-page').style.display = 'none';
                    </script>''',
                    content_type="text/html; charset=utf-8",
                ))
                page = context.new_page()
                page.goto(BOYI + "/ocr")
                page.get_by_role("button", name="百世原页", exact=True).click()
                embedded = page.frame_locator('[data-best-live-frame]')
                expect(embedded.locator('.g-page')).to_be_attached()
                frame = next(frame for frame in page.frames if frame.url == ENTRY)
                self.assertEqual(frame.locator('.g-header').evaluate(
                    "header => header.getBoundingClientRect().height"), 0)
                frame.evaluate("""() => {
                  window.rootMutations = 0;
                  new MutationObserver(records => {
                    window.rootMutations += records.filter(record =>
                      record.target === document.documentElement).length;
                  }).observe(document.documentElement, {attributes:true, attributeFilter:['class','style']});
                  document.documentElement.classList.add('synthetic-hidden-page');
                  document.documentElement.style.setProperty('--synthetic-draft', 'kept');
                }""")
                # Chromium suspends rAF in a display:none iframe. Reveal the
                # frame while its lazy native shell is still unmeasurable, so
                # an observer/measurement loop would actually execute.
                page.locator('#hide-entry').evaluate('style => style.remove()')
                rendered_frames = """async count => {
                  for(let i=0;i<count;i++) await new Promise(requestAnimationFrame);
                }"""
                frame.evaluate(rendered_frames, 12)
                first_count = frame.evaluate('window.rootMutations')
                self.assertGreater(first_count, 0)
                frame.evaluate(rendered_frames, 12)
                self.assertEqual(frame.evaluate('window.rootMutations'), first_count)
                frame.locator('.g-page').evaluate("shell => shell.style.removeProperty('display')")
                expect(embedded.locator('#code')).to_be_visible()
                expect(embedded.locator('.g-header')).to_be_hidden()
                expect(embedded.locator('.m-header-menu')).to_have_css('top', '0px')
                expect(embedded.locator('.g-main')).to_have_css('padding-top', '37px')
                expect(embedded.locator('#code')).to_have_value('keep hidden native draft')
                self.assertTrue(frame.evaluate("window.keptInput === document.querySelector('#code')"))
                expect(page.locator('#unsaved')).to_have_value('keep this draft')
            finally:
                context.close()

    def test_layout_preserves_native_tabs_drafts_and_spa_navigation(self):
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright, tempfile.TemporaryDirectory() as profile:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Chromium is required for the extension regression")
            context = playwright.chromium.launch_persistent_context(
                profile, channel="chromium", headless=True,
                args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
            )
            entry_reads = 0
            try:
                def respond(route):
                    nonlocal entry_reads
                    if route.request.url.startswith(BOYI):
                        body = host_html()
                    else:
                        body = NATIVE_ENTRY
                        if route.request.url == ENTRY:
                            entry_reads += 1
                    route.fulfill(body=body, content_type="text/html; charset=utf-8")

                context.route("**/*", respond)
                page = context.new_page()
                page.goto(BOYI + "/ocr")
                page.get_by_role("button", name="百世原页", exact=True).click()
                embedded = page.frame_locator('[data-best-live-frame]')
                toggle = page.locator('[data-best-layout-toggle]')
                expect(toggle).to_be_visible()
                expect(embedded.locator('.g-header')).to_be_hidden()
                expect(embedded.locator('.m-header-menu')).to_be_visible()
                expect(embedded.locator('.m-header-menu')).to_have_css('top', '0px')
                expect(embedded.locator('.g-main')).to_have_css('padding-top', '37px')
                expect(embedded.get_by_role('tab', name='草稿箱')).to_be_visible()
                expect(embedded.get_by_role('tab', name='运单录入')).to_be_visible()
                expect(embedded.get_by_role('button', name='Native toolbar')).to_be_visible()
                expect(embedded.locator('section.order-btns')).to_have_css('position', 'static')
                expect(embedded.locator('section.order-btns')).to_have_css('top', 'auto')
                expect(embedded.locator('.s-table-toolbar')).to_have_css('top', '42px')
                expect(embedded.locator('.coupon-window-container')).to_have_css('top', '0px')
                expect(embedded.locator('.specialArea-container')).to_have_css('top', '100px')
                toolbar_box = embedded.locator('section.order-btns').bounding_box()
                field_box = embedded.locator('#code').bounding_box()
                self.assertLessEqual(toolbar_box['y'] + toolbar_box['height'], field_box['y'])
                expect(embedded.locator('#native-map')).to_be_visible()
                embedded.locator('#code').fill('keep native draft')
                frame = next(frame for frame in page.frames if frame.url == ENTRY)
                frame.evaluate("window.keptInput = document.querySelector('#code')")
                scroll_geometry = """() => ({
                  toolbar:document.querySelector('section.order-btns').getBoundingClientRect().top,
                  field:document.querySelector('#code').getBoundingClientRect().top,
                  scroll:window.scrollY
                })"""
                def assert_scrolls_with_form():
                    before_scroll = frame.evaluate(scroll_geometry)
                    frame.evaluate("window.scrollTo(0, 100)")
                    after_scroll = frame.evaluate(scroll_geometry)
                    delta = after_scroll['scroll'] - before_scroll['scroll']
                    self.assertGreater(delta, 0)
                    self.assertAlmostEqual(after_scroll['toolbar'] - before_scroll['toolbar'], -delta)
                    self.assertAlmostEqual(after_scroll['field'] - before_scroll['field'], -delta)
                    frame.evaluate("window.scrollTo(0, 0)")

                assert_scrolls_with_form()

                # Wrong-origin host messages and messages from the frame itself
                # must not change the native layout or its controlling button.
                page.evaluate("""() => window.postMessage({type:'boyi-best-layout-state',
                  available:true, focused:false},location.origin)""")
                frame.evaluate("""() => window.postMessage({type:'boyi-best-layout-set',
                  focused:false},location.origin)""")
                expect(toggle).to_have_attribute('data-focused', 'true')
                expect(embedded.locator('.g-header')).to_be_hidden()
                toggle.click()
                expect(embedded.locator('.g-header')).to_be_visible()
                expect(embedded.locator('.m-header-menu')).to_have_css('top', '64px')
                expect(embedded.locator('.g-main')).to_have_css('padding-top', '101px')
                expect(embedded.locator('section.order-btns')).to_have_css('position', 'static')
                expect(embedded.locator('section.order-btns')).to_have_css('top', 'auto')
                expect(embedded.locator('.s-table-toolbar')).to_have_css('top', '106px')
                expect(embedded.locator('#code')).to_have_value('keep native draft')
                assert_scrolls_with_form()

                # A user's expanded-menu preference survives leaving and returning.
                frame.evaluate("history.pushState({}, '', '/synthetic-native-menu')")
                expect(toggle).to_be_hidden()
                expect(embedded.locator('.g-header')).to_be_visible()
                expect(embedded.locator('section.order-btns')).to_have_css('position', 'fixed')
                expect(embedded.locator('section.order-btns')).to_have_css('top', '97px')
                frame.evaluate("url => history.pushState({}, '', new URL(url).pathname)", ENTRY)
                expect(toggle).to_be_visible()
                expect(embedded.locator('.g-header')).to_be_visible()
                expect(embedded.locator('section.order-btns')).to_have_css('position', 'static')
                toggle.click()
                expect(embedded.locator('.g-header')).to_be_hidden()
                frame.evaluate("history.pushState({}, '', '/synthetic-native-menu')")
                expect(embedded.locator('.g-header')).to_be_visible()
                frame.evaluate("url => history.pushState({}, '', new URL(url).pathname)", ENTRY)
                expect(embedded.locator('.g-header')).to_be_hidden()

                # Measure responsive native dimensions, never crop fixed pixels.
                frame.evaluate("""() => {
                  document.querySelector('.g-header').style.height='82px';
                  document.querySelector('.g-main').style.paddingTop='119px';
                  document.querySelector('.m-header-menu').style.top='82px';
                  document.querySelectorAll('.order-btns').forEach(toolbar=>toolbar.style.top='115px');
                  document.querySelector('.s-table-toolbar').style.top='124px';
                  window.dispatchEvent(new Event('resize'));
                }""")
                expect(embedded.locator('.g-main')).to_have_css('padding-top', '37px')
                expect(embedded.locator('.m-header-menu')).to_have_css('top', '0px')
                expect(embedded.locator('section.order-btns')).to_have_css('position', 'static')
                expect(embedded.locator('.s-table-toolbar')).to_have_css('top', '42px')
                toolbar_box = embedded.locator('section.order-btns').bounding_box()
                field_box = embedded.locator('#code').bounding_box()
                self.assertLessEqual(toolbar_box['y'] + toolbar_box['height'], field_box['y'])
                frame.evaluate("""async () => {
                  for(let i=0;i<6;i++) {
                    window.dispatchEvent(new Event('resize'));
                    await new Promise(requestAnimationFrame);
                  }
                }""")
                expect(embedded.locator('.m-header-menu')).to_have_css('top', '0px')
                expect(embedded.locator('.g-main')).to_have_css('padding-top', '37px')
                expect(embedded.locator('#code')).to_have_value('keep native draft')
                self.assertTrue(frame.evaluate("window.keptInput === document.querySelector('#code')"))
                self.assertEqual(entry_reads, 1)

                standalone = context.new_page()
                standalone.goto(ENTRY)
                expect(standalone.locator('.g-header')).to_be_visible()
                expect(standalone.locator('.m-header-menu')).to_have_css('top', '64px')
                expect(standalone.locator('.g-main')).to_have_css('padding-top', '101px')
            finally:
                context.close()

    def test_reloading_extension_requires_host_refresh_and_preserves_drafts(self):
        from playwright.sync_api import expect, sync_playwright

        version = json.loads((EXTENSION / "manifest.json").read_text())["version"]
        with sync_playwright() as playwright, tempfile.TemporaryDirectory() as profile:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Chromium is required for the extension regression")
            context = playwright.chromium.launch_persistent_context(
                profile, channel="chromium", headless=True,
                args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
            )
            try:
                context.route("**/*", lambda route: route.fulfill(
                    body=host_html() if route.request.url.startswith(BOYI) else NATIVE_ENTRY,
                    content_type="text/html; charset=utf-8",
                ))
                page = context.new_page()
                page.goto(BOYI + "/ocr")
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                page.locator('[data-entry-add-provider="best"]').click()
                expect(page.locator('[data-best-live-frame]').first).to_have_attribute("data-best-extension", version)
                with worker.expect_event("close"):
                    worker.evaluate("setTimeout(() => chrome.runtime.reload(), 100)")
                page.locator('[data-entry-add-provider="best"]').click()
                failed = page.locator('[data-best-live-frame]').nth(1)
                expect(failed).to_have_attribute("data-best-extension", "failed")
                expect(failed).to_have_attribute("data-original-page-prepare-error", "extension-reloaded")
                self.assertIsNone(failed.get_attribute("src"))
                expect(page.locator('#unsaved')).to_have_value("keep this draft")

            finally:
                context.close()

    def test_login_returns_to_embedded_form_and_preserves_manual_tab_choice(self):
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Chromium is required for the extension regression")
            with tempfile.TemporaryDirectory() as profile:
                context = playwright.chromium.launch_persistent_context(
                    profile, channel="chromium", headless=True,
                    args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
                )
                try:
                    entry_reads = 0

                    def respond(route):
                        nonlocal entry_reads
                        url = route.request.url
                        if url == BOYI + "/ocr":
                            body = host_html()
                        elif url == MENU:
                            logged_in = "sid=synthetic-best-session" in (route.request.header_value("cookie") or "")
                            route.fulfill(json={"code": "200", "vo": {"menuTreeNode": {"children": []}}}
                                          if logged_in else {"code": "40001", "msgs": []})
                            return
                        elif url == BEST + "/login":
                            body = f'<button onclick="location.href=\'{BEST}/complete-test\'">完成测试登录</button>'
                        elif url == BEST + "/complete-test":
                            route.fulfill(
                                content_type="text/html; charset=utf-8",
                                headers={"Set-Cookie": "sid=synthetic-best-session; Path=/; Secure; HttpOnly; SameSite=Lax"},
                                body=f'''<script>
                                localStorage.setItem("best-test-context","synthetic");
                                document.cookie="userName=synthetic-best-user; Path=/; SameSite=Lax";
                                location.replace({ENTRY!r});</script>''',
                            )
                            return
                        elif url == OLD_ENTRY:
                            body = NATIVE_ENTRY
                        elif url == ENTRY:
                            entry_reads += 1
                            if "sid=synthetic-best-session" in (route.request.header_value("cookie") or ""):
                                # BEST needs the original page's browser storage,
                                # which a direct cross-site iframe cannot share.
                                body = NATIVE_ENTRY + f'''<script>
                                if(localStorage.getItem('best-test-context')!=='synthetic' ||
                                  !document.cookie.includes('userName=synthetic-best-user'))
                                  location.replace({(BEST + '/login')!r});
                                </script>'''
                            else:
                                # Real BEST paints cached entry tabs before its
                                # protected request rejects the expired session.
                                body = NATIVE_ENTRY + f'''<script>
                                fetch({MENU!r}).then(r=>r.json()).then(result=>{{
                                  if(result.code==='40001') setTimeout(()=>location.replace({(BEST + "/login")!r}),500);
                                }});
                                </script>'''
                        else:
                            route.fulfill(status=404, body="")
                            return
                        route.fulfill(body=body, content_type="text/html; charset=utf-8")

                    context.route("**/*", respond)
                    page = context.new_page()
                    page.goto(BOYI + "/ocr")
                    pages_before = len(context.pages)
                    page.get_by_role("button", name="百世原页", exact=True).click()
                    expect(page.locator('[data-best-login-required]')).to_be_visible()
                    self.assertEqual(len(context.pages), pages_before)
                    expect(page.locator('[data-entry-tab][data-entry-provider="best"]')).to_have_count(1)
                    with context.expect_page() as popup_event:
                        page.get_by_role("button", name="打开百世扫码登录").click()
                    popup = popup_event.value
                    expect(popup.get_by_role("button", name="完成测试登录")).to_be_visible()
                    self.assertFalse(popup.is_closed())
                    # A user may switch Boyi's internal tab while scanning.
                    page.locator('[data-entry-frame-panel][data-entry-provider="best"]').evaluate(
                        "panel=>panel.classList.remove('is-active')"
                    )
                    with popup.expect_event("close"):
                        popup.get_by_role("button", name="完成测试登录").click()
                    embedded = page.frame_locator('[data-best-live-frame]')
                    expect(embedded.locator('[role=tabpanel][aria-hidden=false] #sendPhone')).to_be_visible()
                    expect(embedded.get_by_role("tab", name="运单录入")).to_have_attribute("aria-selected", "true")
                    expect(page.locator('#unsaved')).to_have_value("keep this draft")
                    expect(page.locator('[data-best-login-required]')).to_have_count(0)
                    expect(page.locator('[data-entry-frame-panel][data-entry-provider="best"]')).to_have_class(
                        "entry-frame-panel is-active"
                    )
                    self.assertEqual(page.url, BOYI + "/ocr")
                    self.assertEqual(entry_reads, 4)  # Initial embed, popup, login redirect, restored embed.

                    # Native choices after initial entry must not be overridden.
                    embedded.locator('#code').fill("local-unsaved-draft")
                    embedded.get_by_role("tab", name="草稿箱").click()
                    frame = next(frame for frame in page.frames if frame.url == ENTRY)
                    frame.evaluate("document.body.appendChild(document.createElement('p'))")
                    expect(embedded.get_by_role("tab", name="草稿箱")).to_have_attribute("aria-selected", "true")
                    embedded.get_by_role("tab", name="运单录入").click()
                    expect(embedded.locator('#code')).to_have_value("local-unsaved-draft")

                    # Browser-level original tabs retain the native default.
                    standalone = context.new_page()
                    standalone.goto(ENTRY)
                    expect(standalone.get_by_role("tab", name="草稿箱")).to_have_attribute("aria-selected", "true")
                    self.assertFalse(standalone.is_closed())

                    # The retired form is untouched even when embedded by Boyi.
                    old_entry = page.evaluate_handle("""url => {
                      const frame = document.createElement('iframe');
                      frame.id = 'retired-best-form'; frame.src = url;
                      document.body.appendChild(frame); return frame;
                    }""", OLD_ENTRY)
                    expect(page.frame_locator('#retired-best-form').get_by_role(
                        "tab", name="草稿箱"
                    )).to_have_attribute("aria-selected", "true")
                    old_entry.evaluate("frame => frame.remove()")

                    # Reopening an already logged-in embed creates no browser tab.
                    page.bring_to_front()
                    pages_before = len(context.pages)
                    page.get_by_role("button", name="百世原页", exact=True).click()
                    frames = page.locator('[data-best-live-frame]')
                    expect(frames).to_have_count(2)
                    expect(frames.nth(1).content_frame.locator('#sendPhone')).to_be_visible()
                    self.assertEqual(len(context.pages), pages_before)
                    expect(frames.nth(0).content_frame.locator('#code')).to_have_value("local-unsaved-draft")
                finally:
                    context.close()
