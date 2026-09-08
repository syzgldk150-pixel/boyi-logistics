"""Execute the real host bridge with an immediately ready, cached iframe."""

import json
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader


class _SettingsFrameParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attributes = None

    def handle_starttag(self, tag, attrs):
        if tag == "iframe" and "data-plugin-settings-frame" in dict(attrs):
            self.attributes = dict(attrs)


class PluginSettingsInitializationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the host bridge regression")
    def test_cached_frame_context_waits_for_its_host_listener(self):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        env = Environment(loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}{% block scripts %}{% endblock %}"}),
            FileSystemLoader(template_dir),
        ]), autoescape=True)
        rendered = env.get_template("automation_plugin_settings.html").render(
            automation_id="synthetic-settings", bridge_session="synthetic-bridge-session",
            instance_name="Settings", plugin_version="1.0.0", settings_mode="custom",
            configured=False, return_url="/automations", return_label="Automation",
        )
        parser = _SettingsFrameParser()
        parser.feed(rendered)
        scripts = re.findall(r"<script>(.*?)</script>", rendered, re.DOTALL)
        self.assertEqual(len(scripts), 1)
        self.assertIsNotNone(parser.attributes)
        result = subprocess.run(
            [shutil.which("node"), "-e", _CACHED_FRAME_HARNESS],
            input=json.dumps({"attributes": parser.attributes, "source": scripts[0]}),
            text=True, capture_output=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["initial_context_calls"], 1, observed)
        self.assertEqual(observed["reload_context_calls"], 2, observed)
        self.assertEqual(observed["responses"], 2, observed)
        self.assertTrue(observed["listener_ready_at_each_navigation"], observed)
        self.assertTrue(observed["foreign_messages_ignored"], observed)


_CACHED_FRAME_HARNESS = r"""
const vm = require('node:vm');
const {attributes, source} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const listeners = [];
const tasks = [];
const calls = [];
const responses = [];
const navigationReady = [];
class Element { constructor() { this.dataset = {}; } }
class Frame extends Element {
  constructor() {
    super();
    this.dataset.settingsSrc = attributes['data-settings-src'];
    this.contentWindow = {postMessage: payload => responses.push(payload)};
  }
  set src(value) {
    this.currentSrc = value;
    navigationReady.push(listeners.length > 0);
    // A cached package can execute before later parser-blocking host scripts.
    emit(this.contentWindow, 'synthetic-bridge-session');
  }
  get src() { return this.currentSrc; }
}
const frame = new Frame();
const feedback = new Element();
const page = {querySelector: selector => selector === '[data-plugin-settings-frame]' ? frame : feedback};
function emit(sourceWindow, bridgeSession) {
  const event = {source: sourceWindow, data: {
    type: 'boyi.settings.request', bridge_session: bridgeSession,
    request_id: `context-${navigationReady.length}`, operation: 'context', payload: {},
  }};
  for (const listener of listeners) tasks.push(listener(event));
}
if (attributes.src) frame.src = attributes.src;
vm.runInNewContext(source, {
  HTMLIFrameElement: Frame, HTMLElement: Element,
  document: {querySelector: () => page},
  window: {
    ConsoleUI: {pageRequest: () => async (endpoint, options) => {
      calls.push({endpoint, body: JSON.parse(options.body)});
      return {ok: true, json: async () => ({ok: true, data: {settings: {}}})};
    }},
    addEventListener: (type, listener) => {if (type === 'message') listeners.push(listener);},
  },
});
(async () => {
  await Promise.all(tasks);
  const initialCalls = calls.length;
  frame.src = frame.src;
  await Promise.all(tasks);
  const reloadCalls = calls.length;
  emit({}, 'synthetic-bridge-session');
  emit(frame.contentWindow, 'another-frame-session');
  await Promise.all(tasks);
  process.stdout.write(JSON.stringify({
    initial_context_calls: initialCalls, reload_context_calls: reloadCalls,
    responses: responses.length, listener_ready_at_each_navigation: navigationReady.every(Boolean),
    foreign_messages_ignored: calls.length === reloadCalls,
  }));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
