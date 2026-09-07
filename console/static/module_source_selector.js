(() => {
  "use strict";
  const fetch = window.ConsoleUI.pageRequest();
  const select = document.querySelector(".main-content:not([hidden]) [data-source-selector]");
  if (!select || select.dataset.initialized) return;
  select.dataset.initialized = "true";
  const requested = new URLSearchParams(location.search).get("source_id") || "";
  const status = document.createElement("span");
  status.setAttribute("role", "status");
  select.parentElement.append(status);
  const refresh = () => {
    select.dispatchEvent(new Event("module-source-change"));
  };
  select.addEventListener("change", () => {
    window.ConsoleUI.updatePageQuery("source_id", select.value);
    refresh();
  });
  select.closest(".main-content").addEventListener("console:querychange", event => {
    const value = new URL(event.detail.url).searchParams.get("source_id") || "";
    if (!Array.from(select.options).some(option => option.value === value)) {
      status.textContent = "所选来源不存在或无权访问。";
      return;
    }
    select.value = value;
    status.textContent = "";
    refresh();
  });
  fetch(`/module-data-sources/${select.dataset.sourceSelector}`, {credentials: "same-origin"})
    .then(async response => {
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error("来源列表暂时无法读取。");
      for (const source of data.data.sources) {
        const label = source.display_name + (source.status === "history_only" ? " · 历史" : "");
        select.add(new Option(label, source.source_id));
      }
      if (requested) {
        if (!Array.from(select.options).some(option => option.value === requested)) throw new Error("所选来源不存在或无权访问。");
        select.value = requested;
        refresh();
      }
    }).catch(error => {status.textContent = error.message;});
})();
