(() => {
  "use strict";

  // 业务手册 → 计算规则 (frontend-spec 54 规则编辑, 55 试算与审核). The Agent
  // validates every save, submission, approval and trial; this page edits the
  // form, shows results and forwards actions. Buttons are only hidden by role.
  const STATUS = { DRAFT: "DRAFT", PENDING: "PENDING", APPROVED: "ACTIVE", RETIRED: "RETIRED" };
  const OUTCOME_TEXT = { AMOUNT: "按档计费", NEGOTIATE: "需协商", NOT_PROVIDED: "可不提供服务", MANUAL: "需人工判断", INVALID: "输入不合理" };
  const FIRST_FLOOR = { MANUAL: "需人工判断", NO_CHARGE: "不收上楼费", BASE: "只收基础费" };
  const ACTION_TEXT = { COPY: "复制为草稿", SAVE: "保存草稿", CHECK: "勾选核对", SUBMIT: "提交审核", WITHDRAW: "退回草稿", APPROVE: "审核生效", RETIRE: "作废" };
  // Fixed block order (spec 54); the source path is where each block keeps its 出处.
  const BLOCKS = [
    { key: "base", title: "基础费（无电梯）", source: ["base", "source"] },
    { key: "elevator", title: "有电梯", source: ["elevator", "source"] },
    { key: "floor_tiers", title: "无电梯楼层加收", tiers: true },
    { key: "max_floor", title: "无电梯最高楼层", source: ["max_floor_source"] },
    { key: "first_floor", title: "送到 1 楼" },
    { key: "piece_tiers", title: "单件加收", tiers: true },
    { key: "piece_limit", title: "单件重量上限", source: ["piece_limit_source"] },
    { key: "reminders", title: "计算结果附带的提醒" },
  ];

  function init() {
    const page = document.querySelector("[data-handbook]");
    const root = document.querySelector("[data-fee-rules]");
    if (!page || !root || !window.HandbookUI || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const UI = window.HandbookUI;
    const { escapeHtml, badge, formatTime, request, confirmAction } = UI;
    const { canEdit, superAdmin } = UI.role(page);
    const base = "/handbook/fee-rules";
    const $ = (selector) => root.querySelector(selector);
    const feedback = UI.feedback($("[data-fee-status]"));
    const state = { versions: [], current: null, busy: false, dirty: false, tab: "content", expanded: new Set(["base"]), trial: null, trialRuns: 0, allBoundary: false };

    // -- helpers -----------------------------------------------------------------------

    const get = (object, path) => path.reduce((value, key) => (value == null ? undefined : value[key]), object);
    function set(object, path, value) {
      let target = object;
      path.slice(0, -1).forEach((key, index) => {
        if (target[key] == null || typeof target[key] !== "object") target[key] = /^\d+$/.test(path[index + 1]) ? [] : {};
        target = target[key];
      });
      target[path[path.length - 1]] = value;
    }
    const filled = (source) => Boolean(String(source?.document || "").trim() && String(source?.location || "").trim());
    const num = (value) => (value === "" || value == null ? "—" : String(value));

    function sourceList(content) {
      const list = [];
      BLOCKS.forEach((block) => {
        if (block.tiers) (content[block.key] || []).forEach((tier, index) => list.push({ block: block.key, label: `${block.title}第 ${index + 1} 档`, ok: filled(tier.source) }));
        else if (block.source) list.push({ block: block.key, label: block.title, ok: filled(get(content, block.source)) });
      });
      return list;
    }

    function blockSourceState(block, content) {
      if (block.tiers) {
        const tiers = content[block.key] || [];
        if (!tiers.length) return block.key === "piece_tiers" ? "none" : "missing";
        return tiers.every((tier) => filled(tier.source)) ? "ok" : "missing";
      }
      return block.source ? (filled(get(content, block.source)) ? "ok" : "missing") : "none";
    }

    function summary(block, content) {
      const range = (values) => { const list = values.filter((value) => value !== "" && value != null).map(Number); if (!list.length) return "—"; const low = Math.min(...list); const high = Math.max(...list); return low === high ? String(low) : `${low}–${high}`; };
      switch (block.key) {
        case "base": case "elevator": { const charge = content[block.key] || {}; return `固定 ${num(charge.fixed)} 元 + ${num(charge.per_kg)} 元/kg`; }
        case "floor_tiers": { const tiers = content.floor_tiers || []; return tiers.length ? `${tiers.length} 档 · ${num(tiers[0].floor_from)}–${num(tiers[tiers.length - 1].floor_to)} 楼 · ${range(tiers.map((tier) => tier.per_kg))} 元/kg` : "未设置"; }
        case "max_floor": return `${num(content.max_floor_without_elevator)} 楼 · 超过需协商`;
        case "first_floor": return FIRST_FLOOR[content.first_floor] || "未选择";
        case "piece_tiers": { const tiers = content.piece_tiers || []; return tiers.length ? `${tiers.length} 档 · ${num(tiers[0].over_kg)}–${num(tiers[tiers.length - 1].up_to_kg)}kg · ${range(tiers.map((tier) => tier.per_piece))} 元/件` : "不加收"; }
        case "piece_limit": return `${num(content.piece_limit_kg)}kg · 超过需协商`;
        case "reminders": return `${(content.reminders || []).length} 条`;
        default: return "";
      }
    }

    // Draft checks shown on the right; submission needs the first three.
    function checks(record) {
      const content = record.content || {};
      const sources = sourceList(content);
      const okSources = sources.filter((item) => item.ok).length;
      const open = (record.checks || []).filter((item) => !item.done).length;
      return [
        { key: "basic", label: "基本信息", ok: Boolean(record.version_label && record.effective_from), detail: !record.effective_from ? "执行日期未填" : !record.version_label ? "版本名称未填" : "已填写", gate: true },
        { key: "content", label: "规则内容", ok: okSources === sources.length, detail: `${okSources} / ${sources.length} 项出处完整`, gate: true, tab: "content" },
        { key: "checks", label: "待核对", ok: open === 0, detail: open ? `${open} 项未勾选` : `${(record.checks || []).length} 项已勾选`, gate: true, tab: "checks" },
        { key: "trial", label: "试算", ok: state.trialRuns > 0, detail: state.trialRuns ? `本次已试算 ${state.trialRuns} 次` : "建议提交前试算", tab: "trial" },
      ];
    }

    // -- list --------------------------------------------------------------------------

    function renderList() {
      const list = $("[data-fee-list]");
      const drafts = state.versions.filter((item) => item.status === "DRAFT").length;
      const pending = state.versions.filter((item) => item.status === "PENDING").length;
      $("[data-fee-summary]").textContent = `${state.versions.length} 个版本${drafts ? ` · ${drafts} 个草稿` : ""}${pending ? ` · ${pending} 个待审核` : ""}`;
      UI.setTabCount("fees", state.versions.length);
      if (!state.versions.length) {
        list.innerHTML = '<li class="hb-empty hb-empty--row"><i data-feather="file-text"></i><p>还没有规则版本。</p></li>';
        if (window.feather) window.feather.replace();
        return;
      }
      let html = "";
      let group = "";
      state.versions.forEach((item) => {
        if (item.fee_name !== group) { group = item.fee_name; html += `<li class="hb-rows__group"><b>${escapeHtml(group)}</b></li>`; }
        const when = item.status === "APPROVED" ? `生效于 ${formatTime(item.approved_at)}` : item.status === "PENDING" ? `提交于 ${formatTime(item.submitted_at)}` : `保存于 ${formatTime(item.updated_at)}`;
        html += `<li><button class="hb-row" type="button" data-fee-open="${escapeHtml(item.rule_id)}" aria-current="${state.current?.rule_id === item.rule_id}">
          <span class="hb-row__top"><span class="hb-row__title">${escapeHtml(item.version_label)}</span>${badge(STATUS[item.status])}</span>
          <span class="hb-row__meta"${item.effective_from ? "" : ' data-tone="warn"'}>执行日期 ${item.effective_from ? escapeHtml(item.effective_from) : "未填"}</span>
          <span class="hb-row__time">${escapeHtml(when)}</span></button>
          ${item.status === "APPROVED" && canEdit ? `<button class="hb-link hb-row__aside" type="button" data-fee-copy="${escapeHtml(item.rule_id)}">复制</button>` : ""}</li>`;
      });
      list.innerHTML = html;
    }

    // -- detail ------------------------------------------------------------------------

    function input(path, value, { editable, mode = "decimal", int = false, wide = false, placeholder = "" } = {}) {
      return `<input ${editable ? "" : "disabled"} inputmode="${mode}" data-f="${path.join(".")}"${int ? " data-int" : ""}${wide ? ' class="hb-input--wide"' : ""} value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(placeholder)}">`;
    }

    function sourceFields(path, source, editable) {
      return `<div class="hb-fee__source">
        <label class="hb-cell hb-cell--doc"><span>出处文档</span>${input([...path, "document"], source?.document, { editable, mode: "text", placeholder: "例如：融辉物流网络管理手册 V6.1" })}</label>
        <label class="hb-cell"><span>页码或条款</span>${input([...path, "location"], source?.location, { editable, mode: "text", placeholder: "第五篇第五章 第 50 页" })}</label>
        <label class="hb-cell"><span>链接（选填）</span>${input([...path, "link"], source?.link, { editable, mode: "url", placeholder: "https://" })}</label></div>`;
    }

    function blockBody(block, content, editable) {
      const cell = (label, path, opts = {}) => `<label class="hb-cell"><span>${label}</span>${input(path, get(content, path), { editable, ...opts })}</label>`;
      switch (block.key) {
        case "base": case "elevator":
          return `<div class="hb-fee__cells">${cell("固定金额（元）", [block.key, "fixed"])}${cell("每公斤（元）", [block.key, "per_kg"])}</div>${sourceFields(block.source, get(content, block.source), editable)}`;
        case "max_floor":
          return `<div class="hb-fee__cells">${cell("最高楼层", ["max_floor_without_elevator"], { mode: "numeric", int: true })}<p class="hb-meta">超过这个楼层：需协商（可不提供服务）。</p></div>${sourceFields(block.source, get(content, block.source), editable)}`;
        case "piece_limit":
          return `<div class="hb-fee__cells">${cell("上限（公斤）", ["piece_limit_kg"])}<p class="hb-meta">单件超过上限：需协商。</p></div>${sourceFields(block.source, get(content, block.source), editable)}`;
        case "first_floor":
          return `<div class="hb-fee__cells"><label class="hb-cell"><span>处理方式</span><select ${editable ? "" : "disabled"} data-f="first_floor">${Object.entries(FIRST_FLOOR).map(([key, label]) => `<option value="${key}" ${content.first_floor === key ? "selected" : ""}>${label}</option>`).join("")}</select></label></div>`;
        case "reminders":
          return `<label class="hb-cell hb-cell--full"><span>一行一条，随计算结果一起返回（最多 10 条）</span><textarea ${editable ? "" : "disabled"} rows="3" data-reminders>${escapeHtml((content.reminders || []).join("\n"))}</textarea></label>`;
        case "floor_tiers": case "piece_tiers": {
          const tiers = content[block.key] || [];
          const rows = tiers.map((tier, index) => {
            const path = [block.key, String(index)];
            const cells = block.key === "floor_tiers"
              ? `${cell("从（楼）", [...path, "floor_from"], { mode: "numeric", int: true })}${cell("到（楼）", [...path, "floor_to"], { mode: "numeric", int: true })}${cell("每公斤（元）", [...path, "per_kg"])}`
              : `${cell("单件超过（公斤）", [...path, "over_kg"])}${cell("至（公斤）", [...path, "up_to_kg"])}${cell("每件（元）", [...path, "per_piece"])}`;
            return `<div class="hb-tier"><div class="hb-tier__head"><b>第 ${index + 1} 档</b>${editable ? `<button class="hb-link" data-tone="danger" type="button" data-fee-remove="${block.key}" data-index="${index}">删除</button>` : ""}</div>
              <div class="hb-fee__cells">${cells}</div>${sourceFields([...path, "source"], tier.source, editable)}</div>`;
          }).join("");
          const hint = block.key === "floor_tiers" ? "从 2 楼起连续分档，最后一档到无电梯最高楼层。" : "按单件重量区间连续分档，最后一档到单件重量上限。";
          return `<p class="hb-meta">${hint}</p>${rows || '<p class="hb-meta">还没有分档。</p>'}${editable ? `<button class="ghost-btn hb-btn-sm" type="button" data-fee-add="${block.key}"><i data-feather="plus"></i><span>添加一档</span></button>` : ""}`;
        }
        default: return "";
      }
    }

    function renderContentTab(record, editable) {
      const content = record.content || {};
      return `${BLOCKS.map((block) => {
        const open = state.expanded.has(block.key);
        const source = blockSourceState(block, content);
        const sourceBadge = source === "ok" ? '<span class="hb-badge" data-tone="active">出处完整</span>' : source === "missing" ? '<span class="hb-badge" data-tone="pending">缺出处</span>' : "";
        return `<section class="hb-fold" data-open="${open}">
          <button class="hb-fold__head" type="button" data-fold="${block.key}" aria-expanded="${open}">
            <i data-feather="${open ? "chevron-down" : "chevron-right"}"></i><b>${block.title}</b><span class="hb-fold__summary">${escapeHtml(summary(block, content))}</span>${sourceBadge}</button>
          ${open ? `<div class="hb-fold__body">${blockBody(block, content, editable)}</div>` : ""}</section>`;
      }).join("")}
      <p class="hb-alert" data-tone="info"><i data-feather="info"></i><span>计算顺序由系统固定：单件超上限 → 需协商；有电梯 → 有电梯收费；无电梯超过最高楼层 → 需协商；否则基础费 + 楼层加收 + 单件加收。</span></p>`;
    }

    function renderChecksTab(record) {
      const editable = canEdit && (record.status === "DRAFT" || record.status === "PENDING");
      const textEditable = canEdit && record.status === "DRAFT";
      const list = record.checks || [];
      return `<p class="hb-meta">逐项对照手册原文核对，全部勾选后才能生效。${record.status === "PENDING" ? "审核中仍可勾选，记得点“保存勾选”。" : ""}</p>
        <ul class="hb-checkitems">${list.map((item, index) => `<li><input type="checkbox" data-check="${index}" ${item.done ? "checked" : ""} ${editable ? "" : "disabled"} aria-label="事项 ${index + 1} 已核对">
          <input class="hb-checkitems__text" ${textEditable ? "" : "disabled"} data-check-text="${index}" value="${escapeHtml(item.text)}" aria-label="事项 ${index + 1}">
          ${textEditable ? `<button class="hb-link" type="button" data-fee-remove-check="${index}" aria-label="删除事项 ${index + 1}">×</button>` : ""}</li>`).join("") || '<li class="hb-meta">没有待核对事项。</li>'}</ul>
        <div class="hb-actions">${textEditable ? '<button class="ghost-btn hb-btn-sm" type="button" data-fee-add-check><i data-feather="plus"></i><span>添加事项</span></button>' : ""}
          ${record.status === "PENDING" && canEdit ? '<button class="primary-btn hb-btn-sm" type="button" data-fee-action="checks">保存勾选</button>' : ""}</div>`;
    }

    function renderTrialTab(record) {
      const content = record.content || {};
      if (!canEdit) return '<div class="hb-empty"><i data-feather="lock"></i><p>试算需要“业务手册”的可操作权限。</p></div>';
      const values = state.trialInputs || {};
      const field = (label, attr, value, mode = "decimal") => `<label class="hb-cell"><span>${label}</span><input inputmode="${mode}" ${attr} value="${escapeHtml(value)}"></label>`;
      return `<section class="hb-card hb-card--inset"><h3>试算</h3><p class="hb-meta">按已保存的 ${escapeHtml(record.version_label)} 内容计算${record.status === "APPROVED" ? "" : "；未生效版本只供核对"}。</p>
        <div class="hb-fee__cells hb-fee__cells--trial">
          ${field("结算重量（kg）", 'data-t="weight"', values.weight ?? "200")}
          <label class="hb-cell"><span>电梯</span><select data-t="elevator"><option value="false">无电梯</option><option value="true" ${values.elevator === "true" ? "selected" : ""}>有电梯</option></select></label>
          ${field("楼层", 'data-t="floor"', values.floor ?? "5", "numeric")}
          ${(content.piece_tiers || []).map((tier, index) => field(`${num(tier.over_kg)}–${num(tier.up_to_kg)}kg 件数`, `data-t-piece="${index}"`, values[`piece${index}`] ?? "0", "numeric")).join("")}
          ${field("超上限件数", 'data-t="over"', values.over ?? "0", "numeric")}
        </div>
        <div class="hb-actions"><button class="primary-btn" type="button" data-fee-trial>试算</button><button class="ghost-btn" type="button" data-fee-trial="boundary">分界试算表</button><span class="hb-meta">↵ 回车即可试算</span></div></section>
        <div data-fee-trial-result>${state.trial ? trialHtml(state.trial, record) : ""}</div>`;
    }

    function trialHtml(data, record) {
      const result = data.result;
      const inputs = data.inputs;
      const amount = result.outcome === "AMOUNT" ? `¥ ${escapeHtml(result.amount)}` : escapeHtml(OUTCOME_TEXT[result.outcome] || result.outcome);
      let html = `<section class="hb-card hb-card--inset hb-trial">
        <div class="hb-card__head"><h3>试算结果</h3>${result.reportable ? '<span class="hb-badge" data-tone="active">已生效规则</span>' : '<span class="hb-badge" data-tone="pending">未生效 · 只供核对</span>'}</div>
        <div class="hb-trial__amount"><b>${amount}</b><span class="hb-meta">按 ${escapeHtml(result.rule_version || record.version_label)} 规则计算</span></div>
        <p class="hb-meta">${escapeHtml(inputs.weight)}kg · ${escapeHtml(inputs.floor)} 楼 · ${inputs.elevator ? "有电梯" : "无电梯"}</p>
        <ol class="hb-trial__steps">${result.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>
        ${result.citations.length ? `<p class="hb-meta">出处：${result.citations.map(escapeHtml).join("；")}</p>` : ""}
        ${result.reminders.map((item) => `<p class="hb-alert" data-tone="warn"><i data-feather="info"></i><span>提醒：${escapeHtml(item)}</span></p>`).join("")}</section>`;
      if (data.boundary) {
        const plain = (row) => row.pieces_over_limit === 0 && row.piece_counts.every((count) => count === 0);
        const rows = state.allBoundary ? data.boundary : data.boundary.filter(plain);
        html += `<section class="hb-card hb-card--inset"><div class="hb-card__head"><h3>分界试算表</h3><span class="hb-meta">同一结算重量 ${escapeHtml(inputs.weight)}kg${state.allBoundary ? "" : " · 件数为 0"}</span>
          <button class="hb-link" type="button" data-boundary-toggle>${state.allBoundary ? "只看件数为 0" : `显示全部 ${data.boundary.length} 行`}</button></div>
          <div class="hb-table-wrap"><table class="hb-table hb-table--boundary"><colgroup><col style="width:52px"><col style="width:84px"><col style="width:108px"><col style="width:86px"><col style="width:142px"></colgroup>
          <thead><tr><th>楼层</th><th>电梯</th><th>档位件数</th><th>金额</th><th>结果</th></tr></thead><tbody>
          ${rows.map((row) => `<tr${row.floor === inputs.floor && row.elevator === inputs.elevator && plain(row) ? ' data-current="true"' : ""}><td>${row.floor} 楼</td><td>${row.elevator ? "有电梯" : "无电梯"}</td>
            <td>${row.piece_counts.join(" / ") || "0"}${row.pieces_over_limit ? ` · 超上限 ${row.pieces_over_limit}` : ""}</td><td>${row.outcome === "AMOUNT" ? `¥ ${escapeHtml(row.amount)}` : "—"}</td>
            <td data-tone="${row.outcome === "AMOUNT" ? "ok" : row.outcome === "INVALID" ? "danger" : "warn"}">${escapeHtml(OUTCOME_TEXT[row.outcome] || row.outcome)}</td></tr>`).join("")}</tbody></table></div>
          <p class="hb-meta">超出最高楼层需协商；1 楼按“送到 1 楼”的处理方式；未生效版本不对外报价。</p></section>`;
      }
      return html;
    }

    function renderEventsTab(record) {
      const events = record.events || [];
      return events.length ? `<ul class="hb-events">${events.map((item) => `<li><span class="hb-meta">${escapeHtml(formatTime(item.created_at))}</span><b>${escapeHtml(ACTION_TEXT[item.action] || item.action)}</b></li>`).join("")}</ul>`
        : '<p class="hb-meta">还没有操作记录。</p>';
    }

    function renderSide(record) {
      const list = checks(record);
      const gates = list.filter((item) => item.gate && !item.ok);
      const passed = list.filter((item) => item.ok).length;
      const items = list.map((item) => `<li data-tone="${item.ok ? "ok" : item.gate ? "warn" : "muted"}">${item.tab ? `<button class="hb-link" type="button" data-fee-tab="${item.tab}">${item.label}</button>` : `<b>${item.label}</b>`}<span>${escapeHtml(item.detail)}</span></li>`).join("");
      if (record.status === "DRAFT") {
        return `<section class="hb-card hb-side"><div class="hb-card__head"><h3>生效检查</h3><span class="hb-meta" data-tone="${gates.length ? "warn" : "ok"}">${gates.length ? `还差 ${gates.length} 项` : "可提交"}</span></div>
          <div class="hb-progress"><span style="width:${(passed / list.length) * 100}%"></span></div><p class="hb-meta">${passed} / ${list.length} 项通过</p>
          <ul class="hb-checklist hb-checklist--stack">${items}</ul>
          <p class="hb-alert" data-tone="info"><i data-feather="lock"></i><span><b>草稿状态仅限内部核对</b><br>不会对外报价，也不影响线上单据。</span></p></section>`;
      }
      if (record.status === "PENDING") {
        const ready = !gates.length;
        const date = record.effective_from || "（未填执行日期）";
        return `<section class="hb-card hb-side"><div class="hb-card__head"><h3>审核</h3>${badge("PENDING")}</div>
          <p class="hb-meta" data-tone="${ready ? "ok" : "warn"}"><i data-feather="${ready ? "check-circle" : "alert-triangle"}"></i>${ready ? "已满足生效条件" : `还差 ${gates.length} 项：${escapeHtml(gates.map((item) => item.detail).join("；"))}`}</p>
          <ul class="hb-checklist hb-checklist--stack">${items}</ul>
          <div class="hb-impact"><b>通过并生效后</b><span>开单日期在 ${escapeHtml(date)}（含）之后的单据按这版规则计算；之前的单据不受影响。</span></div>
          <div class="hb-impact"><b>生效时点</b><span>${escapeHtml(date)} 00:00 · 以总部通知为准</span></div>
          ${canEdit ? `<div class="hb-actions hb-actions--split"><button class="ghost-btn" type="button" data-fee-action="withdraw">退回草稿</button>
            ${superAdmin ? `<button class="primary-btn" type="button" data-fee-action="approve" ${ready ? "" : "disabled"}><i data-feather="check"></i><span>通过并生效</span></button>` : ""}</div>` : ""}
          ${superAdmin ? '<div class="hb-danger-row"><button class="hb-btn-danger-text" type="button" data-fee-action="retire"><i data-feather="trash-2"></i>作废此版本</button><span class="hb-meta">不可恢复，需二次确认</span></div>' : '<p class="hb-meta">已提交，等待超级管理员审核。</p>'}
          ${(record.events || []).length ? `<div class="hb-block"><h3>操作记录</h3>${renderEventsTab({ events: record.events.slice(0, 4) })}</div>` : ""}</section>`;
      }
      return `<section class="hb-card hb-side"><div class="hb-card__head"><h3>已生效</h3>${badge("ACTIVE")}</div>
        <div class="hb-impact"><b>适用范围</b><span>开单日期在 ${escapeHtml(record.effective_from || "")}（含）之后、下一版执行日期之前的单据按这版计算。</span></div>
        <p class="hb-meta">已生效的版本不能修改；要改单价或档位，请复制为新草稿。</p>
        ${canEdit ? '<button class="primary-btn" type="button" data-fee-action="copy"><i data-feather="copy"></i><span>复制为新草稿</span></button>' : ""}
        ${superAdmin ? '<div class="hb-danger-row"><button class="hb-btn-danger-text" type="button" data-fee-action="retire"><i data-feather="trash-2"></i>作废此版本</button><span class="hb-meta">不可恢复，需二次确认</span></div>' : ""}</section>`;
    }

    function renderBar(record) {
      const gates = checks(record).filter((item) => item.gate && !item.ok);
      if (record.status === "DRAFT") {
        const saved = state.dirty ? '<i class="hb-dot" data-tone="warn"></i>有未保存的修改' : `<i class="hb-dot" data-tone="ok"></i>已保存 ${escapeHtml(formatTime(record.updated_at))}`;
        if (!canEdit) return `<div class="hb-card__bar"><span class="hb-meta">${saved}</span></div>`;
        const reason = state.dirty ? "请先保存草稿" : gates.length ? `还差 ${gates.length} 项，补齐后可提交` : "";
        return `<div class="hb-card__bar"><span class="hb-meta" data-fee-saved>${saved}</span><div class="hb-actions">
          ${superAdmin ? '<button class="hb-btn-danger-text" type="button" data-fee-action="retire">作废</button>' : ""}
          <button class="ghost-btn" type="button" data-fee-action="save">保存草稿</button>
          <span class="hb-submit-wrap"><button class="primary-btn" type="button" data-fee-action="submit" ${reason ? "disabled" : ""}>提交审核</button>${reason ? `<span class="hb-meta" data-tone="warn">${escapeHtml(reason)}</span>` : ""}</span></div></div>`;
      }
      if (record.status === "PENDING") return `<div class="hb-card__bar"><span class="hb-meta">已提交 ${escapeHtml(formatTime(record.submitted_at))}<br><i data-feather="lock"></i>审核中，规则内容不可修改；退回草稿后可继续编辑</span></div>`;
      return `<div class="hb-card__bar"><span class="hb-meta">生效于 ${escapeHtml(formatTime(record.approved_at))}<br><i data-feather="lock"></i>已生效版本只读；要修改请复制为新草稿</span></div>`;
    }

    function renderEditor() {
      const node = $("[data-fee-editor]");
      const record = state.current;
      if (!record) {
        node.innerHTML = '<div class="hb-empty"><i data-feather="file-text"></i><p>从左侧选择一个规则版本。</p></div>';
        if (window.feather) window.feather.replace();
        return;
      }
      const editable = record.status === "DRAFT" && canEdit;
      const openChecks = (record.checks || []).filter((item) => !item.done).length;
      const sub = record.status === "PENDING" ? `执行日期 ${record.effective_from || "未填"} · 提交于 ${formatTime(record.submitted_at)}`
        : record.status === "APPROVED" ? `执行日期 ${record.effective_from} · 生效于 ${formatTime(record.approved_at)}` : "计算方式由系统固定，只填数字与出处";
      const tabs = [["content", "规则内容"], ["checks", `待核对${openChecks ? ` <span class="hb-tab__count" data-tone="warn">${openChecks}</span>` : ""}`], ["trial", "试算"], ["events", "操作记录"]];
      const bodies = { content: () => renderContentTab(record, editable), checks: () => renderChecksTab(record), trial: () => renderTrialTab(record), events: () => renderEventsTab(record) };
      node.innerHTML = `
        <div class="hb-card__head"><div><h2 id="fee-editor-title">${escapeHtml(record.fee_name)} <span class="hb-chip hb-chip--sm">${escapeHtml(record.version_label)}</span> ${badge(STATUS[record.status])}</h2>
          <p class="hb-card__sub">${escapeHtml(sub)}</p></div></div>
        <div class="hb-fee__basic">
          <label class="hb-cell hb-cell--doc"><span>版本名称</span><input ${editable ? "" : "disabled"} maxlength="60" data-meta="version_label" value="${escapeHtml(record.version_label)}"></label>
          <label class="hb-cell"><span>执行日期（以总部通知为准）</span><input ${editable ? "" : "disabled"} type="date" data-meta="effective_from" value="${escapeHtml(record.effective_from || "")}"></label>
          ${editable ? (record.effective_from ? "" : '<span class="hb-meta" data-tone="warn"><i data-feather="alert-triangle"></i>未填执行日期，暂不能生效</span>')
            : `<span class="hb-meta"><i data-feather="lock"></i>${record.status === "PENDING" ? "审核中，字段锁定" : "已生效，只读"}</span>`}
        </div>
        <div class="hb-subtabs" role="tablist" aria-label="规则详情">
          ${tabs.map(([key, label]) => `<button type="button" role="tab" aria-selected="${state.tab === key}" data-fee-tab="${key}">${label}</button>`).join("")}
          ${state.tab === "content" ? `<button class="hb-link hb-subtabs__aside" type="button" data-fold-all>${state.expanded.size >= BLOCKS.length ? "全部收起" : "全部展开"}</button>` : ""}
        </div>
        <div class="hb-fee__grid"><div class="hb-fee__main">${bodies[state.tab]()}</div>${renderSide(record)}</div>
        ${renderBar(record)}`;
      if (window.feather) window.feather.replace();
    }

    // -- data --------------------------------------------------------------------------

    async function loadList() {
      state.versions = (await request(base)).versions || [];
      renderList();
    }

    async function openVersion(ruleId) {
      const record = await request(`${base}/${encodeURIComponent(ruleId)}`);
      if (state.current?.rule_id !== ruleId) { state.trial = null; state.trialRuns = 0; state.tab = record.status === "PENDING" ? "trial" : "content"; state.expanded = new Set(["base"]); }
      state.current = record;
      state.dirty = false;
      renderList();
      renderEditor();
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
        feedback.error("操作没有完成", error.message || "操作失败");
      } finally {
        state.busy = false;
        unlock();
      }
    }

    function payload() {
      const record = state.current;
      return { version_label: String(record.version_label || "").trim(), effective_from: record.effective_from || null, content: record.content,
        checks: (record.checks || []).map((item) => ({ text: String(item.text || "").trim(), done: Boolean(item.done) })) };
    }

    function rememberTrialInputs() {
      const values = {};
      root.querySelectorAll("[data-t], [data-t-piece]").forEach((node) => { values[node.dataset.t || `piece${node.dataset.tPiece}`] = node.value; });
      state.trialInputs = values;
      return values;
    }

    function runTrial(boundary) {
      const record = state.current;
      const values = rememberTrialInputs();
      const weight = String(values.weight || "").trim();
      const floor = Number(values.floor);
      const counts = (record.content.piece_tiers || []).map((_tier, index) => Number(values[`piece${index}`] || 0));
      const over = Number(values.over || 0);
      const problems = [];
      if (!/^\d+(\.\d+)?$/.test(weight) || Number(weight) <= 0) problems.push("结算重量要大于 0");
      if (!Number.isInteger(floor) || floor < 1) problems.push("楼层要是 1 或以上的整数");
      if ([...counts, over].some((count) => !Number.isInteger(count) || count < 0)) problems.push("件数要是 0 或正整数");
      if (problems.length) { feedback.error("输入不合理，没有试算", problems.join("；")); return; }
      run("正在试算……", async () => {
        const data = await request(`${base}/${encodeURIComponent(record.rule_id)}/trial`, {
          settlement_weight_kg: weight, elevator: values.elevator === "true", floor, piece_counts: counts, pieces_over_limit: over, boundary,
        });
        state.trial = { ...data, inputs: { weight, floor, elevator: values.elevator === "true" } };
        state.trialRuns += 1;
        renderEditor();
      });
    }

    async function leaveOk() {
      if (!state.dirty) return true;
      return confirmAction({ title: "有未保存的修改", body: "离开后本次对草稿的修改不会保存。", confirm: "放弃并离开", cancel: "继续编辑", danger: true });
    }

    // -- events ------------------------------------------------------------------------

    function markDirty() {
      state.dirty = true;
      const saved = $("[data-fee-saved]");
      if (saved) saved.innerHTML = '<i class="hb-dot" data-tone="warn"></i>有未保存的修改';
      const submit = $('[data-fee-action="submit"]');
      if (submit) submit.disabled = true;
    }

    root.addEventListener("input", (event) => {
      const target = event.target;
      const record = state.current;
      if (!record || target.closest("[data-t], [data-t-piece]") || target.matches("[data-t], [data-t-piece]")) return;
      if (target.dataset.f) {
        const raw = target.value.trim();
        const value = target.hasAttribute("data-int") && raw !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw;
        set(record.content, target.dataset.f.split("."), value);
      } else if (target.dataset.meta) {
        record[target.dataset.meta] = target.value || (target.dataset.meta === "effective_from" ? null : "");
      } else if (target.hasAttribute("data-reminders")) {
        record.content.reminders = target.value.split("\n").map((line) => line.trim()).filter(Boolean);
      } else if (target.dataset.checkText !== undefined) {
        record.checks[Number(target.dataset.checkText)].text = target.value;
      } else if (target.dataset.check !== undefined) {
        record.checks[Number(target.dataset.check)].done = target.checked;
        if (record.status !== "DRAFT") return;
      } else return;
      markDirty();
    });

    root.addEventListener("change", (event) => {
      // Selects and date inputs also refresh summaries and checks on change.
      if (event.target.matches("select[data-f], [data-meta='effective_from'], [data-check]") && state.current) {
        if (event.target.matches("[data-check]") && state.current.status !== "DRAFT") return;
        renderEditor();
      }
    });

    root.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target.matches("[data-t], [data-t-piece]")) { event.preventDefault(); runTrial(false); }
    });

    root.addEventListener("click", async (event) => {
      const target = event.target.closest("button");
      if (!target || target.disabled || state.busy) return;
      const record = state.current;
      const url = (action, id = record?.rule_id) => `${base}/${encodeURIComponent(id)}/${action}`;
      if (target.dataset.feeOpen) {
        if (target.dataset.feeOpen === record?.rule_id || !(await leaveOk())) return;
        run("正在读取……", () => openVersion(target.dataset.feeOpen));
      } else if (target.dataset.feeCopy) {
        if (!(await leaveOk())) return;
        run("正在复制……", async () => { const created = await request(url("copy", target.dataset.feeCopy), {}); await loadList(); await openVersion(created.rule_id); }, "已复制为新草稿");
      } else if (target.dataset.feeTab && record) {
        if (target.dataset.feeTab === "trial") rememberTrialInputs();
        state.tab = target.dataset.feeTab; renderEditor();
      } else if (target.dataset.fold && record) {
        const key = target.dataset.fold;
        if (state.expanded.has(key)) state.expanded.delete(key); else state.expanded.add(key);
        renderEditor();
      } else if (target.hasAttribute("data-fold-all") && record) {
        state.expanded = state.expanded.size >= BLOCKS.length ? new Set() : new Set(BLOCKS.map((block) => block.key));
        renderEditor();
      } else if (target.hasAttribute("data-boundary-toggle")) {
        state.allBoundary = !state.allBoundary; renderEditor();
      } else if (target.dataset.feeAdd && record) {
        const list = record.content[target.dataset.feeAdd] = record.content[target.dataset.feeAdd] || [];
        const last = list[list.length - 1];
        list.push(target.dataset.feeAdd === "floor_tiers"
          ? { floor_from: last ? Number(last.floor_to) + 1 : 2, floor_to: last ? Number(last.floor_to) + 1 : 2, per_kg: "", source: { ...(last?.source || {}) } }
          : { over_kg: last?.up_to_kg || "", up_to_kg: "", per_piece: "", source: { ...(last?.source || {}) } });
        state.dirty = true; renderEditor();
      } else if (target.dataset.feeRemove && record) {
        record.content[target.dataset.feeRemove].splice(Number(target.dataset.index), 1);
        state.dirty = true; renderEditor();
      } else if (target.hasAttribute("data-fee-add-check") && record) {
        record.checks = [...(record.checks || []), { text: "", done: false }];
        state.dirty = true; renderEditor();
        root.querySelector(`[data-check-text="${record.checks.length - 1}"]`)?.focus();
      } else if (target.dataset.feeRemoveCheck !== undefined && record) {
        record.checks.splice(Number(target.dataset.feeRemoveCheck), 1);
        state.dirty = true; renderEditor();
      } else if (target.hasAttribute("data-fee-trial") && record) {
        runTrial(target.dataset.feeTrial === "boundary");
      } else if (target.dataset.feeAction && record) {
        const action = target.dataset.feeAction;
        if (action === "approve" && !(await confirmAction({ title: "审核通过并生效？", body: `生效后，开单日期在 ${record.effective_from}（含）之后的单据按 ${record.version_label} 计算；之前的单据不受影响。`, confirm: "通过并生效" }))) return;
        if (action === "retire" && !(await confirmAction({ title: "作废这个版本？", body: "作废后不可恢复，机器人不会再使用该版本，也无法恢复。", confirm: "确认作废", danger: true }))) return;
        if (action === "withdraw" && !(await confirmAction({ title: "退回草稿？", body: "退回后版本回到草稿，可以继续修改，需要重新提交审核。", confirm: "退回草稿" }))) return;
        const labels = { save: "已保存草稿", checks: "已保存勾选", submit: "已提交审核", withdraw: "已退回草稿", approve: "已审核生效", retire: "已作废", copy: "已复制为新草稿" };
        run("正在处理……", async () => {
          if (action === "save") await request(url("save"), { ...payload(), row_version: record.row_version });
          else if (action === "checks") await request(url("checks"), { checks: payload().checks, row_version: record.row_version });
          else if (action === "copy") { const created = await request(url("copy"), {}); await loadList(); await openVersion(created.rule_id); return; }
          else await request(url(action), { row_version: record.row_version });
          await loadList();
          if (action === "retire") { state.current = null; state.dirty = false; renderEditor(); renderList(); } else { state.dirty = false; await openVersion(record.rule_id); }
        }, labels[action]);
      }
    });

    run("正在读取……", loadList);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
