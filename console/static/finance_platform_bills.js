// 融辉/韵达对账单 (frontend-spec ㉓a/㉓b/㉓c, rules 16). “查询”经 Console → Agent 直连平台，不存库。
// The page reshapes what the platform returned for checking: columns whose value is the same on every row
// move to the account bar, 一级/二级 columns share one two-line cell, amounts are right-aligned with one
// formatter, a single record is shown as KPIs + a field grid, and totals come from the platform footer or are
// summed from the rows shown (never from guesses). Values themselves are never changed.
(() => {
  "use strict";
  const main = document.querySelector(".main-content:not([hidden])") || document;
  const root = main.querySelector("[data-platform-bills]");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  const fetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
  const platform = root.dataset.platform;
  const NAME = platform === "ronghui" ? "融辉" : "韵达";
  const maxSpan = Number(root.dataset.maxSpan || 31);
  const filterKeys = new Set(JSON.parse(root.dataset.filterKeys || "[]"));
  const $ = (selector) => root.querySelector(selector);
  const $$ = (selector) => [...root.querySelectorAll(selector)];
  const els = {
    form: $("[data-bill-filters]"), account: $("[data-bill-account]"), start: $("[data-bill-start]"), end: $("[data-bill-end]"),
    dateType: $("[data-bill-date-type]"), query: $("[data-bill-query]"), dateError: $("[data-bill-date-error]"),
    exportBtn: $("[data-bill-export]"), trail: $("[data-bill-trail]"), context: $("[data-bill-context]"), strip: $("[data-bill-strip]"),
    body: $("[data-bill-body]"), pager: $("[data-bill-pager]"), pageSize: $("[data-bill-page-size]"), total: $("[data-bill-total]"),
    pages: $("[data-bill-pages]"), status: $("[data-bill-status]"),
  };
  const VIEW_TITLES = platform === "ronghui"
    ? { summary: "统计查询", summary_detail: "明细数据", detail: "明细查询", balance: "预付款余额" }
    : { summary: "交易汇总", daily: "交易汇总·按天", detail: "交易明细", branches: "分部预付款" };
  const YUNDA_TABS = { ac_trade_sum_query: "summary", ac_trade_sum_day: "daily", ac_trade_detail_es: "detail" };
  const DATED = platform === "ronghui" ? new Set(["summary", "summary_detail", "detail"]) : new Set(["summary", "daily", "detail"]);
  const PAGED = platform === "ronghui" ? new Set(["summary_detail", "detail"]) : new Set(["summary", "daily", "detail", "branches"]);
  // 分部余额本身不按日期查，但点账号下钻要用这里的日期，所以照常显示。
  const SHOW_DATES = platform === "ronghui" ? DATED : new Set([...DATED, "branches"]);

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const text = (value) => (value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value));
  const pad = (value) => String(value).padStart(2, "0");
  const ymd = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;

  // -- amounts: exact decimal strings, one formatter (thousands + at least two decimals) --------------
  const NUMERIC = /^-?\d{1,15}(\.\d{1,8})?$/;
  const isNumber = (value) => NUMERIC.test(text(value).trim());
  function scaleOf(values) { return values.reduce((max, value) => Math.max(max, (text(value).split(".")[1] || "").length), 2); }
  function toUnits(value, scale) {
    const raw = text(value).trim();
    const negative = raw.startsWith("-");
    const [whole, frac = ""] = raw.replace("-", "").split(".");
    const units = BigInt(whole || "0") * 10n ** BigInt(scale) + BigInt((frac + "0".repeat(scale)).slice(0, scale) || "0");
    return negative ? -units : units;
  }
  function fromUnits(units, scale) {
    const negative = units < 0n;
    const digits = (negative ? -units : units).toString().padStart(scale + 1, "0");
    return `${negative ? "-" : ""}${digits.slice(0, -scale)}.${digits.slice(-scale)}`;
  }
  function sum(values) {
    const list = values.filter(isNumber);
    const scale = scaleOf(list);
    return fromUnits(list.reduce((total, value) => total + toUnits(value, scale), 0n), scale);
  }
  function money(value, { sign = false } = {}) {
    const raw = text(value).trim();
    if (!isNumber(raw)) return raw;
    const negative = raw.startsWith("-");
    const [whole, frac = ""] = raw.replace("-", "").split(".");
    const grouped = whole.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    const body = `${grouped}.${frac.padEnd(2, "0")}`;
    if (negative && /[1-9]/.test(raw)) return `-${body}`;
    return sign && /[1-9]/.test(raw) ? `+${body}` : body;
  }
  const negative = (value) => text(value).trim().startsWith("-") && /[1-9]/.test(text(value));

  // -- state ---------------------------------------------------------------------------------------
  const now = new Date();
  els.start.value = ymd(new Date(now.getFullYear(), now.getMonth(), 1));
  els.end.value = ymd(now);
  els.end.max = ymd(now);
  let current = null; // {action, params, page, label}
  let trail = [];
  let result = null;
  let groupPage = 1;
  let busy = false;
  let lastColumns = [];
  let exportRows = null; // {header: [...], rows: [[...]]}

  const account = () => els.account.value;
  const activeView = () => $("[data-bill-view][aria-selected=true]").dataset.billView;
  function say(message) { els.status.textContent = message || ""; }

  function selectView(action) {
    const button = $(`[data-bill-view="${action}"]`);
    if (!button) return;
    $$("[data-bill-view]").forEach((item) => {
      const on = item === button;
      item.setAttribute("aria-selected", String(on));
      item.tabIndex = on ? 0 : -1;
    });
  }

  // -- dates: the platform limit is enforced in the control, not after the query (rules 16) ----------
  function dateProblem() {
    const start = els.start.value; const end = els.end.value;
    if (!start || !end) return "请选择开始和结束日期。";
    const days = Math.round((new Date(`${end}T00:00:00`) - new Date(`${start}T00:00:00`)) / 86400000) + 1;
    if (days < 1) return "结束日期不能早于开始日期。";
    if (days > maxSpan) return `平台一次最多查询 ${maxSpan} 天，当前选了 ${days} 天，请缩短区间。`;
    return "";
  }
  function syncDates() {
    const show = SHOW_DATES.has(current?.action || activeView());
    $$("[data-bill-dates]").forEach((item) => { item.hidden = !show; });
    const problem = show ? dateProblem() : "";
    els.dateError.hidden = !problem;
    els.dateError.textContent = problem;
    els.query.disabled = Boolean(problem) || busy;
    if (els.start.value) {
      const limit = new Date(`${els.start.value}T00:00:00`);
      limit.setDate(limit.getDate() + maxSpan - 1);
      els.end.max = ymd(limit < now ? limit : now);
    }
  }
  function checkedDates() {
    const problem = dateProblem();
    if (problem) throw new Error(problem);
    return { start: els.start.value, end: els.end.value };
  }

  // -- loading / empty / failure states (spec 50, rules 16) ------------------------------------------
  function hideExtras() {
    els.context.hidden = true; els.strip.hidden = true; els.pager.hidden = true; els.exportBtn.disabled = true; exportRows = null;
  }
  function skeleton() {
    hideExtras();
    const count = Math.max(lastColumns.length, 6);
    const head = lastColumns.length ? lastColumns.map((title) => `<th scope="col">${escapeHtml(title)}</th>`).join("") : Array.from({ length: count }, () => '<th scope="col"><span class="pb-skel pb-skel--head"></span></th>').join("");
    const row = `<tr>${Array.from({ length: count }, () => '<td><span class="pb-skel"></span></td>').join("")}</tr>`;
    els.body.innerHTML = `<div class="pb-table-wrap"><table class="pb-table" aria-busy="true"><thead><tr>${head}</tr></thead><tbody>${row.repeat(6)}</tbody></table></div>
      <p class="pb-state">正在从${NAME}读取最新数据…</p>`;
  }
  function stateBlock(kind, title, detail = "", action = "") {
    hideExtras();
    els.body.innerHTML = `<div class="pb-empty" data-kind="${kind}"><b>${escapeHtml(title)}</b>${detail ? `<span>${detail}</span>` : ""}${action}</div>`;
  }

  // -- query -----------------------------------------------------------------------------------------
  async function run(state) {
    if (busy) return;
    busy = true;
    current = state;
    renderTrail();
    syncDates();
    say(`正在从${NAME}读取最新数据`);
    skeleton();
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(state.params)) {
      if (key === "filters") query.set("filters", JSON.stringify(value));
      else if (value !== undefined && value !== "") query.set(key, String(value));
    }
    if (PAGED.has(state.action)) query.set("page", String(state.page || 1));
    try {
      const response = await fetch(`/finance/platform-bills/${platform}?${query}`, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 403) {
        stateBlock("denied", "没有查看对账单的权限", "需要身份“后台页面”中的“财务”为可见；请联系超级管理员。");
        return;
      }
      if (!response.ok || !payload.ok) throw new Error(payload.message || "读取失败，请稍后重试。");
      result = payload.data;
      groupPage = 1;
      render();
      say(`已读取${VIEW_TITLES[state.action] || ""}`);
    } catch (error) {
      result = null;
      stateBlock("error", `没有读到${NAME}的数据`, escapeHtml(error.message || "读取失败，请稍后重试。"), '<button type="button" class="ghost-btn" data-bill-retry>重试</button>');
      say(error.message || "读取失败");
    } finally {
      busy = false;
      syncDates();
    }
  }

  function baseParams(action, extra = {}) {
    const params = { account_id: account(), action, ...extra };
    if (DATED.has(action)) {
      Object.assign(params, checkedDates());
      if (platform === "ronghui") params.date_type = els.dateType.value;
    }
    if (PAGED.has(action)) params.page_size = els.pageSize.value;
    return params;
  }

  function freshQuery(action) {
    trail = [];
    try {
      const filters = platform === "yunda" && action !== "branches" ? {} : undefined;
      run({ action, page: 1, label: VIEW_TITLES[action], params: baseParams(action, filters ? { filters } : {}) });
    } catch (error) {
      syncDates();
      say(error.message);
    }
  }

  // -- shared pieces -----------------------------------------------------------------------------------
  function renderContext(items) {
    const list = items.filter(([, value]) => text(value).trim());
    els.context.hidden = !list.length;
    els.context.innerHTML = list.map(([label, value]) => `<span><em>${escapeHtml(label)}</em>${escapeHtml(value)}</span>`).join("");
  }

  function renderStrip(cells) {
    els.strip.hidden = !cells.length;
    els.strip.innerHTML = cells.map(({ label, value, tone = "", note = "" }) => `<div class="pb-strip__cell"><span>${escapeHtml(label)}</span>
      <b data-tone="${tone}">${escapeHtml(value)}</b>${note ? `<small>${escapeHtml(note)}</small>` : ""}</div>`).join("");
  }

  function renderPager(total, page, size, onPage) {
    const pages = Math.max(1, Math.ceil(total / Math.max(size, 1)));
    els.pager.hidden = false;
    els.total.textContent = `共 ${total.toLocaleString("zh-CN")} 条`;
    if (pages <= 1) { els.pages.innerHTML = ""; return; }
    const numbers = new Set([1, pages, page - 1, page, page + 1].filter((item) => item >= 1 && item <= pages));
    const ordered = [...numbers].sort((a, b) => a - b);
    let html = `<button type="button" data-bill-page="${page - 1}" ${page <= 1 ? "disabled" : ""} aria-label="上一页">‹</button>`;
    ordered.forEach((item, index) => {
      if (index && item - ordered[index - 1] > 1) html += '<span class="pb-pager__gap">…</span>';
      html += `<button type="button" data-bill-page="${item}" ${item === page ? 'aria-current="page"' : ""}>${item}</button>`;
    });
    html += `<button type="button" data-bill-page="${page + 1}" ${page >= pages ? "disabled" : ""} aria-label="下一页">›</button>`;
    els.pages.innerHTML = html;
    els.pages.onclick = (event) => {
      const button = event.target.closest("[data-bill-page]");
      if (button && !button.disabled && !button.hasAttribute("aria-current")) onPage(Number(button.dataset.billPage));
    };
  }

  function sourceContext() {
    const where = platform === "ronghui" ? ["网点", `${result.site_name || ""}${result.site_code ? `（${result.site_code}）` : ""}`] : ["账号", result.user_id || ""];
    return [where, ["查询时间", result.fetched_at || ""]];
  }

  // -- generic platform table (detail, drill-downs, Yunda views) --------------------------------------
  function columnKinds(columns, rows) {
    return columns.map((column) => {
      const values = rows.map((row) => text(row[column.field]).trim()).filter(Boolean);
      const numeric = values.length > 0 && values.every(isNumber);
      const dated = values.length > 0 && values.every((value) => /^\d{4}-\d{2}-\d{2}/.test(value));
      const constant = rows.length > 1 && !numeric && !column.drill && values.length === rows.length && new Set(values).size === 1;
      return { ...column, numeric, dated, constant };
    });
  }

  function pairColumns(kinds) {
    const used = new Set();
    const out = [];
    kinds.forEach((column, index) => {
      if (used.has(index) || column.constant) return;
      if (column.title.includes("一级") && !column.numeric) {
        const partner = kinds.findIndex((other, otherIndex) => otherIndex !== index && !other.constant && other.title === column.title.replace("一级", "二级"));
        if (partner >= 0) {
          used.add(partner);
          out.push({ pair: true, title: column.title.replace("一级", "").replace(/^\/|\/$/g, "") || "项目", first: column, second: kinds[partner] });
          return;
        }
      }
      out.push(column);
    });
    return out;
  }

  function toneFor(column, value) {
    const title = column.title;
    if (/收入/.test(title)) return "income";
    if (/支出/.test(title)) return "expense";
    if (/期初|前余额/.test(title)) return "muted";
    return negative(value) ? "expense" : "";
  }

  function cellHtml(column, row) {
    if (column.pair) {
      const first = text(row[column.first.field]).trim();
      const second = text(row[column.second.field]).trim();
      return `<td class="pb-cell--pair"><span>${escapeHtml(first || "—")}</span>${second ? `<small>${escapeHtml(second)}</small>` : ""}</td>`;
    }
    const value = text(row[column.field]).trim();
    if (!value) return `<td class="${column.numeric ? "num" : ""}"><span class="pb-null">—</span></td>`;
    if (column.drill) return `<td><button type="button" class="pb-link" data-bill-drill="${escapeHtml(column.field)}">${escapeHtml(value)} ›</button></td>`;
    if (column.numeric) return `<td class="num" data-tone="${toneFor(column, value)}">${escapeHtml(money(value))}</td>`;
    return `<td class="${column.dated ? "pb-cell--time" : ""}">${escapeHtml(value)}</td>`;
  }

  function renderGeneric() {
    const columns = result.columns || [];
    const rows = result.rows || [];
    const kinds = columnKinds(columns, rows);
    const constants = kinds.filter((column) => column.constant);
    const shown = pairColumns(kinds);
    lastColumns = shown.map((column) => column.title);
    renderContext([...sourceContext(), ...constants.map((column) => [column.title, text(rows[0][column.field])])]);
    renderFooterStrip(columns);
    if (!rows.length) {
      els.body.innerHTML = `<div class="pb-empty"><b>该区间没有记录</b><span>${DATED.has(current.action) ? `可以放宽日期区间（最多 ${maxSpan} 天）后再查。` : "平台没有返回数据。"}</span></div>`;
    } else {
      const drills = kinds.filter((column) => column.drill);
      const head = shown.map((column) => `<th scope="col" class="${column.numeric ? "num" : ""}">${escapeHtml(column.title)}</th>`).join("");
      const body = rows.map((row, index) => {
        const single = drills.length === 1 && text(row[drills[0].field]).trim();
        return `<tr data-row="${index}" ${single ? 'class="pb-row--link"' : ""}>${shown.map((column) => cellHtml(column, row)).join("")}</tr>`;
      }).join("");
      els.body.innerHTML = `<div class="pb-table-wrap"><table class="pb-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
      els.body.querySelector("tbody").onclick = (event) => {
        const tr = event.target.closest("tr[data-row]");
        if (!tr) return;
        const row = rows[Number(tr.dataset.row)];
        const button = event.target.closest("[data-bill-drill]");
        const column = button ? drills.find((item) => item.field === button.dataset.billDrill) : drills.length === 1 && tr.classList.contains("pb-row--link") ? drills[0] : null;
        if (!column) return;
        if (platform === "yunda") yundaDrill(row, column.drill, text(row[column.field]));
      };
    }
    const total = Number(result.total || 0);
    if (PAGED.has(current.action)) renderPager(total, Number(result.page || 1), Number(result.page_size || els.pageSize.value), (page) => run({ ...current, page }));
    else { els.pager.hidden = false; els.total.textContent = `共 ${total} 条`; els.pages.innerHTML = ""; }
    exportRows = { header: columns.map((column) => column.title), rows: rows.map((row) => columns.map((column) => text(row[column.field]))) };
    els.exportBtn.disabled = !rows.length;
  }

  function renderFooterStrip(columns) {
    const footer = result.footer;
    const cells = [];
    if (Array.isArray(footer) && footer.length) {
      for (let i = 0; i + 1 < footer.length; i += 2) {
        const label = text(footer[i]).replace(/[：:]\s*$/, "");
        const value = text(footer[i + 1]);
        const tone = /收入/.test(label) ? "income" : /支出/.test(label) ? "expense" : negative(value) ? "expense" : "";
        cells.push({ label, value: isNumber(value) ? money(value) : value, tone, note: /收支差/.test(label) ? "收支差 = 收入 − 支出" : "" });
      }
    } else if (footer && typeof footer === "object" && Object.keys(footer).length) {
      const titles = Object.fromEntries(columns.map((column) => [column.field, column.title]));
      Object.entries(footer).forEach(([key, value]) => cells.push({ label: `${titles[key] || key}合计`, value: isNumber(value) ? money(value) : text(value), tone: negative(value) ? "expense" : "" }));
    }
    if (cells.length) cells.push({ label: current.action === "detail" ? "明细条数" : "条数", value: `${Number(result.total || 0).toLocaleString("zh-CN")} 条` });
    if (cells.length && platform === "yunda" && current.action !== "detail") {
      cells[cells.length - 1].note = "汇总与明细的统计口径以平台为准，合计可能不同";
    }
    renderStrip(cells);
  }

  // -- 融辉 统计查询: one row per 结算类型 with 笔数 / 合计 / 占比 / 最近结算日 (spec ㉓a) ------------------
  function ronghuiGroups() {
    const rows = result.rows || [];
    const amountField = (result.columns || []).find((column) => /金额/.test(column.title))?.field || "BALANCE_CUR_MONEY";
    const counts = new Map((result.groups || []).map((group) => [`${group.center_code}|${group.site_code}|${group.balance_type}`, group]));
    const byType = new Map();
    rows.forEach((row) => {
      const type = text(row.BALANCE_TYPE).trim();
      if (!type) return;
      const key = `${text(row.CENTER_NAME_CODE)}|${text(row.SITE_NAME_CODE)}|${type}`;
      const item = byType.get(type) || { type, amounts: [], groups: [], targets: [] };
      item.amounts.push(text(row[amountField]));
      if (!item.targets.some((target) => target.key === key)) {
        item.targets.push({ key, center_code: text(row.CENTER_NAME_CODE), site_code: text(row.SITE_NAME_CODE), balance_type: type });
        if (counts.has(key)) item.groups.push(counts.get(key));
      }
      byType.set(type, item);
    });
    return [...byType.values()].map((item) => {
      const dates = item.groups.map((group) => group.latest_date).filter(Boolean);
      const known = item.groups.length === item.targets.length;
      return {
        type: item.type, amount: sum(item.amounts), targets: item.targets,
        count: known ? item.groups.reduce((total, group) => total + Number(group.count || 0), 0) : null,
        latest: dates.length && item.groups.every((group) => group.latest_date) ? dates.sort().at(-1) : null,
        latestNote: item.groups.map((group) => group.latest_note).find(Boolean) || "",
      };
    });
  }

  function renderRonghuiSummary() {
    const groups = ronghuiGroups();
    const net = sum(groups.map((group) => group.amount));
    const income = sum(groups.filter((group) => !negative(group.amount)).map((group) => group.amount));
    const deduction = sum(groups.filter((group) => negative(group.amount)).map((group) => group.amount));
    const scale = scaleOf(groups.map((group) => group.amount));
    const absTotal = groups.reduce((total, group) => total + (toUnits(group.amount, scale) < 0n ? -toUnits(group.amount, scale) : toUnits(group.amount, scale)), 0n);
    const counts = groups.every((group) => group.count !== null) ? groups.reduce((total, group) => total + group.count, 0) : null;
    const constants = columnKinds(result.columns || [], result.rows || []).filter((column) => column.constant && !["BALANCE_TYPE"].includes(column.field));
    renderContext([...sourceContext(), ...constants.map((column) => [column.title, text(result.rows[0][column.field])])]);
    renderStrip([
      { label: "结算净额", value: money(net), tone: negative(net) ? "expense" : "" },
      { label: "收入类", value: money(income, { sign: true }), tone: "income" },
      { label: "扣减类", value: money(deduction), tone: "expense" },
      { label: "结算笔数", value: counts === null ? "—" : `${counts.toLocaleString("zh-CN")} 笔` },
    ]);
    lastColumns = ["结算类型", "笔数", "金额合计", "占比", "最近结算日", "明细"];
    if (!groups.length) {
      els.body.innerHTML = `<div class="pb-empty"><b>该区间没有结算记录</b><span>可以放宽日期区间（最多 ${maxSpan} 天）或换一个日期口径后再查。</span></div>`;
      els.pager.hidden = true;
      return;
    }
    const size = Number(els.pageSize.value) || 20;
    const pages = Math.max(1, Math.ceil(groups.length / size));
    groupPage = Math.min(groupPage, pages);
    const visible = groups.slice((groupPage - 1) * size, groupPage * size);
    const ratio = (group) => {
      if (!absTotal) return 0;
      const units = toUnits(group.amount, scale);
      return Number(((units < 0n ? -units : units) * 10000n) / absTotal) / 100;
    };
    els.body.innerHTML = `<div class="pb-table-wrap"><table class="pb-table pb-table--summary">
      <colgroup><col style="width:300px"><col style="width:96px"><col style="width:180px"><col style="width:214px"><col style="width:150px"><col style="width:122px"></colgroup>
      <thead><tr><th scope="col">结算类型</th><th scope="col" class="num">笔数</th><th scope="col" class="num">金额合计</th><th scope="col">占比</th><th scope="col">最近结算日</th><th scope="col" class="num">明细</th></tr></thead>
      <tbody>${visible.map((group, index) => {
        const share = ratio(group);
        return `<tr class="pb-row--link" data-group="${(groupPage - 1) * size + index}">
          <td>${escapeHtml(group.type)}</td><td class="num">${group.count === null ? '<span class="pb-null">—</span>' : group.count.toLocaleString("zh-CN")}</td>
          <td class="num" data-tone="${negative(group.amount) ? "expense" : ""}">${escapeHtml(money(group.amount))}</td>
          <td><span class="pb-share"><span class="pb-share__track"><span style="width:${Math.min(100, share)}%"></span></span><small>${share.toFixed(1)}%</small></span></td>
          <td class="pb-cell--time">${group.latest ? escapeHtml(group.latest) : `<span class="pb-null" title="${escapeHtml(group.latestNote)}">—</span>`}</td>
          <td class="num"><button type="button" class="pb-link">查看明细 ›</button></td></tr>`;
      }).join("")}</tbody>
      <tfoot><tr><th scope="row">合计</th><td class="num">${counts === null ? "—" : counts.toLocaleString("zh-CN")}</td>
        <td class="num" data-tone="${negative(net) ? "expense" : ""}">${escapeHtml(money(net))}</td><td>${absTotal ? "100.0%" : "—"}</td><td><span class="pb-null">—</span></td><td></td></tr></tfoot>
      </table></div>`;
    els.body.querySelector("tbody").onclick = (event) => {
      const tr = event.target.closest("tr[data-group]");
      if (tr) ronghuiDrill(groups[Number(tr.dataset.group)]);
    };
    renderPager(groups.length, groupPage, size, (page) => { groupPage = page; renderRonghuiSummary(); });
    els.total.textContent = `共 ${groups.length} 个结算类型 · 平台统计 ${Number(result.total || 0)} 行`;
    exportRows = { header: ["结算类型", "笔数", "金额合计", "占比", "最近结算日"],
      rows: groups.map((group) => [group.type, group.count ?? "", group.amount, `${ratio(group).toFixed(1)}%`, group.latest || ""]) };
    els.exportBtn.disabled = false;
  }

  // -- 1 record → KPIs + field grid (spec ㉓b) ----------------------------------------------------------
  const KPIS = [
    { match: /目前结算余额|当前结算余额/, label: "当前结算余额" },
    { match: /真实结算余额/, label: "真实结算余额" },
    { match: /警戒结算金额/, label: "警戒结算金额", tone: "warn", note: "平台设置的警戒线" },
    { match: /保证金|押金/, label: "保证金 / 押金" },
  ];
  function renderBalance() {
    const columns = result.columns || [];
    const rows = result.rows || [];
    renderContext(sourceContext());
    lastColumns = [];
    if (!rows.length) {
      stateBlock("empty", `${NAME}平台未返回预付款账户`, '这个网点可能没有开通预付款，或账号没有该菜单权限；可到 <a href="/automation-accounts">业务账号</a> 检查登录与授权。');
      renderContext(sourceContext());
      return;
    }
    if (rows.length > 1) { renderGeneric(); return; }
    const row = rows[0];
    const kpis = KPIS.map((kpi) => ({ ...kpi, column: columns.find((column) => kpi.match.test(column.title)) })).filter((kpi) => kpi.column);
    els.body.innerHTML = `${kpis.length ? `<div class="pb-kpis">${kpis.map((kpi) => {
      const value = text(row[kpi.column.field]);
      return `<div class="pb-kpi" data-tone="${kpi.tone || ""}"><span>${escapeHtml(kpi.column.title)}</span><b>${escapeHtml(isNumber(value) ? money(value) : value || "—")}</b>${kpi.note ? `<small>${escapeHtml(kpi.note)}</small>` : ""}</div>`;
    }).join("")}</div>` : ""}
      <dl class="pb-fields">${columns.map((column) => {
        const value = text(row[column.field]).trim();
        return `<div><dt>${escapeHtml(column.title)}</dt><dd class="${isNumber(value) ? "num" : ""}">${value ? escapeHtml(isNumber(value) ? money(value) : value) : '<span class="pb-null">—</span>'}</dd></div>`;
      }).join("")}</dl>`;
    els.pager.hidden = true;
    exportRows = { header: columns.map((column) => column.title), rows: [columns.map((column) => text(row[column.field]))] };
    els.exportBtn.disabled = false;
  }

  function render() {
    hideExtras();
    if (platform === "ronghui" && current.action === "summary") renderRonghuiSummary();
    else if (platform === "ronghui" && current.action === "balance") renderBalance();
    else renderGeneric();
  }

  // -- drill-downs ------------------------------------------------------------------------------------
  function renderTrail() {
    els.trail.replaceChildren();
    els.trail.hidden = !trail.length;
    if (!trail.length) return;
    const back = document.createElement("button");
    back.type = "button";
    back.className = "pb-link";
    back.textContent = "‹ 返回上一级";
    back.addEventListener("click", () => {
      const previous = trail.pop();
      if (previous.params.start) { els.start.value = previous.params.start; els.end.value = previous.params.end; }
      selectView(previous.action);
      run(previous);
    });
    const path = document.createElement("span");
    path.textContent = [...trail, current].map((item) => item.label).join(" › ");
    els.trail.append(back, path);
  }

  function ronghuiDrill(group) {
    const target = group.targets[0];
    const state = { action: "summary_detail", page: 1, label: `${VIEW_TITLES.summary_detail}：${group.type}`,
      params: { ...current.params, action: "summary_detail", page_size: els.pageSize.value,
        center_code: target.center_code, site_code: target.site_code, balance_type: target.balance_type } };
    trail.push(current);
    run(state);
  }

  // 平台规则：跳转参数名取列的 skipData，值优先取所点行，其次沿用当前查询条件。
  function yundaDrill(row, drill, value) {
    const action = YUNDA_TABS[drill.tab];
    if (!action) { say(`平台跳转到未核实的页面（${drill.tab}），已停止。`); return; }
    const fromBranches = current.action === "branches";
    const filters = { ...(current.params.filters || {}) };
    let { start, end } = current.params;
    if (fromBranches) {
      try { ({ start, end } = checkedDates()); } catch (error) { say(error.message); return; }
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
      if (!filterKeys.has(key)) { stateBlock("error", "这一项暂时不能下钻", `平台下钻参数“${escapeHtml(key)}”尚未核实，请联系维护。`); return; }
      filters[key] = text(row[key]);
    }
    const name = text(row.branch_name || row.parent_project_name || row.trade_date || value);
    const state = { action, page: 1, label: `${VIEW_TITLES[action]}：${name}`,
      params: { account_id: account(), action, start, end, page_size: els.pageSize.value, filters } };
    trail.push(current);
    selectView(action);
    run(state);
  }

  // -- events -----------------------------------------------------------------------------------------
  function resetView(message) {
    trail = []; current = null; result = null; renderTrail(); hideExtras(); syncDates();
    els.body.innerHTML = `<div class="pb-empty"><b>${escapeHtml(message)}</b><span>每次查询都直接读取平台最新数据，不显示旧数据。</span></div>`;
  }

  $$("[data-bill-view]").forEach((button) => button.addEventListener("click", () => {
    if (busy) return;
    selectView(button.dataset.billView);
    lastColumns = [];
    resetView("选择条件后点“查询”");
    if (!dateProblem() || !SHOW_DATES.has(button.dataset.billView)) freshQuery(button.dataset.billView);
  }));
  els.account.addEventListener("change", () => { resetView("选择条件后点“查询”"); if (!dateProblem()) freshQuery(activeView()); });
  [els.start, els.end].forEach((input) => input.addEventListener("change", syncDates));
  els.dateType?.addEventListener("change", syncDates);
  $$("[data-bill-range]").forEach((button) => button.addEventListener("click", () => {
    const today = new Date();
    const range = button.dataset.billRange;
    const start = range === "month" ? new Date(today.getFullYear(), today.getMonth(), 1) : new Date(today.getFullYear(), today.getMonth(), today.getDate() - Number(range) + 1);
    els.start.value = ymd(start);
    els.end.value = ymd(today);
    syncDates();
  }));
  els.pageSize.addEventListener("change", () => {
    if (!current) return;
    if (platform === "ronghui" && current.action === "summary") { groupPage = 1; renderRonghuiSummary(); return; }
    run({ ...current, page: 1, params: { ...current.params, page_size: els.pageSize.value } });
  });
  // 已下钻时“查询”按新日期重查当前这一级，保留下钻条件；否则从当前页签重新开始。
  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    if (!current || !trail.length) { freshQuery(activeView()); return; }
    try {
      const params = { ...current.params, page_size: els.pageSize.value };
      if (DATED.has(current.action)) {
        Object.assign(params, checkedDates());
        if (platform === "ronghui") params.date_type = els.dateType.value;
      }
      run({ ...current, page: 1, params });
    } catch (error) {
      say(error.message);
    }
  });
  root.addEventListener("click", (event) => {
    if (event.target.closest("[data-bill-retry]") && current) run(current);
  });
  els.exportBtn.addEventListener("click", () => {
    if (!exportRows || !current) return;
    const quote = (value) => `"${text(value).replace(/"/g, '""')}"`;
    const lines = [exportRows.header.map(quote).join(","), ...exportRows.rows.map((row) => row.map(quote).join(","))];
    const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${NAME}${current.label.replace(/[\\/:*?"<>|]/g, "_")}-${(result?.fetched_at || "").replace(/[: ]/g, "")}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });

  syncDates();
  freshQuery(activeView());
})();
