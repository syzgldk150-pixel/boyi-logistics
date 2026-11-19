(() => {
  "use strict";

  // 业务手册 → 计算规则 / 报价: the form people fill for a block-built fee rule. The
  // parameters come from the Agent (shared/fee_engine Param.as_dict); this file only draws
  // inputs, reads them back as plain values and shows a result. The Agent validates and
  // computes everything; nothing here decides an amount.
  const OUTCOME = { AMOUNT: "金额", MISSING: "缺少参数", NEGOTIATE: "需协商", NOT_PROVIDED: "可不提供服务", MANUAL: "需人工判断", ACTUAL: "实报实销", INVALID: "输入不合理" };

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

  function range(param) {
    const parts = [];
    if (param.min != null) parts.push(`≥ ${param.min}`);
    if (param.max != null) parts.push(`≤ ${param.max}`);
    return parts.join("，");
  }

  function pieceRows(param, rows) {
    const list = rows && rows.length ? rows : [{ count: 1 }];
    return list.map((row, index) => `<div class="hb-piece-row" data-piece-row="${index}">
        <label class="hb-cell hb-cell--xs"><span>件数</span><input inputmode="numeric" data-piece-field="count" value="${esc(row.count ?? 1)}"></label>
        ${param.fields.map((field) => `<label class="hb-cell hb-cell--sm"><span>${esc(field.label)}${field.unit ? `（${esc(field.unit)}）` : ""}</span><input inputmode="decimal" data-piece-field="${esc(field.key)}" value="${esc(row[field.key] ?? "")}"></label>`).join("")}
        <button class="hb-link" data-tone="danger" type="button" data-piece-remove="${esc(param.key)}" data-index="${index}" aria-label="删除第 ${index + 1} 行">删除</button></div>`).join("");
  }

  function field(param, value) {
    const label = `${esc(param.label)}${param.unit ? `（${esc(param.unit)}）` : ""}${param.required === false ? " · 选填" : ""}`;
    const hint = [param.hint, range(param)].filter(Boolean).join("；");
    const hintHtml = hint ? `<small class="hb-meta">${esc(hint)}</small>` : "";
    if (param.type === "choice") {
      return `<label class="hb-cell"><span>${label}</span><select data-param="${esc(param.key)}"><option value="">请选择</option>
        ${param.options.map((option) => `<option value="${esc(option.value)}" ${value === option.value ? "selected" : ""}>${esc(option.label)}</option>`).join("")}</select>${hintHtml}</label>`;
    }
    if (param.type === "bool") {
      return `<label class="hb-cell"><span>${label}</span><select data-param="${esc(param.key)}"><option value="">请选择</option>
        <option value="true" ${value === true ? "selected" : ""}>是</option><option value="false" ${value === false ? "selected" : ""}>否</option></select>${hintHtml}</label>`;
    }
    if (param.type === "pieces") {
      return `<fieldset class="hb-cell hb-cell--full hb-pieces" data-param="${esc(param.key)}" data-pieces><legend>${label}</legend>${hintHtml}
        <div data-piece-rows>${pieceRows(param, value)}</div>
        <button class="ghost-btn hb-btn-sm" type="button" data-piece-add="${esc(param.key)}"><i data-feather="plus"></i><span>添加一行</span></button></fieldset>`;
    }
    return `<label class="hb-cell"><span>${label}</span><input inputmode="${param.integer ? "numeric" : "decimal"}" data-param="${esc(param.key)}" value="${esc(value ?? param.default ?? "")}" placeholder="${param.default != null ? esc(`默认 ${param.default}`) : ""}">${hintHtml}</label>`;
  }

  function render(params, values = {}) {
    if (!params.length) return '<p class="hb-meta">这条规则不需要填写参数。</p>';
    return `<div class="hb-fee__cells hb-fee__cells--form">${params.map((param) => field(param, values[param.key])).join("")}</div>`;
  }

  function read(root, params) {
    const values = {};
    params.forEach((param) => {
      const node = root.querySelector(`[data-param="${CSS.escape(param.key)}"]`);
      if (!node) return;
      if (param.type === "pieces") {
        const rows = [...node.querySelectorAll("[data-piece-row]")].map((row) => {
          const item = {};
          row.querySelectorAll("[data-piece-field]").forEach((input) => {
            const raw = input.value.trim();
            if (raw === "") return;
            item[input.dataset.pieceField] = input.dataset.pieceField === "count" && /^\d+$/.test(raw) ? Number(raw) : raw;
          });
          return item;
        }).filter((item) => Object.keys(item).some((key) => key !== "count"));
        if (rows.length) values[param.key] = rows;
        return;
      }
      const raw = node.value.trim();
      if (raw === "") return;
      if (param.type === "bool") values[param.key] = raw === "true";
      else if (param.type === "number" && param.integer && /^-?\d+$/.test(raw)) values[param.key] = Number(raw);
      else values[param.key] = raw;
    });
    return values;
  }

  // Add or remove a pieces row inside ``root``; returns true when the click was handled.
  function handleClick(target, params, root) {
    const key = target.dataset.pieceAdd || target.dataset.pieceRemove;
    if (!key) return false;
    const param = params.find((item) => item.key === key);
    const box = root.querySelector(`[data-param="${CSS.escape(key)}"] [data-piece-rows]`);
    if (!param || !box) return true;
    const current = read(root, [param])[key] || [];
    if (target.dataset.pieceAdd) current.push({ count: 1 });
    else current.splice(Number(target.dataset.index), 1);
    box.innerHTML = pieceRows(param, current);
    return true;
  }

  function outcomeText(result) {
    return result.outcome === "AMOUNT" ? `¥ ${esc(result.amount)}` : esc(result.outcome_text || OUTCOME[result.outcome] || result.outcome);
  }

  function resultBody(result, { steps = true } = {}) {
    return `${result.reason ? `<p class="hb-meta" data-tone="warn">${esc(result.reason)}</p>` : ""}
      ${steps && result.steps?.length ? `<ol class="hb-trial__steps">${result.steps.map((step) => `<li>${esc(step)}</li>`).join("")}</ol>` : ""}
      ${result.citations?.length ? `<p class="hb-meta">出处：${result.citations.map(esc).join("；")}</p>` : ""}
      ${(result.reminders || []).map((item) => `<p class="hb-alert" data-tone="warn"><i data-feather="info"></i><span>提醒：${esc(item)}</span></p>`).join("")}`;
  }

  window.FeeForm = { OUTCOME, esc, render, read, handleClick, outcomeText, resultBody };
})();
