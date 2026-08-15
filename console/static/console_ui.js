(() => {
  const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const loadedScriptSources = new Set(
    Array.from(document.scripts)
      .map((script) => script.src)
      .filter(Boolean)
  );
  const permanentHeadSelectors = [
    "meta[charset]",
    "meta[name='viewport']",
    "title",
    "link[href^='/static/style.css']",
  ];
  const headAssetLoadTimeoutMs = 15000;

  let initialHeadMarked = false;
  let navigationController = null;
  let navigationSeq = 0;
  const openTabs = new Map();
  let activeTabKey = "";
  let currentPageRuntime = createPageRuntime();

  function createPageRuntime() {
    return {
      intervals: new Set(),
      timeouts: new Set(),
      listeners: [],
    };
  }

  function refreshIcons(root = document) {
    if (!window.feather) {
      return;
    }
    if (root === document && typeof window.feather.replace === "function") {
      window.feather.replace();
      return;
    }
    if (!window.feather.icons || typeof root.querySelectorAll !== "function") {
      return;
    }
    root.querySelectorAll("[data-feather]").forEach((node) => {
      const name = node.getAttribute("data-feather");
      const icon = window.feather.icons[name];
      if (!icon || typeof icon.toSvg !== "function") {
        return;
      }
      const attrs = {};
      Array.from(node.attributes).forEach((attr) => {
        if (attr.name !== "data-feather") {
          attrs[attr.name] = attr.value;
        }
      });
      const baseClass = `feather feather-${name}`;
      attrs.class = attrs.class ? `${attrs.class} ${baseClass}` : baseClass;
      const template = document.createElement("template");
      template.innerHTML = icon.toSvg(attrs).trim();
      const svg = template.content.firstElementChild;
      if (svg) {
        node.replaceWith(svg);
      }
    });
  }

  function cleanupPageRuntime(runtime = currentPageRuntime) {
    if (!runtime) {
      return;
    }
    runtime.listeners.forEach(({ target, type, listener, options }) => {
      target.removeEventListener(type, listener, options);
    });
    runtime.intervals.forEach((id) => window.clearInterval(id));
    runtime.timeouts.forEach((id) => window.clearTimeout(id));
    runtime.listeners = [];
    runtime.intervals.clear();
    runtime.timeouts.clear();
    if (runtime === currentPageRuntime) {
      currentPageRuntime = createPageRuntime();
    }
  }

  function applyReducedMotionState() {
    document.body.classList.toggle("reduce-motion", reducedMotionQuery.matches);
  }

  function initReveal() {
    requestAnimationFrame(() => {
      document.body.classList.add("ui-ready");
    });
  }

  function bindOnce(element, key) {
    const attr = `data-console-bound-${key}`;
    if (!element || element.hasAttribute(attr)) {
      return false;
    }
    element.setAttribute(attr, "true");
    return true;
  }

  function normalizeLabel(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function getPageBodyClass(body = document.body) {
    const transient = new Set(["ui-ready", "reduce-motion", "content-loading"]);
    return Array.from(body.classList)
      .filter((name) => !transient.has(name))
      .join(" ");
  }

  function getTabKey(url) {
    const pathname = url.pathname || "/";
    if (pathname === "/" || pathname === "/portal") {
      return "/";
    }
    if (pathname.startsWith("/documents") || pathname.startsWith("/ocr")) {
      return "/ocr";
    }
    if (pathname.startsWith("/waybills") && !pathname.endsWith("/print")) {
      return "/waybills";
    }
    if (pathname.startsWith("/tracking")) {
      return "/tracking";
    }
    if (pathname.startsWith("/receipts")) {
      return "/receipts";
    }
    if (pathname.startsWith("/modules/customer-service")) {
      return "/modules/customer-service";
    }
    if (pathname.startsWith("/dispatch")) {
      return "/dispatch";
    }
    if (pathname.startsWith("/line-haul-contacts")) {
      return "/line-haul-contacts";
    }
    if (pathname.startsWith("/automations")) {
      return "/automations";
    }
    if (pathname.startsWith("/automation-accounts")) {
      return "/automation-accounts";
    }
    if (pathname.startsWith("/settings/accounts")) {
      return "/settings/accounts";
    }
    return pathname;
  }

  function getIconNameFromNode(node) {
    if (!node) {
      return "";
    }
    const dataIcon = node.getAttribute("data-feather");
    if (dataIcon) {
      return dataIcon;
    }
    const className = String(node.getAttribute("class") || "");
    const match = className.match(/(?:^|\s)feather-([a-z0-9-]+)/);
    return match ? match[1] : "";
  }

  function getNavMeta(tabKey) {
    const candidateGroups = [
      Array.from(document.querySelectorAll("[data-nav-list] .nav-link[href]")),
      Array.from(document.querySelectorAll("[data-shell-home-link][href]")),
    ];
    for (const links of candidateGroups) {
      for (const link of links) {
        const href = link.getAttribute("href");
        if (!href || href === "#") {
          continue;
        }
        const linkUrl = new URL(href, window.location.href);
        if (getTabKey(linkUrl) !== tabKey) {
          continue;
        }
        const label = normalizeLabel(link.querySelector("span")?.textContent || link.textContent);
        const icon = getIconNameFromNode(link.querySelector("[data-feather], .feather"));
        return { label, icon };
      }
    }
    return { label: "", icon: "" };
  }

  function getDocumentTabTitle(nextDocument, fallbackUrl) {
    const main = nextDocument.querySelector(".main-content");
    const fromDataset = normalizeLabel(main?.getAttribute("data-console-tab-title"));
    if (fromDataset) {
      return fromDataset;
    }
    const fromTitle = normalizeLabel(nextDocument.querySelector(".page-title")?.textContent);
    if (fromTitle) {
      return fromTitle;
    }
    const documentTitle = normalizeLabel(nextDocument.title).split("|")[0].trim();
    if (documentTitle) {
      return documentTitle;
    }
    return fallbackUrl.pathname || "/";
  }

  function getTabLists() {
    return Array.from(document.querySelectorAll("[data-console-tab-list]"));
  }

  function getTabTemplate(list) {
    return list?.closest("[data-console-tabs]")?.querySelector("[data-console-tab-template]") || document.querySelector("[data-console-tab-template]");
  }

  function findTabElement(list, tabKey) {
    return Array.from(list.querySelectorAll("[data-console-tab]")).find((element) => element.dataset.consoleTabKey === tabKey) || null;
  }

  function updateTabElement(tabElement, tab) {
    tabElement.dataset.consoleTabKey = tab.key;
    tabElement.classList.toggle("is-pinned", Boolean(tab.pinned));
    tabElement.classList.toggle("is-active", tab.key === activeTabKey);
    tabElement.setAttribute("aria-selected", String(tab.key === activeTabKey));
    tabElement.setAttribute("tabindex", tab.key === activeTabKey ? "0" : "-1");

    const title = tabElement.querySelector("[data-console-tab-title]");
    if (title) {
      title.textContent = tab.title;
    }
    const icon = tabElement.querySelector("[data-console-tab-icon]");
    if (icon) {
      if (tab.icon) {
        icon.setAttribute("data-feather", tab.icon);
        icon.removeAttribute("hidden");
      } else {
        icon.setAttribute("hidden", "hidden");
      }
    }
    const closeButton = tabElement.querySelector("[data-console-tab-close]");
    if (closeButton) {
      closeButton.hidden = Boolean(tab.pinned);
      closeButton.disabled = Boolean(tab.pinned);
    }
  }

  function buildTabElement(tab, list) {
    const template = getTabTemplate(list);
    if (!list || !template) {
      return null;
    }

    const fragment = template.content ? template.content.cloneNode(true) : null;
    const tabElement = fragment?.querySelector("[data-console-tab]") || document.createElement("div");
    if (!tabElement.hasAttribute("data-console-tab")) {
      tabElement.className = "console-tab";
      tabElement.setAttribute("data-console-tab", "");
      tabElement.setAttribute("role", "tab");
      tabElement.innerHTML = '<button class="console-tab-title" type="button" data-console-tab-activate><i data-console-tab-icon></i><span data-console-tab-title></span></button><button class="console-tab-close" type="button" data-console-tab-close title="关闭标签" aria-label="关闭标签"><i data-feather="x"></i></button>';
    }

    tabElement.querySelector("[data-console-tab-activate]")?.addEventListener("click", () => {
      activateTab(tab.key);
    });
    tabElement.querySelector("[data-console-tab-close]")?.addEventListener("click", (event) => {
      event.stopPropagation();
      closeTab(tab.key);
    });

    list.appendChild(tabElement);
    updateTabElement(tabElement, tab);
    return tabElement;
  }

  function renderTabs() {
    const homeTab = openTabs.get("/");
    const otherTabs = Array.from(openTabs.values()).filter((tab) => tab.key !== "/");
    const tabs = homeTab ? [homeTab, ...otherTabs] : otherTabs;
    getTabLists().forEach((list) => {
      const tabsRoot = list.closest("[data-console-tabs]");
      if (tabsRoot) {
        tabsRoot.hidden = tabs.length === 0;
      }
      Array.from(list.querySelectorAll("[data-console-tab]")).forEach((tabElement) => {
        if (!openTabs.has(tabElement.dataset.consoleTabKey || "")) {
          tabElement.remove();
        }
      });

      tabs.forEach((tab) => {
        const tabElement = findTabElement(list, tab.key) || buildTabElement(tab, list);
        if (tabElement) {
          list.appendChild(tabElement);
          updateTabElement(tabElement, tab);
        }
      });
    });
    refreshIcons();
  }

  function getTabHeadNodes(tabKey) {
    return Array.from(document.querySelectorAll("[data-console-tab-head]")).filter(
      (node) => node.getAttribute("data-console-tab-head") === tabKey
    );
  }

  function syncActiveHead(tab) {
    document.querySelectorAll("[data-console-tab-head]").forEach((node) => {
      if (node.getAttribute("data-console-tab-head") !== tab.key) {
        node.remove();
      }
    });
    tab.headNodes.forEach((node) => {
      if (!node.isConnected) {
        document.head.appendChild(node);
      }
    });
  }

  function ensureOverviewPlaceholder(currentKey) {
    if (currentKey === "/" || openTabs.has("/")) {
      return;
    }
    openTabs.set("/", {
      key: "/",
      url: new URL("/", window.location.origin),
      title: "概览",
      icon: "grid",
      pinned: true,
      placeholder: true,
      main: null,
      aside: null,
      runtime: null,
      bodyClass: "",
      shellClass: "",
      documentTitle: "概览",
      headNodes: [],
      lastActivated: 0,
    });
  }

  function ensureInitialTab() {
    if (openTabs.size || !document.querySelector("[data-console-tabs]")) {
      return;
    }
    const shell = document.querySelector(".app-shell");
    const main = shell?.querySelector(":scope > .main-content");
    if (!shell || !main) {
      return;
    }

    const url = new URL(window.location.href);
    const key = getTabKey(url);
    ensureOverviewPlaceholder(key);
    const navMeta = getNavMeta(key);
    const title = navMeta.label || normalizeLabel(main.getAttribute("data-console-tab-title")) || normalizeLabel(main.querySelector(".page-title")?.textContent) || "首页大盘";
    main.dataset.consoleTabKey = key;
    const aside = shell.querySelector(":scope > .right-sidebar");
    if (aside) {
      aside.dataset.consoleTabKey = key;
    }
    markInitialDynamicHead(key);

    const tab = {
      key,
      url,
      title,
      icon: navMeta.icon || "grid",
      pinned: key === "/",
      main,
      aside,
      runtime: currentPageRuntime,
      bodyClass: getPageBodyClass(document.body),
      shellClass: shell.className,
      documentTitle: document.title,
      headNodes: getTabHeadNodes(key),
      lastActivated: Date.now(),
    };
    openTabs.set(key, tab);
    renderTabs();
    activateTab(key, { pushState: false, skipScroll: true });
  }

  function applyPageChrome(tab) {
    const shell = document.querySelector(".app-shell");
    if (shell && tab.shellClass) {
      shell.className = tab.shellClass;
    }
    document.body.className = tab.bodyClass || "";
    applyReducedMotionState();
    document.body.classList.add("ui-ready");
    document.title = tab.documentTitle || tab.title;
  }

  function activateTab(tabKey, options = {}) {
    const tab = openTabs.get(tabKey);
    if (!tab) {
      return false;
    }
    if (tab.placeholder) {
      void ensureModuleTab(tab.url, { reload: true, pushState: options.pushState !== false });
      return false;
    }
    syncActiveHead(tab);
    openTabs.forEach((item) => {
      if (item.placeholder || !item.main) {
        return;
      }
      const active = item.key === tabKey;
      item.main.hidden = !active;
      if (item.aside) {
        item.aside.hidden = !active;
      }
    });
    tab.main.hidden = false;

    activeTabKey = tabKey;
    tab.lastActivated = Date.now();
    currentPageRuntime = tab.runtime;
    renderTabs();
    applyPageChrome(tab);
    updateActiveNav(tab.url.pathname);
    if (options.pushState !== false) {
      window.history.pushState({ consolePartial: true, tabKey }, "", tab.url.href);
    }
    if (!options.skipScroll) {
      window.scrollTo({ top: 0, left: 0 });
    }
    return true;
  }

  function pickNextTab(closedTab) {
    const candidates = Array.from(openTabs.values()).filter((tab) => tab.key !== closedTab.key);
    candidates.sort((a, b) => b.lastActivated - a.lastActivated);
    return candidates[0] || null;
  }

  function closeTab(tabKey, options = {}) {
    const tab = openTabs.get(tabKey);
    if (!tab || (tab.pinned && !options.force) || (openTabs.size <= 1 && !options.force)) {
      return false;
    }
    const wasActive = activeTabKey === tabKey;
    const nextTab = pickNextTab(tab);
    cleanupPageRuntime(tab.runtime);
    tab.main.remove();
    tab.aside?.remove();
    tab.headNodes.forEach((node) => node.remove());
    openTabs.delete(tabKey);
    renderTabs();

    if (wasActive && options.activateNext !== false && nextTab) {
      activateTab(nextTab.key);
    }
    return true;
  }

  function initNotices(root = document) {
    root.querySelectorAll("[data-notice-close]").forEach((button) => {
      if (!bindOnce(button, "notice")) {
        return;
      }
      button.addEventListener("click", () => {
        const notice = button.closest("[data-notice]");
        if (!notice) {
          return;
        }
        notice.classList.add("is-closing");
        window.setTimeout(() => {
          notice.remove();
          refreshIcons();
        }, 180);
      });
    });
  }

  function initCollapses(root = document) {
    root.querySelectorAll("[data-collapse-trigger]").forEach((trigger) => {
      if (!bindOnce(trigger, "collapse")) {
        return;
      }
      const targetSelector = trigger.getAttribute("data-collapse-target");
      const target = targetSelector ? document.querySelector(targetSelector) : null;
      if (!target) {
        return;
      }

      const sync = (expanded) => {
        trigger.setAttribute("aria-expanded", String(expanded));
        target.hidden = !expanded;
      };

      sync(trigger.getAttribute("aria-expanded") !== "false");

      trigger.addEventListener("click", () => {
        const expanded = trigger.getAttribute("aria-expanded") !== "false";
        sync(!expanded);
      });
    });
  }

  function initSubmitStates() {
    if (document.documentElement.hasAttribute("data-console-submit-bound")) {
      return;
    }
    document.documentElement.setAttribute("data-console-submit-bound", "true");

    document.addEventListener("submit", (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.matches("[data-ui-submit]")) {
        return;
      }

      const submitter = event.submitter || form.querySelector('button[type="submit"], input[type="submit"]');
      if (!(submitter instanceof HTMLElement)) {
        return;
      }

      submitter.setAttribute("aria-busy", "true");
      submitter.setAttribute("disabled", "disabled");

      if (!submitter.querySelector(".btn-spinner")) {
        const spinner = document.createElement("span");
        spinner.className = "btn-spinner";
        spinner.setAttribute("aria-hidden", "true");
        submitter.appendChild(spinner);
      }
    });

    window.addEventListener("pageshow", () => {
      document.querySelectorAll('[aria-busy="true"]').forEach((element) => {
        element.removeAttribute("aria-busy");
        element.removeAttribute("disabled");
        const spinner = element.querySelector(".btn-spinner");
        if (spinner) {
          spinner.remove();
        }
      });
    });
  }

  function updateActiveNav(pathname = window.location.pathname) {
    const currentPath = pathname || "/";
    const navLinks = Array.from(document.querySelectorAll("[data-nav-list] .nav-link, .mobile-bottom-nav__item[href]"));
    const hasSpecificNavMatch = navLinks.some((link) => {
      const href = link.getAttribute("href");
      if (!href || href === "#" || href === "/") {
        return false;
      }
      return (
        currentPath.startsWith(href) ||
        (href === "/ocr" && currentPath.startsWith("/documents")) ||
        (href === "/dispatch" && currentPath.startsWith("/dispatch"))
      );
    });
    navLinks.forEach((link) => {
      link.classList.remove("active");
      const href = link.getAttribute("href");
      if (!href || href === "#") {
        return;
      }
      if (href === "/") {
        if (currentPath === "/" || currentPath === "/portal" || (currentPath.startsWith("/modules") && !hasSpecificNavMatch)) {
          link.classList.add("active");
        }
        return;
      }
      if (
        currentPath.startsWith(href) ||
        (href === "/ocr" && currentPath.startsWith("/documents")) ||
        (href === "/dispatch" && currentPath.startsWith("/dispatch"))
      ) {
        link.classList.add("active");
      }
    });
  }

  function initGlobalTrackingSearch(root = document) {
    root.querySelectorAll("[data-global-tracking-search]").forEach((form) => {
      if (!(form instanceof HTMLFormElement) || !bindOnce(form, "global-search")) {
        return;
      }
      const input = form.querySelector("[data-global-tracking-input]");
      form.addEventListener("submit", (event) => {
        const value = input ? input.value.trim() : "";
        if (!value) {
          event.preventDefault();
          input?.focus();
          return;
        }
        input.value = value;
        if (document.querySelector("[data-console-tabs]")) {
          event.preventDefault();
          const url = new URL(form.getAttribute("action") || "/tracking", window.location.href);
          url.searchParams.set(input?.getAttribute("name") || "tracking_number", value);
          navigateContent(url, { reload: true });
        }
      });
    });
  }

  function initAvatarUpload(root = document) {
    root.querySelectorAll("[data-avatar-upload-form]").forEach((form) => {
      if (!(form instanceof HTMLFormElement) || !bindOnce(form, "avatar")) {
        return;
      }
      const input = form.querySelector("[data-avatar-upload-input]");
      const image = form.querySelector("[data-avatar-image]");
      if (!input || !image) {
        return;
      }

      input.addEventListener("change", async () => {
        const file = input.files && input.files[0];
        if (!file) {
          return;
        }
        if (!file.type.startsWith("image/")) {
          input.value = "";
          return;
        }

        form.setAttribute("aria-busy", "true");
        try {
          const payload = new FormData(form);
          const response = await fetch(form.action, {
            method: "POST",
            body: payload,
            headers: {
              Accept: "application/json",
              "X-Requested-With": "XMLHttpRequest",
            },
          });
          const result = await response.json();
          if (!response.ok || !result.ok) {
            throw new Error(result.message || "头像上传失败。");
          }
          image.src = result.avatar_url;
        } catch (error) {
          alert(error.message || "头像上传失败。");
        } finally {
          form.removeAttribute("aria-busy");
          input.value = "";
        }
      });
    });
  }

  function getMobileNavigationConfig() {
    const source = document.querySelector("#mobile-navigation-data");
    if (!source) return { routes: [], navigation: [], isSyncSupported: false };
    try {
      const parsed = JSON.parse(source.textContent || "{}");
      return {
        routes: Array.isArray(parsed.routes) ? parsed.routes : [],
        navigation: Array.isArray(parsed.navigation) ? parsed.navigation : [],
        isSyncSupported: Boolean(parsed.isSyncSupported),
      };
    } catch (_error) {
      return { routes: [], navigation: [], isSyncSupported: false };
    }
  }

  function initMobileNavigation() {
    const sheet = document.querySelector("[data-mobile-more-sheet]");
    const openButton = document.querySelector("[data-mobile-more-open]");
    const closeButton = sheet?.querySelector("[data-mobile-more-close]");
    const editor = sheet?.querySelector("[data-mobile-navigation-editor]");
    const editButton = sheet?.querySelector("[data-mobile-navigation-edit-toggle]");
    const form = sheet?.querySelector("[data-mobile-navigation-form]");
    const cancelButton = sheet?.querySelector("[data-mobile-navigation-cancel]");
    const status = sheet?.querySelector("[data-mobile-navigation-status]");
    if (!sheet || !openButton || !editor || !editButton || !form || sheet.hasAttribute("data-mobile-navigation-bound")) return;
    sheet.setAttribute("data-mobile-navigation-bound", "true");

    const config = getMobileNavigationConfig();
    const navigationByRoute = new Map(config.navigation.filter((item) => item && typeof item.route === "string").map((item) => [item.route, item]));
    let lastFocused = null;
    const announce = (message) => { if (status) status.textContent = message; };
    const focusableInSheet = () => Array.from(sheet.querySelectorAll('a[href], button:not([disabled]), select:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter((element) => !element.hidden && element.offsetParent !== null);
    const resetEditor = () => {
      form.hidden = true;
      editor.classList.remove("is-editing");
      editButton.setAttribute("aria-expanded", "false");
    };
    const closeSheet = ({ restoreFocus = true } = {}) => {
      if (sheet.open && typeof sheet.close === "function") sheet.close();
      else sheet.removeAttribute("open");
      document.body.classList.remove("has-mobile-sheet");
      openButton.setAttribute("aria-expanded", "false");
      resetEditor();
      if (restoreFocus) window.setTimeout(() => lastFocused?.focus(), 0);
    };
    const openSheet = () => {
      lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : openButton;
      if (typeof sheet.showModal === "function") {
        if (!sheet.open) sheet.showModal();
      } else sheet.setAttribute("open", "");
      document.body.classList.add("has-mobile-sheet");
      openButton.setAttribute("aria-expanded", "true");
      window.setTimeout(() => (closeButton || editButton).focus(), 0);
    };
    const updateSlotNumbers = () => {
      Array.from(form.querySelectorAll("[data-mobile-navigation-slot]")).forEach((slot, index) => {
        const number = slot.querySelector(".mobile-navigation-slot__index");
        const label = slot.querySelector("label .sr-only");
        const up = slot.querySelector('[data-mobile-navigation-move="up"]');
        const down = slot.querySelector('[data-mobile-navigation-move="down"]');
        if (number) number.textContent = String(index + 1);
        if (label) label.textContent = `第 ${index + 1} 个快捷入口`;
        if (up) up.disabled = index === 0;
        if (down) down.disabled = index === 2;
      });
    };
    const currentRoutes = () => Array.from(form.querySelectorAll("[data-mobile-navigation-select]")).map((select) => select.value);
    const updateBottomNavigation = (routes) => {
      const slots = Array.from(document.querySelectorAll("[data-mobile-nav-slot]"));
      routes.forEach((route, index) => {
        const slot = slots[index];
        const item = navigationByRoute.get(route);
        if (!slot || !item) return;
        slot.href = item.route;
        slot.dataset.mobileRoute = item.route;
        const label = slot.querySelector("span");
        if (label) label.textContent = item.mobile_label || item.label;
        const icon = slot.querySelector("svg, [data-feather]");
        if (icon) {
          const marker = document.createElement("i");
          marker.setAttribute("data-feather", item.icon || "circle");
          icon.replaceWith(marker);
          refreshIcons(slot);
        }
      });
      updateActiveNav();
    };

    openButton.addEventListener("click", openSheet);
    closeButton?.addEventListener("click", () => closeSheet());
    sheet.addEventListener("cancel", (event) => { event.preventDefault(); closeSheet(); });
    sheet.addEventListener("click", (event) => { if (event.target === sheet) closeSheet(); });
    sheet.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const focusable = focusableInSheet();
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    sheet.addEventListener("close", () => {
      document.body.classList.remove("has-mobile-sheet");
      openButton.setAttribute("aria-expanded", "false");
    });
    document.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest("[data-mobile-more-route]")) closeSheet({ restoreFocus: false });
    }, true);
    editButton.addEventListener("click", () => {
      if (!config.isSyncSupported) { announce("应急 Basic Auth 不支持同步移动底栏偏好。"); return; }
      const editing = form.hidden;
      form.hidden = !editing;
      editor.classList.toggle("is-editing", editing);
      editButton.setAttribute("aria-expanded", String(editing));
      if (editing) { updateSlotNumbers(); form.querySelector("select")?.focus(); }
    });
    cancelButton?.addEventListener("click", resetEditor);
    form.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-mobile-navigation-move]") : null;
      if (!target) return;
      const slot = target.closest("[data-mobile-navigation-slot]");
      const slots = Array.from(form.querySelectorAll("[data-mobile-navigation-slot]"));
      const index = slots.indexOf(slot);
      const direction = target.getAttribute("data-mobile-navigation-move");
      if (index < 0 || !direction) return;
      if (direction === "up" && index > 0) slots[index - 1].before(slot);
      if (direction === "down" && index < slots.length - 1) slots[index + 1].after(slot);
      updateSlotNumbers();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const routes = currentRoutes();
      if (routes.length !== 3 || new Set(routes).size !== 3 || routes.some((route) => !navigationByRoute.has(route))) {
        announce("请选择三个不同且有效的模块后再保存。");
        return;
      }
      const saveButton = form.querySelector("[data-mobile-navigation-save]");
      if (saveButton) saveButton.disabled = true;
      try {
        const response = await fetch("/settings/profile/mobile-navigation", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json", "X-Requested-With": "ConsoleMobileNavigation" },
          body: JSON.stringify({ routes }),
        });
        const result = await response.json();
        if (!response.ok || !result.ok || !Array.isArray(result.data?.routes)) throw new Error(result.error?.message || "移动底栏偏好未保存。");
        updateBottomNavigation(result.data.routes);
        announce("移动底栏已保存，并将在其他设备的下次打开时生效。");
        resetEditor();
      } catch (error) {
        announce(error?.message || "移动底栏偏好未保存。");
      } finally {
        if (saveButton) saveButton.disabled = false;
      }
    });
    updateSlotNumbers();
  }

  function isPermanentHeadNode(node) {
    if (!(node instanceof Element)) {
      return true;
    }
    return permanentHeadSelectors.some((selector) => node.matches(selector));
  }

  function markInitialDynamicHead(tabKey = getTabKey(new URL(window.location.href))) {
    if (initialHeadMarked) {
      return;
    }
    initialHeadMarked = true;
    Array.from(document.head.children).forEach((node) => {
      if (!isPermanentHeadNode(node)) {
        node.setAttribute("data-console-page-head", "true");
        node.setAttribute("data-console-tab-head", tabKey);
      }
    });
  }

  function runInlineScript(code) {
    if (!code || !code.trim()) {
      return;
    }

    const originalDocumentAdd = document.addEventListener;
    const originalDocumentRemove = document.removeEventListener;
    const originalWindowAdd = window.addEventListener;
    const originalWindowRemove = window.removeEventListener;
    const originalSetInterval = window.setInterval;
    const originalWindowClearInterval = window.clearInterval;
    const originalSetTimeout = window.setTimeout;
    const originalWindowClearTimeout = window.clearTimeout;
    const runtime = currentPageRuntime;

    document.addEventListener = function patchedDocumentAdd(type, listener, options) {
      if (type === "DOMContentLoaded" && typeof listener === "function") {
        listener.call(document, new Event("DOMContentLoaded"));
        return;
      }
      originalDocumentAdd.call(document, type, listener, options);
      runtime.listeners.push({ target: document, type, listener, options });
    };

    document.removeEventListener = function patchedDocumentRemove(type, listener, options) {
      originalDocumentRemove.call(document, type, listener, options);
      runtime.listeners = runtime.listeners.filter(
        (item) => !(item.target === document && item.type === type && item.listener === listener)
      );
    };

    window.addEventListener = function patchedWindowAdd(type, listener, options) {
      if (type === "load" && typeof listener === "function") {
        listener.call(window, new Event("load"));
        return;
      }
      originalWindowAdd.call(window, type, listener, options);
      runtime.listeners.push({ target: window, type, listener, options });
    };

    window.removeEventListener = function patchedWindowRemove(type, listener, options) {
      originalWindowRemove.call(window, type, listener, options);
      runtime.listeners = runtime.listeners.filter(
        (item) => !(item.target === window && item.type === type && item.listener === listener)
      );
    };

    window.setInterval = function patchedSetInterval(...args) {
      const id = originalSetInterval.apply(window, args);
      runtime.intervals.add(id);
      return id;
    };

    window.clearInterval = function patchedClearInterval(id) {
      runtime.intervals.delete(id);
      return originalWindowClearInterval.call(window, id);
    };

    window.setTimeout = function patchedSetTimeout(...args) {
      const id = originalSetTimeout.apply(window, args);
      runtime.timeouts.add(id);
      return id;
    };

    window.clearTimeout = function patchedClearTimeout(id) {
      runtime.timeouts.delete(id);
      return originalWindowClearTimeout.call(window, id);
    };

    try {
      const script = document.createElement("script");
      script.textContent = `(function(){\n${code}\n})();`;
      document.body.appendChild(script);
      script.remove();
    } finally {
      document.addEventListener = originalDocumentAdd;
      document.removeEventListener = originalDocumentRemove;
      window.addEventListener = originalWindowAdd;
      window.removeEventListener = originalWindowRemove;
      window.setInterval = originalSetInterval;
      window.clearInterval = originalWindowClearInterval;
      window.setTimeout = originalSetTimeout;
      window.clearTimeout = originalWindowClearTimeout;
    }
  }

  function appendExternalScript(scriptNode, target = document.body, tabKey = "") {
    const src = new URL(scriptNode.getAttribute("src"), window.location.href).href;
    if (loadedScriptSources.has(src)) {
      return Promise.resolve(null);
    }

    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      Array.from(scriptNode.attributes).forEach((attr) => {
        script.setAttribute(attr.name, attr.value);
      });
      script.src = src;
      script.async = false;
      script.setAttribute("data-console-page-script", "true");
      if (tabKey) {
        script.setAttribute("data-console-tab-head", tabKey);
      }
      script.addEventListener("load", () => {
        loadedScriptSources.add(src);
        resolve(script);
      });
      script.addEventListener("error", () => reject(new Error(`Script load failed: ${src}`)));
      target.appendChild(script);
    });
  }

  function isStylesheetLink(node) {
    if (node.tagName !== "LINK") {
      return false;
    }
    return (node.getAttribute("rel") || "")
      .split(/\s+/)
      .some((value) => value.toLowerCase() === "stylesheet");
  }

  function throwIfHeadSyncAborted(signal) {
    if (signal?.aborted) {
      throw new DOMException("Page head synchronization aborted.", "AbortError");
    }
  }

  function appendStylesheet(linkNode, tabKey, signal) {
    const rawHref = linkNode.getAttribute("href");
    if (!rawHref) {
      return Promise.reject(new Error("Stylesheet link is missing href."));
    }
    const href = new URL(rawHref, window.location.href).href;

    return new Promise((resolve, reject) => {
      const link = document.createElement("link");
      Array.from(linkNode.attributes).forEach((attr) => {
        link.setAttribute(attr.name, attr.value);
      });
      link.href = href;
      link.setAttribute("data-console-page-head", "true");
      link.setAttribute("data-console-tab-head", tabKey);

      let settled = false;
      let timeoutId = 0;
      const finish = (error = null) => {
        if (settled) {
          return;
        }
        settled = true;
        window.clearTimeout(timeoutId);
        link.removeEventListener("load", onLoad);
        link.removeEventListener("error", onError);
        signal?.removeEventListener("abort", onAbort);
        if (error) {
          link.remove();
          reject(error);
          return;
        }
        resolve(link);
      };
      const onLoad = () => finish();
      const onError = () => finish(new Error(`Stylesheet load failed: ${href}`));
      const onAbort = () => finish(new DOMException("Stylesheet load aborted.", "AbortError"));

      link.addEventListener("load", onLoad, { once: true });
      link.addEventListener("error", onError, { once: true });
      signal?.addEventListener("abort", onAbort, { once: true });
      if (signal?.aborted) {
        onAbort();
        return;
      }
      timeoutId = window.setTimeout(() => {
        finish(new Error(`Stylesheet load timed out: ${href}`));
      }, headAssetLoadTimeoutMs);
      document.head.appendChild(link);
    });
  }

  async function syncHead(nextDocument, tabKey, signal) {
    const headNodes = [];

    try {
      for (const node of Array.from(nextDocument.head.children)) {
        throwIfHeadSyncAborted(signal);
        if (isPermanentHeadNode(node)) {
          continue;
        }

        if (node.tagName === "SCRIPT") {
          if (node.src) {
            const appended = await appendExternalScript(node, document.head, tabKey);
            if (appended) {
              headNodes.push(appended);
            }
            throwIfHeadSyncAborted(signal);
          } else {
            runInlineScript(node.textContent || "");
          }
          continue;
        }

        if (isStylesheetLink(node)) {
          const appended = await appendStylesheet(node, tabKey, signal);
          headNodes.push(appended);
          continue;
        }

        const imported = document.importNode(node, true);
        imported.setAttribute("data-console-page-head", "true");
        imported.setAttribute("data-console-tab-head", tabKey);
        document.head.appendChild(imported);
        headNodes.push(imported);
      }
      throwIfHeadSyncAborted(signal);
      return headNodes;
    } catch (error) {
      headNodes.forEach((node) => node.remove());
      throw error;
    }
  }

  function syncRightSidebar(nextDocument, shell, main) {
    const currentAside = shell.querySelector(":scope > .right-sidebar");
    const nextAside = nextDocument.querySelector(".app-shell > .right-sidebar");
    if (currentAside && nextAside) {
      currentAside.replaceWith(document.importNode(nextAside, true));
      return;
    }
    if (currentAside && !nextAside) {
      currentAside.remove();
      return;
    }
    if (!currentAside && nextAside) {
      main.insertAdjacentElement("afterend", document.importNode(nextAside, true));
    }
  }

  async function executeBodyScripts(nextDocument) {
    const scripts = Array.from(nextDocument.body.querySelectorAll("script"));
    for (const script of scripts) {
      const src = script.getAttribute("src") || "";
      if (src.includes("/static/console_ui.js")) {
        continue;
      }
      const code = script.textContent || "";
      if (!src && code.includes("ConsoleUI.initPage")) {
        continue;
      }
      if (src) {
        await appendExternalScript(script, document.body);
      } else {
        runInlineScript(code);
      }
    }
  }

  async function ensureModuleTab(url, options = {}) {
    const tabKey = getTabKey(url);
    let existing = openTabs.get(tabKey);
    if (existing?.placeholder) {
      openTabs.delete(tabKey);
      existing = null;
    }
    if (existing && !options.reload) {
      existing.url = url;
      activateTab(tabKey, { pushState: options.pushState !== false });
      return existing;
    }
    if (existing && options.reload) {
      closeTab(tabKey, { force: true, activateNext: false });
    }

    const sequence = ++navigationSeq;
    if (navigationController) {
      navigationController.abort();
    }
    const controller = new AbortController();
    navigationController = controller;
    updateActiveNav(url.pathname);
    document.body.classList.add("content-loading");

    try {
      const runtime = createPageRuntime();
      currentPageRuntime = runtime;
      const response = await fetch(url.href, {
        headers: {
          Accept: "text/html",
          "X-Requested-With": "ConsolePartialNavigation",
        },
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const html = await response.text();
      if (sequence !== navigationSeq) {
        return;
      }
      const nextDocument = new DOMParser().parseFromString(html, "text/html");
      const nextShell = nextDocument.querySelector(".app-shell");
      const nextMain = nextDocument.querySelector(".main-content");
      const shell = document.querySelector(".app-shell");
      if (!nextShell || !nextMain || !shell || nextDocument.body.classList.contains("login-page")) {
        window.location.assign(url.href);
        return;
      }

      const headNodes = await syncHead(nextDocument, tabKey, controller.signal);
      if (sequence !== navigationSeq) {
        headNodes.forEach((node) => node.remove());
        return;
      }

      const importedMain = document.importNode(nextMain, true);
      importedMain.dataset.consoleMain = "";
      importedMain.dataset.consoleTabKey = tabKey;
      importedMain.hidden = true;
      const nextAside = nextShell.querySelector(":scope > .right-sidebar");
      const importedAside = nextAside ? document.importNode(nextAside, true) : null;
      if (importedAside) {
        importedAside.dataset.consoleAside = "";
        importedAside.dataset.consoleTabKey = tabKey;
        importedAside.hidden = true;
      }

      shell.appendChild(importedMain);
      if (importedAside) {
        shell.appendChild(importedAside);
      }

      const navMeta = getNavMeta(tabKey);
      const tab = {
        key: tabKey,
        url,
        title: navMeta.label || getDocumentTabTitle(nextDocument, url),
        icon: navMeta.icon || "file",
        pinned: tabKey === "/",
        main: importedMain,
        aside: importedAside,
        runtime,
        bodyClass: getPageBodyClass(nextDocument.body),
        shellClass: nextShell.className,
        documentTitle: nextDocument.title,
        headNodes,
        lastActivated: Date.now(),
      };
      openTabs.set(tabKey, tab);
      renderTabs();
      activateTab(tabKey, { pushState: options.pushState !== false, skipScroll: true });

      await executeBodyScripts(nextDocument);
      if (sequence !== navigationSeq) {
        return;
      }
      initPage(importedMain);
      if (importedAside) {
        initPage(importedAside);
      }
      window.scrollTo({ top: 0, left: 0 });
      return tab;
    } catch (error) {
      if (error?.name === "AbortError") {
        return;
      }
      console.warn("Partial navigation failed, falling back to full navigation.", error);
      window.location.assign(url.href);
    } finally {
      if (sequence === navigationSeq) {
        navigationController = null;
        document.body.classList.remove("content-loading");
      }
    }
  }

  async function navigateContent(url, options = {}) {
    ensureInitialTab();
    return ensureModuleTab(url, options);
  }

  function shouldHandleSidebarLink(event, link) {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return false;
    }

    if (!(link instanceof HTMLAnchorElement)) {
      return false;
    }

    const href = link.getAttribute("href");
    if (
      !href ||
      href === "#" ||
      href.startsWith("#") ||
      href.startsWith("javascript:") ||
      link.hasAttribute("download")
    ) {
      return false;
    }

    const targetAttr = (link.getAttribute("target") || "").trim().toLowerCase();
    if (targetAttr && targetAttr !== "_self") {
      return false;
    }

    const nextUrl = new URL(href, window.location.href);
    return nextUrl.origin === window.location.origin;
  }

  function initPartialSidebarNavigation() {
    if (document.documentElement.hasAttribute("data-console-nav-bound")) {
      return;
    }
    document.documentElement.setAttribute("data-console-nav-bound", "true");

    document.addEventListener(
      "click",
      (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const link = target?.closest("[data-nav-list] a[href], [data-shell-home-link][href]");
        if (!shouldHandleSidebarLink(event, link)) {
          return;
        }

        event.preventDefault();
        navigateContent(new URL(link.getAttribute("href"), window.location.href));
      },
      true
    );

    window.addEventListener("popstate", (event) => {
      if (
        event.state?.consoleMode === true &&
        window.location.pathname === "/ocr" &&
        document.querySelector("[data-mode-root]")
      ) {
        return;
      }
      const url = new URL(window.location.href);
      const tabKey = getTabKey(url);
      if (openTabs.has(tabKey)) {
        const tab = openTabs.get(tabKey);
        tab.url = url;
        activateTab(tabKey, { pushState: false });
        return;
      }
      navigateContent(url, { pushState: false });
    });
  }

  function initPage(root = document) {
    if (root === document) {
      ensureInitialTab();
    }
    applyReducedMotionState();
    initReveal();
    initNotices(root);
    initCollapses(root);
    initGlobalTrackingSearch(root);
    initAvatarUpload(root);
    if (root === document) {
      initMobileNavigation();
    }
    updateActiveNav();
    refreshIcons(root);
  }

  document.addEventListener("DOMContentLoaded", () => {
    initSubmitStates();
    initPartialSidebarNavigation();
    initPage();
  });

  window.addEventListener("beforeunload", () => {
    cleanupPageRuntime();
  });

  if (typeof reducedMotionQuery.addEventListener === "function") {
    reducedMotionQuery.addEventListener("change", applyReducedMotionState);
  }

  window.ConsoleUI = {
    refreshIcons,
    initPage,
    navigateContent,
    ensureModuleTab,
    activateTab,
    closeTab,
  };
})();
