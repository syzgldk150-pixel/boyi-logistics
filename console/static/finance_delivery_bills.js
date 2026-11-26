// 财务 · 派件账单（邵阳大祥S站，2026-11-24）。“查询”经 Console → Agent：每日到货表 ∩ 融辉结算明细（实时）。
// The Agent composes every amount (融辉结算金额、派送费、增值费、应付) and keeps the bill as a 30-minute snapshot;
// this page only shows it, lets people pick rows to confirm and manage the 派送费 rule. Since 2026-11-26 增值费
// and 其他收入 come from the 融辉 结算明细 (nothing entered by hand); 自提 rows carry no 派送费.
// Amounts are exact decimal strings, never floats.
(() => {
  "use strict";
  const main = document.querySelector(".main-content:not([hidden])") || document;
  const root = main.querySelector("[data-delivery-bills]");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  const fetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
  const canManage = root.dataset.canManage === "true";
  const superAdmin = root.dataset.superAdmin === "true";
  const maxSpan = Number(root.dataset.maxSpan || 31);
  const $ = (selector, scope = root) => scope.querySelector(selector);
  const $$ = (selector, scope = root) => [...scope.querySelectorAll(selector)];
  const els = {
    form: $("[data-db-filters]"), start: $("[data-db-start]"), end: $("[data-db-end]"), query: $("[data-db-query]"),
    dateError: $("[data-db-date-error]"), context: $("[data-db-context]"), strip: $("[data-db-strip]"),
    notices: $("[data-db-notices]"), bulk: $("[data-db-bulk]"), picked: $("[data-db-picked]"),
    confirmPicked: $("[data-db-confirm-picked]"), body: $("[data-db-body]"), status: $("[data-db-status]"),
    search: $("[data-db-search]"), exportBtn: $("[data-db-export]"),
    detail: $("[data-db-detail]"), rules: $("[data-db-rules]"), settings: $("[data-db-settings]"),
    list: $("[data-db-list]"),
  };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const pad = (value) => String(value).padStart(2, "0");
  const ymd = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const say = (message) => { els.status.textContent = message || ""; };
  // 融辉结算金额列可隐藏（只遮表格里的逐单数值），选择记在本浏览器。
  const HIDE_KEY = "boyi.deliveryBills.hideRonghui";
  let hideRonghui = false;
  try { hideRonghui = localStorage.getItem(HIDE_KEY) === "1"; } catch (_) { /* storage blocked */ }
  const EYE_ON = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  const EYE_OFF = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 5.1A10.8 10.8 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-2.2 3.1M6.6 6.6C3.7 8.4 2 12 2 12s3.5 7 10 7a10 10 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M3 3l18 18"/></svg>';

  // -- amounts: server strings (up to 4 decimals) shown with thousands and 2 decimals, half-up ------------
  const NUMERIC = /^-?\d{1,15}(\.\d{1,8})?$/;
  function money(value) {
    const raw = String(value ?? "").trim();
    if (!NUMERIC.test(raw)) return "—";
    const negative = raw.startsWith("-");
    const [whole, frac = ""] = raw.replace("-", "").split(".");
    let units = BigInt(whole || "0") * 100n + BigInt((frac + "00").slice(0, 2));
    if (Number((frac + "000").charAt(2)) >= 5) units += 1n;
    const digits = units.toString().padStart(3, "0");
    const body = `${digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${digits.slice(-2)}`;
    return negative && units > 0n ? `-${body}` : body;
  }
  const amountCell = (value, tone = "") => `<td class="num"${tone ? ` data-tone="${tone}"` : ""}>${value == null ? '<span class="pb-null">—</span>' : esc(money(value))}</td>`;
  const weight = (value) => {
    if (value == null || value === "") return "—";
    const raw = String(value);
    return raw.includes(".") ? raw.replace(/0+$/, "").replace(/\.$/, "") : raw;
  };

  async function request(url, { method = "GET", body = null, write = false } = {}) {
    const headers = { "X-Requested-With": "XMLHttpRequest" };
    let payload = body;
    if (method === "POST") {
      headers["Content-Type"] = "application/json";
      const requestId = crypto.randomUUID();
      headers["X-Browser-Request-UUID"] = requestId;
      payload = JSON.stringify({ ...(body || {}), request_id: requestId });
    }
    const response = await fetch(url, { method, credentials: "same-origin", headers, body: payload });
    const data = await response.json().catch(() => ({}));
    if (response.status === 403) throw new Error(data.message || "当前账号没有权限进行这个操作。");
    if (!response.ok || !data.ok) throw new Error(data.message || data.error?.message || (write ? "操作未完成，请重新查询核对结果。" : "读取失败，请稍后重试。"));
    return data.data;
  }

  // -- state ---------------------------------------------------------------------------------------------
  let bill = null;
  let filter = "all";
  let busy = false;
  const picked = new Set();
  const today = new Date();
  els.start.value = ymd(new Date(today.getFullYear(), today.getMonth(), 1));
  els.end.value = ymd(today);

  function dateProblem() {
    const start = els.start.value; const end = els.end.value;
    if (!start || !end) return "请选择到货表日期范围。";
    const days = Math.round((new Date(`${end}T00:00:00`) - new Date(`${start}T00:00:00`)) / 86400000) + 1;
    if (days < 1) return "结束日期不能早于开始日期。";
    if (days > maxSpan) return `一次最多查询 ${maxSpan} 天到货表，当前选了 ${days} 天，请缩短区间。`;
    return "";
  }
  function syncDates() {
    const problem = dateProblem();
    els.dateError.hidden = !problem;
    els.dateError.textContent = problem;
    els.query.disabled = Boolean(problem) || busy;
  }
  els.start.addEventListener("change", syncDates);
  els.end.addEventListener("change", syncDates);
  $$("[data-db-range]").forEach((button) => button.addEventListener("click", () => {
    const kind = button.dataset.dbRange;
    if (kind === "7") { const start = new Date(today); start.setDate(start.getDate() - 6); els.start.value = ymd(start); els.end.value = ymd(today); }
    if (kind === "month") { els.start.value = ymd(new Date(today.getFullYear(), today.getMonth(), 1)); els.end.value = ymd(today); }
    if (kind === "last-month") {
      els.start.value = ymd(new Date(today.getFullYear(), today.getMonth() - 1, 1));
      els.end.value = ymd(new Date(today.getFullYear(), today.getMonth(), 0));
    }
    syncDates();
  }));
  syncDates();

  function stateBlock(kind, title, detail = "", action = "") {
    els.body.innerHTML = `<div class="pb-empty" data-kind="${kind}"><b>${esc(title)}</b>${detail ? `<span>${esc(detail)}</span>` : ""}${action}</div>`;
  }

  async function load() {
    const problem = dateProblem();
    if (problem) { syncDates(); return; }
    busy = true; syncDates(); picked.clear();
    els.context.hidden = true; els.strip.hidden = true; els.notices.hidden = true; if (els.bulk) els.bulk.hidden = true;
    els.exportBtn.disabled = true;
    stateBlock("loading", "正在读取每日到货表和融辉结算明细…", "融辉结算明细要实时读全，通常需要十几秒。");
    say("正在读取派件账单");
    try {
      const query = new URLSearchParams({ start: els.start.value, end: els.end.value });
      bill = await request(`/finance/delivery-bills?${query}`);
      render();
      say(`已读取：融辉已结算 ${bill.summary.settled_count} 单`);
    } catch (error) {
      bill = null;
      stateBlock("error", "派件账单读取失败", error.message, '<button type="button" class="ghost-btn" data-db-retry>重试</button>');
      say(error.message);
    } finally {
      busy = false; syncDates();
    }
  }
  els.form.addEventListener("submit", (event) => { event.preventDefault(); load(); });

  // -- render ------------------------------------------------------------------------------------------
  const STATUS = { pending: "待确认", confirmed: "已确认" };
  function rowProblem(row) { return row.problems.length > 0 || (row.status === "pending" && !row.can_confirm); }
  function visibleRows() {
    if (!bill) return [];
    const term = els.search.value.trim().toUpperCase();
    return bill.rows.filter((row) => {
      if (term && !row.tracking_number.includes(term)) return false;
      if (filter === "pending") return row.status === "pending";
      if (filter === "confirmed") return row.status === "confirmed";
      if (filter === "problem") return rowProblem(row);
      return true;
    });
  }

  function render() {
    const summary = bill.summary;
    els.context.hidden = false;
    els.context.innerHTML = `<span><em>网点</em>${esc(bill.station)}</span><span><em>到货表日期</em>${esc(bill.start)} 至 ${esc(bill.end)}</span>
      <span><em>融辉结算日期</em>${esc(bill.settlement_window.start)} 至 ${esc(bill.settlement_window.end)}</span>
      <span><em>融辉读取时间</em>${esc(bill.fetched_at || "—")}</span>`;
    renderStrip();
    const notices = [];
    if (bill.missing_dates.length) notices.push(`<p data-tone="warn">每日到货表缺这些日期的统计：${esc(bill.missing_dates.join("、"))}，这几天的单不会出现在账单里。</p>`);
    if (summary.unsettled_count) notices.push(`<p>到货表里还有 ${summary.unsettled_count} 单融辉未结算派送费，不进账单。<button type="button" class="pb-link" data-db-show="unsettled">查看</button></p>`);
    if (bill.missing_in_arrival.length) notices.push(`<p data-tone="warn">融辉在这段日期结算了 ${bill.missing_in_arrival.length} 单派送费，但每日到货表里没有这些单。<button type="button" class="pb-link" data-db-show="missing">查看</button></p>`);
    if (summary.blocked_count) notices.push(`<p data-tone="warn">${summary.blocked_count} 单现在不能确认（看“需处理”）。</p>`);
    els.notices.hidden = !notices.length;
    els.notices.innerHTML = notices.join("");
    renderTable();
  }

  // 汇总条：融辉结算金额、差额（由融辉金额推出）跟表格列共用一个隐藏开关。
  function renderStrip() {
    const summary = bill.summary;
    const masked = (value) => (hideRonghui ? "****" : money(value));
    els.strip.hidden = false;
    els.strip.innerHTML = [
      ["进账单（融辉已结算）", `${summary.settled_count} 单`, `到货表 ${summary.arrival_count} 单`],
      ["融辉结算金额", masked(summary.ronghui_total), "融辉付给网点", true],
      ["派送费", money(summary.delivery_total), "按公斤段：起步价＋超出部分"],
      ["增值费", money(summary.value_added_total), "融辉结算的上楼费、卸货费等"],
      ["其他收入", money(summary.other_income_total), "代收/到付手续费等，不给司机"],
      ["应付合计", money(summary.payable_total), `已确认 ${summary.confirmed_count} · 待确认 ${summary.pending_count}`],
      ["差额", masked(summary.difference_total), "融辉 − 应付", true],
    ].map(([label, value, note, toggle]) => `<div class="pb-strip__cell"><span${toggle ? ' class="db-th-toggle"' : ""}>${esc(label)}${toggle ? eyeButton() : ""}</span><b${toggle && hideRonghui ? ' class="db-masked"' : ""}>${esc(value)}</b><small>${esc(note)}</small></div>`).join("");
  }

  function eyeButton() {
    const label = hideRonghui ? "显示融辉结算金额" : "隐藏融辉结算金额";
    return `<button type="button" class="db-eye" data-db-hide-ronghui aria-pressed="${hideRonghui}" title="${label}" aria-label="${label}">${hideRonghui ? EYE_OFF : EYE_ON}</button>`;
  }

  function toggleRonghui(fromStrip) {
    hideRonghui = !hideRonghui;
    try { localStorage.setItem(HIDE_KEY, hideRonghui ? "1" : "0"); } catch (_) { /* storage blocked */ }
    const scroll = els.body.querySelector(".db-table-wrap")?.scrollTop || 0;
    renderStrip();
    renderTable();
    const wrap = els.body.querySelector(".db-table-wrap");
    if (wrap) wrap.scrollTop = scroll;
    (fromStrip ? els.strip : els.body).querySelector("[data-db-hide-ronghui]")?.focus();
  }

  function renderTable() {
    const rows = visibleRows();
    els.exportBtn.disabled = !rows.length;
    for (const code of [...picked]) if (!bill.rows.some((row) => row.tracking_number === code && row.can_confirm)) picked.delete(code);
    if (!bill.rows.length) {
      stateBlock("empty", "这段日期没有融辉已结算的派件单", "融辉结算一般比到货表晚 0–6 天，最近几天的单可能还没结算。");
      updatePicked();
      return;
    }
    if (!rows.length) { stateBlock("empty", "没有符合条件的单"); updatePicked(); return; }
    const pickable = rows.filter((row) => row.can_confirm);
    const allPicked = pickable.length > 0 && pickable.every((row) => picked.has(row.tracking_number));
    const head = `<tr>${canManage ? `<th scope="col" class="db-pick"><input type="checkbox" aria-label="全选可确认的单" data-db-pick-all ${allPicked ? "checked" : ""} ${pickable.length ? "" : "disabled"}></th>` : ""}
      <th scope="col">单号 / 到货表日期</th><th scope="col">货物 / 配送方式</th><th scope="col">件数 / 结算重量</th>
      <th scope="col"><span class="db-th-toggle">融辉结算金额${eyeButton()}</span></th><th scope="col">派送费</th><th scope="col">增值费</th><th scope="col">其他收入</th><th scope="col">应付合计</th><th scope="col">状态</th></tr>`;
    const body = rows.map((row) => {
      const tip = row.ronghui_types.map((item) => `${item.type} ${money(item.amount)}${item.counted ? "" : "（不计入）"}`).join("\n");
      const status = row.status === "confirmed" ? '<span class="db-badge" data-tone="ok">已确认</span>'
        : row.can_confirm ? '<span class="db-badge">待确认</span>' : '<span class="db-badge" data-tone="warn">需处理</span>';
      const partNames = (part) => row.ronghui_types.filter((item) => item.part === part && Number(item.amount) !== 0).map((item) => item.type);
      const valueNames = partNames("value_added");
      const otherNames = partNames("other");
      const otherTip = row.ronghui_types.filter((item) => item.part === "other").map((item) => `${item.type} ${money(item.amount)}`).join("\n");
      const fees = valueNames.length ? `<small>${esc(valueNames.join("、"))}</small>` : "";
      const deliveryCell = row.no_delivery_fee && row.delivery_fee == null
        ? `<td class="pb-cell--pair num"><span class="pb-null">—</span><small>${esc(row.delivery_method)}不算</small></td>` : amountCell(row.delivery_fee);
      return `<tr class="pb-row--link" data-db-row="${esc(row.tracking_number)}">
        ${canManage ? `<td class="db-pick"><input type="checkbox" aria-label="选择 ${esc(row.tracking_number)}" data-db-pick="${esc(row.tracking_number)}" ${picked.has(row.tracking_number) ? "checked" : ""} ${row.can_confirm ? "" : "disabled"}></td>` : ""}
        <td class="pb-cell--pair"><span class="db-code">${esc(row.tracking_number)}</span><small>${esc(row.arrival_date)}</small></td>
        <td class="pb-cell--pair"><span>${esc(row.goods_name || "—")}</span><small>${esc(row.delivery_method || "—")}</small></td>
        <td class="pb-cell--pair num"><span>${esc(row.pieces ?? "—")} 件</span><small>${esc(weight(row.settlement_weight))} kg</small></td>
        ${hideRonghui ? '<td class="num db-masked">****</td>' : `<td class="num" title="${esc(tip)}">${esc(money(row.ronghui_amount))}</td>`}
        ${deliveryCell}
        <td class="pb-cell--pair num"><span>${esc(money(row.value_added_total))}</span>${fees}</td>
        <td class="pb-cell--pair num" title="${esc(otherTip)}"><span>${esc(money(row.other_income))}</span>${otherNames.length ? `<small>${esc(otherNames.join("、"))}</small>` : ""}</td>
        ${amountCell(row.payable_total)}
        <td>${status}${row.problems.length ? `<small class="db-problem">${esc(row.problems[0])}</small>` : ""}</td></tr>`;
    }).join("");
    els.body.innerHTML = `<div class="pb-table-wrap db-table-wrap"><table class="pb-table db-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
    updatePicked();
  }

  function updatePicked() {
    if (!els.bulk) return;
    els.bulk.hidden = !bill || !bill.rows.some((row) => row.can_confirm);
    els.picked.textContent = `已选 ${picked.size} 单`;
    els.confirmPicked.disabled = !picked.size || busy;
  }

  $$("[data-db-filter]").forEach((tab) => tab.addEventListener("click", () => {
    filter = tab.dataset.dbFilter;
    $$("[data-db-filter]").forEach((item) => { const on = item === tab; item.setAttribute("aria-selected", String(on)); item.tabIndex = on ? 0 : -1; });
    if (bill) renderTable();
  }));
  els.search.addEventListener("input", () => { if (bill) renderTable(); });

  els.body.addEventListener("change", (event) => {
    const box = event.target.closest("[data-db-pick], [data-db-pick-all]");
    if (!box) return;
    if (box.dataset.dbPickAll !== undefined) {
      visibleRows().filter((row) => row.can_confirm).forEach((row) => (box.checked ? picked.add(row.tracking_number) : picked.delete(row.tracking_number)));
      renderTable();
    } else {
      box.checked ? picked.add(box.dataset.dbPick) : picked.delete(box.dataset.dbPick);
      updatePicked();
    }
  });
  els.body.addEventListener("click", (event) => {
    if (event.target.closest("[data-db-retry]")) { load(); return; }
    if (event.target.closest("[data-db-hide-ronghui]")) { toggleRonghui(false); return; }
    if (event.target.closest("input, button, a")) return;
    const row = event.target.closest("[data-db-row]");
    if (row) openDetail(row.dataset.dbRow);
  });
  els.strip.addEventListener("click", (event) => {
    if (event.target.closest("[data-db-hide-ronghui]")) toggleRonghui(true);
  });
  els.notices.addEventListener("click", (event) => {
    const button = event.target.closest("[data-db-show]");
    if (button) showList(button.dataset.dbShow);
  });

  // -- confirm -----------------------------------------------------------------------------------------
  async function confirmRows(codes) {
    const items = codes.map((code) => bill.rows.find((row) => row.tracking_number === code)).filter(Boolean)
      .map((row) => ({ tracking_number: row.tracking_number, fingerprint: row.fingerprint }));
    if (!items.length) return;
    for (let index = 0; index < items.length; index += 200) {
      const result = await request("/finance/delivery-bills/confirm", { method: "POST", write: true,
        body: { snapshot_id: bill.snapshot_id, items: items.slice(index, index + 200) } });
      bill = result.bill;
      const failed = result.results.filter((item) => !item.ok);
      say(`已确认 ${result.succeeded} 单${failed.length ? `，${failed.length} 单未确认` : ""}`);
      if (failed.length) window.alert(`${failed.length} 单未确认：\n${failed.slice(0, 10).map((item) => `${item.tracking_number}：${item.message}`).join("\n")}`);
    }
    picked.clear();
    render();
  }
  els.confirmPicked?.addEventListener("click", async () => {
    if (!picked.size || !bill) return;
    if (!window.confirm(`确认这 ${picked.size} 单的派送费与增值费？确认后应付金额固定，要改需先撤销确认。`)) return;
    busy = true; updatePicked();
    try { await confirmRows([...picked]); } catch (error) { window.alert(error.message); } finally { busy = false; updatePicked(); }
  });

  // -- detail ------------------------------------------------------------------------------------------
  const findRow = (code) => bill?.rows.find((row) => row.tracking_number === code);
  function closeOn(dialog) { $$("[data-db-close]", dialog).forEach((button) => button.addEventListener("click", () => dialog.close())); }
  [els.detail, els.rules, els.settings, els.list].filter(Boolean).forEach(closeOn);
  const PART = { delivery: "派送费", value_added: "增值费（给司机）", other: "其他收入" };

  function openDetail(code) {
    const row = findRow(code);
    if (!row) return;
    $("[data-db-detail-title]", els.detail).textContent = `${row.tracking_number} · ${STATUS[row.status]}`;
    const types = row.ronghui_types.map((item) => `<tr><td>${esc(item.type)}</td><td class="num">${hideRonghui ? "****" : esc(money(item.amount))}</td><td>${item.count}</td><td>${esc(item.date || "—")}</td><td>${item.counted ? esc(PART[item.part] || "计入") : "不计入"}</td></tr>`).join("");
    $("[data-db-detail-body]", els.detail).innerHTML = `
      ${row.problems.map((item) => `<p class="db-alert">${esc(item)}</p>`).join("")}
      <dl class="db-facts"><div><dt>到货表日期</dt><dd>${esc(row.arrival_date)}</dd></div><div><dt>货物</dt><dd>${esc(row.goods_name || "—")}</dd></div>
        <div><dt>配送方式</dt><dd>${esc(row.delivery_method || "—")}</dd></div><div><dt>件数 / 结算重量</dt><dd>${esc(row.pieces ?? "—")} 件 / ${esc(weight(row.settlement_weight))} kg</dd></div>
        ${row.confirmed_by ? `<div><dt>确认</dt><dd>${esc(row.confirmed_by)} · ${esc(row.confirmed_at || "")}</dd></div>` : ""}</dl>
      <h3>融辉结算（合计 ${hideRonghui ? "****" : esc(money(row.ronghui_amount))}）</h3>
      <table class="pb-table db-mini"><thead><tr><th>结算类型</th><th>金额</th><th>笔数</th><th>结算日期</th><th>口径</th></tr></thead><tbody>${types}</tbody></table>
      <h3>派送费 ${row.delivery_fee == null ? "—" : esc(money(row.delivery_fee))}</h3>
      ${row.delivery_steps.length ? `<ol class="db-steps">${row.delivery_steps.map((step) => `<li>${esc(step)}</li>`).join("")}</ol>` : '<p class="db-hint">没有算出派送费。</p>'}
      <h3>增值费 ${esc(money(row.value_added_total))}<small class="db-sub">${row.status === "confirmed" ? "确认时记下的融辉结算金额" : "融辉结算明细里增值费结算类型的合计"}</small></h3>
      <h3>其他收入 ${esc(money(row.other_income))}<small class="db-sub">不给司机</small></h3>
      <h3>应付合计 ${row.payable_total == null ? "—" : esc(money(row.payable_total))}</h3>`;
    const foot = $("[data-db-detail-foot]", els.detail);
    foot.innerHTML = !canManage ? "" : row.status === "confirmed"
      ? '<button type="button" class="ghost-btn" data-db-revoke>撤销确认</button>'
      : `<button type="button" class="primary-btn" data-db-confirm-one ${row.can_confirm ? "" : "disabled"}>确认这单</button>`;
    foot.onclick = async (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      button.disabled = true;
      try {
        if (button.matches("[data-db-confirm-one]")) {
          await confirmRows([row.tracking_number]);
          openDetail(row.tracking_number);
        } else if (button.matches("[data-db-revoke]")) {
          if (!window.confirm("撤销确认后这单按融辉最新结算重新计算，需要再次确认。继续？")) { button.disabled = false; return; }
          const result = await request("/finance/delivery-bills/revoke", { method: "POST", write: true,
            body: { snapshot_id: bill.snapshot_id, tracking_number: row.tracking_number, expected_revision: row.item_revision } });
          bill = result.bill; render(); openDetail(row.tracking_number);
        }
      } catch (error) { window.alert(error.message); button.disabled = false; }
    };
    if (!els.detail.open) els.detail.showModal();
  }

  // -- lists: 未结算 / 到货表缺单 ------------------------------------------------------------------------
  function showList(kind) {
    const title = $("[data-db-list-title]", els.list);
    const body = $("[data-db-list-body]", els.list);
    if (kind === "unsettled") {
      title.textContent = `融辉未结算派送费（${bill.unsettled.length} 单，不进账单）`;
      body.innerHTML = `<table class="pb-table db-mini"><thead><tr><th>单号</th><th>到货表日期</th><th>货物</th><th>配送方式</th><th>融辉已有的结算</th></tr></thead><tbody>
        ${bill.unsettled.map((row) => `<tr><td>${esc(row.tracking_number)}</td><td>${esc(row.arrival_date)}</td><td>${esc(row.goods_name || "—")}</td><td>${esc(row.delivery_method || "—")}</td>
          <td>${row.types.length ? row.types.map((item) => `${esc(item.type)} ${esc(money(item.amount))}`).join("；") : "—"}</td></tr>`).join("")}</tbody></table>`;
    } else {
      title.textContent = `融辉已结算但每日到货表没有（${bill.missing_in_arrival.length} 单）`;
      body.innerHTML = `<p class="db-hint">这些单在融辉有“付派送费”，但每日到货表（目的站${esc(bill.station)}）里任何日期都没有，派件账单不会列出，请核对到货统计。</p>
        <table class="pb-table db-mini"><thead><tr><th>单号</th><th>派送费结算日期</th><th>融辉结算金额</th></tr></thead><tbody>
        ${bill.missing_in_arrival.map((row) => `<tr><td>${esc(row.tracking_number)}</td><td>${esc(row.settled_on)}</td><td class="num">${esc(money(row.ronghui_amount))}</td></tr>`).join("")}</tbody></table>`;
    }
    els.list.showModal();
  }

  // -- 派送费规则 ----------------------------------------------------------------------------------------
  // 2026-11-26：每段只填“到多少公斤”，起点自动接上一段（第 1 段从 0 起，最后一段“以上”），不会漏重量或重叠。
  // 每段“算出来”的钱由 Agent 用同一计费引擎算（rules/trial 不带重量），页面不算钱。
  const ruleForm = $("[data-db-rule-form]");
  const segmentBody = $("[data-db-segments]");
  const segCheck = $("[data-db-seg-check]");
  const kg = (value) => `${weight(value)} kg`;
  const startText = (value) => (value ? weight(value) : "?");
  let roundingLabels = {};
  try { roundingLabels = JSON.parse(els.rules.dataset.roundings || "{}"); } catch (_error) { roundingLabels = {}; }
  let rulesLoaded = [];
  let editorTouched = false;
  let exampleSeq = 0;
  let exampleTimer = 0;
  let fillEditor = () => {};  // set below when the edit form exists

  function todayText() { return ymd(new Date()); }
  // 现在执行的一版：执行日期不晚于今天的最新在用版本（列表已按执行日期倒序）。
  function currentRule(rules) {
    const active = rules.filter((rule) => rule.status === "active");
    return active.find((rule) => rule.effective_from <= todayText()) || active[active.length - 1] || null;
  }
  function exampleText(points) {
    if (!Array.isArray(points) || !points.length) return "—";
    if (points.length === 1) return `${kg(points[0].weight)} ${money(points[0].amount)} 元起`;
    return points.map((point) => `${kg(point.weight)} ${money(point.amount)}`).join(" → ");
  }
  function rangeText(segments, index) {
    const low = weight(segments[index].from);
    return index + 1 < segments.length ? `${low}–${weight(segments[index + 1].from)} kg` : `${low} kg 以上`;
  }
  function ruleTable(rule) {
    const segments = rule.segments || [];
    const examples = rule.examples || [];
    return `<table class="pb-table db-mini db-rule-table"><thead><tr><th>重量范围</th><th>起步价</th><th>起步价含</th><th>超出单价</th><th>算出来</th></tr></thead><tbody>
      ${segments.map((segment, index) => `<tr><td>${esc(rangeText(segments, index))}</td><td class="num">${esc(money(segment.first_price))} 元</td>
        <td class="num">${esc(kg(segment.first_weight))}</td><td class="num">${esc(weight(segment.extra_price))} 元/kg</td>
        <td class="db-seg-example">${esc(exampleText(examples[index]))}</td></tr>`).join("")}</tbody></table>`;
  }
  function ruleCard(rule, current) {
    const future = rule.status === "active" && rule.effective_from > todayText();
    const badge = rule.status !== "active" ? '<span class="db-badge">已作废</span>'
      : rule === current ? '<span class="db-badge" data-tone="ok">现在用的</span>'
        : future ? '<span class="db-badge" data-tone="warn">还没到执行日期</span>' : '<span class="db-badge">较早的一版</span>';
    const rounding = roundingLabels[rule.rounding] || "";
    const retire = superAdmin && rule.status === "active" && !rule.confirmed_count
      ? `<button type="button" class="pb-link" data-db-retire="${rule.id}" data-revision="${rule.revision}">作废</button>` : "";
    return `<article class="db-rule-card" data-db-rule-card>
      <header><b>${esc(rule.effective_from)} 起执行</b>${badge}${retire}</header>
      ${ruleTable(rule)}
      <p class="db-sub">超出部分的零头：${esc(rounding)} · 已确认 ${esc(rule.confirmed_count)} 单 · ${esc(rule.created_by)} ${esc(rule.created_at || "")}${rule.note ? ` · ${esc(rule.note)}` : ""}</p>
    </article>`;
  }
  async function loadRules() {
    const box = $("[data-db-rules-list]");
    box.innerHTML = '<p class="db-hint">正在读取…</p>';
    try {
      const data = await request("/finance/delivery-bills/rules");
      rulesLoaded = data.rules || [];
      const current = currentRule(rulesLoaded);
      const active = rulesLoaded.filter((rule) => rule.status === "active");
      const retired = rulesLoaded.filter((rule) => rule.status !== "active");
      box.innerHTML = (active.length ? active.map((rule) => ruleCard(rule, current)).join("")
        : '<p class="db-alert">还没有在用的派送费规则，账单里的派送费算不出来，也不能确认。</p>')
        + (retired.length ? `<details class="db-rule-retired"><summary>已作废的版本（${retired.length}）</summary>${retired.map((rule) => ruleCard(rule, current)).join("")}</details>` : "")
        + (active.length > 1 ? '<p class="db-hint">每单按到货表日期，用执行日期不晚于那天的最新一版；已有确认单用过的版本不能作废，改价请新增一版。</p>' : "");
      if (!editorTouched) fillEditor(current);
    } catch (error) {
      box.innerHTML = `<p class="db-alert">${esc(error.message)}</p>`;
    }
  }

  function ruleError(message) {
    const node = $("[data-db-rule-error]");
    if (!node) { if (message) say(message); return; }
    node.hidden = !message; node.textContent = message || "";
  }
  $("[data-db-open-rules]").addEventListener("click", () => { ruleError(""); els.rules.showModal(); loadRules(); });
  $("[data-db-rules-list]").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-db-retire]");
    if (!button || !window.confirm("作废这一版派送费规则？")) return;
    try {
      await request(`/finance/delivery-bills/rules/${button.dataset.dbRetire}/retire`, { method: "POST", write: true, body: { expected_revision: Number(button.dataset.revision) } });
      await loadRules();
    } catch (error) { ruleError(error.message); }
  });

  // 编辑表（只有能操作财务的人有）：每行 {to, first_price, first_weight, extra_price}；最后一行没有“到”。
  if (ruleForm) {
  function segmentRow(row, index, count, start) {
    const last = index + 1 === count;
    const range = last ? `<span class="db-seg-range">${esc(startText(start))} kg 以上</span>`
      : `<span class="db-seg-range">${esc(startText(start))} 到 <input data-field="to" inputmode="decimal" value="${esc(row.to ?? "")}" aria-label="第 ${index + 1} 段到多少公斤"> kg</span>`;
    const cell = (field, label, placeholder = "") => `<td><input data-field="${field}" inputmode="decimal" value="${esc(row[field] ?? "")}"${placeholder ? ` placeholder="${esc(placeholder)}"` : ""} aria-label="第 ${index + 1} 段${label}"></td>`;
    return `<tr data-db-segment>
      <td>${range}</td>${cell("first_price", "起步价（元）")}${cell("first_weight", "起步价含（公斤）", start ? weight(start) : "")}${cell("extra_price", "超出单价（元/公斤）")}
      <td class="db-seg-example" data-db-seg-example>—</td>
      <td>${count > 1 ? '<button type="button" class="pb-link" data-db-segment-remove>删除</button>' : ""}</td></tr>`;
  }
  function readRows() {
    return $$("[data-db-segment]", segmentBody).map((row) => Object.fromEntries(
      $$("input[data-field]", row).map((input) => [input.dataset.field, input.value.trim()])));
  }
  function startsOf(rows) { return rows.map((_row, index) => (index === 0 ? "0" : rows[index - 1].to || "")); }
  function drawRows(rows) {
    const list = rows.length ? rows : [{}];
    const starts = startsOf(list);
    segmentBody.innerHTML = list.map((row, index) => segmentRow(row, index, list.length, starts[index])).join("");
    scheduleExamples();
  }
  // 表单 → 接口的公斤段：起点＝上一段的“到”；“起步价含”空着＝这一段起点。
  function readSegments() {
    const rows = readRows();
    const starts = startsOf(rows);
    return rows.map((row, index) => ({
      from: starts[index], first_weight: row.first_weight || starts[index],
      first_price: row.first_price || "", extra_price: row.extra_price || "",
    }));
  }
  fillEditor = (rule) => {
    const segments = rule?.segments || [];
    drawRows(segments.map((segment, index) => ({
      to: segments[index + 1]?.from ?? "", first_price: segment.first_price,
      first_weight: segment.first_weight, extra_price: segment.extra_price,
    })));
    if (rule?.rounding) ruleForm.querySelector("[name=rounding]").value = rule.rounding;
  };
  // 改“到”时只更新后面各行显示的起点，不重画（保留光标）。
  function refreshStarts() {
    const rows = readRows();
    const starts = startsOf(rows);
    $$("[data-db-segment]", segmentBody).forEach((row, index) => {
      const range = row.querySelector(".db-seg-range");
      const input = range.querySelector("input");
      range.firstChild.textContent = input ? `${startText(starts[index])} 到 ` : `${startText(starts[index])} kg 以上`;
      row.querySelector('[data-field="first_weight"]').placeholder = starts[index] ? weight(starts[index]) : "";
    });
  }
  function scheduleExamples() {
    window.clearTimeout(exampleTimer);
    exampleTimer = window.setTimeout(drawExamples, 350);
  }
  async function drawExamples() {
    const cells = $$("[data-db-seg-example]", segmentBody);
    const segments = readSegments();
    const blank = segments.some((segment) => Object.values(segment).some((value) => !value));
    const seq = ++exampleSeq;
    if (blank) {
      cells.forEach((cell) => { cell.textContent = "—"; });
      segCheck.textContent = "";
      return;
    }
    try {
      const result = await request("/finance/delivery-bills/rules/trial", { method: "POST",
        body: { rule: { segments, rounding: ruleForm.querySelector("[name=rounding]").value } } });
      if (seq !== exampleSeq) return;
      cells.forEach((cell, index) => { cell.textContent = exampleText(result.examples?.[index]); });
      segCheck.textContent = "";
    } catch (error) {
      if (seq !== exampleSeq) return;
      cells.forEach((cell) => { cell.textContent = "—"; });
      segCheck.textContent = error.message;
    }
  }
  segmentBody.addEventListener("input", (event) => {
    editorTouched = true;
    if (event.target.dataset.field === "to") refreshStarts();
    scheduleExamples();
  });
  segmentBody.addEventListener("click", (event) => {
    const button = event.target.closest("[data-db-segment-remove]");
    if (!button) return;
    editorTouched = true;
    const rows = readRows();
    rows.splice($$("[data-db-segment]", segmentBody).indexOf(button.closest("[data-db-segment]")), 1);
    drawRows(rows);
  });
  $("[data-db-segment-add]").addEventListener("click", () => {
    editorTouched = true;
    drawRows([...readRows(), {}]);
    $$('[data-db-segment] [data-field="to"]', segmentBody).at(-1)?.focus();
  });
  ruleForm.querySelector("[name=rounding]").addEventListener("change", () => { editorTouched = true; scheduleExamples(); });
  drawRows([]);

  $("[data-db-rule-trial]").addEventListener("click", async () => {
    const data = Object.fromEntries(new FormData(ruleForm).entries());
    const out = $("[data-db-rule-trial-result]");
    ruleError("");
    if (!data.trial_weight?.trim()) { out.textContent = "请先填结算重量。"; return; }
    out.textContent = "计算中…";
    try {
      const result = await request("/finance/delivery-bills/rules/trial", { method: "POST",
        body: { rule: { segments: readSegments(), rounding: data.rounding }, settlement_weight: data.trial_weight.trim() } });
      out.innerHTML = `<b>派送费 ${esc(money(result.amount))} 元</b><details class="db-steps-box"><summary>怎么算的</summary>
        <ol class="db-steps">${result.steps.map((step) => `<li>${esc(step)}</li>`).join("")}</ol></details>`;
    } catch (error) { out.textContent = ""; ruleError(error.message); }
  });
  ruleForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!superAdmin) return;
    const data = Object.fromEntries(new FormData(ruleForm).entries());
    ruleError("");
    if (!data.effective_from) { ruleError("请先填执行日期。"); return; }
    if (!window.confirm(`保存 ${data.effective_from} 起执行的派送费规则？到货表日期在这天及以后、还没确认的单都按这一版算。`)) return;
    try {
      await request("/finance/delivery-bills/rules", { method: "POST", write: true,
        body: { segments: readSegments(), rounding: data.rounding, effective_from: data.effective_from, note: data.note?.trim() || null } });
      ruleForm.reset();
      editorTouched = false;
      $("[data-db-rule-trial-result]").textContent = "";
      await loadRules();
      say("派送费规则已保存，请重新查询账单。");
    } catch (error) { ruleError(error.message); }
  });
  }

  // -- 设置（超级管理员） ---------------------------------------------------------------------------------
  if (els.settings) {
    const form = $("[data-db-settings-form]", els.settings);
    const error = $("[data-db-settings-error]", els.settings);
    let revision = null;
    $("[data-db-open-settings]").addEventListener("click", async () => {
      error.hidden = true;
      els.settings.showModal();
      try {
        const data = await request("/finance/delivery-bills/settings");
        revision = data.revision;
        form.lookahead_days.value = data.lookahead_days;
        form.include_types.value = data.include_types.join("\n");
        form.exclude_types.value = data.exclude_types.join("\n");
        form.value_added_types.value = data.value_added_types.join("\n");
        form.no_fee_methods.value = data.no_fee_methods.join("\n");
      } catch (failure) { error.hidden = false; error.textContent = failure.message; }
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const lines = (value) => value.split("\n").map((item) => item.trim()).filter(Boolean);
      const days = form.lookahead_days.value.trim();
      try {
        await request("/finance/delivery-bills/settings", { method: "POST", write: true, body: {
          lookahead_days: /^\d+$/.test(days) ? Number(days) : days, include_types: lines(form.include_types.value),
          exclude_types: lines(form.exclude_types.value), value_added_types: lines(form.value_added_types.value),
          no_fee_methods: lines(form.no_fee_methods.value), expected_revision: revision } });
        els.settings.close();
        say("设置已保存，请重新查询账单。");
      } catch (failure) { error.hidden = false; error.textContent = failure.message; }
    });
  }

  // -- export ----------------------------------------------------------------------------------------------
  els.exportBtn.addEventListener("click", () => {
    const rows = visibleRows();
    const header = ["单号", "到货表日期", "货物", "配送方式", "件数", "结算重量", "融辉结算金额", "派送费", "增值费", "其他收入", "应付合计", "差额", "状态", "确认人"];
    const cells = rows.map((row) => [row.tracking_number, row.arrival_date, row.goods_name, row.delivery_method, row.pieces ?? "",
      weight(row.settlement_weight), money(row.ronghui_amount), row.delivery_fee == null ? "" : money(row.delivery_fee),
      money(row.value_added_total), money(row.other_income), row.payable_total == null ? "" : money(row.payable_total),
      row.difference == null ? "" : money(row.difference), STATUS[row.status], row.confirmed_by || ""]);
    const csv = [header, ...cells].map((line) => line.map((value) => `"${String(value ?? "").replace(/"/g, '""')}"`).join(",")).join("\r\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
    link.download = `派件账单_${bill.start}_${bill.end}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });
})();
