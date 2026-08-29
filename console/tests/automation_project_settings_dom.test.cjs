"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function dataProperty(name) {
  return name
    .slice(5)
    .split("-")
    .map((part, index) => (index ? `${part[0].toUpperCase()}${part.slice(1)}` : part))
    .join("");
}

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.attributes = new Map();
    this.dataset = {};
    this.listeners = new Map();
    this.queries = new Map();
    this.parentElement = null;
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.innerHTML = "";
    this.textContent = "";
  }

  setAttribute(name, value) {
    const normalized = String(name);
    const text = String(value);
    this.attributes.set(normalized, text);
    if (normalized.startsWith("data-")) this.dataset[dataProperty(normalized)] = text;
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (String(name).startsWith("data-")) delete this.dataset[dataProperty(String(name))];
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    this.listeners.set(type, listeners.filter((candidate) => candidate !== listener));
  }

  dispatchEvent(event) {
    if (!event.target) event.target = this;
    (this.listeners.get(event.type) || []).forEach((listener) => listener.call(this, event));
    return true;
  }

  appendChild(child) {
    child.parentElement = this;
    return child;
  }

  setQuery(selector, values) {
    this.queries.set(selector, Array.isArray(values) ? values : [values]);
  }

  querySelectorAll(selector) {
    return this.queries.get(selector) || [];
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  matches(selector) {
    return String(selector).split(",").some((part) => {
      let candidate = part.trim();
      const checked = candidate.endsWith(":checked");
      if (checked) candidate = candidate.slice(0, -8);
      if (candidate === "form") return this.tagName === "FORM" && (!checked || this.checked);
      const match = candidate.match(/^\[([^=\]]+)(?:=['\"]?([^'\"\]]+)['\"]?)?\]$/);
      if (!match || !this.attributes.has(match[1])) return false;
      if (match[2] !== undefined && this.getAttribute(match[1]) !== match[2]) return false;
      return !checked || this.checked;
    });
  }

  closest(selector) {
    let current = this;
    while (current) {
      if (current.matches(selector)) return current;
      current = current.parentElement;
    }
    return null;
  }

  checkValidity() { return true; }
  reportValidity() { return true; }
  focus() { this.focused = true; }
}

class FakeDocument extends FakeElement {
  constructor() {
    super("document");
    this.readyState = "complete";
    this.documentElement = new FakeElement("html");
  }
}

class FakeCustomEvent {
  constructor(type, options = {}) {
    this.type = type;
    this.detail = options.detail;
    this.bubbles = Boolean(options.bubbles);
    this.target = null;
  }
}

const document = new FakeDocument();
let reloadCalls = 0;
let uuidCounter = 0;
const window = {
  crypto: { randomUUID: () => `12345678-1234-4234-8234-${String(++uuidCounter).padStart(12, "0")}` },
  location: { reload: () => { reloadCalls += 1; } },
  setTimeout,
  clearTimeout,
  confirm: () => false,
};

global.window = window;
global.document = document;
global.Element = FakeElement;
global.HTMLElement = FakeElement;
global.HTMLButtonElement = FakeElement;
global.HTMLDialogElement = FakeElement;
global.HTMLFormElement = FakeElement;
global.HTMLInputElement = FakeElement;
global.HTMLSelectElement = FakeElement;
global.HTMLTextAreaElement = FakeElement;
global.CustomEvent = FakeCustomEvent;

const scriptPath = process.argv[2];
if (!scriptPath) throw new Error("automation_approval_policy.js path is required");
vm.runInThisContext(fs.readFileSync(scriptPath, "utf8"), { filename: scriptPath });

function response(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
  };
}

function buildProject(automationId) {
  const form = new FakeElement("form");
  const instance = form.appendChild(new FakeElement("section"));
  instance.setAttribute("data-plugin-instance", "");
  instance.setAttribute("data-automation-id", automationId);
  instance.setAttribute("data-project-configuration-version", "1");

  const summary = instance.appendChild(new FakeElement("p"));
  summary.setAttribute("data-plugin-instance-feedback", "");
  summary.hidden = true;
  const button = form.appendChild(new FakeElement("button"));
  button.setAttribute("data-plugin-configuration-save", "");
  button.innerHTML = "<span>保存项目设置</span>";
  const local = form.appendChild(new FakeElement("p"));
  local.setAttribute("data-plugin-settings-feedback", "");
  local.hidden = true;
  const scheduleKind = form.appendChild(new FakeElement("select"));
  scheduleKind.setAttribute("data-plugin-schedule-kind", "");
  scheduleKind.value = "none";
  const consoleEntrypoint = form.appendChild(new FakeElement("input"));
  consoleEntrypoint.setAttribute("data-plugin-entrypoint", "");
  consoleEntrypoint.value = "console";
  consoleEntrypoint.checked = true;
  const runButton = form.appendChild(new FakeElement("button"));
  runButton.setAttribute("data-run-now", "");

  form.setQuery("[data-plugin-instance]", instance);
  form.setQuery("[data-plugin-settings-feedback]", local);
  form.setQuery("[data-plugin-config-path]", []);
  form.setQuery("[data-plugin-account-role]", []);
  form.setQuery("[data-plugin-resource-role]", []);
  form.setQuery("[data-plugin-entrypoint]:checked", consoleEntrypoint);
  form.setQuery("[data-plugin-schedule-kind]", scheduleKind);
  form.setQuery("[data-automation-toggle]", []);
  form.setQuery("[data-run-now]", runButton);
  instance.setQuery("[data-plugin-instance-feedback]", summary);
  instance.setQuery("[data-plugin-worker-select]", []);

  let savedEvents = 0;
  form.addEventListener("automation:plugin-configuration-saved", () => { savedEvents += 1; });
  return { button, form, instance, local, summary, savedEvents: () => savedEvents };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
}

(async () => {
  let fetchCalls = 0;
  const outcomes = [
    response(200, {
      ok: true,
      data: {
        project_configuration_version: 2,
        schedule_runtime_state: "ACTIVE",
        scheduler_refresh_completed: true,
      },
      message: "项目设置已保存，运行中定时已按新配置刷新。",
    }),
    response(200, {
      ok: true,
      data: {
        project_configuration_version: 2,
        schedule_runtime_state: "REFRESH_FAILED",
        scheduler_refresh_completed: false,
      },
      message: "项目设置已保存，但运行中调度器刷新失败；请使用同一请求重试。",
    }),
    response(403, { ok: false, message: "只有超级管理员可以保存自动化项目设置。" }),
  ];
  global.fetch = async () => {
    fetchCalls += 1;
    return outcomes.shift();
  };

  const saved = buildProject("daily-sign");
  document.dispatchEvent({ type: "click", target: saved.button });
  document.dispatchEvent({ type: "click", target: saved.button });
  await settle();
  assert.equal(fetchCalls, 1, "busy save button must not submit twice");
  assert.equal(saved.savedEvents(), 1);
  assert.equal(saved.instance.dataset.projectConfigurationVersion, "2");
  assert.equal(saved.summary.hidden, false);
  assert.equal(saved.summary.dataset.kind, "success");
  assert.equal(saved.local.hidden, true);

  const refreshFailed = buildProject("refresh-failed");
  document.dispatchEvent({ type: "click", target: refreshFailed.button });
  await settle();
  assert.equal(fetchCalls, 2);
  assert.equal(refreshFailed.savedEvents(), 0);
  assert.equal(refreshFailed.local.hidden, false);
  assert.equal(refreshFailed.local.dataset.kind, "warning");
  assert.ok(refreshFailed.button.dataset.requestId, "partial success must retain request identity");

  const rejected = buildProject("rejected");
  document.dispatchEvent({ type: "click", target: rejected.button });
  await settle();
  assert.equal(fetchCalls, 3);
  assert.equal(rejected.savedEvents(), 0);
  assert.equal(rejected.local.hidden, false);
  assert.equal(rejected.local.dataset.kind, "error");
  assert.equal(rejected.local.getAttribute("role"), "alert");
  assert.equal(rejected.button.dataset.requestId, undefined);
  assert.equal(reloadCalls, 0, "successful save must not hide its result behind a reload");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
