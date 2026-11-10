(() => {
  "use strict";

  // Overview “待确认”: customer-service problems waiting for a reply. Read-only;
  // “处理” opens the customer-service page, which queries and opens that problem.
  // The message center (monitoring.js) owns the tab and asks this script to render rows.
  const TIER_TEXT = { L0: "新问题", L1: "AI 建议", L2: "按模板", L3: "AI 已自动回复" };
  const REFRESH_MS = 60000;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function badge(scenario) {
    if (!scenario) return '<span class="overview-pending__badge">待 AI 判断</span>';
    if (scenario.locked) return `<span class="overview-pending__badge is-locked" title="${escapeHtml(scenario.name)}"><i data-feather="lock"></i>必须人工</span>`;
    const tone = scenario.tier === "L0" ? "is-new" : (scenario.tier === "L3" ? "is-auto" : "is-advice");
    const icon = scenario.tier === "L0" ? "alert-circle" : (scenario.tier === "L2" ? "file-text" : "zap");
    return `<span class="overview-pending__badge ${tone}" title="${escapeHtml(scenario.name)}"><i data-feather="${icon}"></i>${escapeHtml(TIER_TEXT[scenario.tier] || scenario.tier)}</span>`;
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
    const dashboard = document.querySelector("[data-monitoring-dashboard]");
    const page = dashboard?.closest(".main-content") || document;
    if (!dashboard || dashboard.dataset.pendingBound === "true") return;
    dashboard.dataset.pendingBound = "true";
    const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
    let timer = null;
    let latest = null;
    let failure = "";

    const blocks = () => Array.from(page.querySelectorAll("[data-overview-pending]"));
    const publish = (available) => {
      dashboard.dispatchEvent(new CustomEvent("overview:pending", {
        detail: { available, total: latest ? Number(latest.total || 0) : null },
      }));
    };

    function render(list, status) {
      if (!list) return;
      if (failure) {
        list.innerHTML = "";
        if (status) { status.textContent = `待确认暂不可用：${failure}`; status.dataset.status = "error"; }
        return;
      }
      if (!latest) {
        if (status) status.textContent = "正在读取待确认问题件…";
        return;
      }
      const items = latest.items || [];
      list.innerHTML = items.map((item) => {
        const key = [item.source_id, item.external_id, item.source_direction].join("|");
        const href = `/modules/customer-service?open=${encodeURIComponent(key)}&q=${encodeURIComponent(item.waybill_no || "")}`;
        const wait = waited(item.updated_at || item.created_at);
        const system = item.platform === "yunda" ? "yunda" : "ronghui";
        const title = `${item.problem_type ? `【${item.problem_type}】` : ""}${item.problem_text || "问题件"}`;
        return `<li class="monitor-item monitor-item--pending">
          <div class="monitor-item-head">
            <span class="monitor-source-tag" data-system="${system}">${system === "yunda" ? "韵达" : "融辉"}</span>
            <span class="monitor-item-kind">问题件</span>
            <span class="monitor-item-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
            <div class="monitor-item-aside">
              ${badge(item.ai_scenario)}
              <span class="monitor-item-time">${wait ? `已等 ${escapeHtml(wait)}` : ""}</span>
              <a class="primary-btn btn--small overview-pending__open" href="${escapeHtml(href)}" data-console-navigate>处理</a>
            </div>
          </div>
          <div class="monitor-item-meta"><span>运单 ${escapeHtml(item.waybill_no || "—")}</span></div>
        </li>`;
      }).join("");
      if (status) {
        const more = (latest.total || 0) > items.length ? `显示最早的 ${items.length} 条，共 ${latest.total} 条` : "";
        status.textContent = items.length
          ? ([more, latest.errors ? "部分来源最近采集未成功，数据可能不是最新" : ""].filter(Boolean).join("；") || "来自客户服务已发布数据，按等待时间排序")
          : "没有等待回复的问题件。";
        status.dataset.status = latest.errors ? "warning" : "";
      }
      if (window.feather) window.feather.replace();
    }

    async function load() {
      if (!dashboard.isConnected) return;
      try {
        const response = await pageFetch("/customer-service/pending-summary", { headers: { Accept: "application/json" } });
        if (response.status === 401 || response.status === 403) {
          blocks().forEach((node) => { node.hidden = true; }); // No customer-service permission.
          latest = null;
          publish(false);
          return;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.message || "客服数据暂不可用");
        latest = data;
        failure = "";
        const total = Number(data.total || 0);
        blocks().forEach((node) => { node.hidden = false; });
        page.querySelectorAll("[data-overview-pending-total]").forEach((node) => { node.textContent = String(total); });
        page.querySelectorAll("[data-overview-pending-note]").forEach((node) => { node.textContent = `问题件 ${total}`; });
        page.querySelectorAll("[data-overview-pending-more]").forEach((node) => {
          node.textContent = `在客户服务中查看全部 ${total} 条待确认 →`;
        });
        publish(true);
      } catch (error) {
        failure = error.message || "请稍后刷新";
        blocks().forEach((node) => { node.hidden = false; });
        page.querySelectorAll("[data-overview-pending-total]").forEach((node) => { node.textContent = "—"; });
        publish(true);
      } finally {
        clearTimeout(timer);
        if (dashboard.isConnected) timer = setTimeout(() => { if (!document.hidden) load(); else timer = setTimeout(load, REFRESH_MS); }, REFRESH_MS);
      }
    }

    dashboard.addEventListener("overview:pending-render", (event) => render(event.detail?.list, event.detail?.status));
    dashboard.addEventListener("overview:pending-reload", () => { load(); });
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
