"""Exercise native iframe load ordering, retry state and extension SPA mounting."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


CONSOLE = Path(__file__).resolve().parents[1]


class OriginalPageLifecycleTests(unittest.TestCase):
    def run_node(self, script, sources):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("Node.js is required for browser lifecycle regressions")
        result = subprocess.run(
            [node, "--input-type=commonjs", "-e", script],
            input=json.dumps(sources), text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fast_load_and_explicit_reload_have_correct_state(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const sources = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const between = (text, a, b) => text.slice(text.indexOf(a), text.indexOf(b, text.indexOf(a)));
const timers = new Map(); let serial = 0;
const window = new EventTarget();
window.setTimeout = callback => { timers.set(++serial, callback); return serial; };
window.clearTimeout = id => timers.delete(id);
class Frame extends EventTarget {
  constructor() { super(); this.dataset = {}; this.attrs = {}; this.isConnected = true; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  set src(value) { this.attrs.src = value; this.dispatchEvent(new Event('load')); }
}
eval(between(sources.loading, '  function showOriginalPageLoading(', '  function initRonghuiLiveInstance('));
const chip = {}, notice = {setAttribute() {}}, frame = new Frame();
const entryFrameSrc = () => 'https://tms.ronghuiwl.com/module/index?mv=index';
window.addEventListener('console:ocr-mode-change', () => showOriginalPageLoading(frame, chip, notice, '融辉'), {once:true});
// Simulate an iframe which completes immediately when its src is assigned.
eval(between(sources.template, '    const bindEntryPanelFrame =', '    function createEntryTab(')
  + '\nbindEntryPanelFrame({}, "yunda", frame);');
assert.equal(frame.getAttribute('aria-busy'), 'false');
assert.equal(notice.hidden, true);
assert.equal(timers.size, 0);
const ronghui = new Frame(); let preparationRequested = false;
ronghui.addEventListener('console:original-page-prepare', () => {
  assert.equal(ronghui.referrerPolicy, 'no-referrer');
  preparationRequested = true;
});
eval(between(sources.template, '    const bindEntryPanelFrame =', '    function createEntryTab(')
  + '\nbindEntryPanelFrame({}, "ronghui", ronghui);');
assert.equal(preparationRequested, true);
assert.equal(ronghui.getAttribute('src'), null);
assert.equal(ronghui.dataset.entryPendingSrc, entryFrameSrc());
frame.dispatchEvent(new Event('console:original-page-reload'));
assert.equal(frame.getAttribute('aria-busy'), 'true');
assert.equal(notice.hidden, false);
assert.equal(timers.size, 1);
Array.from(timers.values())[0]();
assert.equal(frame.getAttribute('aria-busy'), 'false');
assert.equal(chip.textContent, '加载未完成');
assert.match(notice.textContent, /新窗口登录/);
frame.dispatchEvent(new Event('console:original-page-reload'));
frame.dispatchEvent(new Event('error'));
assert.equal(chip.textContent, '加载失败');
assert.equal(timers.size, 0);
frame.dispatchEvent(new Event('console:original-page-reload'));
frame.src = entryFrameSrc();
assert.equal(notice.hidden, true);
assert.equal(timers.size, 0);
""", {
            "loading": (CONSOLE / "static/js/yunda_entry_mode.js").read_text(encoding="utf-8"),
            "template": (CONSOLE / "templates/document.html").read_text(encoding="utf-8"),
        })

    def test_extension_mounts_after_dashboard_to_entry_navigation(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const sources = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const location = {origin:'https://boyi.homes', pathname:'/'};
let observer, frames = [], messages = 0, reloads = 0, navigations = 0, finishPreparation;
const document = {querySelectorAll:() => frames};
class MutationObserver { constructor(callback) { observer = callback; } observe() {} }
const chrome = {runtime:{sendMessage:() => { messages++; return new Promise(resolve => finishPreparation = resolve); }}};
eval(sources.content);
assert.equal(typeof observer, 'function');
location.pathname = '/ocr';
const source = 'https://tms.ronghuiwl.com/module/index?mv=index';
const frame = new EventTarget();
frame.dataset = {entryPendingSrc:source}; frame.isConnected = true;
Object.defineProperty(frame, 'src', {set(value) { assert.equal(value, source); navigations++; }});
frame.addEventListener('console:original-page-reload', () => reloads++);
frames = [frame];
observer([]); observer([]);
assert.equal(messages, 0); // Parsing the iframe must not race the host's load listeners.
frame.dataset.entryFrameBound = '1';
frame.dispatchEvent(new Event('console:original-page-prepare'));
assert.equal(messages, 1);
assert.equal(navigations, 0); // No original request until browser login adaptation finishes.
finishPreparation({ok:true});
setImmediate(() => {
  assert.equal(messages, 1);
  assert.equal(frame.dataset.ronghuiExtension, sources.version);
  assert.equal(reloads, 1);
  assert.equal(navigations, 1);
  observer([]);
  assert.equal(messages, 1);
  frame.dataset.entryPendingSrc = source;
  frame.dispatchEvent(new Event('console:original-page-prepare'));
  assert.equal(messages, 2);
  assert.equal(navigations, 1);
  finishPreparation({ok:false});
  setImmediate(() => {
    assert.equal(frame.dataset.ronghuiExtension, 'failed');
    assert.equal(navigations, 1);
    observer([]);
    assert.equal(messages, 2); // A failed preparation is not silently retried on DOM changes.
  });
});
""", {
            "content": (CONSOLE / "static/browser_extensions/ronghui/content.js").read_text(encoding="utf-8"),
            "version": json.loads((CONSOLE / "static/browser_extensions/ronghui/manifest.json").read_text())["version"],
        })

    def test_extension_preparation_accepts_host_shell_but_rejects_other_origins_and_frames(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const sources = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let handler, updates = 0;
const chrome = {
  declarativeNetRequest:{getSessionRules:async()=>[], updateSessionRules:async()=>{updates++;}},
  webRequest:{onHeadersReceived:{addListener(){}}},
  runtime:{onMessage:{addListener(callback){handler=callback;}}},
  tabs:{onRemoved:{addListener(){}}}, cookies:{getAll:async()=>[]}
};
eval(sources.background);
const prepare = sender => new Promise(resolve => handler({type:'prepare-ronghui-embed'}, sender, resolve));
(async () => {
  assert.deepEqual(await prepare({url:'https://boyi.homes/', frameId:0, tab:{id:1}}), {ok:true});
  assert.equal(updates, 1);
  assert.deepEqual(await prepare({url:'https://other.example/ocr', frameId:0, tab:{id:1}}), {ok:false});
  assert.deepEqual(await prepare({url:'https://boyi.homes/ocr', frameId:2, tab:{id:1}}), {ok:false});
  assert.equal(updates, 1);
})();
""", {
            "background": (CONSOLE / "static/browser_extensions/ronghui/background.js").read_text(encoding="utf-8"),
        })


if __name__ == "__main__":
    unittest.main()
