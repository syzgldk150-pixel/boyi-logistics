// Customer share links (spec ⑩a / ⑩b). All dynamic text is written with textContent.
(() => {
  "use strict";
  const page = document.querySelector(".main-content:not([hidden]) [data-share-page]") || document.querySelector("[data-share-page]");
  const source = document.getElementById("share-listing");
  if (!page || !source) return;
  const scope = page.closest(".main-content") || document;
  const $ = (selector) => scope.querySelector(selector);
  const $$ = (selector) => Array.from(scope.querySelectorAll(selector));
  let listing = JSON.parse(source.textContent || "{}");
  const request = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);

  const rows = $("[data-share-rows]");
  const toast = $("[data-share-toast]");
  const createDialog = $("[data-share-create-dialog]");
  const customerDialog = $("[data-share-customer-dialog]");
  let toastTimer = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function showToast(message, kind = "") {
    if (!toast) return;
    toast.textContent = message;
    toast.dataset.kind = kind;
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 3200);
  }

  async function post(url, body) {
    const requestId = window.crypto?.randomUUID?.();
    if (!requestId) throw new Error("浏览器未提供安全请求标识。");
    const response = await request(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-Browser-Request-UUID": requestId },
      body: JSON.stringify({ ...body, request_id: requestId }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || payload?.ok !== true) throw new Error(payload?.message || "操作失败，请重试。");
    return payload.data;
  }

  async function getJSON(url) {
    const response = await request(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
    const payload = await response.json().catch(() => null);
    if (!response.ok || payload?.ok !== true) throw new Error(payload?.message || "读取失败，请重试。");
    return payload.data;
  }

  async function reload() {
    listing = await getJSON("/receipts/shares/data");
    render();
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_error) {
      const area = el("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      return copied;
    }
  }

  // ---- list ----------------------------------------------------------------
  function visibleShares() {
    const keyword = ($("[data-share-search]")?.value || "").trim().toLowerCase();
    const state = $("[data-share-state-filter]")?.value || "";
    return (listing.shares || []).filter((share) =>
      (!state || share.state === state)
      && (!keyword || [share.customer_name, share.customer_code, share.url].some((value) => String(value || "").toLowerCase().includes(keyword))));
  }

  function twoLines(main, sub) {
    const cell = el("td");
    cell.append(el("span", "share-cell-main", main), el("span", "share-cell-sub", sub));
    cell.title = `${main} ${sub}`.trim();
    return cell;
  }

  function opButton(label, handler, disabled = false) {
    const button = el("button", "", label);
    button.type = "button";
    button.disabled = disabled;
    button.addEventListener("click", handler);
    return button;
  }

  function render() {
    const shares = visibleShares();
    $("[data-share-count]").textContent = `共 ${(listing.shares || []).length} 条`;
    $("[data-share-unconfigured]").hidden = Boolean(listing.configured);
    rows.replaceChildren();
    for (const share of shares) {
      const row = el("tr");
      const link = el("td", "", share.url ? share.url.replace(/^https?:\/\//, "") : "未配置密钥");
      link.title = share.url || "";
      const kinds = el("td", "share-kinds", share.image_types_text);
      const stateCell = el("td");
      stateCell.append(el("span", `share-state share-state--${share.state}`, share.state_label));
      const refreshNote = share.refresh_message ? `${share.last_refreshed_text || "未刷新"} · ${share.refresh_message}` : "等待首次刷新";
      stateCell.title = refreshNote;
      const ops = el("td");
      const box = el("div", "share-ops");
      box.append(opButton("复制", async () => {
        const copied = await copyText(share.url);
        showToast(copied ? (share.has_access_code ? "链接已复制（访问码请另行告知客户）" : "链接已复制") : "复制失败，请手动复制。", copied ? "" : "error");
      }, !share.url));
      if (share.state === "active") box.append(opButton("停用", () => act(share, "disable", "停用后客户立即无法打开这条链接，确定停用？")));
      else if (share.state === "expired") box.append(opButton("续期", () => act(share, "renew", "", { expires_days: 30 })));
      else box.append(opButton("启用", () => act(share, "enable")));
      box.append(opButton("重置", () => act(share, "reset", "重置后旧链接立即失效，需要把新链接重新发给客户。确定重置？"), !listing.configured));
      ops.append(box);
      row.append(
        twoLines(share.customer_name, share.customer_code), link,
        twoLines(share.naming_label, share.naming_template), kinds,
        el("td", "", share.expires_text), el("td", "", `${share.view_count} 次`), stateCell,
        el("td", "", share.last_viewed_text || "—"), ops);
      row.title = `共 ${share.item_count} 张图片；${refreshNote}`;
      rows.append(row);
    }
    const empty = $("[data-share-empty]");
    empty.hidden = (listing.shares || []).length > 0;
    scope.querySelector(".share-table").hidden = !empty.hidden;
    window.ConsoleUI?.refreshIcons?.();
  }

  async function act(share, action, confirmText = "", extra = {}) {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      await post(`/receipts/shares/${share.id}/${action}`, { record_version: share.record_version, ...extra });
      await reload();
      showToast({ disable: "链接已停用", enable: "链接已启用", renew: "已续期 30 天", reset: "链接已重置，请复制新链接" }[action] || "已更新");
    } catch (error) {
      showToast(error.message, "error");
      reload().catch(() => {});
    }
  }

  $("[data-share-search]")?.addEventListener("input", render);
  $("[data-share-state-filter]")?.addEventListener("change", render);

  // ---- dialogs ---------------------------------------------------------------
  function bindClose(dialog) {
    dialog.querySelectorAll("[data-share-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  }
  bindClose(createDialog);
  bindClose(customerDialog);

  function feedback(node, message, kind = "") {
    if (!node) return;
    node.textContent = message;
    node.dataset.kind = kind;
  }

  // ---- ⑩b create -------------------------------------------------------------
  const customerSelect = $("[data-share-customer]");
  const templateInput = $("[data-share-template]");
  const example = $("[data-share-example]");
  const createFeedback = $("[data-share-create-feedback]");
  const createSubmit = $("[data-share-create-submit]");
  let exampleTimer = 0;

  function fillCustomers(select, withNew = false) {
    select.replaceChildren();
    if (withNew) select.append(new Option("＋ 新建客户", ""));
    for (const customer of listing.customers || []) {
      if (!customer.is_active) continue;
      select.append(new Option(`${customer.name}（${customer.code}）`, String(customer.id)));
    }
    if (!withNew && !select.options.length) select.append(new Option("请先在“客户与规则”里添加客户", ""));
  }

  function selectedKinds() {
    return $$("[data-share-kind]").filter((box) => box.checked).map((box) => box.value);
  }

  function updateCreateState() {
    const ok = Boolean(customerSelect.value) && selectedKinds().length > 0 && listing.configured;
    createSubmit.disabled = !ok;
  }

  function refreshExample() {
    window.clearTimeout(exampleTimer);
    exampleTimer = window.setTimeout(async () => {
      const customer = (listing.customers || []).find((item) => String(item.id) === customerSelect.value);
      const params = new URLSearchParams({ template: templateInput.value, short_name: customer?.short_name || "" });
      try {
        example.textContent = (await getJSON(`/receipts/shares/naming-example?${params}`)).example;
        feedback(createFeedback, "生成后可在列表随时停用或重置");
      } catch (error) {
        example.textContent = "—";
        feedback(createFeedback, error.message, "error");
      }
    }, 250);
  }

  function openCreate() {
    fillCustomers(customerSelect);
    const kinds = $("[data-share-kinds]");
    kinds.replaceChildren();
    for (const kind of listing.defaults.image_kinds) {
      const label = el("label");
      const box = el("input");
      box.type = "checkbox";
      box.value = kind.value;
      box.checked = true;
      box.dataset.shareKind = "";
      box.addEventListener("change", updateCreateState);
      label.append(box, document.createTextNode(` ${kind.label}`));
      kinds.append(label);
    }
    const chips = $("[data-share-chips]");
    chips.replaceChildren();
    for (const field of listing.defaults.naming_fields) {
      const chip = el("button", "", field);
      chip.type = "button";
      chip.addEventListener("click", () => {
        const start = templateInput.selectionStart ?? templateInput.value.length;
        const end = templateInput.selectionEnd ?? start;
        templateInput.setRangeText(`{${field}}`, start, end, "end");
        templateInput.focus();
        refreshExample();
      });
      chips.append(chip);
    }
    // Sharing with the same customer again starts from their latest link's settings.
    applyPrevious();
    $("[data-share-code]").value = "";
    feedback(createFeedback, listing.configured ? "生成后可在列表随时停用或重置" : "服务器尚未配置共享链接密钥", listing.configured ? "" : "error");
    updateCreateState();
    refreshExample();
    createDialog.showModal();
  }

  function applyPrevious() {
    const previous = (listing.shares || []).find((share) => String(share.customer_id) === customerSelect.value);
    templateInput.value = previous?.naming_template || listing.defaults.naming_template;
    if (previous) $$("[data-share-kind]").forEach((box) => { box.checked = previous.image_types.includes(box.value); });
  }

  customerSelect.addEventListener("change", () => { applyPrevious(); updateCreateState(); refreshExample(); });
  templateInput.addEventListener("input", refreshExample);
  $$('input[name="scope"]').forEach((radio) => radio.addEventListener("change", () => {
    $("[data-share-dates]").hidden = scope.querySelector('input[name="scope"]:checked').value !== "by_date";
  }));
  $("[data-share-code-random]").addEventListener("click", () => {
    const digits = new Uint32Array(1);
    window.crypto.getRandomValues(digits);
    $("[data-share-code]").value = String(digits[0] % 10000).padStart(4, "0");
  });
  $$("[data-share-open-create]").forEach((button) => button.addEventListener("click", openCreate));

  $("[data-share-create-form]").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (createSubmit.disabled) return;
    createSubmit.disabled = true;
    try {
      const data = await post("/receipts/shares", {
        customer_id: Number(customerSelect.value),
        scope: scope.querySelector('input[name="scope"]:checked').value,
        date_from: $("[data-share-date-from]").value, date_to: $("[data-share-date-to]").value,
        image_types: selectedKinds(),
        expires_days: Number(scope.querySelector('input[name="expires_days"]:checked').value),
        access_code: $("[data-share-code]").value.trim(),
        naming_template: templateInput.value.trim(),
      });
      const text = data.access_code ? `${data.share.url}\n访问码：${data.access_code}` : data.share.url;
      const copied = await copyText(text);
      createDialog.close();
      await reload();
      showToast(copied ? (data.access_code ? "链接和访问码已复制" : "链接已复制") : "链接已生成，请在列表中复制");
    } catch (error) {
      feedback(createFeedback, error.message, "error");
    } finally {
      updateCreateState();
    }
  });

  // ---- customers and sender rules ---------------------------------------------
  const customerPick = $("[data-share-customer-pick]");
  const rulesBox = $("[data-share-rules]");
  const customerFeedback = $("[data-share-customer-feedback]");

  function addRule(rule = { field: "sender_name", op: "contains", value: "" }) {
    const row = el("div", "share-rule");
    const field = el("select");
    for (const item of listing.defaults.match_fields) field.append(new Option(item.label, item.value));
    field.value = rule.field;
    const op = el("select");
    op.append(new Option("包含", "contains"), new Option("等于", "eq"));
    op.value = rule.op;
    const value = el("input");
    value.maxLength = 120;
    value.value = rule.value;
    value.placeholder = "至少 2 个字";
    const remove = el("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "删除这条规则");
    remove.addEventListener("click", () => row.remove());
    row.append(field, op, value, remove);
    rulesBox.append(row);
  }

  function currentMatch() {
    return {
      mode: scope.querySelector('input[name="match_mode"]:checked').value,
      conditions: Array.from(rulesBox.children).map((row) => {
        const [field, op, value] = row.querySelectorAll("select, input");
        return { field: field.value, op: op.value, value: value.value.trim() };
      }),
    };
  }

  function loadCustomer() {
    const customer = (listing.customers || []).find((item) => String(item.id) === customerPick.value);
    $("[data-share-customer-name]").value = customer?.name || "";
    $("[data-share-customer-short]").value = customer?.short_name || "";
    scope.querySelector(`input[name="match_mode"][value="${customer?.match.mode || "any"}"]`).checked = true;
    rulesBox.replaceChildren();
    (customer?.match.conditions || [undefined]).forEach((rule) => addRule(rule));
    feedback(customerFeedback, customer ? `客户编码 ${customer.code}` : "新建客户后即可为其生成链接");
  }

  function openCustomers() {
    if (createDialog.open) createDialog.close();
    fillCustomers(customerPick, true);
    loadCustomer();
    $("[data-share-senders]").replaceChildren();
    customerDialog.showModal();
  }

  customerPick.addEventListener("change", loadCustomer);
  $("[data-share-rule-add]").addEventListener("click", () => addRule());
  $$("[data-share-open-customers]").forEach((button) => button.addEventListener("click", openCustomers));

  $("[data-share-sender-search]").addEventListener("click", async () => {
    const list = $("[data-share-senders]");
    list.replaceChildren();
    try {
      const found = await getJSON(`/receipts/shares/senders?${new URLSearchParams({ q: $("[data-share-sender-keyword]").value.trim() })}`);
      if (!found.length) list.append(el("li", "", "没有找到发货人"));
      for (const item of found) {
        const line = el("li");
        line.append(el("strong", "", item.sender_name), el("span", "", `${item.sender_phone || "无电话"} · ${item.waybills} 单`),
          opButton("按名称加规则", () => addRule({ field: "sender_name", op: "eq", value: item.sender_name })));
        list.append(line);
      }
    } catch (error) {
      feedback(customerFeedback, error.message, "error");
    }
  });

  $("[data-share-rule-preview]").addEventListener("click", async () => {
    try {
      const data = await post("/receipts/shares/preview", { match: currentMatch() });
      feedback(customerFeedback, `当前规则匹配 ${data.matching_waybills}${data.capped ? "+" : ""} 张运单${data.sample.length ? "，例如 " + data.sample.join("、") : ""}`);
    } catch (error) {
      feedback(customerFeedback, error.message, "error");
    }
  });

  $("[data-share-customer-form]").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = await post("/receipts/shares/customers", {
        customer_id: customerPick.value ? Number(customerPick.value) : null,
        name: $("[data-share-customer-name]").value.trim(),
        short_name: $("[data-share-customer-short]").value.trim(),
        match: currentMatch(),
      });
      await reload();
      fillCustomers(customerPick, true);
      customerPick.value = String(data.customer_id);
      loadCustomer();
      feedback(customerFeedback, `已保存，当前规则匹配 ${data.matching_waybills} 张运单`);
    } catch (error) {
      feedback(customerFeedback, error.message, "error");
    }
  });

  render();
})();
