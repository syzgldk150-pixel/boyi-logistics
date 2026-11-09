(() => {
  "use strict";

  // Overview “待我确认”: customer-service problems waiting for a reply. Read-only;
  // “处理” opens the customer-service page, which queries and opens that problem.
  const TIER_TEXT = { L0: "新问题", L1: "AI 建议", L2: "按模板", L3: "自动处理" };
  const REFRESH_MS = 60000;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function badge(scenario) {
    if (!scenario) return '<span class="overview-pending__badge">待 AI 判断</span>';
    if (scenario.locked) return `<span class="overview-pending__badge" title="${escapeHtml(scenario.name)}">必须人工</span>`;
    const tone = scenario.tier === "L0" ? "is-new" : "is-advice";
    return `<span class="overview-pending__badge ${tone}" title="${escapeHtml(scenario.name)}">${escapeHtml(TIER_TEXT[scenario.tier] || scenario.tier)}</span>`;
  }

  function waited(value) {
    const time = Date.parse(String(value || "").replace(" ", "T"));
    if (!Number.isFinite(time)) return "";
    const minutes = Math.max(0, Math.round((Date.now() - time) / 60000));
    if (minutes < 60) return `${minutes} 分钟`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时`;
    return `${Math.floor(minutes / 1440)} 天`;
  }

  function init() {
    const root = document.querySelector("[data-overview-pending]");
    if (!root || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const list = root.querySelector("[data-overview-pending-list]");
    const count = root.querySelector("[data-overview-pending-count]");
    const status = root.querySelector("[data-overview-pending-status]");
    let timer = null;
    const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);

    async function load() {
      if (!root.isConnected) return;
      try {
        const response = await pageFetch("/customer-service/pending-summary", { headers: { Accept: "application/json" } });
        if (response.status === 401 || response.status === 403) {
          root.hidden = true; // No customer-service permission: the block stays out of the way.
          return;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.message || "客服数据暂不可用");
        root.hidden = false;
        count.textContent = String(data.total || 0);
        const items = data.items || [];
        list.innerHTML = items.length ? items.map((item) => {
          const key = [item.source_id, item.external_id, item.source_direction].join("|");
          const href = `/modules/customer-service?open=${encodeURIComponent(key)}&q=${encodeURIComponent(item.waybill_no || "")}`;
          const wait = waited(item.updated_at || item.created_at);
          return `<div class="overview-pending__row" role="listitem">
            <span class="overview-pending__src">${item.platform === "yunda" ? "韵达" : "融辉"} · 问题件</span>
            <span>${badge(item.ai_scenario)}</span>
            <span class="overview-pending__text" title="${escapeHtml(item.problem_text)}">${escapeHtml(item.problem_type ? `【${item.problem_type}】` : "")}${escapeHtml(item.problem_text)}</span>
            <span class="overview-pending__no">${escapeHtml(item.waybill_no)}</span>
            <span class="overview-pending__wait">${wait ? `已等 ${escapeHtml(wait)}` : ""}</span>
            <a class="ghost-btn overview-pending__open" href="${escapeHtml(href)}" data-console-navigate>处理</a>
          </div>`;
        }).join("") : '<p class="overview-pending__empty">没有等待回复的问题件。</p>';
        const more = (data.total || 0) > items.length ? `显示最早的 ${items.length} 条，共 ${data.total} 条` : "";
        status.textContent = [more, data.errors ? "部分来源最近采集未成功，数据可能不是最新" : ""].filter(Boolean).join("；") || "来自客户服务已发布数据";
      } catch (error) {
        root.hidden = false;
        list.innerHTML = "";
        status.textContent = `待我确认暂不可用：${error.message || "请稍后刷新"}`;
      } finally {
        if (window.feather) window.feather.replace();
        clearTimeout(timer);
        if (root.isConnected) timer = setTimeout(() => { if (!document.hidden) load(); else timer = setTimeout(load, REFRESH_MS); }, REFRESH_MS);
      }
    }

    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
