"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class FakeClassList {
  constructor(owner) {
    this.owner = owner;
    this.values = new Set();
  }

  [Symbol.iterator]() {
    return this.values[Symbol.iterator]();
  }

  replace(value) {
    this.values = new Set(String(value || "").split(/\s+/).filter(Boolean));
    this.sync();
  }

  add(...values) {
    values.filter(Boolean).forEach((value) => this.values.add(value));
    this.sync();
  }

  remove(...values) {
    values.forEach((value) => this.values.delete(value));
    this.sync();
  }

  contains(value) {
    return this.values.has(value);
  }

  toggle(value, force) {
    const enabled = force === undefined ? !this.contains(value) : Boolean(force);
    if (enabled) this.values.add(value);
    else this.values.delete(value);
    this.sync();
    return enabled;
  }

  sync() {
    const value = Array.from(this.values).join(" ");
    if (value) this.owner.attributeValues.set("class", value);
    else this.owner.attributeValues.delete("class");
  }
}

function dataProperty(name) {
  return name
    .slice(5)
    .split("-")
    .map((part, index) => (index ? `${part[0].toUpperCase()}${part.slice(1)}` : part))
    .join("");
}

class FakeElement {
  constructor(tagName, { className = "", textContent = "" } = {}) {
    this.tagName = String(tagName).toUpperCase();
    this.attributeValues = new Map();
    this.classList = new FakeClassList(this);
    this.dataset = {};
    this.children = [];
    this.parentElement = null;
    this.hidden = false;
    this.disabled = false;
    this.textContent = textContent;
    this.isConnected = true;
    if (className) this.className = className;
  }

  get className() {
    return this.attributeValues.get("class") || "";
  }

  set className(value) {
    this.classList.replace(value);
  }

  get attributes() {
    return Array.from(this.attributeValues, ([name, value]) => ({ name, value }));
  }

  setAttribute(name, value) {
    const normalized = String(name);
    const text = String(value);
    if (normalized === "class") {
      this.className = text;
      return;
    }
    this.attributeValues.set(normalized, text);
    if (normalized.startsWith("data-")) this.dataset[dataProperty(normalized)] = text;
  }

  getAttribute(name) {
    return this.attributeValues.has(name) ? this.attributeValues.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributeValues.has(name);
  }

  removeAttribute(name) {
    this.attributeValues.delete(name);
    if (name.startsWith("data-")) delete this.dataset[dataProperty(name)];
  }

