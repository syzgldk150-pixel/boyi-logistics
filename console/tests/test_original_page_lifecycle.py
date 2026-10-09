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
frame.dispatchEvent(new Event('console:original-page-prepare-failed'));
assert.equal(frame.getAttribute('aria-busy'), 'false');
assert.equal(chip.textContent, '扩展准备失败');
assert.match(notice.textContent, /刷新整个博益网页/);
assert.equal(timers.size, 0);
frame.dataset.originalPagePrepareError = 'extension-reloaded';
frame.dispatchEvent(new Event('console:original-page-prepare-failed'));
assert.equal(chip.textContent, '请刷新整个网页');
assert.match(notice.textContent, /先保存其他页签/);
assert.match(notice.textContent, /仅点这里的“重新加载”无法恢复/);
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
const handlers = []; let updates = 0;
const chrome = {
  declarativeNetRequest:{getSessionRules:async()=>[], updateSessionRules:async()=>{updates++;}},
  webRequest:{onHeadersReceived:{addListener(){}}},
  runtime:{onMessage:{addListener(callback){handlers.push(callback);}}},
  storage:{session:{get:async()=>({}),set:async()=>{}}},
  tabs:{onRemoved:{addListener(){}}}, cookies:{getAll:async()=>[]}
};
eval(sources.background);
const prepare = (sender, type='prepare-ronghui-embed') =>
  new Promise(resolve => handlers[0]({type}, sender, resolve));
