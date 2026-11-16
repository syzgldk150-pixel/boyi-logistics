(() => {
  "use strict";

  // 业务手册 → 计算规则. The Agent validates every save, submission, approval and
  // trial; this page only edits the form, shows results and forwards actions.
  // canEdit / superAdmin only hide buttons: the Console and the Agent enforce them.
  const STATUS_TEXT = { DRAFT: "草稿", PENDING: "待审核", APPROVED: "已生效", RETIRED: "已作废" };
  const OUTCOME_TEXT = { AMOUNT: "金额", NEGOTIATE: "需协商", NOT_PROVIDED: "可不提供服务", MANUAL: "需人工判断", INVALID: "输入不合理" };
  const FIRST_FLOOR = { MANUAL: "需人工判断", NO_CHARGE: "不收上楼费", BASE: "只收基础费" };
  const ACTION_TEXT = { COPY: "复制为草稿", SAVE: "保存", CHECK: "勾选核对", SUBMIT: "提交审核", WITHDRAW: "退回草稿", APPROVE: "审核生效", RETIRE: "作废" };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function initTabs(page) {
    const tabs = [...page.querySelectorAll("[data-handbook-tab]")];
    function select(tab) {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
        document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
      });
      try { window.localStorage.setItem("handbook.tab", tab.dataset.handbookTab); } catch (_error) { /* optional */ }
    }
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => select(tab));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        const next = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        select(next);
        next.focus();
      });
    });
    let remembered = null;
    try { remembered = window.localStorage.getItem("handbook.tab"); } catch (_error) { /* optional */ }
    const initial = tabs.find((tab) => tab.dataset.handbookTab === remembered);
    if (initial) select(initial);
  }

  function init() {
    const page = document.querySelector("[data-handbook]");
    const root = document.querySelector("[data-fee-rules]");
    if (!page || !root || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    initTabs(page);
    const canEdit = page.dataset.canEdit === "true";
    const superAdmin = page.dataset.superAdmin === "true";
    const base = "/handbook/fee-rules";
    const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
    const $ = (selector) => root.querySelector(selector);
    const state = { versions: [], current: null, busy: false, dirty: false };

    function status(text, tone = "") {
      const node = $("[data-fee-status]");
      node.textContent = text || "";
      node.dataset.tone = tone;
    }

    async function request(url, payload) {
      const options = payload === undefined ? { headers: { Accept: "application/json" } } : {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Browser-Request-UUID": window.crypto.randomUUID() },
        body: JSON.stringify(payload),
      };
      const response = await pageFetch(url, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) throw new Error(data.message || data.error?.message || `HTTP ${response.status}`);
      return data.data || {};
    }

    async function run(label, work) {
      if (state.busy) return;
      state.busy = true;
      root.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      status(label);
      try {
        await work();
        status("");
      } catch (error) {
        status(error.message || "操作失败", "error");
      } finally {
        state.busy = false;
        root.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      }
    }

    function renderList() {
      const list = $("[data-fee-list]");
      if (!state.versions.length) {
        list.innerHTML = '<li class="handbook__empty">还没有规则版本。</li>';
        return;
      }
      list.innerHTML = state.versions.map((item) => `<li><button class="handbook__entry" type="button" data-fee-open="${escapeHtml(item.rule_id)}" aria-current="${state.current?.rule_id === item.rule_id}">
        <span>${escapeHtml(item.fee_name)} · ${escapeHtml(item.version_label)}<br><small>${item.effective_from ? `执行日期 ${escapeHtml(item.effective_from)}` : "未填执行日期"}</small></span>
        <span class="handbook__badge" data-status="${escapeHtml(item.status === "APPROVED" ? "ACTIVE" : item.status)}">${escapeHtml(STATUS_TEXT[item.status] || item.status)}</span></button></li>`).join("");
    }

    async function loadList() {
      state.versions = (await request(base)).versions || [];
      renderList();
    }

    async function openVersion(ruleId) {
      state.current = await request(`${base}/${encodeURIComponent(ruleId)}`);
      state.dirty = false;
      renderList();
      renderEditor();
    }

    // -- form ------------------------------------------------------------------------

    const sourceCells = (prefix, source, editable) => `
      <label class="fee__cell fee__cell--doc"><span>出处文档</span><input ${editable} data-f="${prefix}.source.document" value="${escapeHtml(source?.document)}" placeholder="例如：融辉物流网络管理手册 V6.1"></label>
      <label class="fee__cell"><span>页码或条款</span><input ${editable} data-f="${prefix}.source.location" value="${escapeHtml(source?.location)}" placeholder="第五篇第五章 第 50 页"></label>
      <label class="fee__cell fee__cell--doc"><span>链接（选填）</span><input ${editable} data-f="${prefix}.source.link" value="${escapeHtml(source?.link)}" placeholder="https://"></label>`;

    const charge = (key, title, value, editable) => `
      <fieldset class="fee__group"><legend>${title}</legend><div class="fee__row">
        <label class="fee__cell"><span>固定金额（元）</span><input ${editable} inputmode="decimal" data-f="${key}.fixed" value="${escapeHtml(value?.fixed)}"></label>
        <label class="fee__cell"><span>每公斤（元）</span><input ${editable} inputmode="decimal" data-f="${key}.per_kg" value="${escapeHtml(value?.per_kg)}"></label>
        ${sourceCells(key, value?.source, editable)}</div></fieldset>`;

    function tierRows(list, kind, editable) {
      return (list || []).map((tier, index) => {
        const key = `${kind}.${index}`;
        const cells = kind === "floor_tiers"
          ? `<label class="fee__cell"><span>从（楼）</span><input ${editable} inputmode="numeric" data-f="${key}.floor_from" data-int value="${escapeHtml(tier.floor_from)}"></label>
             <label class="fee__cell"><span>到（楼）</span><input ${editable} inputmode="numeric" data-f="${key}.floor_to" data-int value="${escapeHtml(tier.floor_to)}"></label>
             <label class="fee__cell"><span>每公斤（元）</span><input ${editable} inputmode="decimal" data-f="${key}.per_kg" value="${escapeHtml(tier.per_kg)}"></label>`
          : `<label class="fee__cell"><span>单件超过（公斤）</span><input ${editable} inputmode="decimal" data-f="${key}.over_kg" value="${escapeHtml(tier.over_kg)}"></label>
             <label class="fee__cell"><span>至（公斤）</span><input ${editable} inputmode="decimal" data-f="${key}.up_to_kg" value="${escapeHtml(tier.up_to_kg)}"></label>
             <label class="fee__cell"><span>每件（元）</span><input ${editable} inputmode="decimal" data-f="${key}.per_piece" value="${escapeHtml(tier.per_piece)}"></label>`;
        const remove = editable ? "" : `<button class="ghost-btn fee__remove" type="button" data-fee-remove="${kind}" data-index="${index}" aria-label="删除第 ${index + 1} 档">删除</button>`;
        return `<div class="fee__row fee__tier"><span class="fee__tier-no">第 ${index + 1} 档</span>${cells}${sourceCells(key, tier.source, editable)}${remove}</div>`;
      }).join("");
    }

    function renderEditor() {
      const node = $("[data-fee-editor]");
      const record = state.current;
      if (!record) {
        node.innerHTML = '<p class="handbook__empty">从左侧选择一个版本。</p>';
        return;
      }
      const content = record.content || {};
      const draft = record.status === "DRAFT" && canEdit;
      const editable = draft ? "" : "disabled";
      const checksEditable = canEdit && (record.status === "DRAFT" || record.status === "PENDING");
      const addTier = (kind, label) => draft ? `<button class="ghost-btn" type="button" data-fee-add="${kind}"><i data-feather="plus"></i><span>${label}</span></button>` : "";
      const pending = (record.checks || []).filter((item) => !item.done).length;
      const blockers = [];
      if (!record.effective_from) blockers.push("还没有填写执行日期");
      if (pending) blockers.push(`还有 ${pending} 项未核对`);
      const actions = [];
      if (draft) actions.push('<button class="primary-btn" type="button" data-fee-action="save"><i data-feather="save"></i><span>保存草稿</span></button>',
        '<button class="ghost-btn" type="button" data-fee-action="submit"><i data-feather="send"></i><span>提交审核</span></button>');
      if (record.status === "PENDING" && canEdit) actions.push('<button class="ghost-btn" type="button" data-fee-action="checks">保存勾选</button>',
        '<button class="ghost-btn" type="button" data-fee-action="withdraw">退回草稿</button>');
      if (record.status === "PENDING" && superAdmin) actions.push(blockers.length
        ? `<span class="handbook__warn">暂不能生效：${escapeHtml(blockers.join("；"))}</span>`
        : '<button class="primary-btn" type="button" data-fee-action="approve"><i data-feather="check"></i><span>审核通过并生效</span></button>');
      if (record.status === "PENDING" && !superAdmin) actions.push('<span>已提交，等待超级管理员审核。</span>');
      if (record.status === "APPROVED" && canEdit) actions.push('<button class="ghost-btn" type="button" data-fee-action="copy"><i data-feather="copy"></i><span>复制为新草稿</span></button>');
      if (superAdmin && record.status !== "RETIRED") actions.push('<button class="ghost-btn" type="button" data-fee-action="retire">作废</button>');

      node.innerHTML = `
        <div class="handbook__panel-head"><h2 id="fee-editor-title">${escapeHtml(record.fee_name)}规则</h2>
          <span class="handbook__badge" data-status="${escapeHtml(record.status === "APPROVED" ? "ACTIVE" : record.status)}">${escapeHtml(STATUS_TEXT[record.status] || record.status)}</span></div>
        ${record.status === "APPROVED" ? '<p class="fee__hint">已生效的版本不能修改；要改单价或档位，请复制为新草稿。</p>' : ""}
        <div class="fee__row">
          <label class="fee__cell fee__cell--doc"><span>版本名称</span><input ${editable} maxlength="60" data-meta="version_label" value="${escapeHtml(record.version_label)}"></label>
          <label class="fee__cell"><span>执行日期（以总部通知为准）</span><input ${editable} type="date" data-meta="effective_from" value="${escapeHtml(record.effective_from || "")}"></label>
        </div>
        ${charge("base", "基础费（无电梯）", content.base, editable)}
        ${charge("elevator", "有电梯", content.elevator, editable)}
        <fieldset class="fee__group"><legend>无电梯楼层加收（每公斤，从 2 楼起连续分档）</legend>
          ${tierRows(content.floor_tiers, "floor_tiers", editable)}${addTier("floor_tiers", "添加一档")}</fieldset>
        <fieldset class="fee__group"><legend>无电梯最高楼层（超过即“需协商”）</legend><div class="fee__row">
          <label class="fee__cell"><span>最高楼层</span><input ${editable} inputmode="numeric" data-f="max_floor_without_elevator" data-int value="${escapeHtml(content.max_floor_without_elevator)}"></label>
          ${sourceCells("max_floor", content.max_floor_source, editable)}</div></fieldset>
        <fieldset class="fee__group"><legend>送到 1 楼</legend><div class="fee__row">
          <label class="fee__cell"><span>处理方式</span><select ${editable} data-f="first_floor">${Object.entries(FIRST_FLOOR).map(([key, label]) => `<option value="${key}" ${content.first_floor === key ? "selected" : ""}>${label}</option>`).join("")}</select></label></div></fieldset>
        <fieldset class="fee__group"><legend>单件加收（按件，重量区间连续）</legend>
          ${tierRows(content.piece_tiers, "piece_tiers", editable)}${addTier("piece_tiers", "添加一档")}</fieldset>
        <fieldset class="fee__group"><legend>单件重量上限（超过即“需协商”）</legend><div class="fee__row">
          <label class="fee__cell"><span>上限（公斤）</span><input ${editable} inputmode="decimal" data-f="piece_limit_kg" value="${escapeHtml(content.piece_limit_kg)}"></label>
          ${sourceCells("piece_limit", content.piece_limit_source, editable)}</div></fieldset>
        <label class="handbook__field"><span>计算结果附带的提醒（一行一条）</span>
          <textarea ${editable} rows="2" data-meta="reminders">${escapeHtml((content.reminders || []).join("\n"))}</textarea></label>
        <fieldset class="fee__group"><legend>待核对事项（全部勾选后才能生效）</legend>
          ${(record.checks || []).map((item, index) => `<div class="handbook__check fee__check"><input type="checkbox" id="fee-check-${index}" data-check="${index}" ${item.done ? "checked" : ""} ${checksEditable ? "" : "disabled"}>
            <input class="fee__check-text" ${editable} aria-label="事项 ${index + 1}" data-check-text="${index}" value="${escapeHtml(item.text)}">
            ${draft ? `<button class="ghost-btn fee__remove" type="button" data-fee-remove-check="${index}">删除</button>` : ""}</div>`).join("") || "<p>没有待核对事项。</p>"}
          ${draft ? '<button class="ghost-btn" type="button" data-fee-add-check><i data-feather="plus"></i><span>添加事项</span></button>' : ""}</fieldset>
        <div class="handbook__actions">${actions.join("")}</div>
        <div class="handbook__block" data-fee-trial-block>
          <h3>试算</h3>
          <p class="fee__hint">按<b>已保存</b>的内容计算；草稿和待审核版本的结果只供核对，不会对外报金额。</p>
          <div class="fee__row">
            <label class="fee__cell"><span>结算重量（公斤）</span><input inputmode="decimal" data-t="weight" value="200"></label>
            <label class="fee__cell"><span>电梯</span><select data-t="elevator"><option value="false">无电梯</option><option value="true">有电梯</option></select></label>
            <label class="fee__cell"><span>楼层</span><input inputmode="numeric" data-t="floor" value="5"></label>
            ${(content.piece_tiers || []).map((tier, index) => `<label class="fee__cell"><span>单件 ${escapeHtml(tier.over_kg)}–${escapeHtml(tier.up_to_kg)} 公斤（件）</span><input inputmode="numeric" data-t-piece="${index}" value="0"></label>`).join("")}
            <label class="fee__cell"><span>单件超过上限（件）</span><input inputmode="numeric" data-t="over" value="0"></label>
          </div>
          <div class="handbook__actions"${canEdit ? "" : " hidden"}><button class="ghost-btn" type="button" data-fee-trial><i data-feather="play"></i><span>试算</span></button>
            <button class="ghost-btn" type="button" data-fee-trial="boundary"><i data-feather="grid"></i><span>试算并列出分界表</span></button></div>
          <div data-fee-trial-result></div>
        </div>
        ${(record.events || []).length ? `<div class="handbook__block"><h3>操作记录</h3><ul>${record.events.map((item) => `<li>${escapeHtml(item.created_at?.slice(0, 16).replace("T", " "))} ${escapeHtml(ACTION_TEXT[item.action] || item.action)}</li>`).join("")}</ul></div>` : ""}`;
      if (window.feather) window.feather.replace();
    }

    // Reads the form back into the content object; numbers stay strings for Decimal.
    function readForm() {
      const content = JSON.parse(JSON.stringify(state.current.content || {}));
      root.querySelectorAll("[data-f]").forEach((input) => {
        const path = input.dataset.f.replace(/^max_floor\.source/, "max_floor_source").replace(/^piece_limit\.source/, "piece_limit_source").split(".");
        let target = content;
        path.slice(0, -1).forEach((key) => {
          const index = /^\d+$/.test(key) ? Number(key) : key;
          if (target[index] === undefined || target[index] === null) target[index] = {};
          target = target[index];
        });
        const raw = input.value.trim();
        target[path[path.length - 1]] = input.hasAttribute("data-int") ? (raw === "" || Number.isNaN(Number(raw)) ? raw : Number(raw)) : raw;
      });
      content.reminders = $("[data-meta=reminders]").value.split("\n").map((line) => line.trim()).filter(Boolean);
      const checks = (state.current.checks || []).map((item, index) => ({
        text: root.querySelector(`[data-check-text="${index}"]`)?.value.trim() ?? item.text,
        done: root.querySelector(`[data-check="${index}"]`)?.checked ?? item.done,
      }));
      return {
        version_label: $("[data-meta=version_label]").value.trim(),
        effective_from: $("[data-meta=effective_from]").value || null,
        content,
        checks,
      };
    }

    function keepEdits() {
      const form = readForm();
      state.current = { ...state.current, ...form, content: form.content };
      state.dirty = true;
    }

    function renderTrial(data) {
      const result = data.result;
      const amount = result.outcome === "AMOUNT" ? `${escapeHtml(result.amount)} 元` : escapeHtml(OUTCOME_TEXT[result.outcome] || result.outcome);
      let html = `<p><b>结果：${amount}</b>${result.reportable ? "" : "（未生效版本，只供核对）"}</p>
        <ol>${result.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>
        ${result.citations.length ? `<p>出处：${result.citations.map(escapeHtml).join("；")}</p>` : ""}
        ${result.reminders.map((item) => `<p class="handbook__warn">${escapeHtml(item)}</p>`).join("")}`;
      if (data.boundary) {
        html += `<table class="handbook__trial"><caption>分界试算（结算重量 ${escapeHtml($("[data-t=weight]").value)} 公斤）</caption>
          <thead><tr><th scope="col">电梯</th><th scope="col">楼层</th><th scope="col">各档件数</th><th scope="col">超上限</th><th scope="col">结果</th></tr></thead><tbody>
          ${data.boundary.map((row) => `<tr><td>${row.elevator ? "有" : "无"}</td><td>${row.floor}</td><td>${row.piece_counts.join(" / ") || "—"}</td><td>${row.pieces_over_limit}</td>
            <td>${row.outcome === "AMOUNT" ? `${escapeHtml(row.amount)} 元` : escapeHtml(OUTCOME_TEXT[row.outcome] || row.outcome)}</td></tr>`).join("")}</tbody></table>`;
      }
      $("[data-fee-trial-result]").innerHTML = html;
    }

    root.addEventListener("input", (event) => {
      if (event.target.closest("[data-fee-editor]") && !event.target.closest("[data-fee-trial-block]")) state.dirty = true;
    });

    root.addEventListener("click", (event) => {
      const target = event.target.closest("button");
      if (!target || state.busy) return;
      const record = state.current;
      const url = (action) => `${base}/${encodeURIComponent(record.rule_id)}/${action}`;
      if (target.dataset.feeOpen) {
        if (state.dirty && !window.confirm("当前草稿有未保存的修改，确定离开吗？")) return;
        run("正在读取……", () => openVersion(target.dataset.feeOpen));
      } else if (target.dataset.feeAdd && record) {
        keepEdits();
        const list = state.current.content[target.dataset.feeAdd] = state.current.content[target.dataset.feeAdd] || [];
        const last = list[list.length - 1];
        list.push(target.dataset.feeAdd === "floor_tiers"
          ? { floor_from: last ? Number(last.floor_to) + 1 : 2, floor_to: last ? Number(last.floor_to) + 1 : 2, per_kg: "", source: last?.source || {} }
          : { over_kg: last?.up_to_kg || "", up_to_kg: "", per_piece: "", source: last?.source || {} });
        renderEditor();
      } else if (target.dataset.feeRemove && record) {
        keepEdits();
        state.current.content[target.dataset.feeRemove].splice(Number(target.dataset.index), 1);
        renderEditor();
      } else if (target.hasAttribute("data-fee-add-check") && record) {
        keepEdits();
        state.current.checks = [...state.current.checks, { text: "", done: false }];
        renderEditor();
      } else if (target.dataset.feeRemoveCheck && record) {
        keepEdits();
        state.current.checks.splice(Number(target.dataset.feeRemoveCheck), 1);
        renderEditor();
      } else if (target.dataset.feeAction && record) {
        const action = target.dataset.feeAction;
        if (action === "approve" && !window.confirm("确认审核通过？生效后机器人会按这版规则给开单日期在执行日期之后的单据计算金额。")) return;
        if (action === "retire" && !window.confirm("作废后这个版本不再使用，也不能恢复。继续吗？")) return;
        if (action === "submit" && state.dirty) { status("请先保存草稿再提交审核。", "error"); return; }
        run("正在处理……", async () => {
          if (action === "save") {
            await request(url("save"), { ...readForm(), row_version: record.row_version });
          } else if (action === "checks") {
            await request(url("checks"), { checks: readForm().checks, row_version: record.row_version });
          } else if (action === "copy") {
            const created = await request(url("copy"), {});
            await loadList();
            await openVersion(created.rule_id);
            return;
          } else {
            await request(url(action), { row_version: record.row_version });
          }
          await loadList();
          if (action === "retire") { state.current = null; state.dirty = false; renderEditor(); renderList(); } else { await openVersion(record.rule_id); }
        });
      } else if (target.hasAttribute("data-fee-trial") && record) {
        const pieceCounts = [...root.querySelectorAll("[data-t-piece]")].map((input) => Number(input.value || 0));
        run("正在试算……", async () => {
          renderTrial(await request(url("trial"), {
            settlement_weight_kg: $("[data-t=weight]").value.trim(),
            elevator: $("[data-t=elevator]").value === "true",
            floor: Number($("[data-t=floor]").value),
            piece_counts: pieceCounts,
            pieces_over_limit: Number($("[data-t=over]").value || 0),
            boundary: target.dataset.feeTrial === "boundary",
          }));
        });
      }
    });

    run("正在读取……", loadList);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
