(() => {
  "use strict";
  const root = document.querySelector(".main-content:not([hidden]) [data-module-plugin-icons]");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  const fetch = window.ConsoleUI.pageRequest();
  const list = root.querySelector("[data-module-plugin-icon-list]");
  const manageUrl = root.dataset.manageUrl;

  function icon(plugin) {
    const url = new URL(manageUrl, window.location.href);
    url.searchParams.set("plugin", plugin.automation_id);
    const link = document.createElement("a");
    link.className = "module-plugin-icon";
    link.href = url.pathname + url.search;
    link.setAttribute("role", "listitem");
    link.title = `${plugin.name} · ${plugin.status_label}`;
    link.setAttribute("aria-label", `${plugin.name}，${plugin.status_label}，打开插件`);
    const glyph = document.createElement("span");
    glyph.className = "module-plugin-icon__glyph";
    glyph.setAttribute("aria-hidden", "true");
    glyph.textContent = Array.from(plugin.name.trim())[0] || "插";
    const dot = document.createElement("span");
    dot.className = "module-plugin-icon__dot";
    dot.dataset.tone = plugin.tone;
    dot.setAttribute("aria-hidden", "true");
    link.append(glyph, dot);
    link.addEventListener("click", (event) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      // Each icon filters the same data-source tab; reload so the filter applies.
      window.ConsoleUI.navigateContent(url, {reload: true});
    });
    return link;
  }

  fetch(`/module-data-sources/${encodeURIComponent(root.dataset.module)}/plugins`, {credentials: "same-origin"})
    .then(response => response.json().then(payload => ({response, payload})))
    .then(({response, payload}) => {
      if (!response.ok || !payload?.ok) return;
      const plugins = payload.data?.plugins || [];
      if (!plugins.length) return;
      list.replaceChildren(...plugins.map(icon));
      root.hidden = false;
    })
    .catch(() => {});
})();