(async () => {
  assert.deepEqual(await prepare({url:'https://boyi.homes/', frameId:0, tab:{id:1}}), {ok:true});
  assert.equal(updates, 1);
  assert.deepEqual(await prepare({url:'https://other.example/ocr', frameId:0, tab:{id:1}}), {ok:false});
  assert.deepEqual(await prepare({url:'https://boyi.homes/ocr', frameId:2, tab:{id:1}}), {ok:false});
  assert.equal(updates, 1);
  assert.deepEqual(await prepare({url:'https://boyi.homes/ocr', frameId:0, tab:{id:1}}, 'prepare-best-embed'), {ok:true});
  assert.equal(updates, 1); // Best must not replace Ronghui's HTTP redirect rules.
  assert.deepEqual(await prepare({url:'https://other.example/ocr', frameId:0, tab:{id:1}}, 'prepare-best-embed'), {ok:false});
})();
""", {
            "background": (CONSOLE / "static/browser_extensions/ronghui/background.js").read_text(encoding="utf-8"),
        })


    def test_ronghui_opens_native_entry_once_and_reuses_existing_tabs(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const sources = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
function run(existing, candidates=1) {
  let clicks=0, active=null, observer;
  const entries=existing?[{title:'运单录入'}]:[];
  const root={classList:{contains:()=>false,toggle(){}}};
  const menus=Array.from({length:candidates},()=>({textContent:'运单录入',closest:()=>null,
    click(){clicks++;entries.push({title:'运单录入'});active=entries[0];}}));
  const tabs={getTabs:()=>entries,getActiveTab:()=>active,activeTab:t=>{active=t;}};
  const parent={postMessage(){}};
  const context={location:{origin:'https://tms.ronghuiwl.com',pathname:'/module/index',ancestorOrigins:['https://boyi.homes']},
    window:{parent,mini:{get:()=>tabs},addEventListener(){},dispatchEvent(){}},
    document:{documentElement:root,head:{append(){}},createElement:()=>({}),querySelectorAll:()=>menus},
    MutationObserver:class {constructor(fn){observer=fn;}observe(){}},
    requestAnimationFrame:fn=>fn(),Event,console};
  vm.runInNewContext(sources.layout,context);
  observer();observer();
  assert.equal(clicks,existing||candidates!==1?0:1);
  if(candidates===1) assert.equal(active,entries[0]);
  entries.length=0;observer(); // Closing a form must never allocate another one.
  assert.equal(clicks,existing||candidates!==1?0:1);
}
run(false);run(true);run(false,2);
""", {"layout": (CONSOLE / "static/browser_extensions/ronghui/layout-shell.js").read_text(encoding="utf-8")})

    def test_address_enter_and_blur_use_current_value_without_changing_native_business(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const sources = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const RH = 'https://tms.ronghuiwl.com';
function setup(ancestors=[RH, 'https://boyi.homes'], path='/widget/home', delayed=false) {
  let value='test address', ready=!delayed, load;
  const calls=[];
  const address={getValue:()=>value, fire(type, event, extra) {
    assert.equal(this,address);
    calls.push({type, value:event?.value, event, extra});
    return 'native result';
  }};
  const native=address.fire;
  const save=()=>{}, print=()=>{};
  const window={mini:{get:id=>ready && id==='ACCEPT_MAN_ADDRESS'?address:null},
    crud:{saveMethodCs:save},printBillNew1:print,
    addEventListener(type,fn){assert.equal(type,'load');load=fn;}};
  const context={location:{origin:RH,pathname:path,ancestorOrigins:ancestors},window};
  vm.runInNewContext(sources.adapter,context);
  return {address, native, calls, window, save, print, context,
    setValue:v=>{value=v;},finish:()=>{ready=true;load?.();}};
}
const s=setup();
// Programmatic/autofilled values may not fire valuechanged; retry via Enter must work.
assert.equal(s.address.fire('enter',{}),'native result');
assert.equal(s.calls.at(-1).value,'test address');
s.setValue('updated address');
s.address.fire('blur',undefined,'preserved');
assert.equal(s.calls.at(-1).value,'updated address');
assert.equal(s.calls.at(-1).extra,'preserved');
// Explicit event values and unrelated events must retain native semantics.
for (const value of ['', 'event address', 0]) {
  const event={value};s.address.fire('enter',event);
  assert.equal(s.calls.at(-1).event,event);
  assert.equal(s.calls.at(-1).value,value);
}
s.address.fire('valuechanged',{});
assert.equal(s.calls.at(-1).value,undefined);
s.setValue('');s.address.fire('blur',{});
assert.equal(s.calls.at(-1).value,'');
const installed=s.address.fire;
s.finish();vm.runInNewContext(sources.adapter,s.context);
assert.equal(s.address.fire,installed);
assert.equal(s.window.crud.saveMethodCs,s.save);
assert.equal(s.window.printBillNew1,s.print);
const delayed=setup(undefined,undefined,true);
assert.equal(delayed.address.fire,delayed.native);
delayed.finish();assert.notEqual(delayed.address.fire,delayed.native);
for (const ancestors of [[],[RH],[RH,'https://other.example'],['https://boyi.homes'],
    [RH,'https://boyi.homes','https://other.example']]) {
  const excluded=setup(ancestors);
  assert.equal(excluded.address.fire,excluded.native);
}
const other=setup(undefined,'/module/index');
assert.equal(other.address.fire,other.native);
""", {"adapter": (CONSOLE / "static/browser_extensions/ronghui/entry-events.js").read_text(encoding="utf-8")})

    def test_login_return_is_bound_to_the_created_tab_and_exact_origin(self):
        self.run_node(r"""
const assert = require('node:assert/strict');
const sources = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const listeners = [], stored = {}, updates = [], sent = [], removed = [];
const chrome = {
  declarativeNetRequest:{getSessionRules:async()=>[], updateSessionRules:async()=>{}},
  webRequest:{onHeadersReceived:{addListener(){}}},
  runtime:{onMessage:{addListener(fn){listeners.push(fn);}}}, cookies:{getAll:async()=>[]},
  storage:{session:{set:async obj=>Object.assign(stored,obj),get:async key=>({[key]:stored[key]}),remove:async key=>{delete stored[key];}}},
  tabs:{onRemoved:{addListener(){}},create:async()=>({id:91}),
    update:async(id, value)=>{updates.push({id,value});},
    sendMessage:async(id,msg)=>{sent.push({id,msg});return {ok:true};},
    remove:async id=>{removed.push(id);}}
};
eval(sources.background);
const invoke=(message,sender)=>new Promise(resolve=>listeners.at(-1)(message,sender,resolve));
(async()=>{
  const request={type:'open-original-login',provider:'ronghui',entryId:'entry-2'};
  assert.deepEqual(await invoke(request,{url:'https://other.example/ocr',frameId:0,tab:{id:3}}),{ok:false});
  assert.deepEqual(await invoke(request,{url:'https://boyi.homes/ocr',frameId:2,tab:{id:3}}),{ok:false});
  assert.equal(updates.length,0);
  assert.deepEqual(await invoke(request,{url:'https://boyi.homes/ocr',frameId:0,tab:{id:3}}),{ok:true});
  assert.deepEqual(stored['original-login-91'],{hostTabId:3,entryId:'entry-2',provider:'ronghui'});
  const ready={type:'original-login-ready',provider:'ronghui'};
  assert.deepEqual(await invoke(ready,{url:'https://tms.ronghuiwl.com/system/login',frameId:0,tab:{id:91}}),{ok:false});
  assert.deepEqual(await invoke(ready,{url:'https://tms.ronghuiwl.com/module/index',frameId:0,tab:{id:92}}),{ok:false});
  assert.equal(sent.length,0);
  assert.deepEqual(await invoke(ready,{url:'https://tms.ronghuiwl.com/module/index',frameId:0,tab:{id:91}}),{ok:true});
  assert.deepEqual(sent,[{id:3,msg:{type:'original-login-complete',provider:'ronghui',entryId:'entry-2'}}]);
  assert.deepEqual(removed,[91]);
  assert.deepEqual(stored,{});
  assert.deepEqual(await invoke(ready,{url:'https://tms.ronghuiwl.com/module/index',frameId:0,tab:{id:91}}),{ok:false});
  assert.equal(sent.length,1);
})();
""", {"background": (CONSOLE / "static/browser_extensions/ronghui/background.js").read_text(encoding="utf-8")})


if __name__ == "__main__":
    unittest.main()