  appendChild(child) {
    if (child.parentElement) {
      child.parentElement.children = child.parentElement.children.filter(
        (candidate) => candidate !== child
      );
    }
    child.parentElement = this;
    child.isConnected = true;
    this.children.push(child);
    return child;
  }

  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter(
        (candidate) => candidate !== this
      );
    }
    this.parentElement = null;
    this.isConnected = false;
  }

  addEventListener() {}

  removeEventListener() {}

  focus() {}

  matches(selector) {
    const value = selector.trim();
    if (!value) return false;
    if (value.startsWith(".")) {
      const attributeIndex = value.indexOf("[");
      const className = value.slice(1, attributeIndex < 0 ? undefined : attributeIndex);
      if (!this.classList.contains(className)) return false;
      return attributeIndex < 0 || this.matches(value.slice(attributeIndex));
    }
    if (value.startsWith("[")) {
      const match = value.match(/^\[([^=\]]+)(?:=['\"]?([^'\"\]]+)['\"]?)?\]$/);
      if (!match || !this.hasAttribute(match[1])) return false;
      return match[2] === undefined || this.getAttribute(match[1]) === match[2];
    }
    return this.tagName === value.toUpperCase();
  }

  descendantElements() {
    const result = [];
    const visit = (element) => {
      element.children.forEach((child) => {
        result.push(child);
        visit(child);
      });
    };
    visit(this);
    return result;
  }

  querySelectorAll(selector) {
    if (selector.startsWith(":scope > ")) {
      const directSelector = selector.slice(9);
      return this.children.filter((child) => child.matches(directSelector));
    }
    const selectors = selector.split(",").map((value) => value.trim());
    return this.descendantElements().filter((element) =>
      selectors.some((candidate) => element.matches(candidate))
    );
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  closest(selector) {
    let current = this;
    while (current) {
      if (current.matches(selector)) return current;
      current = current.parentElement;
    }
    return null;
  }
}

function createTabElement() {
  const tab = new FakeElement("div", { className: "console-tab" });
  tab.setAttribute("data-console-tab", "");
  tab.setAttribute("role", "tab");
  const activate = new FakeElement("button", { className: "console-tab-title" });
  activate.setAttribute("data-console-tab-activate", "");
  const icon = new FakeElement("i");
  icon.setAttribute("data-console-tab-icon", "");
  const title = new FakeElement("span");
  title.setAttribute("data-console-tab-title", "");
  activate.appendChild(icon);
  activate.appendChild(title);
  const close = new FakeElement("button", { className: "console-tab-close" });
  close.setAttribute("data-console-tab-close", "");
  tab.appendChild(activate);
  tab.appendChild(close);
  return tab;
}

class FakeTemplate extends FakeElement {
  constructor() {
    super("template");
    this.setAttribute("data-console-tab-template", "");
    this.content = {
      cloneNode() {
        const fragment = new FakeElement("fragment");
        fragment.appendChild(createTabElement());
        return fragment;
      },
    };
  }
}

class FakeDocument {
  constructor() {
    this.listeners = new Map();
    this.documentElement = new FakeElement("html");
    this.head = new FakeElement("head");
    this.body = new FakeElement("body", { className: "finance-page" });
    this.scripts = [];
    this.title = "财务结算 | 物流 Agent 控制台";

    this.shell = new FakeElement("div", { className: "app-shell" });
    this.main = new FakeElement("main", { className: "main-content" });
    this.main.setAttribute("data-console-tab-title", "财务模块");
    const pageTitle = new FakeElement("h1", {
      className: "page-title",
      textContent: "财务结算",
    });
    this.tabsRoot = new FakeElement("div", { className: "console-tab-bar" });
    this.tabsRoot.setAttribute("data-console-tabs", "");
    this.tabList = new FakeElement("div", { className: "console-tab-strip" });
    this.tabList.setAttribute("data-console-tab-list", "");
    this.template = new FakeTemplate();
    this.tabsRoot.appendChild(this.tabList);
    this.tabsRoot.appendChild(this.template);
    this.main.appendChild(pageTitle);
    this.main.appendChild(this.tabsRoot);
    this.shell.appendChild(this.main);
    this.body.appendChild(this.shell);

    this.homeLink = this.createNavLink("/", "概览", "grid");
    this.financeLink = this.createNavLink(
      "/modules/finance",
      "财务模块",
      "dollar-sign"
    );
  }

  createNavLink(href, label, iconName) {
    const link = new FakeElement("a", { className: "nav-link" });
    link.setAttribute("href", href);
    const icon = new FakeElement("i");
    icon.setAttribute("data-feather", iconName);
    const title = new FakeElement("span", { textContent: label });
    link.appendChild(icon);
    link.appendChild(title);
    return link;
  }

  allElements() {
    return [this.documentElement, this.head, this.body, ...this.body.descendantElements()];
  }

  querySelectorAll(selector) {
    if (selector === "[data-nav-list] .nav-link[href]") {
      return [this.homeLink, this.financeLink];
    }
    if (selector === "[data-shell-home-link][href]") return [this.homeLink];
    if (
      selector ===
      "[data-nav-list] .nav-link, .mobile-bottom-nav__item[href]"
    ) {
      return [this.homeLink, this.financeLink];
    }
    const selectors = selector.split(",").map((value) => value.trim());
    return this.allElements().filter((element) =>
      selectors.some((candidate) => element.matches(candidate))
    );
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  createElement(tagName) {
    return new FakeElement(tagName);
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener() {}

  dispatch(type) {
    (this.listeners.get(type) || []).forEach((listener) =>
      listener.call(this, { type, target: this })
    );
  }
}

const document = new FakeDocument();
const locationState = new URL("https://boyi.homes/modules/finance");
const windowListeners = new Map();
const window = {
  document,
  location: {
    href: locationState.href,
    origin: locationState.origin,
    pathname: locationState.pathname,
    assign() {
      throw new Error("deep-link boot unexpectedly fell back to full navigation");
    },
  },
  history: {
    pushState() {
      throw new Error("initial deep-link boot must not push a second history entry");
    },
  },
  matchMedia() {
    return { matches: false, addEventListener() {} };
  },
  addEventListener(type, listener) {
    const listeners = windowListeners.get(type) || [];
    listeners.push(listener);
    windowListeners.set(type, listeners);
  },
  removeEventListener() {},
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  scrollTo() {},
};

global.window = window;
global.document = document;
global.Element = FakeElement;
global.HTMLElement = FakeElement;
global.HTMLAnchorElement = FakeElement;
global.HTMLFormElement = FakeElement;
global.Event = class Event {
  constructor(type) {
    this.type = type;
  }
};
global.requestAnimationFrame = (callback) => callback();
global.fetch = async () => {
  throw new Error("deep-link boot unexpectedly attempted a network request");
};

const consoleUiPath = process.argv[2];
if (!consoleUiPath) throw new Error("console_ui.js path is required");
vm.runInThisContext(fs.readFileSync(consoleUiPath, "utf8"), {
  filename: consoleUiPath,
});
document.dispatch("DOMContentLoaded");

const tabs = document.tabList.querySelectorAll("[data-console-tab]");
assert.equal(tabs.length, 2);
assert.deepEqual(
  tabs.map((tab) => tab.dataset.consoleTabKey),
  ["/", "/modules/finance"]
);

const [overviewTab, financeTab] = tabs;
assert.equal(
  overviewTab.querySelector("[data-console-tab-title]").textContent,
  "概览"
);
assert.equal(overviewTab.classList.contains("is-pinned"), true);
assert.equal(overviewTab.classList.contains("is-active"), false);
assert.equal(overviewTab.getAttribute("aria-selected"), "false");
assert.equal(overviewTab.querySelector("[data-console-tab-close]").hidden, true);
assert.equal(overviewTab.querySelector("[data-console-tab-close]").disabled, true);
assert.equal(window.ConsoleUI.closeTab("/"), false);
assert.equal(document.tabList.querySelectorAll("[data-console-tab]").length, 2);

assert.equal(financeTab.dataset.consoleTabKey, "/modules/finance");
assert.equal(financeTab.classList.contains("is-active"), true);
assert.equal(financeTab.getAttribute("aria-selected"), "true");
assert.equal(document.main.hidden, false);
assert.equal(document.main.dataset.consoleTabKey, "/modules/finance");
assert.equal(document.financeLink.classList.contains("active"), true);
assert.equal(document.homeLink.classList.contains("active"), false);
