(() => {
  "use strict";
  const main = document.querySelector(".main-content:not([hidden])") || document;
  const root = main.querySelector("[data-finance-ledger]");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  const fetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
  const canManage = root.dataset.canManage === "true";
  const superAdmin = root.dataset.superAdmin === "true";
  const status = root.querySelector("[data-finance-status]");
  const $ = selector => root.querySelector(selector);
  const STATUS_TEXT = {draft: "草稿", submitted: "已提交", cancelled: "已冲销"};
  const TYPE_TEXT = {manual: "记账", opening: "期初", business_doc: "业务单据", source_line: "来源流水", settlement: "核销", reversal: "冲销", closing: "结转"};
  const ROOT_TEXT = {asset: "资产", liability: "负债", equity: "所有者权益", cost: "成本", profit_loss: "损益"};
  const ACCOUNT_TYPE_TEXT = {cash: "现金", bank: "银行", fund: "其他货币资金", transit: "资金在途", receivable: "应收类", payable: "应付类", suspense: "暂记", income: "收入", cost: "成本", expense: "费用", equity: "权益", other: "其他"};
  let accounts = [];
  let settings = null;

  // Amounts stay decimal strings; arithmetic uses integer ten-thousandths.
  function units(text) {
    const value = String(text ?? "").trim().replace(/,/g, "");
    const match = /^(\d+)(?:\.(\d{0,4}))?$/.exec(value);
    if (!match) return null;
    return BigInt(match[1]) * 10000n + BigInt((match[2] || "").padEnd(4, "0"));
  }
  function money(text) {
    if (text === null || text === undefined || text === "") return "";
    const negative = String(text).startsWith("-");
    let value = units(String(text).replace(/^-/, ""));
    if (value === null) return String(text);
    value = (value + 50n) / 100n;
    const whole = (value / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return `${negative ? "-" : ""}${whole}.${(value % 100n).toString().padStart(2, "0")}`;
  }
  const zeroBlank = text => (units(text) === 0n ? "" : money(text));
  function cell(row, text, className) {
    const item = document.createElement("td");
    item.textContent = text ?? "";
    if (className) item.className = className;
    row.append(item);
    return item;
  }
  function empty(body, columns, text) {
    const row = document.createElement("tr");
    const item = cell(row, text, "finance-empty");
    item.colSpan = columns;
    body.replaceChildren(row);
  }
  function say(text, target = status) { target.textContent = text || ""; }
  // Local calendar date (the ledger is kept in China time, not UTC).
  const today = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  };
  const thisMonth = () => today().slice(0, 7);

  async function get(url) {
    const response = await fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) throw new Error(payload.message || "读取失败，请稍后重试。");
    return payload.data;
  }
  async function send(url, body) {
    const requestId = crypto.randomUUID();
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "X-Browser-Request-UUID": requestId},
      body: JSON.stringify({...body, request_id: requestId}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) throw new Error(payload.message || payload.error?.message || "操作未完成，请刷新后核对。");
    return payload.data;
  }

  async function loadAccounts(force = false) {
    if (!accounts.length || force) accounts = await get("/finance/accounts");
    return accounts;
  }
  const postable = () => accounts.filter(item => !item.is_group && item.is_active && !item.frozen);
  const accountLabel = item => `${item.code} ${item.name}`;

  // ---------- tabs ----------
  const loaders = {};
  const loaded = new Set();
  const tabs = [...root.querySelectorAll("[role=tab]")];
  function activate(tab) {
    tabs.forEach(item => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      root.querySelector(`[data-panel="${item.dataset.tab}"]`).hidden = !selected;
    });
    say("");
    if (!loaded.has(tab.dataset.tab)) {
      loaded.add(tab.dataset.tab);
      loaders[tab.dataset.tab]?.().catch(error => say(error.message));
    }
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activate(tab));
    tab.addEventListener("keydown", event => {
      const step = {ArrowRight: 1, ArrowLeft: -1}[event.key];
      if (!step) return;
      const next = tabs[(index + step + tabs.length) % tabs.length];
      next.focus();
      activate(next);
    });
  });

  // ---------- vouchers ----------
  const voucherMonth = $("[data-voucher-month]");
  const voucherStatus = $("[data-voucher-status]");
  const voucherRows = $("[data-voucher-rows]");
  voucherMonth.value = thisMonth();
  loaders.vouchers = async () => {
    empty(voucherRows, 8, "正在读取…");
    const params = new URLSearchParams({month: voucherMonth.value || thisMonth()});
    if (voucherStatus.value) params.set("status", voucherStatus.value);
    const rows = await get(`/finance/vouchers?${params}`);
    if (!rows.length) { empty(voucherRows, 8, "本月没有凭证。"); return; }
    voucherRows.replaceChildren(...rows.map(voucher => {
      const row = document.createElement("tr");
      const link = document.createElement("button");
      link.type = "button";
      link.className = "finance-link";
      link.textContent = voucher.voucher_no || `草稿 #${voucher.id}`;
      link.addEventListener("click", () => openVoucher(voucher.id).catch(error => say(error.message)));
      cell(row, "").append(link);
      cell(row, voucher.posting_date);
      cell(row, TYPE_TEXT[voucher.voucher_type] || voucher.voucher_type);
      cell(row, voucher.summary);
      cell(row, money(voucher.total_debit), "num");
      cell(row, money(voucher.total_credit), "num");
      cell(row, STATUS_TEXT[voucher.status], `finance-state finance-state--${voucher.status}`);
      cell(row, voucher.created_by);
      return row;
    }));
  };
  $("[data-voucher-refresh]").addEventListener("click", () => loaders.vouchers().catch(error => say(error.message)));

  const dialog = main.querySelector("[data-voucher-dialog]");
  const form = dialog.querySelector("[data-voucher-form]");
  const lineRows = dialog.querySelector("[data-line-rows]");
  const voucherMessage = dialog.querySelector("[data-voucher-message]");
  const actions = dialog.querySelector("[data-voucher-actions]");
  let current = null;
  dialog.querySelectorAll("[data-dialog-close]").forEach(button => button.addEventListener("click", () => dialog.close()));

  function addLine(line = {}, editable = true) {
    const row = document.createElement("tr");
    const input = (name, value, attrs = {}) => {
      const field = document.createElement("input");
      field.className = "review-input";
      field.name = name;
      field.value = value || "";
      field.disabled = !editable;
      Object.assign(field, attrs);
      field.addEventListener("input", updateTotals);
      return field;
    };
    const summary = input("summary", line.summary, {maxLength: 255});
    summary.setAttribute("aria-label", "分录摘要");
    const account = document.createElement("select");
    account.className = "review-input";
    account.name = "account_code";
    account.disabled = !editable;
    account.setAttribute("aria-label", "科目");
    account.add(new Option("选择科目", ""));
    const options = editable ? postable() : accounts.filter(item => item.code === line.account_code);
    options.forEach(item => account.add(new Option(accountLabel(item), item.code)));
    if (line.account_code && ![...account.options].some(option => option.value === line.account_code)) {
      account.add(new Option(`${line.account_code} ${line.account_name || ""}`.trim(), line.account_code));
    }
    account.value = line.account_code || "";
    const waybill = input("waybill_no", line.waybill_no, {maxLength: 128});
    waybill.setAttribute("aria-label", "运单号");
    const debit = input("debit", units(line.debit) ? money(line.debit).replace(/,/g, "") : "", {inputMode: "decimal"});
    debit.setAttribute("aria-label", "借方金额");
    const credit = input("credit", units(line.credit) ? money(line.credit).replace(/,/g, "") : "", {inputMode: "decimal"});
    credit.setAttribute("aria-label", "贷方金额");
    [summary, account, waybill].forEach(field => cell(row, "").append(field));
    cell(row, "", "num").append(debit);
    cell(row, "", "num").append(credit);
    const remove = cell(row, "");
    if (editable) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ghost-btn ghost-btn--danger";
      button.textContent = "删除";
      button.addEventListener("click", () => { row.remove(); updateTotals(); });
      remove.append(button);
    }
    lineRows.append(row);
  }
  function collectLines() {
    return [...lineRows.querySelectorAll("tr")].map(row => {
      const value = name => row.querySelector(`[name=${name}]`).value.trim();
      return {summary: value("summary"), account_code: value("account_code"), waybill_no: value("waybill_no") || null,
        debit: value("debit"), credit: value("credit")};
    });
  }
  function updateTotals() {
    let debit = 0n; let credit = 0n;
    for (const line of collectLines()) {
      debit += units(line.debit) ?? 0n;
      credit += units(line.credit) ?? 0n;
    }
    const text = value => money(`${value / 10000n}.${(value % 10000n).toString().padStart(4, "0")}`);
    dialog.querySelector("[data-line-debit]").textContent = text(debit);
    dialog.querySelector("[data-line-credit]").textContent = text(credit);
    dialog.querySelector("[data-line-credit]").classList.toggle("finance-unbalanced", debit !== credit);
  }
  dialog.querySelector("[data-line-add]").addEventListener("click", () => { addLine({summary: form.summary.value}); updateTotals(); });

  function button(text, handler, className = "ghost-btn") {
    const item = document.createElement("button");
    item.type = "button";
    item.className = className;
    item.textContent = text;
    item.addEventListener("click", async () => {
      item.disabled = true;
      try { await handler(); } catch (error) { say(error.message, voucherMessage); } finally { item.disabled = false; }
    });
    return item;
  }
  const draftBody = () => ({voucher_type: form.voucher_type.value, posting_date: form.posting_date.value,
    summary: form.summary.value.trim(), lines: collectLines()});

  async function afterChange(message, voucher) {
    loaded.delete("trial"); loaded.delete("ledger"); loaded.delete("journal");
    await loaders.vouchers();
    if (voucher) await showVoucher(voucher); else dialog.close();
    say(message, voucher ? voucherMessage : status);
  }

  async function showVoucher(voucher) {
    current = voucher;
    const editable = canManage && (!voucher || voucher.status === "draft");
    dialog.querySelector("[data-voucher-title]").textContent = voucher
      ? `${voucher.voucher_no || `草稿 #${voucher.id}`} · ${STATUS_TEXT[voucher.status]}` : "新建凭证";
    form.posting_date.value = voucher?.posting_date || today();
    form.voucher_type.value = voucher?.voucher_type === "opening" ? "opening" : "manual";
    form.summary.value = voucher?.summary || "";
    [form.posting_date, form.voucher_type, form.summary].forEach(field => { field.disabled = !editable; });
    dialog.querySelector("[data-line-add]").hidden = !editable;
    lineRows.replaceChildren();
    (voucher?.lines?.length ? voucher.lines : [{}, {}]).forEach(line => addLine(line, editable));
    updateTotals();
    say(voucher?.status === "cancelled" ? `已于 ${voucher.cancelled_at} 由 ${voucher.cancelled_by} 冲销：${voucher.cancel_reason}` : "", voucherMessage);
    actions.replaceChildren();
    if (!voucher && canManage) {
      actions.append(button("保存草稿", async () => afterChange("草稿已保存。", await send("/finance/vouchers", draftBody())), "primary-btn"));
    } else if (voucher?.status === "draft" && canManage) {
      const revision = () => ({expected_revision: current.revision});
      actions.append(
        button("删除草稿", async () => {
          if (!window.confirm("确定删除这张草稿凭证？")) return;
          await send(`/finance/vouchers/${voucher.id}/delete`, revision());
          await afterChange("草稿已删除。", null);
        }, "ghost-btn ghost-btn--danger"),
        button("保存草稿", async () => afterChange("草稿已保存。", await send(`/finance/vouchers/${voucher.id}`, {...draftBody(), ...revision()}))),
        button("保存并提交", async () => {
          const saved = await send(`/finance/vouchers/${voucher.id}`, {...draftBody(), ...revision()});
          current = saved;
          await afterChange("凭证已提交入账。", await send(`/finance/vouchers/${voucher.id}/submit`, {expected_revision: saved.revision}));
        }, "primary-btn"),
      );
    } else if (voucher?.status === "submitted" && superAdmin) {
      actions.append(button("冲销凭证", async () => {
        const reason = window.prompt("冲销原因（必填）：");
        if (!reason || !reason.trim()) return;
        await afterChange("凭证已冲销。", await send(`/finance/vouchers/${voucher.id}/cancel`, {expected_revision: voucher.revision, reason: reason.trim()}));
      }, "ghost-btn ghost-btn--danger"));
    }
    if (!dialog.open) dialog.showModal();
  }
  async function openVoucher(id) {
    await loadAccounts();
    await showVoucher(await get(`/finance/vouchers/${id}`));
  }
  $("[data-voucher-new]")?.addEventListener("click", async () => {
    try { await loadAccounts(); await showVoucher(null); } catch (error) { say(error.message); }
  });

  // ---------- trial balance ----------
  const trialMonth = $("[data-trial-month]");
  trialMonth.value = thisMonth();
  loaders.trial = async () => {
    const body = $("[data-trial-rows]");
    empty(body, 7, "正在读取…");
    const params = new URLSearchParams({month: trialMonth.value || thisMonth()});
    if ($("[data-trial-zero]").checked) params.set("include_zero", "1");
    const data = await get(`/finance/reports/trial-balance?${params}`);
    const badge = $("[data-trial-balanced]");
    badge.hidden = false;
    badge.textContent = data.balanced ? "试算平衡" : "试算不平衡";
    badge.className = `finance-badge ${data.balanced ? "finance-badge--ok" : "finance-badge--bad"}`;
    if (!data.rows.length) empty(body, 7, "本月没有发生额，也没有余额。");
    else body.replaceChildren(...data.rows.map(item => {
      const row = document.createElement("tr");
      row.className = item.is_group ? "finance-row--group" : "";
      const name = cell(row, `${item.code} ${item.name}`);
      name.style.paddingInlineStart = `${12 + item.level * 16}px`;
      ["opening_debit", "opening_credit", "period_debit", "period_credit", "closing_debit", "closing_credit"]
        .forEach(key => cell(row, zeroBlank(item[key]), "num"));
      return row;
    }));
    const foot = document.createElement("tr");
    cell(foot, "合计");
    ["opening_debit", "opening_credit", "period_debit", "period_credit", "closing_debit", "closing_credit"]
      .forEach(key => cell(foot, money(data.totals[key]), "num"));
    $("[data-trial-totals]").replaceChildren(foot);
  };
  $("[data-trial-refresh]").addEventListener("click", () => loaders.trial().catch(error => say(error.message)));

  // ---------- account ledger ----------
  const ledgerAccount = $("[data-ledger-account]");
  $("[data-ledger-start]").value = `${thisMonth()}-01`;
  $("[data-ledger-end]").value = today();
  loaders.ledger = async () => {
    await loadAccounts();
    if (!ledgerAccount.options.length) {
      accounts.filter(item => item.is_active).forEach(item => ledgerAccount.add(new Option(`${"　".repeat((item.code.length - 4) / 2)}${accountLabel(item)}`, item.code)));
      ledgerAccount.value = "1001";
    }
    const body = $("[data-ledger-rows]");
    empty(body, 9, "正在读取…");
    const params = new URLSearchParams({account: ledgerAccount.value, start: $("[data-ledger-start]").value, end: $("[data-ledger-end]").value});
    const data = await get(`/finance/reports/ledger?${params}`);
    const opening = document.createElement("tr");
    opening.className = "finance-row--group";
    cell(opening, data.start_date); cell(opening, ""); cell(opening, ""); cell(opening, "期初余额"); cell(opening, "");
    cell(opening, "", "num"); cell(opening, "", "num"); cell(opening, data.opening.direction); cell(opening, money(data.opening.balance), "num");
    const rows = data.rows.map(item => {
      const row = document.createElement("tr");
      cell(row, item.posting_date);
      const link = document.createElement("button");
      link.type = "button"; link.className = "finance-link"; link.textContent = item.voucher_no;
      link.addEventListener("click", () => openVoucher(item.voucher_id).catch(error => say(error.message)));
      cell(row, "").append(link);
      cell(row, `${item.account_code} ${item.account_name}`); cell(row, item.summary); cell(row, item.waybill_no || "");
      cell(row, zeroBlank(item.debit), "num"); cell(row, zeroBlank(item.credit), "num");
      cell(row, item.direction); cell(row, money(item.balance), "num");
      return row;
    });
    const closing = document.createElement("tr");
    closing.className = "finance-row--group";
    cell(closing, data.end_date); cell(closing, ""); cell(closing, ""); cell(closing, "本期合计 / 期末余额"); cell(closing, "");
    cell(closing, money(data.totals.debit), "num"); cell(closing, money(data.totals.credit), "num");
    cell(closing, data.closing.direction); cell(closing, money(data.closing.balance), "num");
    body.replaceChildren(opening, ...rows, closing);
  };
  $("[data-ledger-refresh]").addEventListener("click", () => loaders.ledger().catch(error => say(error.message)));

  // ---------- journal ----------
  const journalMonth = $("[data-journal-month]");
  journalMonth.value = thisMonth();
  loaders.journal = async () => {
    const body = $("[data-journal-rows]");
    empty(body, 7, "正在读取…");
    const data = await get(`/finance/reports/journal?month=${encodeURIComponent(journalMonth.value || thisMonth())}`);
    if (!data.vouchers.length) { empty(body, 7, "本月没有已提交的凭证。"); return; }
    const rows = [];
    for (const voucher of data.vouchers) {
      voucher.lines.forEach((line, index) => {
        const row = document.createElement("tr");
        if (index === 0) row.className = "finance-row--first";
        if (voucher.status === "cancelled") row.classList.add("finance-row--cancelled");
        cell(row, index ? "" : voucher.posting_date);
        const no = cell(row, "");
        if (!index) {
          const link = document.createElement("button");
          link.type = "button"; link.className = "finance-link"; link.textContent = voucher.voucher_no;
          link.addEventListener("click", () => openVoucher(voucher.id).catch(error => say(error.message)));
          no.append(link);
        }
        cell(row, line.summary); cell(row, `${line.account_code} ${line.account_name || ""}`);
        cell(row, zeroBlank(line.debit), "num"); cell(row, zeroBlank(line.credit), "num");
        cell(row, index ? "" : STATUS_TEXT[voucher.status]);
        rows.push(row);
      });
    }
    body.replaceChildren(...rows);
  };
  $("[data-journal-refresh]").addEventListener("click", () => loaders.journal().catch(error => say(error.message)));

  // ---------- accounts ----------
  const accountDialog = main.querySelector("[data-account-dialog]");
  const accountForm = accountDialog.querySelector("[data-account-form]");
  const accountMessage = accountDialog.querySelector("[data-account-message]");
  accountDialog.querySelectorAll("[data-dialog-close]").forEach(item => item.addEventListener("click", () => accountDialog.close()));
  loaders.accounts = async () => {
    const body = $("[data-account-rows]");
    const columns = superAdmin ? 7 : 6;
    empty(body, columns, "正在读取…");
    await loadAccounts(true);
    body.replaceChildren(...accounts.map(item => {
      const row = document.createElement("tr");
      if (item.is_group) row.className = "finance-row--group";
      cell(row, item.code);
      const name = cell(row, item.name);
      name.style.paddingInlineStart = `${12 + (item.code.length - 4) * 8}px`;
      cell(row, ROOT_TEXT[item.root_type]); cell(row, ACCOUNT_TYPE_TEXT[item.account_type]);
      cell(row, item.normal_balance === "debit" ? "借" : "贷");
      cell(row, [!item.is_active && "已停用", item.frozen && "已冻结", item.is_group && "有下级", item.requires_waybill && "需运单号"].filter(Boolean).join(" · ") || "可记账");
      if (superAdmin) {
        const edit = document.createElement("button");
        edit.type = "button"; edit.className = "ghost-btn"; edit.textContent = "编辑";
        edit.addEventListener("click", () => openAccount(item));
        cell(row, "").append(edit);
      }
      return row;
    }));
  };
  function openAccount(item) {
    const editing = Boolean(item);
    accountDialog.querySelector("[data-account-title]").textContent = editing ? `编辑科目 ${item.code}` : "新增科目";
    accountForm.reset();
    accountForm.code.value = item?.code || "";
    accountForm.code.disabled = editing;
    accountForm.name.value = item?.name || "";
    accountForm.account_type.value = item?.account_type || "";
    accountForm.requires_waybill.checked = Boolean(item?.requires_waybill);
    accountForm.is_active.checked = item ? item.is_active : true;
    accountForm.frozen.checked = Boolean(item?.frozen);
    accountDialog.querySelectorAll("[data-edit-only]").forEach(node => { node.hidden = !editing; });
    const syncTopOnly = () => accountDialog.querySelectorAll("[data-top-only]").forEach(node => {
      node.hidden = editing || accountForm.code.value.trim().length > 4;
    });
    accountForm.code.oninput = syncTopOnly;
    syncTopOnly();
    say("", accountMessage);
    const actionsBox = accountDialog.querySelector("[data-account-actions]");
    actionsBox.replaceChildren();
    const run = async (work, message) => {
      try { await work(); accountDialog.close(); accounts = []; await loaders.accounts(); say(message); }
      catch (error) { say(error.message, accountMessage); }
    };
    if (editing) {
      if (!item.has_entries && !item.is_group) {
        actionsBox.append(button("删除", () => run(async () => {
          if (!window.confirm(`确定删除科目 ${item.code}？`)) throw new Error("已取消。");
          await send(`/finance/accounts/${item.code}/delete`, {expected_revision: item.revision});
        }, "科目已删除。"), "ghost-btn ghost-btn--danger"));
      }
      actionsBox.append(button("保存", () => run(() => {
        const body = {expected_revision: item.revision, name: accountForm.name.value.trim(), is_active: accountForm.is_active.checked,
          frozen: accountForm.frozen.checked, requires_waybill: accountForm.requires_waybill.checked};
        if (accountForm.account_type.value && accountForm.account_type.value !== item.account_type) body.account_type = accountForm.account_type.value;
        return send(`/finance/accounts/${item.code}`, body);
      }, "科目已保存。"), "primary-btn"));
    } else {
      actionsBox.append(button("新增", () => run(() => {
        const code = accountForm.code.value.trim();
        const body = {code, name: accountForm.name.value.trim(), requires_waybill: accountForm.requires_waybill.checked};
        if (accountForm.account_type.value) body.account_type = accountForm.account_type.value;
        if (code.length === 4) Object.assign(body, {root_type: accountForm.root_type.value, normal_balance: accountForm.normal_balance.value});
        return send("/finance/accounts", body);
      }, "科目已新增。"), "primary-btn"));
    }
    accountDialog.showModal();
  }
  $("[data-account-new]")?.addEventListener("click", () => openAccount(null));

  // ---------- settings ----------
  const settingsForm = $("[data-settings-form]");
  loaders.settings = async () => {
    await loadAccounts(true);
    settings = await get("/finance/settings");
    settingsForm.start_month.value = settings.start_month || "";
    settingsForm.start_month.disabled = !superAdmin || settings.start_month_locked;
    settingsForm.querySelector("[data-settings-lock]").textContent = settings.start_month_locked
      ? "总账已有分录，启用月已锁定。" : "启用月之前的日期不能记账；期初凭证只能记在启用月。一旦有分录即锁定。";
    settingsForm.querySelectorAll("select[data-account-type]").forEach(select => {
      select.replaceChildren(...postable().filter(item => item.account_type === select.dataset.accountType)
        .map(item => new Option(accountLabel(item), item.code)));
      select.value = settings[select.name];
      select.disabled = !superAdmin;
    });
  };
  settingsForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (!superAdmin || !settings) return;
    const body = {expected_revision: settings.revision};
    if (!settings.start_month_locked && settingsForm.start_month.value) body.start_month = settingsForm.start_month.value;
    settingsForm.querySelectorAll("select[data-account-type]").forEach(select => {
      if (select.value && select.value !== settings[select.name]) body[select.name] = select.value;
    });
    if (Object.keys(body).length === 1 || (body.start_month === settings.start_month && Object.keys(body).length === 2)) { say("没有修改。"); return; }
    try { settings = null; await send("/finance/settings", body); await loaders.settings(); say("财务设置已保存。"); }
    catch (error) { say(error.message); await loaders.settings().catch(() => {}); }
  });

  activate(tabs[0]);
})();
