// 融辉/韵达对账单：点“查询”经 Console → Agent 直连平台，表格列与数值按平台原样显示。
(() => {
  "use strict";
  const main = document.querySelector(".main-content:not([hidden])") || document;
  const root = main.querySelector("[data-platform-bills]");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  const fetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
  const platform = root.dataset.platform;
  const maxSpan = Number(root.dataset.maxSpan || 31);
  const filterKeys = new Set(JSON.parse(root.dataset.filterKeys || "[]"));
  const $ = selector => root.querySelector(selector);
  const $$ = selector => [...root.querySelectorAll(selector)];
  const els = {
    status: $("[data-bill-status]"), meta: $("[data-bill-meta]"), footer: $("[data-bill-footer]"),
    head: $("[data-bill-head]"), rows: $("[data-bill-rows]"), trail: $("[data-bill-trail]"),
    pager: $("[data-bill-pager]"), pageInfo: $("[data-bill-page-info]"), prev: $("[data-bill-prev]"), next: $("[data-bill-next]"),
    start: $("[data-bill-start]"), end: $("[data-bill-end]"), dateType: $("[data-bill-date-type]"),
    pageSize: $("[data-bill-page-size]"), query: $("[data-bill-query]"), exportBtn: $("[data-bill-export]"),
  };
  const VIEW_TITLES = platform === "ronghui"
    ? {summary: "统计查询", summary_detail: "明细数据", detail: "明细查询", balance: "预付款余额"}
    : {summary: "交易汇总查询", daily: "交易汇总-按天", detail: "交易明细", branches: "分部预付款"};
  const YUNDA_TABS = {ac_trade_sum_query: "summary", ac_trade_sum_day: "daily", ac_trade_detail_es: "detail"};
  const DATED = platform === "ronghui" ? new Set(["summary", "summary_detail", "detail"]) : new Set(["summary", "daily", "detail"]);
  const PAGED = platform === "ronghui" ? new Set(["summary_detail", "detail"]) : new Set(["summary", "daily", "detail", "branches"]);
  // 分部余额本身不按日期查，但点账号下钻要用这里的日期，所以照常显示。
  const SHOW_DATES = platform === "ronghui" ? DATED : new Set([...DATED, "branches"]);

  const pad = value => String(value).padStart(2, "0");
  const ymd = date => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const now = new Date();
  els.start.value = ymd(new Date(now.getFullYear(), now.getMonth(), 1));
  els.end.value = ymd(now);

  let account = ($("[data-bill-account][aria-selected=true]") || $("[data-bill-account]"))?.dataset.billAccount || "";
  let current = null;   // {action, params, page, label}
  let trail = [];       // previous query states for “返回”
  let result = null;
  let busy = false;

  function say(text) { els.status.textContent = text || ""; }
  function selectTab(buttons, active) {
    buttons.forEach(button => {
      const on = button === active;
      button.setAttribute("aria-selected", String(on));
      button.tabIndex = on ? 0 : -1;
    });
  }
  function clearTable(text) {
    result = null;
    els.head.replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.className = "finance-empty";
    cell.textContent = text;
    row.append(cell);
    els.rows.replaceChildren(row);
    els.meta.textContent = "";
    els.footer.hidden = true;
    els.pager.hidden = true;
    els.exportBtn.disabled = true;
  }
  function syncControls(action) {
    $$("[data-bill-dates]").forEach(item => { item.hidden = !SHOW_DATES.has(action); });
    $$("[data-bill-paging]").forEach(item => { item.hidden = !PAGED.has(action); });
  }
  function checkedDates() {
    const start = els.start.value, end = els.end.value;
    if (!start || !end) throw new Error("请选择开始和结束日期。");
    const days = Math.round((new Date(`${end}T00:00:00`) - new Date(`${start}T00:00:00`)) / 86400000) + 1;
    if (days < 1) throw new Error("结束日期不能早于开始日期。");
    if (days > maxSpan) throw new Error(`平台一次最多查询 ${maxSpan} 天，请缩短日期范围。`);
    return {start, end};
  }

  function baseParams(action, extra = {}) {
    const params = {account_id: account, action, ...extra};
    if (DATED.has(action)) {
      Object.assign(params, checkedDates());
      if (platform === "ronghui") params.date_type = els.dateType.value;
    }
    if (PAGED.has(action)) params.page_size = els.pageSize.value;
    return params;
  }

  async function run(state) {
    if (busy) return;
    busy = true;
    els.query.disabled = true;
    current = state;
    renderTrail();
    syncControls(state.action);
    say(`正在从${platform === "ronghui" ? "融辉" : "韵达"}读取最新数据…`);
    clearTable("正在读取…");
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(state.params)) {
      if (key === "filters") query.set("filters", JSON.stringify(value));
      else if (value !== undefined && value !== "") query.set(key, String(value));
    }
    if (PAGED.has(state.action)) query.set("page", String(state.page || 1));
    try {
      const response = await fetch(`/finance/platform-bills/${platform}?${query}`, {
        credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}});
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.message || "读取失败，请稍后重试。");
      result = payload.data;
      render();
      say("");
    } catch (error) {
      clearTable("未读取到数据。");
      say(error.message || "读取失败，请稍后重试。");
    } finally {
      busy = false;
      els.query.disabled = false;
    }
  }

  const NUMERIC = /^-?\d{1,9}(\.\d+)?$/;
  function text(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function render() {
    const columns = result.columns || [];
    const head = document.createElement("tr");
    for (const column of columns) {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = column.title;
      head.append(th);
    }
    els.head.replaceChildren(head);
    const body = [];
    for (const row of result.rows || []) {
      const tr = document.createElement("tr");
      const rowDrill = platform === "ronghui" && current.action === "summary";
      for (const column of columns) {
        const td = document.createElement("td");
        const value = text(row[column.field]);
        if (NUMERIC.test(value)) td.className = "num";
        const drill = platform === "yunda" ? column.drill : null;
        if ((drill && value) || (rowDrill && column.field === "BALANCE_TYPE")) {
          const link = document.createElement("button");
          link.type = "button";
          link.className = "bill-link";
          link.textContent = value;
          link.addEventListener("click", () => (drill ? yundaDrill(row, drill, value) : ronghuiDrill(row)));
          td.append(link);
        } else {
          td.textContent = value;
        }
        tr.append(td);
      }
      body.push(tr);
    }
    if (!body.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "finance-empty";
      td.colSpan = Math.max(columns.length, 1);
      td.textContent = "平台没有返回数据。";
      tr.append(td);
      body.push(tr);
    }
    els.rows.replaceChildren(...body);
    const where = platform === "ronghui" ? `${result.site_name || ""}（${result.site_code || ""}）` : `账号 ${result.user_id || ""}`;
    els.meta.textContent = `数据来自${platform === "ronghui" ? "融辉" : "韵达"}平台 · ${where} · 查询时间 ${result.fetched_at} · 共 ${result.total} 条`;
    renderFooter();
    const pages = Math.max(1, Math.ceil((result.total || 0) / (result.page_size || 1)));
    els.pager.hidden = !PAGED.has(current.action) || pages <= 1;
    els.pageInfo.textContent = `第 ${result.page} / ${pages} 页`;
    els.prev.disabled = result.page <= 1;
    els.next.disabled = result.page >= pages;
    els.exportBtn.disabled = !(result.rows || []).length;
  }

  function renderFooter() {
    const footer = result.footer;
    let line = "";
    if (Array.isArray(footer) && footer.length) {
      for (let i = 0; i < footer.length; i += 2) line += `${text(footer[i])} ${text(footer[i + 1])}　`;
    } else if (footer && typeof footer === "object" && Object.keys(footer).length) {
      const titles = Object.fromEntries((result.columns || []).map(column => [column.field, column.title]));
      line = Object.entries(footer).map(([key, value]) => `${titles[key] || key}：${text(value)}`).join("　");
    }
    els.footer.textContent = line ? `平台合计：${line.trim()}` : "";
    els.footer.hidden = !line;
  }

  function renderTrail() {
    els.trail.replaceChildren();
    els.trail.hidden = !trail.length;
    if (!trail.length) return;
    const back = document.createElement("button");
    back.type = "button";
    back.className = "ghost-btn";
    back.textContent = "返回上一级";
    back.addEventListener("click", () => {
      const previous = trail.pop();
      if (previous.params.start) { els.start.value = previous.params.start; els.end.value = previous.params.end; }
      selectView(previous.action);
      run(previous);
    });
    const path = document.createElement("span");
    path.textContent = [...trail, current].map(item => item.label).join(" › ");
    els.trail.append(back, path);
  }

  function selectView(action) {
    const button = $(`[data-bill-view="${action}"]`);
    if (button) selectTab($$("[data-bill-view]"), button);
  }

  function ronghuiDrill(row) {
    const state = {action: "summary_detail", page: 1, label: `${VIEW_TITLES.summary_detail}：${text(row.BALANCE_TYPE)}`,
      params: {...current.params, action: "summary_detail", page_size: els.pageSize.value,
        center_code: text(row.CENTER_NAME_CODE), site_code: text(row.SITE_NAME_CODE), balance_type: text(row.BALANCE_TYPE)}};
    trail.push(current);
    run(state);
  }

  // 平台规则：跳转参数名取列的 skipData，值优先取所点行，其次沿用当前查询条件。
  function yundaDrill(row, drill, value) {
    const action = YUNDA_TABS[drill.tab];
    if (!action) { say(`平台跳转到未核实的页面（${drill.tab}），已停止。`); return; }
    const fromBranches = current.action === "branches";
    const filters = {...(current.params.filters || {})};
    let {start, end} = current.params;
    if (fromBranches) {
      try { ({start, end} = checkedDates()); } catch (error) { say(error.message); return; }
      filters.workType = "branch";
    }
    for (const key of drill.keys) {
      if (key === "initSearch") continue;
      const has = Object.prototype.hasOwnProperty.call(row, key) && row[key] !== null && row[key] !== "";
      if (key === "start_time" || key === "end_time") {
        if (has && !fromBranches && /^\d{4}-\d{2}-\d{2}$/.test(text(row[key]))) {
          if (key === "start_time") start = text(row[key]); else end = text(row[key]);
        }
        continue;
      }
      if (!has) continue;
      if (!filterKeys.has(key)) { say(`平台下钻参数“${key}”尚未核实，已停止，请联系维护。`); return; }
      filters[key] = text(row[key]);
    }
    const name = text(row.branch_name || row.parent_project_name || row.trade_date || value);
    const state = {action, page: 1, label: `${VIEW_TITLES[action]}：${name}`,
      params: {account_id: account, action, start, end, page_size: els.pageSize.value, filters}};
    trail.push(current);
    selectView(action);
    run(state);
  }

  function freshQuery(action) {
    trail = [];
    try {
      const filters = platform === "yunda" && action !== "branches" ? {} : undefined;
      const params = baseParams(action, filters ? {filters} : {});
      run({action, page: 1, label: VIEW_TITLES[action], params});
    } catch (error) {
      say(error.message);
    }
  }

  function activeView() { return $("[data-bill-view][aria-selected=true]").dataset.billView; }

  $$("[data-bill-view]").forEach(button => button.addEventListener("click", () => {
    selectTab($$("[data-bill-view]"), button);
    trail = [];
    current = null;
    renderTrail();
    syncControls(button.dataset.billView);
    clearTable(`点“查询”读取${VIEW_TITLES[button.dataset.billView]}。`);
    say("");
  }));
  $$("button[data-bill-account]").forEach(button => button.addEventListener("click", () => {
    selectTab($$("button[data-bill-account]"), button);
    account = button.dataset.billAccount;
    trail = [];
    current = null;
    renderTrail();
    clearTable(`点“查询”读取${button.textContent.trim()}的最新账单。`);
    say("");
  }));
  // 已下钻时“查询”按新日期/条数重查当前这一级，保留下钻条件；否则从当前页签重新开始。
  els.query.addEventListener("click", () => {
    if (!current || !trail.length) { freshQuery(activeView()); return; }
    try {
      const params = {...current.params, page_size: els.pageSize.value};
      if (DATED.has(current.action)) {
        Object.assign(params, checkedDates());
        if (platform === "ronghui") params.date_type = els.dateType.value;
      }
      run({...current, page: 1, params});
    } catch (error) {
      say(error.message);
    }
  });
  els.prev.addEventListener("click", () => current && run({...current, page: Math.max(1, (result?.page || 1) - 1)}));
  els.next.addEventListener("click", () => current && run({...current, page: (result?.page || 1) + 1}));
  els.exportBtn.addEventListener("click", () => {
    if (!result) return;
    const quote = value => `"${text(value).replace(/"/g, '""')}"`;
    const columns = result.columns || [];
    const lines = [columns.map(column => quote(column.title)).join(",")];
    for (const row of result.rows || []) lines.push(columns.map(column => quote(row[column.field])).join(","));
    const blob = new Blob(["﻿" + lines.join("\r\n")], {type: "text/csv;charset=utf-8"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${platform === "ronghui" ? "融辉" : "韵达"}${current.label.replace(/[\\/:*?"<>|]/g, "_")}-${result.fetched_at.replace(/[: ]/g, "")}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });
  syncControls(activeView());
})();
