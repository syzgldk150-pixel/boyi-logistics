(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ENDPOINTS = {
    summary: "/finance/summary",
    trend: "/finance/trend",
    entries: "/finance/entries",
    mappings: "/finance/fee-mappings",
    batches: "/finance/sync-batches",
    sync: "/finance/sync",
    backfill: "/finance/backfill",
    reviews: "/finance/review-cases",
    analyzeReviews: "/finance/reviews/analyze",
    waybillFacts: "/finance/waybill-facts",
    knowledge: "/finance/knowledge",
    llmStatus: "/settings/llm/status",
  };

  function initFinanceWorkbenches() {
    document.querySelectorAll("[data-finance-workbench]").forEach(initFinanceWorkbench);
  }

  function initFinanceWorkbench(root) {
    if (!root || root.dataset.financeBound === "true") return;
    root.dataset.financeBound = "true";

    const $ = (selector) => root.querySelector(selector);
    const $$ = (selector) => Array.from(root.querySelectorAll(selector));
    const state = {
      activeTab: "overview",
      loadedTabs: new Set(),
      accounts: [],
      bookingFeeItems: {},
      entryPage: 1,
      entryPageSize: 50,
      entryTotal: 0,
      loadingEntries: false,
      loadingMappings: false,
      loadingBatches: false,
      loadingReviews: false,
      loadingWaybillFacts: false,
    };

    const statusNode = $("[data-finance-status]");
    const freshnessNode = $("[data-finance-freshness]");
    const errorNode = $("[data-finance-error]");
    const refreshButton = $("[data-finance-refresh]");
    const platformSelect = $("[data-finance-platform]");
    const accountSelect = $("[data-finance-account]");
    const startDateInput = $("[data-finance-start-date]");
    const endDateInput = $("[data-finance-end-date]");
    const entryForm = $("[data-finance-entry-form]");
    const mappingForm = $("[data-finance-mapping-form]");
    const syncForm = $("[data-finance-sync-form]");
    const backfillForm = $("[data-finance-backfill-form]");
    const reviewForm = $("[data-finance-review-form]");
    const waybillForm = $("[data-finance-waybill-form]");

    function isRootActive() {
      if (!root.isConnected || root.closest("[hidden]")) return false;
      const page = root.closest(".main-content");
      return !page || !page.hidden;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function displayText(value, fallback = "无数据") {
      if (value === null || value === undefined || value === "") return fallback;
      return String(value);
    }

    function moneyText(value) {
      const text = displayText(value);
      return text === "无数据" ? text : `${text} 元`;
    }

    function platformLabel(value) {
      return { ronghui: "融辉", yunda: "韵达" }[String(value || "")] || displayText(value, "未知平台");
    }

    function directionLabel(value) {
      return { income: "收入", expense: "支出" }[String(value || "")] || displayText(value, "未识别");
    }

    function levelLabel(value) {
      return { waybill: "运单级", operating: "运营级" }[String(value || "")] || "待绑定";
    }

    function mappingStatusLabel(value) {
      return { pending: "待绑定", bound: "已绑定" }[String(value || "")] || displayText(value, "待确认");
    }

    function validationStatusLabel(value) {
      return {
        passed: "通过",
        warning: "有警告",
        failed: "失败",
        unavailable: "不可用",
      }[String(value || "")] || "未校验";
    }

    function syncStatusMeta(value) {
      const status = String(value || "");
      return {
        queued: ["排队中", "info"],
        running: ["同步中", "info"],
        success: ["成功", "success"],
        partial_failed: ["部分失败", "warning"],
        failed: ["失败", "error"],
        no_data: ["无数据", "warning"],
      }[status] || [displayText(status, "未知"), ""];
    }

    function setStatus(message, tone = "") {
      if (!statusNode) return;
      statusNode.textContent = message;
      statusNode.dataset.tone = tone;
    }

    function showError(message) {
      if (!errorNode) return;
      errorNode.textContent = message;
      errorNode.hidden = !message;
    }

    function setPanelState(node, message = "", tone = "") {
      if (!node) return;
      node.textContent = message;
      node.dataset.tone = tone;
      node.hidden = !message;
    }

    function setButtonBusy(button, busy, busyLabel) {
      if (!button) return;
      const label = button.querySelector("span") || button;
      if (!button.dataset.originalLabel) button.dataset.originalLabel = label.textContent || "";
      button.disabled = busy;
      button.setAttribute("aria-busy", String(busy));
      label.textContent = busy ? busyLabel : button.dataset.originalLabel;
    }

    async function fetchJson(url, options = {}) {
      const headers = options.body
        ? { "Content-Type": "application/json", ...(options.headers || {}) }
        : (options.headers || {});
      const response = await fetch(url, {
        credentials: "same-origin",
        ...options,
        headers,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        const error = new Error(payload.message || `财务接口请求失败（HTTP ${response.status}）。`);
        error.code = payload.error_code || "FINANCE_REQUEST_FAILED";
        throw error;
      }
      return payload.data ?? payload;
    }

    function newBrowserRequestUuid() {
      if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
        throw new Error("当前浏览器无法生成安全的请求标识，财务计划未提交。");
      }
      return window.crypto.randomUUID();
    }

    function financeCommandOptions(body) {
      return {
        method: "POST",
        headers: {"X-Browser-Request-UUID": newBrowserRequestUuid()},
        body: JSON.stringify(body || {}),
      };
    }

    function financeReceiptText(receipt, prefix) {
      const runId = String(receipt?.run_id || "").trim();
      const workItemId = String(receipt?.work_item_id || "").trim();
      const ids = [runId ? `Run ${runId}` : "", workItemId ? `事项 ${workItemId}` : ""]
        .filter(Boolean)
        .join(" / ");
      return `${prefix}已提交${ids ? `（${ids}）` : ""}，请在事项中心完成审批并查看结果。`;
    }

    function toQuery(params) {
      const query = new URLSearchParams();
      Object.entries(params || {}).forEach(([key, value]) => {
        if (value === null || value === undefined || value === "" || value === "all") return;
        query.set(key, String(value));
      });
      return query.toString();
    }

    function globalFilters() {
      return {
        start_date: startDateInput?.value || "",
        end_date: endDateInput?.value || "",
        platform: platformSelect?.value || "all",
        account_id: accountSelect?.value || "all",
      };
    }

    function formatDateKey(date) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    }

    function parseDateKey(value) {
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
      if (!match) return null;
      const parsed = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
      return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function addDays(date, days) {
      return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
    }

    function setDateRange(start, end, preset) {
      if (startDateInput) startDateInput.value = start;
      if (endDateInput) endDateInput.value = end;
      $$('[data-finance-range]').forEach((button) => {
        const active = button.dataset.financeRange === preset;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      syncEntryDates();
    }

    function applyDatePreset(preset) {
      const today = parseDateKey(root.dataset.today) || new Date();
      if (preset === "month") {
        setDateRange(root.dataset.monthStart || formatDateKey(new Date(today.getFullYear(), today.getMonth(), 1)), formatDateKey(today), preset);
      } else if (preset === "yesterday") {
        const yesterday = formatDateKey(addDays(today, -1));
        setDateRange(yesterday, yesterday, preset);
      } else if (preset === "7days") {
        setDateRange(formatDateKey(addDays(today, -6)), formatDateKey(today), preset);
      } else {
        setDateRange(startDateInput?.value || "", endDateInput?.value || "", "custom");
        startDateInput?.focus();
      }
    }

    function optionLabel(account) {
      const login = displayText(account.login_account, account.account_id || "未命名账号");
      return `${platformLabel(account.platform)} · ${login}`;
    }

    function updateAccountOptions(accounts) {
      const byId = new Map();
      (accounts || []).forEach((account) => {
        const key = `${account.platform || ""}:${account.account_id || ""}`;
        if (account.account_id && !byId.has(key)) byId.set(key, account);
      });
      state.accounts = Array.from(byId.values());
      const selectors = [
        accountSelect,
        $("[data-finance-entry-account]"),
        $("[data-finance-sync-account]"),
        $("[data-finance-backfill-account]"),
      ].filter(Boolean);
      selectors.forEach((select) => {
        const selected = select.value;
        select.innerHTML = '<option value="all">全部账号</option>';
        state.accounts.forEach((account) => {
          const option = document.createElement("option");
          option.value = String(account.account_id || "");
          option.dataset.platform = String(account.platform || "");
          option.textContent = optionLabel(account);
          select.appendChild(option);
        });
        if (Array.from(select.options).some((option) => option.value === selected)) select.value = selected;
      });
      filterAllAccountSelects();
    }

    function filterAccountSelect(select, platform) {
      if (!select) return;
      Array.from(select.options).forEach((option, index) => {
        if (index === 0) return;
        const visible = !platform || platform === "all" || option.dataset.platform === platform;
        option.hidden = !visible;
        option.disabled = !visible;
      });
      if (select.selectedOptions[0]?.disabled) select.value = "all";
    }

    function filterAllAccountSelects() {
      filterAccountSelect(accountSelect, platformSelect?.value || "all");
      filterAccountSelect($("[data-finance-entry-account]"), entryForm?.elements.platform?.value || "all");
      filterAccountSelect($("[data-finance-sync-account]"), syncForm?.elements.platform?.value || "all");
      filterAccountSelect($("[data-finance-backfill-account]"), backfillForm?.elements.platform?.value || "all");
    }

    function activateTab(name, { focus = false } = {}) {
      const tab = $(`[data-finance-tab="${name}"]`);
      const panel = $(`[data-finance-panel="${name}"]`);
      if (!tab || !panel) return;
      state.activeTab = name;
      $$('[data-finance-tab]').forEach((item) => {
        const active = item === tab;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", String(active));
        item.tabIndex = active ? 0 : -1;
      });
      $$('[data-finance-panel]').forEach((item) => { item.hidden = item !== panel; });
      if (focus) tab.focus();
      if (!state.loadedTabs.has(name)) {
        state.loadedTabs.add(name);
        if (name === "entries") loadEntries();
        if (name === "mappings") loadMappings();
        if (name === "reviews") loadReviews();
        if (name === "waybill-facts") loadWaybillFacts();
        if (name === "sync") loadBatches();
      }
    }

    function handleTabKeydown(event) {
      const tabs = $$('[data-finance-tab]');
      const current = tabs.indexOf(event.currentTarget);
      if (current < 0) return;
      let next = current;
      if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      activateTab(tabs[next].dataset.financeTab, { focus: true });
    }

    function setMetricLoading() {
      $$('[data-finance-metric]').forEach((button) => {
        const value = button.querySelector("strong");
        if (!value) return;
        value.textContent = "";
        value.classList.add("finance-skeleton-line");
        value.setAttribute("aria-hidden", "true");
      });
    }

    function renderMetrics(summary) {
      const moneyKeys = new Set(["total_income", "total_expense", "net_change", "waybill_net", "operating_net", "unclassified_net"]);
      const labels = {
        waybill_net: "运单财务净额",
        operating_net: "运营级净额",
        unclassified_net: "未分类净额",
      };
      $$('[data-finance-metric]').forEach((button) => {
        const key = button.dataset.financeMetric;
        if (labels[key]) button.querySelector("span").textContent = labels[key];
        const value = button.querySelector("strong");
        if (!value) return;
        value.classList.remove("finance-skeleton-line");
        value.removeAttribute("aria-hidden");
        value.textContent = moneyKeys.has(key) ? moneyText(summary[key]) : displayText(summary[key], "无数据");
      });
    }

    function renderFreshness(summary) {
      if (!freshnessNode) return;
      const through = displayText(summary.data_through_date);
      const latest = displayText(summary.latest_success_at);
      const validation = validationStatusLabel(summary.validation_status);
      freshnessNode.textContent = `数据截止日期：${through} · 最近成功：${latest} · 校验：${validation}`;
      const classified = $("[data-finance-classified]");
      const unclassified = $("[data-finance-unclassified]");
      const missing = $("[data-finance-missing-waybill]");
      if (classified) classified.textContent = `已分类：${displayText(summary.classified_rows, "0")} 笔 / ${moneyText(summary.classified_net)}`;
      if (unclassified) unclassified.textContent = `未分类：${displayText(summary.unclassified_rows, "0")} 笔 / ${moneyText(summary.unclassified_net)}`;
      if (missing) missing.textContent = `缺失运单号：${displayText(summary.missing_waybill_rows, "0")} 笔 / ${moneyText(summary.missing_waybill_net)}`;
    }

    function renderModelHealth(payload) {
      const node = $("[data-finance-llm-health]");
      if (!node) return;
      const runtime = payload?.runtime || {};
      node.textContent = runtime.configured
        ? `智能模型：${displayText(runtime.provider)} / ${displayText(runtime.model)} / ${displayText(runtime.health)}`
        : "智能模型：未配置（确定性财务任务继续运行）";
    }

    function safePlotRatio(value) {
      const ratio = Number(value);
      if (!Number.isFinite(ratio)) return 0;
      return Math.max(0, Math.min(100, ratio));
    }

    function renderRanking(rows) {
      const list = $("[data-finance-ranking]");
      const empty = $("[data-finance-ranking-empty]");
      const table = $("[data-finance-ranking-table]");
      if (!list || !table) return;
      list.innerHTML = "";
      table.innerHTML = "";
      list.setAttribute("aria-busy", "false");
      empty.hidden = rows.length > 0;
      rows.forEach((row) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "finance-bar-button";
        button.dataset.financeDrillFee = String(row.fee_name || "");
        button.setAttribute("aria-label", `查看${displayText(row.fee_name)}的交易明细，支出${moneyText(row.expense)}`);
        button.innerHTML = `
          <span class="finance-bar-label">${escapeHtml(displayText(row.fee_name))}</span>
          <strong class="finance-bar-value">${escapeHtml(moneyText(row.expense))}</strong>
          <span class="finance-bar-track" aria-hidden="true"><span class="finance-bar-fill" style="--finance-bar:${safePlotRatio(row.expense_plot)}%"></span></span>`;
        item.appendChild(button);
        list.appendChild(item);
        table.insertAdjacentHTML("beforeend", `<tr><td>${escapeHtml(displayText(row.fee_name))}</td><td>${escapeHtml(directionLabel(row.direction))}</td><td data-money>${escapeHtml(moneyText(row.expense))}</td></tr>`);
      });
    }

    function renderAccountCosts(rows) {
      const list = $("[data-finance-account-costs]");
      const empty = $("[data-finance-account-empty]");
      const table = $("[data-finance-account-table]");
      if (!list || !table) return;
      list.innerHTML = "";
      table.innerHTML = "";
      list.setAttribute("aria-busy", "false");
      empty.hidden = rows.length > 0;
      rows.forEach((row) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "finance-bar-button";
        button.dataset.financeDrillAccount = String(row.account_id || "");
        button.dataset.financeDrillPlatform = String(row.platform || "");
        button.setAttribute("aria-label", `查看${optionLabel(row)}的交易明细，运单财务净额${moneyText(row.waybill_net)}，运营级净额${moneyText(row.operating_net)}`);
        button.innerHTML = `
          <span class="finance-bar-label">${escapeHtml(optionLabel(row))}</span>
          <span class="finance-account-values">运单净额 ${escapeHtml(moneyText(row.waybill_net))} / 运营净额 ${escapeHtml(moneyText(row.operating_net))}</span>
          <span class="finance-account-cost-pair" aria-hidden="true">
            <span class="finance-account-cost-row"><small>运单净额</small><span class="finance-bar-track"><span class="finance-bar-fill finance-bar-fill--waybill" style="--finance-bar:${safePlotRatio(row.waybill_net_plot)}%"></span></span></span>
            <span class="finance-account-cost-row"><small>运营净额</small><span class="finance-bar-track"><span class="finance-bar-fill finance-bar-fill--operating" style="--finance-bar:${safePlotRatio(row.operating_net_plot)}%"></span></span></span>
          </span>`;
        list.appendChild(button);
        table.insertAdjacentHTML("beforeend", `<tr><td>${escapeHtml(platformLabel(row.platform))}</td><td>${escapeHtml(displayText(row.login_account, row.account_id))}</td><td data-money>${escapeHtml(moneyText(row.total_expense))}</td><td data-money>${escapeHtml(moneyText(row.waybill_net))}</td><td data-money>${escapeHtml(moneyText(row.operating_net))}</td></tr>`);
      });
    }

    function renderOperatingTrend(rows) {
      const body = $("[data-finance-operating-trend-table]");
      if (!body) return;
      body.innerHTML = rows.length
        ? rows.map((row) => `<tr><td>${escapeHtml(displayText(row.date))}</td><td data-money>${escapeHtml(moneyText(row.income))}</td><td data-money>${escapeHtml(moneyText(row.expense))}</td><td data-money>${escapeHtml(moneyText(row.net_change))}</td></tr>`).join("")
        : '<tr><td colspan="4" class="finance-empty-cell">当前范围没有已确认的运营级费用。</td></tr>';
    }

    function svgNode(name, attributes = {}) {
      const node = document.createElementNS(SVG_NS, name);
      Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
      return node;
    }

    function drawTrendSeries(group, rows, key, toneClass, geometry) {
      const segments = [];
      let current = [];
      rows.forEach((row, index) => {
        if (row[`${key}_plot`] === null || row[`${key}_plot`] === undefined || row[`${key}_plot`] === "") {
          if (current.length) segments.push(current);
          current = [];
          return;
        }
        const x = geometry.left + (rows.length === 1 ? geometry.width / 2 : (index / (rows.length - 1)) * geometry.width);
        const y = geometry.top + ((100 - safePlotRatio(row[`${key}_plot`])) / 100) * geometry.height;
        current.push({ x, y, row });
      });
      if (current.length) segments.push(current);
      segments.forEach((points) => {
        const path = svgNode("path", {
          d: points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" "),
          class: `finance-chart-line finance-chart-line--${toneClass}`,
        });
        group.appendChild(path);
        points.forEach((point) => {
          const circle = svgNode("circle", {
            cx: point.x,
            cy: point.y,
            r: 6,
            class: `finance-chart-point finance-chart-point--${toneClass}`,
            tabindex: 0,
            role: "button",
            "aria-label": `${point.row.date}，${key === "income" ? "收入" : "支出"}${moneyText(point.row[key])}，按回车查看当日明细`,
          });
          const drill = () => drillToEntries({ date: String(point.row.date || "") });
          circle.addEventListener("click", drill);
          circle.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              drill();
            }
          });
          group.appendChild(circle);
        });
      });
    }

    function renderTrend(rows) {
      const chart = $("[data-finance-trend-chart]");
      const shell = $("[data-finance-trend-shell]");
      const skeleton = $("[data-finance-trend-skeleton]");
      const empty = $("[data-finance-trend-empty]");
      const grid = $("[data-finance-trend-grid]");
      const income = $("[data-finance-trend-income]");
      const expense = $("[data-finance-trend-expense]");
      const labels = $("[data-finance-trend-labels]");
      const table = $("[data-finance-trend-table]");
      if (!chart || !grid || !income || !expense || !labels || !table) return;
      [grid, income, expense, labels].forEach((node) => { node.innerHTML = ""; });
      table.innerHTML = rows.map((row) => `<tr><td>${escapeHtml(displayText(row.date))}</td><td data-money>${escapeHtml(moneyText(row.income))}</td><td data-money>${escapeHtml(moneyText(row.expense))}</td><td data-money>${escapeHtml(moneyText(row.net_change))}</td></tr>`).join("");
      skeleton.hidden = true;
      shell?.setAttribute("aria-busy", "false");
      empty.hidden = rows.length > 0;
      chart.hidden = rows.length === 0;
      if (!rows.length) return;
      const geometry = { left: 44, top: 20, width: 736, height: 218 };
      for (let index = 0; index <= 4; index += 1) {
        const y = geometry.top + (index / 4) * geometry.height;
        grid.appendChild(svgNode("line", { x1: geometry.left, y1: y, x2: geometry.left + geometry.width, y2: y, class: "finance-chart-grid" }));
      }
      drawTrendSeries(income, rows, "income", "income", geometry);
      drawTrendSeries(expense, rows, "expense", "expense", geometry);
      const stride = Math.max(1, Math.ceil(rows.length / 6));
      rows.forEach((row, index) => {
        if (index % stride !== 0 && index !== rows.length - 1) return;
        const x = geometry.left + (rows.length === 1 ? geometry.width / 2 : (index / (rows.length - 1)) * geometry.width);
        const label = svgNode("text", { x, y: 266, class: "finance-chart-label", "text-anchor": "middle" });
        label.textContent = String(row.date || "").slice(5);
        labels.appendChild(label);
      });
    }

    function renderPartialFailures(summary) {
      const warning = $("[data-finance-partial-warning]");
      if (!warning) return;
      const failures = Array.isArray(summary.failed_sources) ? summary.failed_sources : [];
      if (!failures.length) {
        warning.hidden = true;
        warning.textContent = "";
        return;
      }
      warning.hidden = false;
      warning.textContent = `部分来源同步失败（${failures.length} 项）。已成功来源的数据仍可查看，请到“同步记录”检查失败原因并重试。`;
    }

    function setOverviewLoading() {
      setMetricLoading();
      const chart = $("[data-finance-trend-chart]");
      const skeleton = $("[data-finance-trend-skeleton]");
      if (chart) chart.hidden = true;
      if (skeleton) skeleton.hidden = false;
      $("[data-finance-trend-shell]")?.setAttribute("aria-busy", "true");
      const ranking = $("[data-finance-ranking]");
      const accountCosts = $("[data-finance-account-costs]");
      [ranking, accountCosts].forEach((node) => {
        if (!node) return;
        node.innerHTML = '<span class="finance-bar-skeleton" style="height:54px;border-radius:8px"></span><span class="finance-bar-skeleton" style="height:54px;border-radius:8px"></span><span class="finance-bar-skeleton" style="height:54px;border-radius:8px"></span>';
        node.setAttribute("aria-busy", "true");
      });
    }

    async function loadOverview() {
      if (!isRootActive()) return;
      showError("");
      setOverviewLoading();
      setButtonBusy(refreshButton, true, "刷新中");
      setStatus("正在查询汇总和趋势数据。", "");
      const query = toQuery(globalFilters());
      const [summaryResult, trendResult, operatingTrendResult, llmResult] = await Promise.allSettled([
        fetchJson(`${ENDPOINTS.summary}?${query}`),
        fetchJson(`${ENDPOINTS.trend}?${query}`),
        fetchJson(`${ENDPOINTS.trend}?${toQuery({ ...globalFilters(), fee_level: "operating" })}`),
        fetchJson(ENDPOINTS.llmStatus),
      ]);
      const failures = [];
      if (summaryResult.status === "fulfilled") {
        const summary = summaryResult.value;
        renderMetrics(summary);
        renderFreshness(summary);
        renderPartialFailures(summary);
        renderRanking(Array.isArray(summary.expense_ranking) ? summary.expense_ranking : []);
        renderAccountCosts(Array.isArray(summary.accounts) ? summary.accounts : []);
        updateAccountOptions(Array.isArray(summary.accounts) ? summary.accounts : []);
      } else {
        failures.push(`汇总：${summaryResult.reason.message}`);
        renderRanking([]);
        renderAccountCosts([]);
      }
      if (trendResult.status === "fulfilled") {
        renderTrend(Array.isArray(trendResult.value.items) ? trendResult.value.items : []);
      } else {
        failures.push(`趋势：${trendResult.reason.message}`);
        renderTrend([]);
      }
      if (operatingTrendResult.status === "fulfilled") {
        renderOperatingTrend(Array.isArray(operatingTrendResult.value.items) ? operatingTrendResult.value.items : []);
      } else {
        failures.push(`运营级趋势：${operatingTrendResult.reason.message}`);
        renderOperatingTrend([]);
      }
      if (llmResult.status === "fulfilled") renderModelHealth(llmResult.value);
      else {
        const node = $("[data-finance-llm-health]");
        if (node) node.textContent = "智能模型：状态不可达";
      }
      setButtonBusy(refreshButton, false, "刷新中");
      const coreFailures = [summaryResult, trendResult].filter((item) => item.status === "rejected").length;
      if (coreFailures === 2) {
        showError(`财务总览未能加载。${failures.join("；")}`);
        setStatus("财务总览加载失败，请修复服务状态后重试。", "error");
      } else if (failures.length) {
        showError(`部分数据未能加载。${failures.join("；")}`);
        setStatus("已展示可用数据，部分区域加载失败。", "warning");
      } else {
        setStatus("财务总览已更新。", "success");
      }
      state.loadedTabs.add("overview");
    }

    function syncEntryDates() {
      if (!entryForm) return;
      entryForm.elements.start_date.value = startDateInput?.value || "";
      entryForm.elements.end_date.value = endDateInput?.value || "";
    }

    function requiredCount(value, fieldName) {
      const number = Number(value);
      if (!Number.isInteger(number) || number < 0) {
        throw new Error(`${fieldName}缺失或格式异常。`);
      }
      return number;
    }

    function drillToEntries({ date = "", feeName = "", accountId = "", platform = "", feeLevel = "" } = {}) {
      activateTab("entries");
      if (!entryForm) return;
      entryForm.elements.start_date.value = date || startDateInput?.value || "";
      entryForm.elements.end_date.value = date || endDateInput?.value || "";
      if (feeName) entryForm.elements.fee_name.value = feeName;
      if (feeLevel) entryForm.elements.fee_level.value = feeLevel;
      if (platform) entryForm.elements.platform.value = platform;
      filterAccountSelect($("[data-finance-entry-account]"), entryForm.elements.platform.value);
      if (accountId && Array.from(entryForm.elements.account_id.options).some((option) => option.value === accountId)) {
        entryForm.elements.account_id.value = accountId;
      }
      state.entryPage = 1;
      loadEntries();
    }

    function formValues(form) {
      const output = {};
      new FormData(form).forEach((value, key) => {
        const text = String(value ?? "").trim();
        if (text && text !== "all") output[key] = text;
      });
      return output;
    }

    function responsiveCell(label, content, attributes = "") {
      return `<td data-label="${escapeHtml(label)}" ${attributes}>${content}</td>`;
    }

    function renderEntries(payload) {
      const body = $("[data-finance-entry-body]");
      const count = $("[data-finance-entry-count]");
      const pageLabel = $("[data-finance-entry-page]");
      const prev = $("[data-finance-entry-prev]");
      const next = $("[data-finance-entry-next]");
      const tableState = $("[data-finance-entry-state]");
      const items = Array.isArray(payload.items) ? payload.items : [];
      state.entryTotal = requiredCount(payload.total, "交易总数");
      state.entryPage = requiredCount(payload.page, "当前页码");
      state.entryPageSize = requiredCount(payload.page_size, "每页条数");
      if (count) count.textContent = `共 ${state.entryTotal} 条，当前显示 ${items.length} 条。`;
      if (pageLabel) pageLabel.textContent = `第 ${state.entryPage} 页`;
      if (prev) prev.disabled = state.entryPage <= 1;
      if (next) next.disabled = state.entryPage * state.entryPageSize >= state.entryTotal;
      if (!body) return;
      if (!items.length) {
        body.innerHTML = '<tr><td colspan="14" class="finance-empty-cell">当前条件没有交易明细。调整日期或筛选条件后重新查询。</td></tr>';
        setPanelState(tableState, "当前条件没有交易明细。", "empty");
        return;
      }
      setPanelState(tableState);
      body.innerHTML = items.map((row) => {
        const account = `${platformLabel(row.platform)} / ${displayText(row.login_account, row.account_id)}`;
        const level = `<span class="finance-pill" data-tone="${row.mapping_status === "bound" ? "success" : "warning"}">${escapeHtml(levelLabel(row.fee_level))} · ${escapeHtml(mappingStatusLabel(row.mapping_status))}</span>`;
        return `<tr>
          ${responsiveCell("日期", escapeHtml(displayText(row.transaction_at, row.business_date)))}
          ${responsiveCell("平台/账号", escapeHtml(account))}
          ${responsiveCell("一级费用", escapeHtml(displayText(row.primary_fee_name)))}
          ${responsiveCell("二级费用", escapeHtml(displayText(row.secondary_fee_name)))}
          ${responsiveCell("方向", escapeHtml(directionLabel(row.direction)))}
          ${responsiveCell("级别", level)}
          ${responsiveCell("对应录单项目", escapeHtml(displayText(row.booking_fee_name)))}
          ${responsiveCell("运单号", escapeHtml(displayText(row.waybill_no)))}
          ${responsiveCell("收入", escapeHtml(moneyText(row.income)), "data-money")}
          ${responsiveCell("支出", escapeHtml(moneyText(row.expense)), "data-money")}
          ${responsiveCell("变动额", escapeHtml(moneyText(row.net_change)), "data-money")}
          ${responsiveCell("期前余额", escapeHtml(moneyText(row.before_balance)), "data-money")}
          ${responsiveCell("期后余额", escapeHtml(moneyText(row.after_balance)), "data-money")}
          ${responsiveCell("备注", `<span class="finance-text-wrap">${escapeHtml(displayText(row.remark))}</span>`)}
        </tr>`;
      }).join("");
    }

    async function loadEntries() {
      if (state.loadingEntries || !entryForm) return;
      state.loadingEntries = true;
      const tableState = $("[data-finance-entry-state]");
      setPanelState(tableState, "正在查询交易明细。", "loading");
      const params = { ...formValues(entryForm), page: state.entryPage, page_size: state.entryPageSize };
      try {
        const payload = await fetchJson(`${ENDPOINTS.entries}?${toQuery(params)}`);
        renderEntries(payload);
        setStatus("交易明细已更新。", "success");
      } catch (error) {
        setPanelState(tableState, `交易明细查询失败：${error.message}`, "error");
        setStatus("交易明细查询失败。", "error");
      } finally {
        state.loadingEntries = false;
      }
    }

    function renderBookingFeeLists(itemsByPlatform) {
      state.bookingFeeItems = Object.fromEntries(
        Object.entries(itemsByPlatform || {}).map(([platform, items]) => [
          platform,
          Array.isArray(items) ? items : [],
        ]),
      );
      $$('[data-finance-booking-fee-items]').forEach((list) => {
        const platform = String(list.dataset.financeBookingFeeItems || "");
        list.innerHTML = state.bookingFeeItems[platform]
          ? state.bookingFeeItems[platform]
              .map((item) => `<option value="${escapeHtml(item)}"></option>`)
              .join("")
          : "";
      });
    }

    function renderMappings(payload) {
      const body = $("[data-finance-mapping-body]");
      const count = $("[data-finance-mapping-count]");
      const tableState = $("[data-finance-mapping-state]");
      const items = Array.isArray(payload.items) ? payload.items : [];
      renderBookingFeeLists(payload.booking_fee_items || {});
      if (count) count.textContent = `共 ${displayText(payload.total)} 项。`;
      if (!body) return;
      if (!items.length) {
        body.innerHTML = '<tr><td colspan="10" class="finance-empty-cell">当前条件没有费用项目。完成账单同步后，新项目会进入待绑定清单。</td></tr>';
        setPanelState(tableState, "当前条件没有费用项目。", "empty");
        return;
      }
      setPanelState(tableState);
      body.innerHTML = items.map((row) => {
        const feeItemId = Number(row.fee_item_id);
        const direction = String(row.direction || "");
        const feeLevel = String(row.fee_level || "");
        const operating = feeLevel === "operating";
        const income = direction === "income";
        const listId = `finance-booking-${String(row.platform || "")}`;
        return `<tr data-finance-mapping-row data-fee-item-id="${feeItemId}" data-direction="${escapeHtml(direction)}" data-platform="${escapeHtml(row.platform || "")}">
          ${responsiveCell("平台", escapeHtml(platformLabel(row.platform)))}
          ${responsiveCell("原始一级费用", escapeHtml(displayText(row.primary_fee_name)))}
          ${responsiveCell("原始二级费用", escapeHtml(displayText(row.secondary_fee_name)))}
          ${responsiveCell("方向", `<span class="finance-pill" data-tone="${income ? "success" : "error"}">${escapeHtml(directionLabel(direction))}</span>`)}
          ${responsiveCell("费用级别", `<select aria-label="费用级别" data-finance-mapping-level><option value="">待确认</option><option value="waybill" ${feeLevel === "waybill" ? "selected" : ""}>运单级</option><option value="operating" ${operating ? "selected" : ""}>运营级</option></select>`)}
          ${responsiveCell("对应录单项目", `<input class="finance-mapping-name" type="text" list="${listId}" value="${escapeHtml(operating ? "" : (row.booking_fee_name || ""))}" placeholder="${operating ? "运营级无录单项目" : "选择真实录单叶子项目"}" data-finance-mapping-booking ${operating ? "disabled" : ""}>`)}
          ${responsiveCell("生效月份", `<div class="finance-row-actions"><input class="finance-mapping-month" type="month" value="${escapeHtml(row.effective_start_month || "")}" aria-label="生效月份" data-finance-mapping-start required><input class="finance-mapping-month" type="month" value="${escapeHtml(row.effective_end_month || "")}" aria-label="失效月份，可选" data-finance-mapping-end></div>`)}
          ${responsiveCell("计入成本", `<label class="finance-toggle-label"><input type="checkbox" ${row.include_in_cost ? "checked" : ""} ${income ? "disabled" : ""} data-finance-mapping-cost><span>${income ? "收入不可计成本" : "计入成本"}</span></label>`)}
          ${responsiveCell("变更原因", '<input class="finance-mapping-reason" type="text" maxlength="240" required placeholder="填写本次确认依据" data-finance-mapping-reason>')}
          ${responsiveCell("操作", `<div class="finance-row-actions"><button class="ghost-btn finance-row-action" type="button" data-finance-mapping-save><span>保存绑定</span></button><span class="visually-hidden" role="status" aria-live="polite" data-finance-mapping-row-status></span></div>`)}
        </tr>`;
      }).join("");
      Array.from(body.querySelectorAll("[data-finance-mapping-row]")).forEach((mappingRow, index) => {
        const booking = mappingRow.querySelector("[data-finance-mapping-booking]");
        const container = booking?.closest("td");
        if (!container) return;
        const subject = document.createElement("input");
        subject.type = "text";
        subject.className = "finance-mapping-name";
        subject.dataset.financeMappingSubject = "";
        subject.placeholder = "标准财务科目（必填）";
        subject.setAttribute("aria-label", "标准财务科目");
        subject.value = items[index]?.canonical_subject_name || items[index]?.secondary_fee_name || items[index]?.primary_fee_name || "";
        container.prepend(subject);
      });
    }

    async function loadMappings() {
      if (state.loadingMappings || !mappingForm) return;
      state.loadingMappings = true;
      const tableState = $("[data-finance-mapping-state]");
      setPanelState(tableState, "正在查询费用项目绑定。", "loading");
      try {
        const payload = await fetchJson(`${ENDPOINTS.mappings}?${toQuery(formValues(mappingForm))}`);
        renderMappings(payload);
        setStatus("费用项目绑定清单已更新。", "success");
      } catch (error) {
        setPanelState(tableState, `费用项目绑定查询失败：${error.message}`, "error");
        setStatus("费用项目绑定查询失败。", "error");
      } finally {
        state.loadingMappings = false;
      }
    }

    function updateMappingLevel(row) {
      const level = row.querySelector("[data-finance-mapping-level]")?.value || "";
      const booking = row.querySelector("[data-finance-mapping-booking]");
      if (!booking) return;
      const operating = level === "operating";
      if (operating) {
        booking.dataset.previousValue = booking.value;
        booking.value = "";
        booking.disabled = true;
        booking.placeholder = "运营级无录单项目";
      } else {
        booking.disabled = false;
        booking.placeholder = "选择真实录单叶子项目";
        if (!booking.value && booking.dataset.previousValue) booking.value = booking.dataset.previousValue;
      }
    }

    async function saveMapping(row, button) {
      const feeItemId = Number(row.dataset.feeItemId || 0);
      const status = row.querySelector("[data-finance-mapping-row-status]");
      const body = {
        direction: row.dataset.direction || "",
        fee_level: row.querySelector("[data-finance-mapping-level]")?.value || "",
        canonical_subject_name: row.querySelector("[data-finance-mapping-subject]")?.value.trim() || "",
        booking_fee_name: row.querySelector("[data-finance-mapping-booking]")?.value.trim() || "",
        requires_waybill: (row.querySelector("[data-finance-mapping-level]")?.value || "") === "waybill",
        effective_start_month: row.querySelector("[data-finance-mapping-start]")?.value || "",
        effective_end_month: row.querySelector("[data-finance-mapping-end]")?.value || "",
        include_in_cost: Boolean(row.querySelector("[data-finance-mapping-cost]")?.checked),
        reason: row.querySelector("[data-finance-mapping-reason]")?.value.trim() || "",
      };
      if (!body.fee_level) {
        status.textContent = "请选择费用级别。";
        status.classList.remove("visually-hidden");
        return;
      }
      if (!body.canonical_subject_name) {
        status.textContent = "请填写标准财务科目。";
        status.classList.remove("visually-hidden");
        return;
      }
      if (!body.effective_start_month || !body.reason) {
        status.textContent = "请填写生效月份和变更原因。";
        status.classList.remove("visually-hidden");
        return;
      }
      setButtonBusy(button, true, "保存中");
      status.textContent = "正在保存绑定。";
      status.classList.remove("visually-hidden");
      try {
        await fetchJson(`${ENDPOINTS.mappings}/${feeItemId}`, { method: "POST", body: JSON.stringify(body) });
        status.textContent = "绑定已保存并生成审计版本。";
        setStatus("费用项目绑定已保存。", "success");
        await loadMappings();
      } catch (error) {
        status.textContent = `保存失败：${error.message}`;
        setStatus("费用项目绑定保存失败。", "error");
      } finally {
        setButtonBusy(button, false, "保存中");
      }
    }

    function renderReviews(payload) {
      const body = $("[data-finance-review-body]");
      const count = $("[data-finance-review-count]");
      const panelState = $("[data-finance-review-state]");
      const items = Array.isArray(payload.items) ? payload.items : [];
      if (count) count.textContent = `共 ${displayText(payload.total)} 个审批项目。`;
      if (!body) return;
      if (!items.length) {
        body.innerHTML = '<tr><td colspan="7" class="finance-empty-cell">当前没有符合条件的审批项目。</td></tr>';
        setPanelState(panelState, "当前没有待处理异常。", "empty");
        return;
      }
      setPanelState(panelState);
      body.innerHTML = items.map((item) => {
        const suggestion = item.suggestion || {};
        const open = item.status === "open";
        const coverage = Number(item.transaction_count || 0) > 0
          ? `${((Number(item.waybill_present_count || 0) / Number(item.transaction_count)) * 100).toFixed(1)}%`
          : "无数据";
        const subject = suggestion.canonical_subject || item.secondary_fee_name || item.primary_fee_name || "";
        const level = suggestion.fee_level === "operating" ? "operating" : suggestion.fee_level === "waybill" ? "waybill" : "";
        const reason = suggestion.reason || "人工依据：";
        const controls = open ? `<div class="finance-review-controls">
          <select data-review-level aria-label="费用层级"><option value="">选择层级</option><option value="waybill" ${level === "waybill" ? "selected" : ""}>运单级</option><option value="operating" ${level === "operating" ? "selected" : ""}>运营级</option></select>
          <input data-review-subject value="${escapeHtml(subject)}" placeholder="标准科目" aria-label="标准科目">
          <input data-review-reason value="${escapeHtml(reason)}" placeholder="确认或驳回理由" aria-label="处理理由">
          <div class="finance-row-actions"><button class="ghost-btn finance-row-action" type="button" data-finance-review-approve>确认并回算</button><button class="ghost-btn finance-row-action" type="button" data-finance-review-reject>驳回建议</button></div>
        </div>` : `<span class="finance-pill">${escapeHtml(item.status)}</span>`;
        const suggestionText = suggestion.reason
          ? `${escapeHtml(suggestion.canonical_subject)} · ${escapeHtml(levelLabel(suggestion.fee_level))}<br><small>${escapeHtml(suggestion.reason)} · 置信度 ${escapeHtml(suggestion.confidence)}</small>`
          : item.ai_status === "failed" ? `分析失败：${escapeHtml(item.ai_error_message || "未知错误")}` : "等待 AI 分析";
        return `<tr data-finance-review-row data-review-id="${Number(item.id)}" data-fee-item-id="${Number(item.fee_item_id)}" data-direction="${escapeHtml(item.direction)}" data-first-seen="${escapeHtml(item.first_seen_date)}">
          ${responsiveCell("项目", `<strong>${escapeHtml(item.secondary_fee_name || item.primary_fee_name)}</strong><br><small>${escapeHtml(platformLabel(item.platform))}</small>`)}
          ${responsiveCell("方向", escapeHtml(directionLabel(item.direction)))}
          ${responsiveCell("日期 / 笔数", `${escapeHtml(item.first_seen_date)} 至 ${escapeHtml(item.last_seen_date)}<br>${Number(item.transaction_count || 0)} 笔`)}
          ${responsiveCell("净额", escapeHtml(moneyText(item.net_change)))}
          ${responsiveCell("运单覆盖", coverage)}
          ${responsiveCell("AI 建议", suggestionText)}
          ${responsiveCell("操作", controls)}
        </tr>`;
      }).join("");
    }

    async function loadReviews() {
      if (state.loadingReviews || !reviewForm) return;
      state.loadingReviews = true;
      const panelState = $("[data-finance-review-state]");
      setPanelState(panelState, "正在读取异常审批单…", "loading");
      try {
        const values = formValues(reviewForm);
        const payload = await fetchJson(`${ENDPOINTS.reviews}?${toQuery(values)}`);
        renderReviews(payload);
      } catch (error) {
        setPanelState(panelState, `审批单读取失败：${error.message}`, "error");
      } finally { state.loadingReviews = false; }
    }

    async function approveReview(row, button) {
      const status = row.querySelector("[data-review-reason]");
      const feeLevel = row.querySelector("[data-review-level]")?.value || "";
      const subject = row.querySelector("[data-review-subject]")?.value.trim() || "";
      const reason = status?.value.trim() || "";
      if (!feeLevel || !subject || !reason) {
        setStatus("确认前必须填写层级、标准科目和理由。", "error");
        return;
      }
      setButtonBusy(button, true, "确认中");
      try {
        await fetchJson(`${ENDPOINTS.mappings}/${Number(row.dataset.feeItemId)}`, {
          method: "POST",
          body: JSON.stringify({
            fee_level: feeLevel,
            canonical_subject_name: subject,
            booking_fee_name: "",
            requires_waybill: feeLevel === "waybill",
            effective_start_month: String(row.dataset.firstSeen || "").slice(0, 7),
            effective_end_month: "",
            include_in_cost: row.dataset.direction === "expense",
            reason,
          }),
        });
        setStatus("规则已确认，并从首次出现月份回算受影响数据。", "success");
        await Promise.all([loadReviews(), loadOverview()]);
      } catch (error) { setStatus(`确认失败：${error.message}`, "error"); }
      finally { setButtonBusy(button, false, "确认并回算"); }
    }

    async function rejectReview(row, button) {
      const reason = row.querySelector("[data-review-reason]")?.value.trim() || "";
      if (!reason) { setStatus("驳回时必须填写理由。", "error"); return; }
      setButtonBusy(button, true, "驳回中");
      try {
        await fetchJson(`${ENDPOINTS.reviews}/${Number(row.dataset.reviewId)}/reject`, { method: "POST", body: JSON.stringify({ reason }) });
        setStatus("审批单已驳回；未创建正式财务映射。", "success");
        await loadReviews();
      } catch (error) { setStatus(`驳回失败：${error.message}`, "error"); }
      finally { setButtonBusy(button, false, "驳回建议"); }
    }

    async function analyzeReviews(button) {
      setButtonBusy(button, true, "分析中");
      try {
        const result = await fetchJson(ENDPOINTS.analyzeReviews, { method: "POST", body: JSON.stringify({ limit: 20 }) });
        setStatus(result.status === "pending" ? "当前没有激活模型，审批单继续保持待分析。" : `AI 分析完成：成功 ${result.completed || 0}，失败 ${result.failed || 0}。`, result.failed ? "warning" : "success");
        await loadReviews();
      } catch (error) { setStatus(`AI 分析失败：${error.message}`, "error"); }
      finally { setButtonBusy(button, false, "分析待处理项目"); }
    }

    function renderWaybillFacts(payload) {
      const body = $("[data-finance-waybill-body]");
      const count = $("[data-finance-waybill-count]");
      const panelState = $("[data-finance-waybill-state]");
      const items = Array.isArray(payload.items) ? payload.items : [];
      if (count) count.textContent = `共 ${displayText(payload.total)} 条科目事实。`;
      if (!body) return;
      if (!items.length) {
        body.innerHTML = '<tr><td colspan="8" class="finance-empty-cell">当前范围没有已分类的运单财务事实。</td></tr>';
        setPanelState(panelState, "无数据", "empty");
        return;
      }
      setPanelState(panelState);
      body.innerHTML = items.map((item) => `<tr>
        ${responsiveCell("日期", escapeHtml(item.business_date))}
        ${responsiveCell("平台 / 账号", `${escapeHtml(platformLabel(item.platform))}<br>${escapeHtml(item.account_id)}`)}
        ${responsiveCell("运单号", `<strong>${escapeHtml(item.waybill_no)}</strong>`)}
        ${responsiveCell("标准科目", escapeHtml(item.subject_name))}
        ${responsiveCell("收入", escapeHtml(moneyText(item.income)))}
        ${responsiveCell("支出", escapeHtml(moneyText(item.expense)))}
        ${responsiveCell("净额", escapeHtml(moneyText(item.net_change)))}
        ${responsiveCell("映射版本", `v${Number(item.mapping_version || 0)}`)}
      </tr>`).join("");
    }

    async function loadWaybillFacts() {
      if (state.loadingWaybillFacts || !waybillForm) return;
      state.loadingWaybillFacts = true;
      const panelState = $("[data-finance-waybill-state]");
      setPanelState(panelState, "正在读取运单财务事实…", "loading");
      try {
        const query = { ...globalFilters(), ...formValues(waybillForm) };
        const payload = await fetchJson(`${ENDPOINTS.waybillFacts}?${toQuery(query)}`);
        renderWaybillFacts(payload);
        const knowledge = await fetchJson(ENDPOINTS.knowledge);
        const link = $("[data-finance-knowledge-link]");
        if (link && knowledge.consistent && knowledge.latest_export?.relative_path) {
          link.href = `/runtime/${encodeURI(knowledge.latest_export.relative_path)}`;
          link.hidden = false;
        }
      } catch (error) { setPanelState(panelState, `运单财务读取失败：${error.message}`, "error"); }
      finally { state.loadingWaybillFacts = false; }
    }

    function renderBatches(payload) {
      const body = $("[data-finance-sync-body]");
      const count = $("[data-finance-sync-count]");
      const tableState = $("[data-finance-sync-state]");
      const items = Array.isArray(payload.items) ? payload.items : [];
      if (count) count.textContent = `共 ${displayText(payload.total)} 个批次。`;
      if (!body) return;
      if (!items.length) {
        body.innerHTML = '<tr><td colspan="10" class="finance-empty-cell">尚无同步记录。可在上方发起手动同步或历史回溯。</td></tr>';
        setPanelState(tableState, "尚无同步记录。", "empty");
        return;
      }
      setPanelState(tableState);
      body.innerHTML = items.map((row) => {
        const [statusLabel, statusTone] = syncStatusMeta(row.status);
        const canRetry = ["failed", "partial_failed"].includes(String(row.status || ""));
        const range = `${displayText(row.requested_start_date)} 至 ${displayText(row.requested_end_date)}`;
        const runResult = `${displayText(row.success_runs)} 成功 / ${displayText(row.failed_runs)} 失败 / ${displayText(row.total_runs)} 总计`;
        const failedSources = Array.isArray(row.failed_sources) ? row.failed_sources : [];
        const errorText = failedSources.length
          ? failedSources.map((source) => `${platformLabel(source.platform)}/${displayText(source.account_id)}/${displayText(source.target_date)}：${displayText(source.error_code)} ${displayText(source.error_message)}`).join("；")
          : displayText(row.error_message);
        const dateStatus = row.earliest_date_status === "confirmed"
          ? "已确认"
          : row.earliest_date_status === "EARLIEST_DATE_UNCONFIRMED"
            ? "最早日期未确认"
            : "不适用";
        return `<tr data-finance-batch-row data-batch-id="${Number(row.id || 0)}">
          ${responsiveCell("批次", `#${escapeHtml(displayText(row.id))}`)}
          ${responsiveCell("触发方式", escapeHtml(displayText(row.trigger_type)))}
          ${responsiveCell("日期范围", escapeHtml(range))}
          ${responsiveCell("状态", `<span class="finance-pill" data-tone="${statusTone}">${escapeHtml(statusLabel)}</span>`)}
          ${responsiveCell("运行结果", escapeHtml(runResult))}
          ${responsiveCell("最早日期确认", escapeHtml(dateStatus))}
          ${responsiveCell("发起人", escapeHtml(displayText(row.requested_by)))}
          ${responsiveCell("开始/结束", `<span class="finance-text-wrap">${escapeHtml(displayText(row.started_at))}<br>${escapeHtml(displayText(row.finished_at))}</span>`)}
          ${responsiveCell("错误", `<span class="finance-text-wrap">${escapeHtml(errorText)}</span>`)}
          ${responsiveCell("操作", canRetry ? '<button class="ghost-btn finance-row-action" type="button" data-finance-retry><span>重试失败批次</span></button>' : '<span class="finance-pill">无需重试</span>')}
        </tr>`;
      }).join("");
    }

    async function loadBatches() {
      if (state.loadingBatches) return;
      state.loadingBatches = true;
      const tableState = $("[data-finance-sync-state]");
      setPanelState(tableState, "正在查询同步记录。", "loading");
      const status = $("[data-finance-sync-status]")?.value || "all";
      try {
        const payload = await fetchJson(`${ENDPOINTS.batches}?${toQuery({ status, page: 1, page_size: 100 })}`);
        renderBatches(payload);
        setStatus("同步记录已更新。", "success");
      } catch (error) {
        setPanelState(tableState, `同步记录查询失败：${error.message}`, "error");
        setStatus("同步记录查询失败。", "error");
      } finally {
        state.loadingBatches = false;
      }
    }

    async function submitSyncAction(form, endpoint, busyLabel) {
      const button = form.querySelector('button[type="submit"]');
      const body = formValues(form);
      setButtonBusy(button, true, busyLabel);
      try {
        const receipt = await fetchJson(endpoint, financeCommandOptions(body));
        setStatus(financeReceiptText(receipt, "财务同步计划"), "warning");
      } catch (error) {
        setStatus(`财务同步计划未提交：${error.message}`, "error");
      } finally {
        setButtonBusy(button, false, busyLabel);
      }
    }

    async function retryBatch(row, button) {
      const batchId = Number(row.dataset.batchId || 0);
      setButtonBusy(button, true, "重试中");
      try {
        const receipt = await fetchJson(
          `${ENDPOINTS.batches}/${batchId}/retry`,
          financeCommandOptions({}),
        );
        setStatus(financeReceiptText(receipt, `批次 #${batchId} 重试计划`), "warning");
      } catch (error) {
        setStatus(`批次重试计划未提交：${error.message}`, "error");
      } finally {
        setButtonBusy(button, false, "重试中");
      }
    }

    $$('[data-finance-tab]').forEach((tab) => {
      tab.addEventListener("click", () => activateTab(tab.dataset.financeTab));
      tab.addEventListener("keydown", handleTabKeydown);
    });
    $$('[data-finance-range]').forEach((button) => button.addEventListener("click", () => applyDatePreset(button.dataset.financeRange)));
    [startDateInput, endDateInput].forEach((input) => input?.addEventListener("change", () => setDateRange(startDateInput.value, endDateInput.value, "custom")));
    refreshButton?.addEventListener("click", loadOverview);
    platformSelect?.addEventListener("change", () => { filterAllAccountSelects(); });
    entryForm?.elements.platform?.addEventListener("change", filterAllAccountSelects);
    syncForm?.elements.platform?.addEventListener("change", filterAllAccountSelects);
    backfillForm?.elements.platform?.addEventListener("change", filterAllAccountSelects);
    entryForm?.addEventListener("submit", (event) => { event.preventDefault(); state.entryPage = 1; loadEntries(); });
    mappingForm?.addEventListener("submit", (event) => { event.preventDefault(); loadMappings(); });
    reviewForm?.addEventListener("submit", (event) => { event.preventDefault(); loadReviews(); });
    waybillForm?.addEventListener("submit", (event) => { event.preventDefault(); loadWaybillFacts(); });
    syncForm?.addEventListener("submit", (event) => { event.preventDefault(); submitSyncAction(syncForm, ENDPOINTS.sync, "提交中"); });
    backfillForm?.addEventListener("submit", (event) => { event.preventDefault(); submitSyncAction(backfillForm, ENDPOINTS.backfill, "创建中"); });
    $("[data-finance-sync-status]")?.addEventListener("change", loadBatches);
    $("[data-finance-entry-prev]")?.addEventListener("click", () => { if (state.entryPage > 1) { state.entryPage -= 1; loadEntries(); } });
    $("[data-finance-entry-next]")?.addEventListener("click", () => { if (state.entryPage * state.entryPageSize < state.entryTotal) { state.entryPage += 1; loadEntries(); } });

    root.addEventListener("click", (event) => {
      const metric = event.target.closest("[data-finance-drill-level]");
      if (metric) {
        const level = metric.dataset.financeDrillLevel;
        if (level === "pending") {
          activateTab("mappings");
          if (mappingForm) mappingForm.elements.status.value = "pending";
          loadMappings();
        } else {
          drillToEntries({ feeLevel: level === "all" ? "" : level });
        }
        return;
      }
      const fee = event.target.closest("[data-finance-drill-fee]");
      if (fee) {
        drillToEntries({ feeName: fee.dataset.financeDrillFee || "" });
        return;
      }
      const account = event.target.closest("[data-finance-drill-account]");
      if (account) {
        drillToEntries({ accountId: account.dataset.financeDrillAccount || "", platform: account.dataset.financeDrillPlatform || "" });
        return;
      }
      const mappingSave = event.target.closest("[data-finance-mapping-save]");
      if (mappingSave) {
        const row = mappingSave.closest("[data-finance-mapping-row]");
        if (row) saveMapping(row, mappingSave);
        return;
      }
      const retry = event.target.closest("[data-finance-retry]");
      if (retry) {
        const row = retry.closest("[data-finance-batch-row]");
        if (row) retryBatch(row, retry);
        return;
      }
      const approve = event.target.closest("[data-finance-review-approve]");
      if (approve) {
        const row = approve.closest("[data-finance-review-row]");
        if (row) approveReview(row, approve);
        return;
      }
      const reject = event.target.closest("[data-finance-review-reject]");
      if (reject) {
        const row = reject.closest("[data-finance-review-row]");
        if (row) rejectReview(row, reject);
        return;
      }
      const analyze = event.target.closest("[data-finance-analyze-reviews]");
      if (analyze) analyzeReviews(analyze);
    });

    root.addEventListener("change", (event) => {
      const level = event.target.closest("[data-finance-mapping-level]");
      if (level) {
        const row = level.closest("[data-finance-mapping-row]");
        if (row) updateMappingLevel(row);
      }
    });

    $$('table.finance-entry-table, table.finance-mapping-table, table.finance-review-table, table.finance-waybill-table, table.finance-sync-table').forEach((table) => table.classList.add("finance-table--responsive"));
    setOverviewLoading();
    syncEntryDates();
    activateTab("overview");
    loadOverview();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initFinanceWorkbenches, { once: true });
  } else {
    initFinanceWorkbenches();
  }
  document.addEventListener("console:page-ready", initFinanceWorkbenches);
})();
