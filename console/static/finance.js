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
  let parties = null;
  async function loadParties(force = false) {
    if (!parties || force) parties = await get("/finance/parties?include_inactive=1");
    return parties;
  }
  const PARTY_TYPE_TEXT = {customer: "客户", platform: "平台", sender: "寄件人", supplier: "供应商", employee: "员工", other: "其他"};
  const partyLabel = item => `${item.name}（${PARTY_TYPE_TEXT[item.party_type] || item.party_type}）${item.is_active ? "" : " · 已停用"}`;
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
    const partySelect = document.createElement("select");
    partySelect.className = "review-input";
    partySelect.name = "party_id";
    partySelect.disabled = !editable;
    partySelect.setAttribute("aria-label", "往来单位");
    partySelect.add(new Option("—", ""));
    (parties || []).filter(item => item.is_active || item.id === line.party_id)
      .forEach(item => partySelect.add(new Option(partyLabel(item), String(item.id))));
    if (line.party_id && ![...partySelect.options].some(option => option.value === String(line.party_id))) {
      partySelect.add(new Option(line.party_name || `#${line.party_id}`, String(line.party_id)));
    }
    partySelect.value = line.party_id ? String(line.party_id) : "";
    const waybill = input("waybill_no", line.waybill_no, {maxLength: 128});
    waybill.setAttribute("aria-label", "运单号");
    const debit = input("debit", units(line.debit) ? money(line.debit).replace(/,/g, "") : "", {inputMode: "decimal"});
    debit.setAttribute("aria-label", "借方金额");
    const credit = input("credit", units(line.credit) ? money(line.credit).replace(/,/g, "") : "", {inputMode: "decimal"});
    credit.setAttribute("aria-label", "贷方金额");
    [summary, account, partySelect, waybill].forEach(field => cell(row, "").append(field));
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
      const party = value("party_id");
      return {summary: value("summary"), account_code: value("account_code"), party_id: party ? Number(party) : null,
        waybill_no: value("waybill_no") || null, debit: value("debit"), credit: value("credit")};
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

  function button(text, handler, className = "ghost-btn", target = voucherMessage) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = className;
    item.textContent = text;
    item.addEventListener("click", async () => {
      item.disabled = true;
      try { await handler(); } catch (error) { say(error.message, target); } finally { item.disabled = false; }
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
    await Promise.all([loadAccounts(), loadParties()]);
    await showVoucher(await get(`/finance/vouchers/${id}`));
  }
  $("[data-voucher-new]")?.addEventListener("click", async () => {
    try { await Promise.all([loadAccounts(), loadParties()]); await showVoucher(null); } catch (error) { say(error.message); }
  });


  // ---------- Boyi waybill confirmation ----------
  const waybillMonth = $("[data-waybill-month]");
  const waybillStatus = $("[data-waybill-status]");
  const waybillRows = $("[data-waybill-rows]");
  waybillMonth.value = thisMonth();
  const WAYBILL_COLUMNS = canManage ? 11 : 9;
  const feeText = value => (String(value ?? "").trim() === "" ? "未填" : money(String(value).trim()));
  async function confirmItems(items) {
    const result = await send("/finance/waybills/confirm-batch", {items});
    const failed = result.results.filter(item => !item.ok);
    loaded.delete("vouchers"); loaded.delete("trial"); loaded.delete("journal"); loaded.delete("parties");
    await loaders.waybills();
    say(failed.length
      ? `确认成功 ${result.succeeded} 张，失败 ${failed.length} 张：${failed.slice(0, 5).map(item => `${item.waybill_id} ${item.message}`).join("；")}`
      : `确认成功 ${result.succeeded} 张。`);
  }
  loaders.waybills = async () => {
    await loadParties();
    empty(waybillRows, WAYBILL_COLUMNS, "正在读取…");
    const params = new URLSearchParams({month: waybillMonth.value || thisMonth()});
    if (waybillStatus.value) params.set("status", waybillStatus.value);
    const data = await get(`/finance/waybills?${params}`);
    const counts = data.counts;
    $("[data-waybill-counts]").textContent = `待确认 ${counts.pending} · 待重新确认 ${counts.changed} · 待冲销 ${counts.cancelled_posted} · 已确认 ${counts.confirmed}`;
    if (!data.rows.length) { empty(waybillRows, WAYBILL_COLUMNS, "没有符合条件的博益开单。"); return; }
    waybillRows.replaceChildren(...data.rows.map(item => {
      const row = document.createElement("tr");
      row.dataset.waybillId = item.waybill_id;
      row.dataset.fingerprint = item.fingerprint;
      const actionable = ["pending", "changed", "cancelled_posted"].includes(item.status);
      if (canManage) {
        const pick = document.createElement("input");
        pick.type = "checkbox"; pick.dataset.pick = ""; pick.disabled = !actionable || item.problems.length > 0;
        pick.setAttribute("aria-label", `选择 ${item.waybill_no}`);
        cell(row, "").append(pick);
      }
      cell(row, item.open_date); cell(row, item.waybill_no); cell(row, item.sender_name || "");
      cell(row, item.payment_method || "未填");
      cell(row, feeText(item.freight_fee), "num"); cell(row, feeText(item.transfer_fee), "num"); cell(row, money(item.total), "num");
      const partyCell = cell(row, "");
      if (canManage && actionable && item.status !== "cancelled_posted") {
        const select = document.createElement("select");
        select.className = "review-input"; select.dataset.party = "";
        select.setAttribute("aria-label", `${item.waybill_no} 的往来单位`);
        select.add(new Option(item.suggested_party_name ? `按规则：${item.suggested_party_name}` : "请选择", ""));
        parties.filter(entry => entry.is_active).forEach(entry => select.add(new Option(partyLabel(entry), String(entry.id))));
        partyCell.append(select);
      } else {
        partyCell.textContent = item.posting?.party_id ? (parties.find(entry => entry.id === item.posting.party_id)?.name || "") : (item.suggested_party_name || "");
      }
      const state = cell(row, item.status_text, `finance-state finance-state--${item.status}`);
      if (item.problems.length) {
        const note = document.createElement("div");
        note.className = "finance-problem"; note.textContent = item.problems.join("；");
        state.append(note);
      }
      if (item.posting?.voucher_no) {
        const link = document.createElement("button");
        link.type = "button"; link.className = "finance-link"; link.textContent = item.posting.voucher_no;
        link.addEventListener("click", () => openVoucher(item.posting.voucher_id).catch(error => say(error.message)));
        state.append(document.createElement("br"), link);
      }
      if (canManage) {
        const actionsCell = cell(row, "");
        const itemPayload = () => {
          const chosen = row.querySelector("[data-party]")?.value;
          return {waybill_id: item.waybill_id, expected_fingerprint: item.fingerprint, ...(chosen ? {party_id: Number(chosen)} : {})};
        };
        if (actionable && !item.problems.length) {
          actionsCell.append(button(item.status === "cancelled_posted" ? "冲销" : "确认", () => confirmItems([itemPayload()]),
            item.status === "cancelled_posted" ? "ghost-btn ghost-btn--danger" : "ghost-btn", status));
        }
        if (item.source === "manual" && item.status !== "cancelled_posted" && item.status !== "reversed") {
          actionsCell.append(button("改金额", () => openAmount(item), "ghost-btn", status));
        }
      }
      return row;
    }));
  };
  $("[data-waybill-refresh]").addEventListener("click", () => loaders.waybills().catch(error => say(error.message)));
  waybillStatus.addEventListener("change", () => loaders.waybills().catch(error => say(error.message)));
  $("[data-waybill-confirm-selected]")?.addEventListener("click", async () => {
    const picked = [...waybillRows.querySelectorAll("tr[data-waybill-id]")].filter(row => row.querySelector("[data-pick]")?.checked);
    if (!picked.length) { say("请勾选要确认的运单。"); return; }
    if (picked.length > 200) { say("一次最多确认 200 张。"); return; }
    try {
      await confirmItems(picked.map(row => {
        const chosen = row.querySelector("[data-party]")?.value;
        return {waybill_id: Number(row.dataset.waybillId), expected_fingerprint: row.dataset.fingerprint, ...(chosen ? {party_id: Number(chosen)} : {})};
      }));
    } catch (error) { say(error.message); }
  });
  const amountDialog = main.querySelector("[data-amount-dialog]");
  const amountForm = amountDialog.querySelector("[data-amount-form]");
  amountDialog.querySelectorAll("[data-dialog-close]").forEach(item => item.addEventListener("click", () => amountDialog.close()));
  async function openAmount(item) {
    const response = await fetch(`/waybills/${item.waybill_id}/edit-data?source=manual`, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}});
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.message || "运单读取失败。");
    if (!data.editable) throw new Error(data.reason || "运单不能修改。");
    amountDialog.querySelector("[data-amount-title]").textContent = `补录金额：${data.waybill_no}`;
    amountForm.freight_fee.value = data.values.field_freight_fee || "";
    amountForm.transfer_fee.value = data.values.field_transfer_fee || "";
    amountForm.payment_method.value = data.values.field_payment_method || "寄付";
    const message = amountDialog.querySelector("[data-amount-message]");
    say("", message);
    amountDialog.querySelector("[data-amount-actions]").replaceChildren(button("保存到运单", async () => {
      const fields = {...data.values, field_freight_fee: amountForm.freight_fee.value.trim(),
        field_transfer_fee: amountForm.transfer_fee.value.trim(), field_payment_method: amountForm.payment_method.value};
      const requestId = crypto.randomUUID();
      const saved = await fetch(`/waybills/${item.waybill_id}/manual-update`, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "X-Browser-Request-UUID": requestId},
        body: JSON.stringify({request_id: requestId, fingerprint: data.fingerprint, fields}),
      });
      const result = await saved.json().catch(() => ({}));
      if (!saved.ok || !result.ok) throw new Error(result.message || "运单保存失败。");
      amountDialog.close();
      await loaders.waybills();
      say(`${data.waybill_no}：${result.message || "金额已写回运单。"}请核对后确认记账。`);
    }, "primary-btn", message));
    amountDialog.showModal();
  }

  // ---------- doc posting rules (settings tab) ----------
  const RULE_KEYS = {"寄付": "prepaid", "到付": "collect", "月结": "monthly"};
  const PARTY_MODE_TEXT = {fixed: "固定往来单位", sender_customer: "按寄件人匹配月结客户", linked_platform: "按运单绑定的平台", choose: "确认时手选"};
  async function loadDocRules() {
    const body = $("[data-doc-rule-rows]");
    const rules = await get("/finance/doc-rules");
    await loadParties();
    const leafOptions = predicate => accounts.filter(item => !item.is_group && item.is_active && predicate(item));
    body.replaceChildren(...rules.map(rule => {
      const row = document.createElement("tr");
      cell(row, rule.payment_method);
      const select = (name, options, value) => {
        const field = document.createElement("select");
        field.className = "review-input"; field.name = name; field.disabled = !superAdmin;
        field.setAttribute("aria-label", `${rule.payment_method} ${name}`);
        options.forEach(([key, label]) => field.add(new Option(label, key)));
        field.value = value ?? "";
        cell(row, "").append(field);
        return field;
      };
      const receivable = select("receivable_account", leafOptions(item => item.account_type === "receivable" && item.requires_party).map(item => [item.code, accountLabel(item)]), rule.receivable_account);
      const freight = select("freight_income_account", leafOptions(item => item.account_type === "income").map(item => [item.code, accountLabel(item)]), rule.freight_income_account);
      const transfer = select("transfer_income_account", leafOptions(item => item.account_type === "income").map(item => [item.code, accountLabel(item)]), rule.transfer_income_account);
      const mode = select("party_mode", Object.entries(PARTY_MODE_TEXT), rule.party_mode);
      const fixed = select("fixed_party_id", [["", "—"], ...parties.filter(item => item.is_active).map(item => [String(item.id), partyLabel(item)])], rule.fixed_party_id ? String(rule.fixed_party_id) : "");
      if (superAdmin) {
        cell(row, "").append(button("保存", async () => {
          await send(`/finance/doc-rules/${RULE_KEYS[rule.payment_method]}`, {
            receivable_account: receivable.value, freight_income_account: freight.value, transfer_income_account: transfer.value,
            party_mode: mode.value, fixed_party_id: mode.value === "fixed" && fixed.value ? Number(fixed.value) : null,
            expected_revision: rule.revision,
          });
          await loadDocRules();
          say(`“${rule.payment_method}”记账规则已保存。`);
        }, "ghost-btn", status));
      }
      return row;
    }));
  }

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


  // ---------- parties & allocation ----------
  const partySelect = $("[data-party-select]");
  const partyAccount = $("[data-party-account]");
  const partyAsOf = $("[data-party-as-of]");
  const openRows = $("[data-open-rows]");
  const allocationRows = $("[data-allocation-rows]");
  const allocationDate = $("[data-allocation-date]");
  const allocationBalance = $("[data-allocation-balance]");
  partyAsOf.value = today();
  if (allocationDate) allocationDate.value = today();
  const signedUnits = text => {
    const negative = String(text).startsWith("-");
    const value = units(String(text).replace(/^-/, "")) ?? 0n;
    return negative ? -value : value;
  };
  const unitsText = value => {
    const negative = value < 0n;
    const abs = negative ? -value : value;
    return `${negative ? "-" : ""}${abs / 10000n}.${(abs % 10000n).toString().padStart(4, "0")}`;
  };
  function fillPartySelect(select, keep) {
    const current = keep ?? select.value;
    select.replaceChildren(new Option("选择往来单位", ""), ...(parties || []).map(item => new Option(partyLabel(item), String(item.id))));
    select.value = current || "";
  }
  async function preparePartyFilters(force = false) {
    await Promise.all([loadAccounts(force), loadParties(force)]);
    fillPartySelect(partySelect);
    fillPartySelect($("[data-statement-party]"));
    if (!partyAccount.options.length || force) {
      const keep = partyAccount.value;
      partyAccount.replaceChildren(new Option("全部往来科目", ""),
        ...accounts.filter(item => item.requires_party && !item.is_group).map(item => new Option(accountLabel(item), item.code)));
      partyAccount.value = keep;
    }
  }
  function updateAllocationBalance() {
    if (!allocationBalance) return;
    let positive = 0n; let negative = 0n;
    openRows.querySelectorAll("tr[data-item-id]").forEach(row => {
      if (!row.querySelector("[data-pick]").checked) return;
      const amount = units(row.querySelector("[data-amount]").value.trim()) ?? 0n;
      if (row.dataset.sign === "+") positive += amount; else negative += amount;
    });
    allocationBalance.textContent = `已选：应收/应付 ${money(unitsText(positive))} · 收款/付款 ${money(unitsText(negative))}`;
    allocationBalance.className = `finance-badge ${positive && positive === negative ? "finance-badge--ok" : ""}`;
  }
  loaders.parties = async () => {
    await preparePartyFilters();
    const partyId = partySelect.value;
    if (!partyId) {
      empty(openRows, canManage ? 9 : 7, "请选择往来单位。");
      allocationRows.replaceChildren();
      $("[data-open-totals]").replaceChildren();
      return;
    }
    empty(openRows, canManage ? 9 : 7, "正在读取…");
    const params = new URLSearchParams({party_id: partyId, as_of: partyAsOf.value || today()});
    if (partyAccount.value) params.set("account", partyAccount.value);
    const [data, allocations] = await Promise.all([
      get(`/finance/open-items?${params}`), get(`/finance/allocations?party_id=${encodeURIComponent(partyId)}`),
    ]);
    if (!data.items.length) empty(openRows, canManage ? 9 : 7, "没有未清项。");
    else openRows.replaceChildren(...data.items.map(item => {
      const row = document.createElement("tr");
      row.dataset.itemId = item.item_id;
      row.dataset.sign = item.outstanding.startsWith("-") ? "-" : "+";
      if (canManage) {
        const pick = document.createElement("input");
        pick.type = "checkbox"; pick.dataset.pick = "";
        pick.setAttribute("aria-label", `选择 ${item.posting_date} ${item.summary}`);
        pick.addEventListener("change", updateAllocationBalance);
        cell(row, "").append(pick);
      }
      cell(row, item.posting_date); cell(row, item.voucher_no || ""); cell(row, item.account_code);
      cell(row, item.waybill_no || ""); cell(row, item.summary);
      cell(row, money(item.amount), "num"); cell(row, money(item.outstanding), "num");
      if (canManage) {
        const amount = document.createElement("input");
        amount.className = "review-input"; amount.dataset.amount = ""; amount.inputMode = "decimal";
        amount.value = money(item.outstanding.replace(/^-/, "")).replace(/,/g, "");
        amount.setAttribute("aria-label", "本次核销金额");
        amount.addEventListener("input", updateAllocationBalance);
        cell(row, "", "num").append(amount);
      }
      return row;
    }));
    const foot = document.createElement("tr");
    const label = cell(foot, `未清合计：${Object.entries(data.totals).map(([code, value]) => `${code} ${money(value)}`).join("；") || "0.00"}`);
    label.colSpan = canManage ? 9 : 7;
    $("[data-open-totals]").replaceChildren(foot);
    updateAllocationBalance();
    if (!allocations.length) { empty(allocationRows, canManage ? 8 : 7, "没有核销记录。"); return; }
    allocationRows.replaceChildren(...allocations.map(item => {
      const row = document.createElement("tr");
      if (item.status === "reversed") row.className = "finance-row--cancelled";
      cell(row, item.allocation_date); cell(row, item.account_code);
      cell(row, {manual: "手工", auto_waybill: "按运单号", daily_aggregate: "按日汇总"}[item.method] || item.method);
      cell(row, money(item.total_amount), "num"); cell(row, item.remark); cell(row, item.created_by);
      cell(row, item.status === "active" ? "有效" : `已撤销：${item.reverse_reason || ""}`);
      if (canManage) {
        const actionsCell = cell(row, "");
        if (item.status === "active") actionsCell.append(button("撤销", async () => {
          const reason = window.prompt("撤销核销原因（必填）：");
          if (!reason || !reason.trim()) return;
          await send(`/finance/allocations/${item.id}/reverse`, {reason: reason.trim()});
          await loaders.parties();
          say("核销已撤销，未清项已恢复。");
        }, "ghost-btn ghost-btn--danger", status));
      }
      return row;
    }));
  };
  $("[data-party-refresh]").addEventListener("click", () => loaders.parties().catch(error => say(error.message)));
  partySelect.addEventListener("change", () => loaders.parties().catch(error => say(error.message)));
  $("[data-allocation-submit]")?.addEventListener("click", async () => {
    const picked = [...openRows.querySelectorAll("tr[data-item-id]")].filter(row => row.querySelector("[data-pick]").checked);
    const accountsPicked = new Set(picked.map(row => row.children[3].textContent));
    if (!picked.length) { say("请勾选要核销的款项。"); return; }
    if (accountsPicked.size !== 1) { say("一次核销只能选择同一科目的款项。"); return; }
    try {
      const result = await send("/finance/allocations", {
        party_id: Number(partySelect.value), account_code: [...accountsPicked][0], allocation_date: allocationDate.value,
        remark: $("[data-allocation-remark]").value.trim(),
        items: picked.map(row => ({item_id: Number(row.dataset.itemId), amount: row.querySelector("[data-amount]").value.trim()})),
      });
      await loaders.parties();
      say(`已核销 ${money(result.total_amount)}。`);
    } catch (error) { say(error.message); }
  });
  $("[data-allocation-daily]")?.addEventListener("click", async () => {
    if (!partySelect.value || !partyAccount.value) { say("请先选择往来单位和具体科目（例如寄付散客 · 112203 寄付待收）。"); return; }
    try {
      const result = await send("/finance/allocations/daily", {
        party_id: Number(partySelect.value), account_code: partyAccount.value, date: allocationDate.value,
      });
      await loaders.parties();
      say(result.allocation
        ? `${allocationDate.value} 应收 ${money(result.receivable)}，收款 ${money(result.received)}，已核销 ${money(result.allocation.total_amount)}，差额 ${money(result.difference)} 保留在未清项。`
        : `${allocationDate.value} 没有可配对的应收和收款（应收 ${money(result.receivable)}，收款 ${money(result.received)}）。`);
    } catch (error) { say(error.message); }
  });
  $("[data-allocation-auto]")?.addEventListener("click", async () => {
    if (!partySelect.value || !partyAccount.value) { say("请先选择往来单位和具体科目。"); return; }
    try {
      const result = await send("/finance/allocations/auto-waybill", {
        party_id: Number(partySelect.value), account_code: partyAccount.value, allocation_date: allocationDate.value,
      });
      await loaders.parties();
      say(result.count ? `按运单号自动核销了 ${result.count} 票。` : "没有可按运单号配对的款项。");
    } catch (error) { say(error.message); }
  });

  const partyDialog = main.querySelector("[data-party-dialog]");
  const partyForm = partyDialog.querySelector("[data-party-form]");
  partyDialog.querySelectorAll("[data-dialog-close]").forEach(item => item.addEventListener("click", () => partyDialog.close()));
  function openParty(item, preset = {}) {
    partyDialog.querySelector("[data-party-title]").textContent = item ? `编辑往来单位：${item.name}` : "新建往来单位";
    partyForm.reset();
    const source = item || preset;
    ["party_type", "name", "phone", "boyi_sender_name", "boyi_sender_phone", "remark"].forEach(key => {
      if (source[key] !== undefined && source[key] !== null) partyForm[key].value = source[key];
    });
    partyForm.is_active.checked = item ? item.is_active : true;
    partyDialog.querySelector("[data-edit-only]").hidden = !item;
    const message = partyDialog.querySelector("[data-party-message]");
    say("", message);
    const actionsBox = partyDialog.querySelector("[data-party-actions]");
    actionsBox.replaceChildren(button(item ? "保存" : "新建", async () => {
      const body = {};
      ["party_type", "name", "phone", "boyi_sender_name", "boyi_sender_phone", "remark"].forEach(key => { body[key] = partyForm[key].value.trim(); });
      try {
        const saved = item
          ? await send(`/finance/parties/${item.id}`, {...body, is_active: partyForm.is_active.checked, expected_revision: item.revision})
          : await send("/finance/parties", body);
        partyDialog.close();
        await preparePartyFilters(true);
        partySelect.value = String(saved.id);
        await loaders.parties();
        say(item ? "往来单位已保存。" : "往来单位已建档。");
      } catch (error) { say(error.message, message); }
    }, "primary-btn"));
    partyDialog.showModal();
  }
  $("[data-party-new]")?.addEventListener("click", async () => { await loadParties(); openParty(null); });
  $("[data-party-edit]")?.addEventListener("click", async () => {
    await loadParties();
    const item = parties.find(entry => String(entry.id) === partySelect.value);
    if (!item) { say("请先选择往来单位。"); return; }
    openParty(item);
  });
  const sendersDialog = main.querySelector("[data-senders-dialog]");
  sendersDialog.querySelectorAll("[data-dialog-close]").forEach(item => item.addEventListener("click", () => sendersDialog.close()));
  $("[data-party-senders]")?.addEventListener("click", async () => {
    const body = sendersDialog.querySelector("[data-sender-rows]");
    empty(body, 5, "正在读取…");
    sendersDialog.showModal();
    try {
      const rows = await get("/finance/parties/sender-candidates");
      if (!rows.length) { empty(body, 5, "没有尚未建档的月结寄件人。"); return; }
      body.replaceChildren(...rows.map(item => {
        const row = document.createElement("tr");
        cell(row, item.sender_name); cell(row, item.sender_phone); cell(row, String(item.waybills), "num"); cell(row, item.last_open_date);
        const create = document.createElement("button");
        create.type = "button"; create.className = "ghost-btn"; create.textContent = "建档";
        create.addEventListener("click", () => {
          sendersDialog.close();
          openParty(null, {party_type: "customer", name: item.sender_name, phone: item.sender_phone,
            boyi_sender_name: item.sender_name, boyi_sender_phone: item.sender_phone});
        });
        cell(row, "").append(create);
        return row;
      }));
    } catch (error) { say(error.message, sendersDialog.querySelector("[data-senders-message]")); }
  });

  // ---------- aging & statement ----------
  $("[data-aging-as-of]").value = today();
  $("[data-statement-start]").value = `${thisMonth()}-01`;
  $("[data-statement-end]").value = today();
  const AGING_KEYS = ["0_30", "31_60", "61_90", "over_90", "unapplied", "total"];
  loaders.aging = async () => {
    await preparePartyFilters();
    const body = $("[data-aging-rows]");
    empty(body, 7, "正在读取…");
    const params = new URLSearchParams({as_of: $("[data-aging-as-of]").value || today(), kind: $("[data-aging-kind]").value});
    const data = await get(`/finance/reports/aging?${params}`);
    if (!data.rows.length) empty(body, 7, "没有未清款项。");
    else body.replaceChildren(...data.rows.map(item => {
      const row = document.createElement("tr");
      cell(row, `${item.party_name}（${PARTY_TYPE_TEXT[item.party_type] || item.party_type}）`);
      AGING_KEYS.forEach(key => cell(row, zeroBlank(item[key]), "num"));
      return row;
    }));
    const foot = document.createElement("tr");
    cell(foot, "合计");
    AGING_KEYS.forEach(key => cell(foot, money(data.totals[key]), "num"));
    $("[data-aging-totals]").replaceChildren(foot);
  };
  $("[data-aging-refresh]").addEventListener("click", () => loaders.aging().catch(error => say(error.message)));
  let statement = null;
  $("[data-statement-refresh]").addEventListener("click", async () => {
    const partyId = $("[data-statement-party]").value;
    if (!partyId) { say("请选择往来单位。"); return; }
    const body = $("[data-statement-rows]");
    empty(body, 8, "正在读取…");
    try {
      const params = new URLSearchParams({party_id: partyId, start: $("[data-statement-start]").value, end: $("[data-statement-end]").value});
      statement = await get(`/finance/reports/statement?${params}`);
      const describe = balances => Object.entries(balances).map(([code, value]) => `${code} ${money(value)}`).join("；") || "0.00";
      $("[data-statement-summary]").textContent = `${statement.party.name}：期初 ${describe(statement.opening)}，期末 ${describe(statement.closing)}`;
      if (!statement.rows.length) empty(body, 8, "期间内没有往来。");
      else body.replaceChildren(...statement.rows.map(item => {
        const row = document.createElement("tr");
        cell(row, item.posting_date); cell(row, item.voucher_no || ""); cell(row, `${item.account_code} ${item.account_name}`);
        cell(row, item.waybill_no || ""); cell(row, item.summary);
        cell(row, zeroBlank(item.increase), "num"); cell(row, zeroBlank(item.decrease), "num"); cell(row, money(item.balance), "num");
        return row;
      }));
      $("[data-statement-export]").disabled = false;
    } catch (error) { say(error.message); }
  });
  $("[data-statement-export]").addEventListener("click", () => {
    if (!statement) return;
    const quote = value => `"${String(value ?? "").replace(/"/g, '""')}"`;
    const lines = [["日期", "凭证号", "科目", "运单号", "摘要", "增加", "减少", "余额"].map(quote).join(",")];
    statement.rows.forEach(item => lines.push([item.posting_date, item.voucher_no, `${item.account_code} ${item.account_name}`,
      item.waybill_no, item.summary, money(item.increase), money(item.decrease), money(item.balance)].map(quote).join(",")));
    const blob = new Blob(["﻿" + lines.join("\r\n")], {type: "text/csv;charset=utf-8"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `对账单-${statement.party.name}-${statement.start_date}-${statement.end_date}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });


  // ---------- sources, files, batches ----------
  const KIND_TEXT = {online: "在线抓取", file: "文件导入"};
  const BATCH_STATUS = {accepted: "已接受", rejected: "已拒绝", superseded: "已被重提替换"};
  const POSTING_TEXT = {posted: "已入账", suspense: "暂记待审", no_account: "未配置资金科目", closed_period: "封账期未改账", unposted: "未入账"};
  const CHECK_TEXT = {schema: "结构", source_key_unique: "唯一键", balance_chain: "余额链", opening_closing: "期初期末", summary: "汇总", complete_period: "整月完整"};
  let sources = [];
  let currentSource = null;
  let currentBatch = null;
  let linePage = 1;
  const sourceRows = $("[data-source-rows]");
  loaders.sources = async () => {
    await loadAccounts();
    empty(sourceRows, 9, "正在读取…");
    sources = await get("/finance/sources");
    if (!sources.length) { empty(sourceRows, 9, "还没有财务来源。"); return; }
    sourceRows.replaceChildren(...sources.map(item => {
      const row = document.createElement("tr");
      cell(row, `${item.display_name}（${item.source_key}）`); cell(row, KIND_TEXT[item.kind]); cell(row, item.provider);
      cell(row, item.producer_instance_id || "未指定");
      const fund = accounts.find(entry => entry.code === item.fund_account);
      cell(row, fund ? accountLabel(fund) : "未配置（不入账）"); cell(row, item.covered_until || "—");
      cell(row, item.kind === "file" ? String(item.pending_files) : "—", "num"); cell(row, item.is_enabled ? "启用" : "停用");
      const actionsCell = cell(row, "");
      actionsCell.append(button("查看", () => openSource(item), "ghost-btn", status));
      if (superAdmin) actionsCell.append(button("编辑", async () => openSourceDialog(item), "ghost-btn", status));
      return row;
    }));
  };
  async function openSource(item) {
    currentSource = item;
    const detail = $("[data-source-detail]");
    detail.hidden = false;
    $("[data-source-detail-title]").textContent = `${item.display_name}（${KIND_TEXT[item.kind]}）`;
    $("[data-file-toolbar]").hidden = item.kind !== "file";
    $("[data-file-table]").hidden = item.kind !== "file";
    $("[data-line-panel]").hidden = true;
    const [batches, files] = await Promise.all([
      get(`/finance/sources/${item.id}/batches`), item.kind === "file" ? get(`/finance/sources/${item.id}/files`) : Promise.resolve([]),
    ]);
    const fileRows = $("[data-file-rows]");
    if (item.kind === "file") {
      if (!files.length) empty(fileRows, canManage ? 6 : 5, "还没有上传账单。");
      else fileRows.replaceChildren(...files.map(file => {
        const row = document.createElement("tr");
        cell(row, file.file_name); cell(row, `${(file.size_bytes / 1024).toFixed(1)} KB`, "num");
        cell(row, {pending: "待解析", consumed: `已解析（批次 ${file.batch_id}）`, discarded: "已作废"}[file.status]);
        cell(row, file.uploaded_by); cell(row, file.uploaded_at || "");
        if (canManage) {
          const actionsCell = cell(row, "");
          if (file.status === "pending") actionsCell.append(button("作废", async () => {
            if (!window.confirm(`作废文件 ${file.file_name}？`)) return;
            await send(`/finance/source-files/${file.id}/discard`, {});
            await openSource(item);
          }, "ghost-btn ghost-btn--danger", status));
        }
        return row;
      }));
    }
    const batchRows = $("[data-batch-rows]");
    if (!batches.length) { empty(batchRows, 9, "还没有提交过批次。"); return; }
    batchRows.replaceChildren(...batches.map(batch => {
      const row = document.createElement("tr");
      const link = document.createElement("button");
      link.type = "button"; link.className = "finance-link"; link.textContent = `#${batch.id}`;
      link.addEventListener("click", () => { linePage = 1; openBatch(batch).catch(error => say(error.message)); });
      cell(row, "").append(link);
      cell(row, `${batch.period_start} ~ ${batch.period_end}`);
      const state = cell(row, BATCH_STATUS[batch.status], `finance-state finance-state--${batch.status}`);
      if (batch.error_code) {
        const note = document.createElement("div");
        note.className = "finance-problem"; note.textContent = `${batch.error_code}：${batch.error_message || ""}`;
        state.append(note);
      }
      cell(row, String(batch.row_count), "num"); cell(row, money(batch.inflow), "num"); cell(row, money(batch.outflow), "num");
      cell(row, (batch.checks || []).map(check => `${CHECK_TEXT[check.check] || check.check}${check.ran ? (check.declared_only ? "（声明）" : "✓") : "（未校验）"}`).join(" "));
      cell(row, batch.evidence_kind === "file" ? `文件 #${batch.file_id}` : "宿主观测的在线请求");
      cell(row, batch.created_at);
      return row;
    }));
  }
  async function openBatch(batch) {
    currentBatch = batch;
    const data = await get(`/finance/source-batches/${batch.id}/lines?page=${linePage}`);
    $("[data-line-panel]").hidden = false;
    $("[data-line-title]").textContent = `批次 #${batch.id} 流水（共 ${data.total} 笔）`;
    const body = $("[data-batch-line-rows]");
    if (!data.rows.length) empty(body, 9, "没有流水。");
    else body.replaceChildren(...data.rows.map(line => {
      const row = document.createElement("tr");
      if (!line.is_active) row.className = "finance-row--cancelled";
      cell(row, line.source_key); cell(row, line.business_date); cell(row, money(line.amount), "num");
      cell(row, line.item_l1); cell(row, line.item_l2); cell(row, line.counterparty); cell(row, line.waybill_no || "");
      cell(row, POSTING_TEXT[line.posting_status] || line.posting_status, `finance-state finance-state--${line.posting_status}`);
      const voucherCell = cell(row, "");
      if (line.voucher_id) {
        const link = document.createElement("button");
        link.type = "button"; link.className = "finance-link"; link.textContent = line.voucher_no || `#${line.voucher_id}`;
        link.addEventListener("click", () => openVoucher(line.voucher_id).catch(error => say(error.message)));
        voucherCell.append(link);
      }
      return row;
    }));
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    $("[data-line-page]").textContent = `第 ${data.page} / ${pages} 页`;
    $("[data-line-prev]").disabled = data.page <= 1;
    $("[data-line-next]").disabled = data.page >= pages;
  }
  $("[data-line-prev]").addEventListener("click", () => { linePage -= 1; openBatch(currentBatch).catch(error => say(error.message)); });
  $("[data-line-next]").addEventListener("click", () => { linePage += 1; openBatch(currentBatch).catch(error => say(error.message)); });
  $("[data-file-upload]")?.addEventListener("click", async () => {
    const input = $("[data-file-input]");
    const file = input.files?.[0];
    if (!currentSource || !file) { say("请先选择账单文件。"); return; }
    if (file.size > 20 * 1024 * 1024) { say("文件不能超过 20MB。"); return; }
    const requestId = crypto.randomUUID();
    try {
      const response = await fetch(`/finance/sources/${currentSource.id}/files`, {
        method: "POST", credentials: "same-origin", body: file,
        headers: {"Content-Type": file.type || "application/octet-stream", "X-Requested-With": "XMLHttpRequest",
          "X-Browser-Request-UUID": requestId, "X-File-Name": encodeURIComponent(file.name)},
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.message || "上传失败。");
      input.value = "";
      await openSource(currentSource);
      say(`已上传 ${file.name}，请到“财务数据源”页运行该来源的连接器解析。`);
    } catch (error) { say(error.message); }
  });
  const sourceDialog = main.querySelector("[data-source-dialog]");
  const sourceForm = sourceDialog.querySelector("[data-source-form]");
  sourceDialog.querySelectorAll("[data-dialog-close]").forEach(item => item.addEventListener("click", () => sourceDialog.close()));
  function openSourceDialog(item) {
    sourceDialog.querySelector("[data-source-title]").textContent = item ? `编辑来源：${item.display_name}` : "新建来源";
    sourceForm.reset();
    sourceDialog.querySelectorAll("[data-create-only]").forEach(node => { node.hidden = Boolean(item); });
    sourceDialog.querySelector("[data-edit-only]").hidden = !item;
    sourceForm.fund_account.replaceChildren(new Option("不配置（只接收流水，不入账）", ""),
      ...accounts.filter(entry => !entry.is_group && entry.is_active && ["cash", "bank", "fund"].includes(entry.account_type))
        .map(entry => new Option(accountLabel(entry), entry.code)));
    if (item) {
      sourceForm.display_name.value = item.display_name; sourceForm.producer_instance_id.value = item.producer_instance_id || "";
      sourceForm.fund_account.value = item.fund_account || ""; sourceForm.remark.value = item.remark || "";
      sourceForm.is_enabled.checked = item.is_enabled;
    }
    const message = sourceDialog.querySelector("[data-source-message]");
    say("", message);
    sourceDialog.querySelector("[data-source-actions]").replaceChildren(button(item ? "保存" : "新建", async () => {
      const common = {display_name: sourceForm.display_name.value.trim(), producer_instance_id: sourceForm.producer_instance_id.value.trim(),
        fund_account: sourceForm.fund_account.value || null, remark: sourceForm.remark.value.trim()};
      if (item) await send(`/finance/sources/${item.id}`, {...common, is_enabled: sourceForm.is_enabled.checked, expected_revision: item.revision});
      else await send("/finance/sources", {...common, source_key: sourceForm.source_key.value.trim(), kind: sourceForm.kind.value,
        provider: sourceForm.provider.value.trim()});
      sourceDialog.close();
      await loaders.sources();
      say(item ? "来源已保存。" : "来源已建立。");
    }, "primary-btn", message));
    sourceDialog.showModal();
  }
  $("[data-source-new]")?.addEventListener("click", async () => { await loadAccounts(); openSourceDialog(null); });

  // ---------- line rules, reviews, anomalies ----------
  const ACTION_TEXT = {none: "无", party_fixed: "固定往来单位", settle_by_waybill: "按运单号核销", internal_transfer: "内部转账"};
  const ruleDialog = main.querySelector("[data-line-rule-dialog]");
  const ruleForm = ruleDialog.querySelector("[data-line-rule-form]");
  ruleDialog.querySelectorAll("[data-dialog-close]").forEach(item => item.addEventListener("click", () => ruleDialog.close()));
  function syncRuleFields() {
    const action = ruleForm.party_action.value;
    ruleDialog.querySelector("[data-rule-counter]").hidden = action === "internal_transfer";
    ruleDialog.querySelector("[data-rule-party]").hidden = !["party_fixed", "settle_by_waybill"].includes(action);
  }
  ruleForm.party_action.addEventListener("change", syncRuleFields);
  function openRuleDialog({rule = null, review = null} = {}) {
    const base = rule || review || {};
    ruleDialog.querySelector("[data-line-rule-title]").textContent = review ? `处理待审：${review.item_l1}${review.item_l2 ? "/" + review.item_l2 : ""}` : (rule ? "编辑规则" : "新增规则");
    ruleForm.reset();
    ruleForm.provider.value = base.provider || ""; ruleForm.item_l1.value = base.item_l1 || ""; ruleForm.item_l2.value = base.item_l2 || "";
    ruleForm.direction.value = base.direction || "in";
    [ruleForm.provider, ruleForm.item_l1, ruleForm.item_l2, ruleForm.direction].forEach(field => { field.disabled = Boolean(review || rule); });
    ruleForm.counter_account.replaceChildren(...accounts.filter(entry => !entry.is_group && entry.is_active && !entry.frozen)
      .map(entry => new Option(accountLabel(entry), entry.code)));
    ruleForm.party_id.replaceChildren(new Option("—", ""), ...parties.filter(entry => entry.is_active).map(entry => new Option(partyLabel(entry), String(entry.id))));
    ruleForm.party_action.value = rule?.party_action || "none";
    if (rule?.counter_account) ruleForm.counter_account.value = rule.counter_account;
    ruleForm.party_id.value = rule?.party_id ? String(rule.party_id) : "";
    ruleForm.requires_waybill.checked = Boolean(rule?.requires_waybill);
    ruleForm.remark.value = rule?.remark || "";
    ruleDialog.querySelector("[data-edit-only]").hidden = !rule;
    ruleForm.is_active.checked = rule ? rule.is_active : true;
    syncRuleFields();
    const message = ruleDialog.querySelector("[data-line-rule-message]");
    say("", message);
    ruleDialog.querySelector("[data-line-rule-actions]").replaceChildren(button(review ? "保存规则并重记" : "保存", async () => {
      const action = ruleForm.party_action.value;
      const body = {party_action: action, requires_waybill: ruleForm.requires_waybill.checked, remark: ruleForm.remark.value.trim()};
      if (action !== "internal_transfer") body.counter_account = ruleForm.counter_account.value;
      if (["party_fixed", "settle_by_waybill"].includes(action) && ruleForm.party_id.value) body.party_id = Number(ruleForm.party_id.value);
      if (review) {
        const result = await send(`/finance/reviews/${review.id}/resolve`, body);
        say(`已保存规则，重记 ${result.reposted_days.length} 天${result.closed_period_days.length ? `；${result.closed_period_days.length} 天在已封账期间未改账` : ""}。`);
      } else {
        await send("/finance/line-rules", {...body, provider: ruleForm.provider.value.trim(), item_l1: ruleForm.item_l1.value.trim(),
          item_l2: ruleForm.item_l2.value.trim(), direction: ruleForm.direction.value, is_active: ruleForm.is_active.checked});
        say("规则已保存，之后提交或重记的流水按新规则入账。");
      }
      ruleDialog.close();
      loaded.delete("trial"); loaded.delete("vouchers");
      await loaders.rules();
    }, "primary-btn", message));
    ruleDialog.showModal();
  }
  loaders.rules = async () => {
    await Promise.all([loadAccounts(), loadParties()]);
    const [reviews, rules, anomalies] = await Promise.all([get("/finance/reviews"), get("/finance/line-rules"), get("/finance/anomalies")]);
    const reviewRows = $("[data-review-rows]");
    if (!reviews.length) empty(reviewRows, canManage ? 7 : 6, "没有待审流水。");
    else reviewRows.replaceChildren(...reviews.map(review => {
      const row = document.createElement("tr");
      cell(row, review.source_name); cell(row, review.item_l1 || "（空）"); cell(row, review.item_l2);
      cell(row, review.direction === "in" ? "流入" : "流出"); cell(row, String(review.line_count), "num"); cell(row, money(review.amount_total), "num");
      if (canManage) cell(row, "").append(button("处理", async () => openRuleDialog({review}), "ghost-btn", status));
      return row;
    }));
    const ruleRows = $("[data-line-rule-rows]");
    if (!rules.length) empty(ruleRows, canManage ? 9 : 8, "还没有规则。");
    else ruleRows.replaceChildren(...rules.map(rule => {
      const row = document.createElement("tr");
      if (!rule.is_active) row.className = "finance-row--cancelled";
      cell(row, rule.provider); cell(row, rule.item_l1); cell(row, rule.item_l2); cell(row, rule.direction === "in" ? "流入" : "流出");
      const account = accounts.find(entry => entry.code === rule.counter_account);
      cell(row, rule.party_action === "internal_transfer" ? "资金在途（设置）" : (account ? accountLabel(account) : rule.counter_account));
      cell(row, ACTION_TEXT[rule.party_action]); cell(row, parties.find(entry => entry.id === rule.party_id)?.name || "");
      cell(row, rule.is_active ? "启用" : "停用");
      if (canManage) cell(row, "").append(button("编辑", async () => openRuleDialog({rule}), "ghost-btn", status));
      return row;
    }));
    const anomalyRows = $("[data-anomaly-rows]");
    if (!anomalies.length) empty(anomalyRows, canManage ? 5 : 4, "没有未处理的异常。");
    else anomalyRows.replaceChildren(...anomalies.map(item => {
      const row = document.createElement("tr");
      cell(row, item.created_at); cell(row, item.kind === "CLOSED_PERIOD_DRIFT" ? "封账期流水变化" : item.kind); cell(row, item.period_month || "");
      cell(row, item.detail?.message ? `${item.detail.day || ""} ${item.detail.message}（原 ${item.detail.old_lines ?? "-"} 笔 → 新 ${item.detail.new_lines ?? "-"} 笔）` : JSON.stringify(item.detail));
      if (canManage) cell(row, "").append(button("确认知悉", async () => {
        const reason = window.prompt("确认原因（必填）：");
        if (!reason || !reason.trim()) return;
        await send(`/finance/anomalies/${item.id}/acknowledge`, {reason: reason.trim()});
        await loaders.rules();
      }, "ghost-btn", status));
      return row;
    }));
  };
  $("[data-line-rule-new]")?.addEventListener("click", async () => { await Promise.all([loadAccounts(), loadParties()]); openRuleDialog(); });

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
      cell(row, [!item.is_active && "已停用", item.frozen && "已冻结", item.is_group && "有下级", item.requires_waybill && "需运单号", item.requires_party && "需往来单位"].filter(Boolean).join(" · ") || "可记账");
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
    accountForm.requires_party.checked = Boolean(item?.requires_party);
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
        if (accountForm.requires_party.checked !== item.requires_party) body.requires_party = accountForm.requires_party.checked;
        if (accountForm.account_type.value && accountForm.account_type.value !== item.account_type) body.account_type = accountForm.account_type.value;
        return send(`/finance/accounts/${item.code}`, body);
      }, "科目已保存。"), "primary-btn"));
    } else {
      actionsBox.append(button("新增", () => run(() => {
        const code = accountForm.code.value.trim();
        const body = {code, name: accountForm.name.value.trim(), requires_waybill: accountForm.requires_waybill.checked};
        if (accountForm.account_type.value) body.account_type = accountForm.account_type.value;
        if (code.length === 4) Object.assign(body, {root_type: accountForm.root_type.value, normal_balance: accountForm.normal_balance.value,
          requires_party: accountForm.requires_party.checked});
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
    await loadDocRules();
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
