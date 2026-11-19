(() => {
  "use strict";

  // 业务手册 → 报价 (internal only). The Agent lists the fees with an approved rule on the
  // billing date, computes one delivery from the values typed here (groups take the larger fee)
  // and keeps every quote. This page builds the form from the fees' parameters and shows the
  // result; it never decides an amount.
  function init() {
    const page = document.querySelector("[data-handbook]");
    const root = document.querySelector("[data-fee-quote]");
    if (!page || !root || !window.HandbookUI || !window.FeeForm || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const UI = window.HandbookUI;
    const Form = window.FeeForm;
    const { escapeHtml, formatTime, request } = UI;
    const $ = (selector) => root.querySelector(selector);
    const feedback = UI.feedback($("[data-quote-status]"));
    const state = { catalog: null, selected: new Set(), values: {}, result: null, busy: false, loadedFor: "" };
    const today = () => { const now = new Date(); return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`; };
    $("[data-quote-date]").value = today();

    function neededParams() {
      if (!state.catalog) return [];
      const keys = new Set();
      state.catalog.fees.filter((fee) => state.selected.has(fee.fee_kind)).forEach((fee) => fee.params.forEach((key) => keys.add(key)));
      return state.catalog.params.filter((param) => keys.has(param.key));
    }

    function rememberValues() {
      const form = $("[data-quote-form]");
      Object.assign(state.values, Form.read(form, neededParams()));
      // Cleared inputs drop their old value.
      neededParams().forEach((param) => { if (!(param.key in Form.read(form, [param]))) delete state.values[param.key]; });
    }

    function renderFees() {
      const box = $("[data-quote-fees]");
      const catalog = state.catalog;
      if (!catalog) { box.innerHTML = '<p class="hb-meta">正在读取……</p>'; return; }
      if (catalog.conflict) {
        box.innerHTML = `<p class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span>${escapeHtml(catalog.conflict)}</span></p>`;
        return;
      }
      const fees = catalog.fees.map((fee) => `<label class="hb-quote__fee"><input type="checkbox" data-quote-fee="${escapeHtml(fee.fee_kind)}" ${state.selected.has(fee.fee_kind) ? "checked" : ""}>
          <span><b>${escapeHtml(fee.name)}</b><small class="hb-meta">${escapeHtml(fee.version)}${fee.group ? ` · 互斥组：${escapeHtml(fee.group)}` : ""}</small></span></label>`).join("");
      const missing = catalog.unavailable.map((item) => `<li><b>${escapeHtml(item.name)}</b>：${escapeHtml(item.reason)}</li>`).join("");
      box.innerHTML = `${fees || '<p class="hb-meta">这个开单日期还没有已生效的计算规则。规则在“计算规则”页签审核生效后才会出现在这里。</p>'}
        ${missing ? `<details class="hb-quote__unavailable"><summary>${catalog.unavailable.length} 种费用在这个日期不能报价</summary><ul>${missing}</ul></details>` : ""}`;
    }

    function renderForm() {
      const params = neededParams();
      $("[data-quote-form]").innerHTML = state.selected.size ? Form.render(params, state.values) : '<p class="hb-meta">先勾选费用。</p>';
      if (window.feather) window.feather.replace();
    }

    function itemHtml(item) {
      const tone = item.outcome === "AMOUNT" ? (item.counted ? "ok" : "") : "warn";
      return `<li class="hb-quote__item" data-counted="${item.counted}">
        <div class="hb-quote__line"><b>${escapeHtml(item.name)}</b><span data-tone="${tone}">${Form.outcomeText(item)}</span></div>
        <p class="hb-meta">${escapeHtml(item.version)}${item.counted ? "" : " · 不计入合计"}</p>
        ${item.note ? `<p class="hb-meta" data-tone="warn">${escapeHtml(item.note)}</p>` : ""}
        ${item.missing?.length ? `<p class="hb-meta" data-tone="warn">还缺少：${item.missing.map(escapeHtml).join("、")}</p>` : ""}
        <details><summary>计算过程与出处</summary>${Form.resultBody({ ...item, reason: item.missing?.length ? "" : item.reason })}</details></li>`;
    }

    function renderResult() {
      const box = $("[data-quote-result]");
      const result = state.result;
      $("[data-quote-copy]").hidden = !result;
      if (!result) return;
      box.innerHTML = `<div class="hb-trial__amount"><b>¥ ${escapeHtml(result.total)}</b><span class="hb-meta">开单日期 ${escapeHtml(result.billing_date)} · ${result.complete ? "全部费用已确定" : "有费用没有确定金额，合计不完整"}</span></div>
        ${result.missing?.length ? `<p class="hb-alert" data-tone="warn"><i data-feather="info"></i><span>还缺少：${result.missing.map(escapeHtml).join("、")}</span></p>` : ""}
        ${(result.unavailable || []).map((item) => `<p class="hb-meta" data-tone="warn">${escapeHtml(item.name)}：${escapeHtml(item.reason)}</p>`).join("")}
        <ul class="hb-quote__items">${result.items.map(itemHtml).join("")}</ul>`;
      if (window.feather) window.feather.replace();
    }

    function copyText() {
      const result = state.result;
      const lines = [`报价（开单日期 ${result.billing_date}）`];
      result.items.forEach((item) => {
        const value = item.outcome === "AMOUNT" ? `${item.amount} 元` : `${item.outcome_text}${item.reason ? `（${item.reason}）` : ""}`;
        lines.push(`${item.name}：${value}${item.counted ? "" : "（不计入合计）"}`);
      });
      lines.push(`合计：${result.total} 元${result.complete ? "" : "（有费用没有确定金额）"}`);
      return lines.join("\n");
    }

    async function loadRecent() {
      const list = $("[data-quote-recent]");
      try {
        const data = await request("/handbook/quote/recent");
        const quotes = data.quotes || [];
        list.innerHTML = quotes.length ? quotes.map((item) => {
          const names = (item.result?.items || []).map((fee) => fee.name).join("、");
          return `<li><span class="hb-meta">${escapeHtml(formatTime(item.created_at))}</span><span>${escapeHtml(names || "—")} · ¥ ${escapeHtml(item.total)}${item.complete ? "" : "（不完整）"}${item.tracking_number ? ` · ${escapeHtml(item.tracking_number)}` : ""}</span>
            <span class="hb-meta">${escapeHtml(item.actor || "")}${item.channel === "console" ? "" : ` · ${escapeHtml(item.channel)}`}</span></li>`;
        }).join("") : '<li class="hb-meta">还没有报价记录。</li>';
      } catch (error) {
        list.innerHTML = `<li class="hb-meta" data-tone="danger">最近报价读取失败：${escapeHtml(error.message)}</li>`;
      }
    }

    async function run(progress, work, success = "") {
      if (state.busy) return;
      state.busy = true;
      const unlock = UI.lockButtons(root);
      feedback.progress(progress);
      try {
        await work();
        if (success) feedback.success(success); else feedback.clear();
      } catch (error) {
        feedback.error("没有完成", error.message || "操作失败");
      } finally {
        state.busy = false;
        unlock();
      }
    }

    async function loadCatalog() {
      const date = $("[data-quote-date]").value || today();
      state.catalog = await request(`/handbook/quote/catalog?billing_date=${encodeURIComponent(date)}`);
      state.loadedFor = date;
      const kinds = new Set(state.catalog.fees.map((fee) => fee.fee_kind));
      state.selected = new Set([...state.selected].filter((kind) => kinds.has(kind)));
      renderFees();
      renderForm();
    }

    function calculate() {
      if (!state.selected.size) { feedback.error("没有计算", "先勾选要收的费用。"); return; }
      rememberValues();
      const tracking = $("[data-quote-tracking]").value.trim();
      run("正在计算……", async () => {
        state.result = await request("/handbook/quote", { billing_date: $("[data-quote-date]").value || today(), fee_kinds: [...state.selected],
          values: state.values, ...(tracking ? { tracking_number: tracking } : {}) });
        renderResult();
        loadRecent();
      });
    }

    root.addEventListener("change", (event) => {
      const target = event.target;
      if (target.matches("[data-quote-fee]")) {
        rememberValues();
        if (target.checked) state.selected.add(target.dataset.quoteFee); else state.selected.delete(target.dataset.quoteFee);
        renderForm();
      } else if (target.matches("[data-quote-date]") && target.value !== state.loadedFor) {
        rememberValues();
        run("正在读取这个日期生效的规则……", loadCatalog);
      }
    });

    root.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target.closest("[data-quote-form]") && event.target.matches("input")) { event.preventDefault(); calculate(); }
    });

    root.addEventListener("click", async (event) => {
      const target = event.target.closest("button");
      if (!target || target.disabled || state.busy) return;
      if (target.closest("[data-quote-form]") && Form.handleClick(target, neededParams(), $("[data-quote-form]"))) return;
      if (target.hasAttribute("data-quote-run")) calculate();
      else if (target.hasAttribute("data-quote-copy") && state.result) {
        try { await navigator.clipboard.writeText(copyText()); feedback.success("已复制报价文字"); } catch (_error) { feedback.error("没有复制", "浏览器不允许写剪贴板，请手动选择复制。"); }
      } else if (target.hasAttribute("data-quote-waybill")) {
        const tracking = $("[data-quote-tracking]").value.trim();
        if (!tracking) { feedback.error("没有读取", "请先填写运单号。"); return; }
        rememberValues();
        run("正在读取运单……", async () => {
          const facts = await request("/handbook/quote/waybill", { tracking_number: tracking });
          $("[data-quote-date]").value = facts.billing_date;
          state.values.settlement_weight = facts.settlement_weight;
          const total = facts.actual_weight || facts.settlement_weight;
          const average = (Number(total) / Number(facts.pieces)).toFixed(2);
          state.values.pieces = [{ count: facts.pieces, weight: facts.pieces === 1 ? total : average }];
          if (facts.billing_date !== state.loadedFor) await loadCatalog(); else renderForm();
          feedback.success("已按运单预填", `开单日期 ${facts.billing_date} · 结算重量 ${facts.settlement_weight} 公斤 · ${facts.pieces} 件${facts.pieces > 1 ? `（单上没有每件重量，按平均每件 ${average} 公斤预填，请按实际改）` : ""}`);
        });
      }
    });

    // Load when the 报价 tab is first opened (or right away when it is the remembered tab).
    function start() {
      if (root.dataset.started === "true" || root.hidden) return;
      root.dataset.started = "true";
      run("正在读取已生效的规则……", async () => { await loadCatalog(); loadRecent(); });
    }
    new MutationObserver(start).observe(root, { attributes: true, attributeFilter: ["hidden"] });
    start();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
