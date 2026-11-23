(() => {
  "use strict";

  // 业务手册 → 计算规则, six-step layout (frontend-spec 59–69, 2026-11-23). One fee version at a
  // time goes through ① 起草 → ② 核对原文 → ③ 拍板 → ④ 试算 → ⑤ 日期与提交 → ⑥ 审核; approved
  // versions show a read-only overview (68). Left: the fee list (240); right: header 56, stepper
  // 72, step body, footer 56 with the auto-save state and 上一步 / 下一步. Every change is saved
  // about a second after typing stops (no 保存草稿 button); two people editing the same draft get
  // “载入最新” instead of overwriting each other (row_version). Step states are derived from the
  // saved version only (lines ticked, questions answered, trials since the last content change,
  // effective date, status). The Agent validates every save, tick, trial, submission, approval,
  // deletion and retirement; this page only edits, shows and forwards.
  const STATUS = { DRAFT: "DRAFT", PENDING: "PENDING", APPROVED: "ACTIVE", RETIRED: "RETIRED" };
  const STATUS_TEXT = { DRAFT: "草稿", PENDING: "审核中", APPROVED: "已生效", RETIRED: "已作废" };
  const STEPS = ["起草", "核对原文", "拍板", "试算", "日期与提交", "审核"];
  const NUM = ["①", "②", "③", "④", "⑤", "⑥"];
  const ACTION_TEXT = { CREATE: "新建费用", DRAFT: "AI 起草", COPY: "复制为草稿", SAVE: "修改规则", EDIT: "修改", CHECK: "核对", DATE: "设执行日期", SUBMIT: "提交审核", WITHDRAW: "退回草稿", APPROVE: "审核生效", RETIRE: "作废", TRIAL: "试算" };
  const RESETS = new Set(["SAVE", "COPY", "CREATE", "DRAFT"]);
  const TYPE = { fixed: "固定金额", rate: "单价×数值", amount: "按数值计金额", sum: "相加", max: "取大", tiers: "分档", when: "按条件",
    lookup: "按选项", clamp: "最低/最高", each_piece: "逐件计费", use: "引用公共部分", none: "无金额" };
  const NO_AMOUNT = { NEGOTIATE: "需协商", NOT_PROVIDED: "可不提供服务", MANUAL: "需人工判断", ACTUAL: "实报实销" };
  const OPS = { eq: "=", ne: "≠", gt: ">", ge: "≥", lt: "<", le: "≤", in: "属于" };
  const PARAM_TYPE = { number: "数字", choice: "单选", bool: "是/否", pieces: "逐件明细" };
  const LEAVES = new Set(["fixed", "rate", "amount", "none"]);
  const SAVE_DELAY = 1000;

  function init() {
    const page = document.querySelector("[data-handbook]");
    const root = document.querySelector("[data-fee-rules]");
    if (!page || !root || !window.HandbookUI || !window.FeeForm || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const UI = window.HandbookUI;
    const Form = window.FeeForm;
    const { escapeHtml, badge, formatTime, request, confirmAction } = UI;
    const { canEdit, superAdmin } = UI.role(page);
    const base = "/handbook/fee-rules";
    const $ = (selector) => root.querySelector(selector);
    const feedback = UI.feedback($("[data-fee-status]"));
    const draft = window.FeeDraft ? window.FeeDraft.create({ UI, onChange: draftChanged }) : null;
    const state = { versions: [], current: null, step: 0, busy: false, search: "", drawer: false, menu: false,
      dirty: false, checksDirty: false, saving: false, saveTimer: 0, saveState: "saved", saveError: "", conflict: null, staleBody: false,
      structureOpen: false, manual: false, expanded: new Set(["formula"]), json: null, jsonError: "", editPath: "", showSame: false,
      editQuestions: false, trial: null, trialValues: {}, pages: new Map(), viewer: { document: "", page: 0, zoom: 100, sig: "" } };

    // -- helpers -----------------------------------------------------------------------

    const get = (object, path) => path.reduce((value, key) => (value == null ? undefined : value[key]), object);
    function set(object, path, value) {
      let target = object;
      path.slice(0, -1).forEach((key, index) => {
        if (target[key] == null || typeof target[key] !== "object") target[key] = /^\d+$/.test(path[index + 1]) ? [] : {};
        target = target[key];
      });
      const last = path[path.length - 1];
      if (value === undefined) delete target[last]; else target[last] = value;
    }
    const filled = (source) => Boolean(String(source?.document || "").trim() && String(source?.location || "").trim());
    const content = () => state.current?.content || {};
    const params = () => (Array.isArray(content().params) ? content().params : []);
    const paramLabel = (key) => params().find((item) => item.key === key)?.label || key;
    const person = (record, id) => (record?.people || {})[String(id || "")] || (id && /^\d+$/.test(String(id)) ? `账号 ${id}` : id || "系统");
    const questions = (record) => (record.checks || []).filter((item) => item.kind === "question");
    const items = (record) => (record.checks || []).filter((item) => !item.kind);
    const openDecisions = (record) => questions(record).filter((item) => item.answer == null || (String(item.answer).startsWith("其他") && !String(item.note || "").trim())).length
      + items(record).filter((item) => !item.done).length;
    const missingSource = (line) => !line.document || !line.location;
    const editableDraft = (record) => canEdit && record?.status === "DRAFT";
    // A new fee holds a placeholder rule (“规则还没有填写”) until ① 起草 gives it one.
    const linesOf = (record) => (record?.empty ? [] : record?.lines || []);

    function pieceField(key) {
      for (const param of params()) {
        const found = (param.fields || []).find((item) => item.key === key);
        if (found) return found.label;
      }
      return key === "count" ? "件数" : key;
    }

    function refText(ref) {
      if (typeof ref === "string") return ref.startsWith("piece.") ? `单件${pieceField(ref.slice(6)).replace(/^单件/, "")}` : paramLabel(ref);
      if (!ref || typeof ref !== "object") return "?";
      if ("const" in ref) return String(ref.const);
      if ("count" in ref) return `${paramLabel(ref.count)}件数${ref.where ? `（${condText(ref.where)}）` : ""}`;
      if ("sum" in ref) return `${paramLabel(ref.sum)}的${pieceField(ref.field)}合计${ref.where ? `（${condText(ref.where)}）` : ""}`;
      if ("max" in ref) return `${paramLabel(ref.max)}的最大${pieceField(ref.field)}`;
      if ("excess" in ref) return `${refText(ref.excess)} 超出 ${ref.over} 的部分`;
      if ("ceil" in ref) return `${refText(ref.ceil)} 每 ${ref.per} 计 1${ref.min != null ? `（至少 ${ref.min}）` : ""}`;
      if ("first" in ref) return (ref.first || []).map(refText).join(" 或 ");
      if ("add" in ref) return `（${(ref.add || []).map(refText).join(" + ")}）`;
      if ("mul" in ref) return `（${(ref.mul || []).map(refText).join(" × ")}）`;
      return "?";
    }

    function valueText(left, value) {
      if (typeof value === "boolean") return value ? "是" : "否";
      const param = typeof left === "string" ? params().find((item) => item.key === left) : null;
      const label = (item) => param?.options?.find((option) => option.value === item)?.label || item;
      return Array.isArray(value) ? value.map(label).join("、") : String(label(value));
    }

    function condText(cond) {
      if (!cond || typeof cond !== "object") return "?";
      if (cond.all) return cond.all.map(condText).join(" 且 ");
      if (cond.any) return `（${cond.any.map(condText).join(" 或 ")}）`;
      if (cond.not) return `不是（${condText(cond.not)}）`;
      return `${refText(cond.left)} ${OPS[cond.op] || cond.op} ${valueText(cond.left, cond.value)}`;
    }

    function walk(node, path, covered, visit) {
      if (!node || typeof node !== "object" || !TYPE[node.type]) return;
      const here = covered || filled(node.source);
      visit(node, path, here);
      (node.items || []).forEach((item, index) => walk(item, [...path, "items", String(index)], here, visit));
      ["item", "else"].forEach((key) => walk(node[key], [...path, key], here, visit));
      ["tiers", "cases", "rows"].forEach((key) => (node[key] || []).forEach((item, index) => walk(item?.then, [...path, key, String(index), "then"], here, visit)));
    }

    function eachRoot(visit) {
      Object.entries(content().definitions || {}).forEach(([name, node]) => walk(node, ["definitions", name], false, visit));
      walk(content().formula, ["formula"], false, visit);
    }

    function summary(node) {
      switch (node.type) {
        case "fixed": return `${node.amount ?? "—"} 元`;
        case "rate": return `${node.rate ?? "—"}${node.unit ? ` ${node.unit}` : ""} × ${refText(node.of)}`;
        case "amount": return refText(node.of);
        case "sum": return `${(node.items || []).length} 项相加`;
        case "max": return `${(node.items || []).length} 项取大`;
        case "tiers": return `按${refText(node.by)}分 ${(node.tiers || []).length} 档`;
        case "when": return `${(node.cases || []).length} 种情况${node.else ? " + 其他" : ""}`;
        case "lookup": return `按${paramLabel(node.by)} ${(node.rows || []).length} 行`;
        case "clamp": return [node.min != null ? `最低 ${node.min}` : "", node.max != null ? `最高 ${node.max}` : ""].filter(Boolean).join(" · ");
        case "each_piece": return `按${paramLabel(node.pieces)}逐行`;
        case "use": return `公共部分“${node.name}”`;
        case "none": return `${NO_AMOUNT[node.outcome] || node.outcome}：${node.reason || ""}`;
        default: return "";
      }
    }

    // The manual pages a version's lines cite, for 来源文档 summaries.
    function sourceText(record) {
      const documents = [...new Set(linesOf(record).map((line) => line.document).filter(Boolean))];
      const pages = [...new Set(linesOf(record).flatMap((line) => line.pages || []))].sort((a, b) => a - b);
      if (!documents.length) return "";
      const span = pages.length ? ` · 第 ${pages[0]}${pages.length > 1 ? `–${pages[pages.length - 1]}` : ""} 页` : "";
      return `${documents.join("、")}${span}`;
    }

    // Trials since the rule content last changed (the Agent counts the same way for submission).
    function scenarios(record) {
      const list = [];
      for (const item of record.events || []) {
        if (RESETS.has(item.action)) break;
        if (item.action === "TRIAL" && !item.detail?.boundary) list.push(item);
      }
      return list;
    }

    function scenarioText(detail) {
      const shown = Object.entries(detail.values || {}).slice(0, 5).map(([key, value]) => {
        const param = params().find((item) => item.key === key);
        const text = value === true ? "是" : value === false ? "否" : param?.options?.find((option) => option.value === value)?.label || value;
        return `${param?.label || key} ${text}${param?.unit ? ` ${param.unit}` : ""}`;
      }).join(" · ");
      const outcome = detail.amount ? `¥${detail.amount}` : Form.OUTCOME[detail.outcome] || detail.outcome || "";
      return `${shown || "（没有填参数）"} → ${outcome}`;
    }

    // -- steps -------------------------------------------------------------------------

    // Each step's state from the saved version: done / its count of open items / a problem.
    function stepInfo(record) {
      const lines = linesOf(record);
      const problems = record.content_problems || [];
      const open = lines.filter((line) => !line.done).length;
      const decisions = openDecisions(record);
      const trials = Number(record.trial_count || 0);
      const job = draft?.job();
      const drafting = job?.status === "RUNNING" && job.rule_id === record.rule_id;
      const submitted = record.status === "PENDING" || record.status === "APPROVED";
      return [
        { done: lines.length > 0 && !problems.length && !drafting, problem: problems.length ? `规则有 ${problems.length} 个问题` : "", note: drafting ? "起草中" : "" },
        { done: lines.length > 0 && open === 0, count: lines.length ? open : 0, note: lines.length ? (open ? `还差 ${open} 行` : `${lines.length} 行`) : "" },
        { done: decisions === 0, count: decisions, note: decisions ? `还差 ${decisions} 个` : questions(record).length ? `${questions(record).length} 个已答` : "" },
        { done: trials > 0, note: trials ? `${trials} 个场景` : "" },
        { done: submitted, note: record.effective_from ? record.effective_from : "" },
        { done: record.status === "APPROVED", note: record.status === "PENDING" ? "审核中" : "" },
      ];
    }

    function firstOpen(record) {
      if (record.status === "PENDING") return 5;
      const info = stepInfo(record);
      const index = info.slice(0, 5).findIndex((item) => !item.done);
      return index < 0 ? 4 : index;
    }

    // -- list --------------------------------------------------------------------------

    function progressText(item) {
      if (item.status === "APPROVED") return `执行 ${item.effective_from || "未填"}`;
      if (item.status === "PENDING") return "等待超级管理员审核";
      if (item.status === "RETIRED") return `作废于 ${formatTime(item.retired_at)}`;
      const progress = item.progress || {};
      if (!progress.lines) return "卡在第 ① 步：还没有初稿";
      if (progress.ticked < progress.lines) return `已核对 ${progress.ticked} / ${progress.lines} 行`;
      if (progress.answered < progress.questions) return `卡在第 ③ 步：还差 ${progress.questions - progress.answered} 个`;
      return item.effective_from ? `核对与拍板已完成 · 执行 ${item.effective_from}` : "核对与拍板已完成 · 日期未填";
    }

    function renderList() {
      const list = $("[data-fee-list]");
      const query = state.search.trim().toLowerCase();
      // 作废的版本不再列出（2026-11-23 用户要求）：机器人和报价都不用它们。
      const shown = state.versions.filter((item) => item.status !== "RETIRED"
        && (!query || `${item.fee_name} ${item.version_label}`.toLowerCase().includes(query)));
      const live = state.versions.filter((item) => item.status !== "RETIRED");
      const fees = new Set(live.map((item) => item.fee_kind)).size;
      const drafts = live.filter((item) => item.status === "DRAFT").length;
      UI.setTabCount("fees", fees);
      $("[data-fee-summary]").textContent = `${fees} 个费用 · ${live.length} 个版本 · ${drafts} 个草稿`;
      if (!shown.length) {
        list.innerHTML = `<li class="fs-list__empty hb-meta">${query ? "没有找到匹配的费用。" : "还没有费用规则。"}</li>`;
        return;
      }
      list.innerHTML = shown.map((item) => {
        const tone = item.status === "DRAFT" && !(item.progress?.lines) ? "warn" : "";
        return `<li><button class="fs-item" type="button" data-fee-open="${escapeHtml(item.rule_id)}" aria-current="${state.current?.rule_id === item.rule_id}">
          <span class="fs-item__top"><b>${escapeHtml(item.fee_name)}</b><span class="fs-item__ver">${escapeHtml(item.version_label)}</span></span>
          <span class="fs-item__meta">${badge(STATUS[item.status])}<span${tone ? ` data-tone="${tone}"` : ""}>${escapeHtml(progressText(item))}</span></span></button></li>`;
      }).join("");
    }

    // -- workspace pieces --------------------------------------------------------------

    function headerHtml(record) {
      const tone = { DRAFT: "draft", PENDING: "pending", APPROVED: "active", RETIRED: "retired" }[record.status];
      return `<header class="fs-head">
        <button class="ghost-btn hb-btn-sm fs-drawer-btn" type="button" data-fee-drawer aria-label="打开费用列表"><i data-feather="menu"></i><span>费用</span></button>
        <h2 id="fee-editor-title"><span>${escapeHtml(record.fee_name)}</span><span class="fs-head__ver">${escapeHtml(record.version_label)}</span>
          <span class="hb-badge" data-tone="${tone}">${STATUS_TEXT[record.status]}</span></h2>
        <div class="fs-head__tools"><button class="ghost-btn hb-btn-sm" type="button" data-fee-history><i data-feather="clock"></i><span>历史</span></button>
          ${menuItems(record).length ? `<span class="fs-menu-wrap"><button class="ghost-btn hb-btn-sm fs-more" type="button" data-fee-menu aria-haspopup="menu" aria-expanded="${state.menu}" aria-label="更多操作">⋯</button>${state.menu ? menuHtml(record) : ""}</span>` : ""}</div>
      </header>`;
    }

    // ⋯ (spec 69 A): only what can be done now; 删除 for never-submitted drafts, 作废 for the rest (super admin).
    function menuItems(record) {
      const list = [];
      if (canEdit && record.status !== "DRAFT" && record.status !== "RETIRED") list.push(["copy", "复制为新草稿"]);
      if (editableDraft(record)) list.push(["rename", "重命名"]);
      if (canEdit && state.versions.some((item) => item.status === "DRAFT")) list.push(["dates", "批量设执行日期"]);
      const danger = [];
      if (editableDraft(record) && record.can_delete) danger.push(["delete", "删除草稿"]);
      else if (superAdmin && record.status !== "RETIRED") danger.push(["retire", "作废此版本"]);
      return list.length && danger.length ? [...list, null, ...danger] : [...list, ...danger];
    }

    function menuHtml(record) {
      return `<div class="fs-menu" role="menu">${menuItems(record).map((item) => (item ? `<button type="button" role="menuitem" data-fee-menu-item="${item[0]}"${["delete", "retire"].includes(item[0]) ? ' data-tone="danger"' : ""}>${item[1]}${item[0] === "retire" ? '<span class="fs-menu__hint">仅超级管理员</span>' : ""}</button>` : '<hr>')).join("")}</div>`;
    }

    function stepperHtml(record) {
      const info = stepInfo(record);
      const locked = record.status === "PENDING";
      const open = firstOpen(record);
      return `<ol class="fs-steps" aria-label="步骤">${STEPS.map((label, index) => {
        const item = info[index];
        const current = index === state.step;
        // A later step shows as done only once the steps before it are (e.g. ③ with no questions).
        const reached = record.status !== "DRAFT" || index <= open;
        const status = current ? "current" : item.done && reached ? "done" : item.problem ? "problem" : "todo";
        const mark = status === "done" ? "✓" : status === "problem" ? "!" : index + 1;
        const count = current && !item.done && item.count ? `<span class="fs-step__count">${item.count}</span>` : "";
        return `<li class="fs-step" data-state="${status}"><button type="button" data-fee-step="${index}" aria-current="${current ? "step" : "false"}">
          <span class="fs-step__dot">${mark}${count}${locked && index < 5 ? '<span class="fs-step__lock" aria-label="已锁定"><i data-feather="lock"></i></span>' : ""}</span>
          <span class="fs-step__name">${label}</span><span class="fs-step__note">${escapeHtml(item.problem || item.note || "")}</span></button></li>`;
      }).join("")}</ol>`;
    }

    function saveText(record) {
      if (record.status === "PENDING") return `<i class="hb-dot" data-tone="ok"></i>已提交 ${escapeHtml(formatTime(record.submitted_at, { withYear: true }))} · ${escapeHtml(person(record, record.submitted_by))}`;
      if (record.status === "APPROVED") return `<i class="hb-dot" data-tone="ok"></i>生效于 ${escapeHtml(formatTime(record.approved_at, { withYear: true }))} · 审核 ${escapeHtml(person(record, record.approved_by))}`;
      if (record.status === "RETIRED") return `<i class="hb-dot"></i>作废于 ${escapeHtml(formatTime(record.retired_at, { withYear: true }))}`;
      if (state.saveState === "failed") return `<i class="hb-dot" data-tone="danger"></i><span data-tone="danger">保存失败：${escapeHtml(state.saveError)}</span> <button class="hb-link fs-link-red" type="button" data-fee-retry>重试</button>`;
      if (state.saveState === "conflict") return '<i class="hb-dot" data-tone="warn"></i>没有保存：草稿已被别人修改';
      if (state.saving || state.dirty || state.checksDirty) return '<i class="hb-dot" data-tone="warn"></i>保存中…';
      return `<i class="hb-dot" data-tone="ok"></i>已自动保存 ${escapeHtml(formatTime(record.updated_at))} · ${escapeHtml(person(record, record.updated_by))}`;
    }

    function footerHtml(record) {
      if (record.status === "APPROVED" || record.status === "RETIRED") {
        return `<footer class="fs-foot"><span class="fs-foot__save">${saveText(record)}</span><span class="hb-meta">只读 · ${STATUS_TEXT[record.status]}</span></footer>`;
      }
      const info = stepInfo(record);
      const step = state.step;
      let reason = "";
      let next = `<button class="primary-btn hb-btn-sm fs-next" type="button" data-fee-next ${info[step]?.done && step < 4 ? "" : "disabled"}>下一步 →</button>`;
      if (record.status === "PENDING") {
        reason = superAdmin ? "等待你审核" : "等待超级管理员审核";
        next = '<button class="primary-btn hb-btn-sm fs-next" type="button" disabled>下一步 →</button>';
      } else if (step === 4) {
        const blockers = record.submit_blockers || [];
        const pending = state.dirty || state.checksDirty || state.saving;
        reason = pending ? "正在保存，稍等" : !record.effective_from ? "填执行日期后可提交" : blockers.length ? blockers[0] : "确认无误后提交审核";
        next = canEdit ? `<button class="primary-btn hb-btn-sm fs-next" type="button" data-fee-submit ${!pending && !blockers.length ? "" : "disabled"}>提交审核</button>` : "";
      } else if (!info[step]?.done) {
        reason = [`先完成起草`, info[1].note ? `${info[1].note}没核对` : "还有行没核对", info[2].count ? `还有 ${info[2].count} 个问题没答` : "还有问题没答", "至少试算 1 个场景"][step] || "";
        if (step === 0 && draft?.mode() === "progress") reason = "AI 起草中，先等它完成";
        if (step === 0 && draft?.mode() === "failed") reason = "起草失败，先重试或换范围";
      }
      return `<footer class="fs-foot"><span class="fs-foot__save" data-fee-saved>${saveText(record)}</span>
        <div class="fs-foot__nav"><span class="hb-meta">${escapeHtml(reason)}</span>
          <button class="ghost-btn hb-btn-sm" type="button" data-fee-prev ${step > 0 ? "" : "disabled"}>上一步</button>${next}</div></footer>`;
    }

    function stepTitle(index, record, right = "", sub = "") {
      const current = index === firstOpen(record) && record.status === "DRAFT";
      const tag = record.status === "PENDING" && index === 5 ? `<span class="hb-badge" data-tone="pending">${superAdmin ? "超级管理员" : "等待审核"}</span>`
        : current ? '<span class="hb-badge">当前步骤</span>' : stepInfo(record)[index].done ? '<span class="hb-badge" data-tone="active">已完成</span>' : "";
      return `<div class="fs-title"><h3>${NUM[index]} ${STEPS[index]} ${tag}</h3>${right ? `<span class="hb-meta">${right}</span>` : ""}</div>${sub ? `<p class="fs-sub-title">${sub}</p>` : ""}`;
    }

    function conflictHtml() {
      if (!state.conflict) return "";
      return `<div class="fs-conflict" role="alert"><div><b>这份草稿已被 ${escapeHtml(state.conflict.by || "别人")} 修改</b>
        <p>你的改动没有覆盖对方。载入最新版本后，再决定怎么合并。</p></div>
        <button class="primary-btn hb-btn-sm" type="button" data-fee-reload>载入最新</button></div>`;
    }

    // -- ① 起草 -------------------------------------------------------------------------

    function input(path, value, { editable, mode = "decimal", wide = false, placeholder = "", blank = "keep", label = "" } = {}) {
      return `<input ${editable ? "" : "disabled"} inputmode="${mode}" data-f="${escapeHtml(path.join("."))}" data-blank="${blank}"${wide ? ' class="hb-input--wide"' : ""} value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(placeholder)}"${label ? ` aria-label="${escapeHtml(label)}"` : ""}>`;
    }

    const cell = (label, path, value, opts = {}) => `<label class="hb-cell"><span>${label}</span>${input(path, value, opts)}</label>`;

    function sourceFields(path, source, editable) {
      if (!source) return editable ? `<button class="hb-link" type="button" data-source-add="${escapeHtml(path.join("."))}">+ 添加出处</button>` : "";
      return `<div class="hb-fee__source">
        <label class="hb-cell hb-cell--doc"><span>出处文档</span>${input([...path, "source", "document"], source.document, { editable, mode: "text", placeholder: "例如：融辉物流网络管理手册 V6.1" })}</label>
        <label class="hb-cell"><span>页码或条款</span>${input([...path, "source", "location"], source.location, { editable, mode: "text", placeholder: "第五篇第五章 第 50 页" })}</label>
        <label class="hb-cell hb-cell--doc"><span>原文摘句（选填）</span>${input([...path, "source", "quote"], source.quote, { editable, mode: "text", blank: "delete" })}</label>
        ${editable ? `<button class="hb-link" data-tone="danger" type="button" data-source-remove="${escapeHtml(path.join("."))}">删除出处</button>` : ""}</div>`;
    }

    function children(node, path, editable, covered) {
      const nested = (child, childPath, title) => `<div class="hb-branch"><p class="hb-branch__title">${escapeHtml(title)}</p>${nodeHtml(child, childPath, editable, covered)}</div>`;
      switch (node.type) {
        case "sum": case "max":
          return (node.items || []).map((item, index) => nested(item, [...path, "items", String(index)], `第 ${index + 1} 项`)).join("");
        case "tiers": {
          const right = node.closed !== "left";
          return (node.tiers || []).map((tier, index) => {
            const tierPath = [...path, "tiers", String(index)];
            return `<div class="hb-tier"><div class="hb-tier__head"><b>第 ${index + 1} 档</b><span class="hb-meta">${right ? "大于起点、不超过终点" : "不小于起点、小于终点"}</span></div>
              <div class="hb-fee__cells">${cell("起点（空 = 不设）", [...tierPath, "over"], tier.over, { editable, blank: "null" })}${cell("终点（空 = 不设）", [...tierPath, "up_to"], tier.up_to, { editable, blank: "null" })}</div>
              ${nodeHtml(tier.then, [...tierPath, "then"], editable, covered)}</div>`;
          }).join("") + (node.else ? nested(node.else, [...path, "else"], "不在各档内") : "");
        }
        case "when":
          return (node.cases || []).map((item, index) => nested(item.then, [...path, "cases", String(index), "then"], `情况 ${index + 1}：${condText(item.if)}`)).join("")
            + (node.else ? nested(node.else, [...path, "else"], "其他情况") : "");
        case "lookup":
          return (node.rows || []).map((row, index) => nested(row.then, [...path, "rows", String(index), "then"], `${paramLabel(node.by)}：${valueText(node.by, row.value)}`)).join("")
            + (node.else ? nested(node.else, [...path, "else"], "其他选项") : "");
        case "clamp": case "each_piece":
          return nodeHtml(node.item, [...path, "item"], editable, covered);
        default: return "";
      }
    }

    function ownFields(node, path, editable) {
      switch (node.type) {
        case "fixed": return `<div class="hb-fee__cells">${cell("金额（元）", [...path, "amount"], node.amount, { editable })}</div>`;
        case "rate": return `<div class="hb-fee__cells">${cell("单价", [...path, "rate"], node.rate, { editable })}${cell("单位（选填）", [...path, "unit"], node.unit, { editable, mode: "text", blank: "delete" })}<p class="hb-meta">× ${escapeHtml(refText(node.of))}</p></div>`;
        case "amount": return `<p class="hb-meta">金额 = ${escapeHtml(refText(node.of))}</p>`;
        case "clamp": return `<div class="hb-fee__cells">${cell("最低（元，空 = 不设）", [...path, "min"], node.min, { editable, blank: "delete" })}${cell("最高（元，空 = 不设）", [...path, "max"], node.max, { editable, blank: "delete" })}</div>`;
        case "use": return `<p class="hb-meta">按公共部分“${escapeHtml(node.name)}”计算。</p>`;
        case "none": return `<div class="hb-fee__cells"><label class="hb-cell"><span>结果</span><select ${editable ? "" : "disabled"} data-f="${escapeHtml([...path, "outcome"].join("."))}">${Object.entries(NO_AMOUNT).map(([key, text]) => `<option value="${key}" ${node.outcome === key ? "selected" : ""}>${text}</option>`).join("")}</select></label>
          <label class="hb-cell hb-cell--doc"><span>原因（随结果显示）</span>${input([...path, "reason"], node.reason, { editable, mode: "text" })}</label></div>`;
        default: return "";
      }
    }

    function nodeHtml(node, path, editable, coveredAbove = false) {
      if (!node || typeof node !== "object" || !TYPE[node.type]) return '<p class="hb-meta" data-tone="warn">这里不是有效的积木，请在“高级：结构 JSON”中修正。</p>';
      const key = path.join(".");
      const open = state.expanded.has(key);
      const covered = coveredAbove || filled(node.source);
      let inside = false;
      if (!open && !covered && !LEAVES.has(node.type)) walk(node, path, false, (child, _p, ok) => { if (LEAVES.has(child.type) && !ok) inside = true; });
      const sourceBadge = filled(node.source) ? '<span class="hb-badge" data-tone="active">有出处</span>'
        : LEAVES.has(node.type) && !covered ? '<span class="hb-badge" data-tone="pending">缺出处</span>'
        : inside ? '<span class="hb-badge" data-tone="pending">里面有缺出处</span>' : "";
      return `<section class="hb-fold hb-fold--node" data-open="${open}">
        <button class="hb-fold__head" type="button" data-fold="${escapeHtml(key)}" aria-expanded="${open}">
          <i data-feather="${open ? "chevron-down" : "chevron-right"}"></i><b>${escapeHtml(node.label || TYPE[node.type])}</b><span class="hb-fold__summary">${escapeHtml(summary(node))}</span>${sourceBadge}</button>
        ${open ? `<div class="hb-fold__body">
          <div class="hb-fee__cells"><label class="hb-cell hb-cell--doc"><span>名称（选填，显示在计算过程里）</span>${input([...path, "label"], node.label, { editable, mode: "text", blank: "delete", placeholder: TYPE[node.type] })}</label></div>
          ${ownFields(node, path, editable)}${sourceFields(path, node.source, editable)}
          ${children(node, path, editable, covered)}</div>` : ""}</section>`;
    }

    function paramsHtml(editable) {
      const list = params();
      const rows = list.map((param) => `<tr><td title="${escapeHtml(param.key)}">${escapeHtml(param.label || param.key)}</td><td>${PARAM_TYPE[param.type] || escapeHtml(param.type)}</td>
        <td>${escapeHtml(param.unit || "")}</td><td>${param.type === "choice" ? escapeHtml((param.options || []).map((item) => item.label).join(" / ")) : param.type === "pieces" ? escapeHtml((param.fields || []).map((item) => item.label).join("、")) : escapeHtml([param.min != null ? `≥${param.min}` : "", param.max != null ? `≤${param.max}` : ""].filter(Boolean).join(" "))}</td>
        <td>${param.required === false ? "选填" : param.default != null ? `默认 ${escapeHtml(String(param.default))}` : "必填"}</td></tr>`).join("");
      return `<div class="fs-sub"><h4>要填的参数 <span class="hb-meta">${list.length} 个</span></h4>
        ${list.length ? `<div class="hb-table-wrap"><table class="hb-table"><thead><tr><th>名称</th><th>类型</th><th>单位</th><th>范围 / 选项</th><th>必填</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="hb-meta">还没有参数。</p>'}
        ${editable ? '<p class="hb-meta">参数的增删改在下方“高级：结构 JSON”中进行。</p>' : ""}</div>`;
    }

    function structureHtml(record, editable) {
      const data = content();
      const problems = record.content_problems || [];
      const definitions = Object.entries(data.definitions || {});
      const jsonText = state.json ?? JSON.stringify(data, null, 2);
      return `${problems.length && !state.dirty ? `<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span>规则有 ${problems.length} 个问题，修正后才能核对和试算：<br>${problems.map(escapeHtml).join("<br>")}</span></div>` : ""}
        ${paramsHtml(editable)}
        <div class="fs-sub"><h4>计算结构 <button class="hb-link" type="button" data-fold-all>${state.expanded.size > 1 ? "全部收起" : "全部展开"}</button></h4>${nodeHtml(data.formula, ["formula"], editable)}</div>
        ${definitions.length ? `<div class="fs-sub"><h4>公共部分</h4>${definitions.map(([name, node]) => `<p class="hb-branch__title">${escapeHtml(name)}</p>${nodeHtml(node, ["definitions", name], editable)}`).join("")}</div>` : ""}
        <div class="fs-sub"><h4>互斥与提醒</h4>
          <div class="hb-fee__cells">${cell("互斥组（同组费用不同时收取，取大）", ["group"], data.group, { editable, mode: "text", blank: "delete", placeholder: "例如：上楼卸货" })}</div>
          <label class="hb-cell hb-cell--full"><span>提醒：一行一条，随计算结果一起返回（最多 10 条）</span><textarea ${editable ? "" : "disabled"} rows="3" data-reminders>${escapeHtml((data.reminders || []).join("\n"))}</textarea></label></div>
        <details class="fs-sub hb-advanced" ${state.json != null ? "open" : ""}><summary>高级：结构 JSON</summary>
          <p class="hb-meta">增删参数或积木、改条件在这里进行。格式见 shared/fee_engine.py；“应用”后自动保存，保存时会完整校验。</p>
          <textarea class="hb-json" rows="16" spellcheck="false" ${editable ? "" : "readonly"} data-json>${escapeHtml(jsonText)}</textarea>
          ${state.jsonError ? `<p class="hb-meta" data-tone="danger">${escapeHtml(state.jsonError)}</p>` : ""}
          ${editable ? '<div class="hb-actions"><button class="ghost-btn hb-btn-sm" type="button" data-json-apply>应用</button><button class="hb-link" type="button" data-json-reset>恢复为当前内容</button></div>' : ""}</details>`;
    }

    function nameFieldHtml(record, editable, note) {
      return `<div class="fs-name"><label for="fee-name-input">费用名称</label>
        <div class="fs-row fs-row--start"><input id="fee-name-input" maxlength="20" data-meta="fee_name" value="${escapeHtml(record.fee_name)}" ${editable ? "" : "disabled"}><span class="hb-meta">${escapeHtml(note)}</span></div></div>`;
    }

    function foldHtml(record, editable) {
      const open = state.structureOpen || state.manual;
      return `<section class="fs-fold" data-open="${open}"><button class="fs-fold__head" type="button" data-fee-structure aria-expanded="${open}">
          <i data-feather="${open ? "chevron-down" : "chevron-right"}"></i><b>查看 / 修改规则结构</b><span class="hb-meta">计算结构、互斥组、参数 JSON 收进「高级」，多数人不用打开</span></button>
        ${open ? `<div class="fs-fold__body">${structureHtml(record, editable)}</div>` : ""}</section>`;
    }

    function lastDraftEvent(record) {
      return (record.events || []).find((item) => item.action === "DRAFT" || item.action === "COPY" || item.action === "CREATE");
    }

    function step1(record, editable) {
      const mode = draft?.mode() || "";
      const lines = linesOf(record);
      const head = (badgeHtml, sub) => `<div class="fs-title"><h3>① 起草 ${badgeHtml}</h3></div><p class="fs-sub-title">${sub}</p>`;
      if (mode === "progress") {
        return `${head('<span class="hb-badge" data-tone="pending">进行中</span>', "正在从手册起草，通常 1–3 分钟。可以离开这一页。")}
          ${draft.html(record.fee_name, editable)}
          ${nameFieldHtml(record, editable, "起草过程中也可以改")}
          <div class="fs-note"><b>这一步只生成初稿</b><span>AI 读出来的每一行都要在第 ② 步对照原页核对；不要把这一步的结果直接当成生效规则。</span></div>`;
      }
      if (mode === "failed") {
        return `${head('<span class="hb-badge" data-tone="danger">需要处理</span>', "上一次起草没有成功。可以重试，或缩小页码范围。")}
          ${draft.html(record.fee_name, editable)}${nameFieldHtml(record, editable, "左栏和报价单上会显示这个名字")}`;
      }
      if (mode === "picker") {
        return `${head(stepTitleBadge(record, 0), "选知识库里的手册和页码，AI 只起草这一项费用。")}
          ${draft.html(record.fee_name, editable)}${nameFieldHtml(record, editable, "左栏和报价单上会显示这个名字")}`;
      }
      if (!lines.length && !state.manual) {
        const choose = editable ? "" : " disabled";
        return `${head(stepTitleBadge(record, 0), "先拿到规则初稿。可以从手册起草，也可以直接手填。")}
          <div class="fs-choices">
            <button class="fs-choice" data-recommended="true" type="button" data-fee-from-manual${choose}><span class="fs-choice__icon"><i data-feather="file-text"></i></span>
              <span class="hb-badge" data-tone="danger">推荐</span><b>从手册起草</b><span class="fs-choice__desc">选飞书知识库里的 PDF，圈出章节或页码，AI 按页读出这项费用的收费条件与金额，生成收费表初稿。</span>
              <span class="fs-choice__foot">PDF · 一次最多 8 页 · 约 1–3 分钟<span aria-hidden="true">→</span></span></button>
            <button class="fs-choice" type="button" data-fee-manual${choose}><span class="fs-choice__icon"><i data-feather="plus"></i></span>
              <b>手动填写</b><span class="fs-choice__desc">直接进规则编辑，自己填收费条件和金额。适合规则简单，或暂时拿不到手册的情况。</span>
              <span class="fs-choice__foot">不经过 AI · 之后仍可补手册出处</span></button>
          </div>
          ${nameFieldHtml(record, editable, "左栏和报价单上会显示这个名字")}
          ${foldHtml(record, editable)}
          <div class="fs-preview"><h4>起草完成后，这一步会变成摘要</h4><div class="fs-preview__cols">
            <div><span>01</span><b>来源文档与页码</b><p>能追到手册的哪一页，第 ② 步用来对照原文</p></div>
            <div><span>02</span><b>收费表初稿</b><p>条件 + 金额，等待第 ② 步逐行核对</p></div>
            <div><span>03</span><b>待拍板问题</b><p>手册没写清的，第 ③ 步由人来定</p></div></div></div>
          <p class="fs-dotnote">起草只是初稿：第 ② 步必须逐行核对原文，第 ③ 步回答没写清的问题。</p>`;
      }
      const event = lastDraftEvent(record);
      const detail = event?.detail || {};
      const origin = event?.action === "DRAFT" ? `AI 起草：${detail.document || "手册"} 第 ${detail.pages || "?"} 页`
        : event?.action === "COPY" ? "复制自上一版" : "手动填写";
      return `${head(stepTitleBadge(record, 0), "规则初稿已经有了；需要时可以重新起草或修改结构。")}
        <div class="fs-summary">
          <div><span>来源文档与页码</span><b>${escapeHtml(sourceText(record) || "还没有出处")}</b><p>${escapeHtml(origin)}${event ? ` · ${escapeHtml(formatTime(event.created_at))} ${escapeHtml(person(record, event.actor_id))}` : ""}</p></div>
          <div><span>收费表初稿</span><b>${lines.length} 行</b><p>${lines.filter(missingSource).length ? `${lines.filter(missingSource).length} 行缺出处，第 ② 步补` : "第 ② 步逐行核对"}</p></div>
          <div><span>待拍板问题</span><b>${questions(record).length} 个</b><p>${questions(record).length ? "第 ③ 步由人来定" : "没有问题，第 ③ 步自动完成"}</p></div>
        </div>
        ${editable && draft ? '<div class="hb-actions"><button class="ghost-btn hb-btn-sm" type="button" data-fee-from-manual><i data-feather="refresh-cw"></i><span>重新起草</span></button><span class="hb-meta">重新起草会替换现在的收费表和问题，改动过的行要重新核对。</span></div>' : ""}
        ${nameFieldHtml(record, editable, "左栏和报价单上会显示这个名字")}
        ${foldHtml(record, editable)}`;
    }

    function stepTitleBadge(record, index) {
      if (record.status !== "DRAFT") return "";
      return index === firstOpen(record) ? '<span class="hb-badge">当前步骤</span>' : stepInfo(record)[index].done ? '<span class="hb-badge" data-tone="active">已完成</span>' : "";
    }

    // -- ② 核对原文 ----------------------------------------------------------------------

    function linePage(line) { return (line.pages || [])[0] || 0; }

    function viewerHtml(record) {
      const viewer = state.viewer;
      const key = `${viewer.document}|${viewer.page}`;
      const image = state.pages.get(key);
      let paper = '<p class="hb-meta">点右侧某一行，这里显示它对应的手册原页。</p>';
      if (viewer.document && viewer.page) {
        paper = image?.error ? `<p class="hb-meta" data-tone="danger">${escapeHtml(image.error)}</p>`
          : image ? `<img alt="${escapeHtml(viewer.document)} 第 ${viewer.page} 页" src="data:image/png;base64,${image}" style="width:${viewer.zoom}%">`
          : '<p class="hb-meta">正在读取手册原页……（第一次打开需要几秒）</p>';
      }
      return `<div class="fs-viewer" data-fee-viewer>
        <div class="fs-viewer__bar"><span><b>${escapeHtml(viewer.document || "手册原页")}</b>${viewer.page ? `<span class="hb-meta">第 ${viewer.page} 页</span>` : ""}</span>
          ${viewer.page ? '<button class="ghost-btn hb-btn-sm" type="button" data-viewer-open>放大</button>' : ""}</div>
        <div class="fs-viewer__paper">${paper}</div>
        <div class="fs-viewer__bar fs-viewer__bar--foot">${viewer.page ? `<span class="fs-pager"><button type="button" class="hb-link" data-viewer-page="${viewer.page - 1}" ${viewer.page > 1 ? "" : "disabled"} aria-label="上一页">‹</button><b>${viewer.page}</b><button type="button" class="hb-link" data-viewer-page="${viewer.page + 1}" aria-label="下一页">›</button></span>
          <span class="fs-pager"><button type="button" class="hb-link" data-viewer-zoom="-25" aria-label="缩小">−</button><span>${viewer.zoom}%</span><button type="button" class="hb-link" data-viewer-zoom="25" aria-label="放大">＋</button></span>` : ""}</div></div>`;
    }

    async function loadPage(document, pageNumber) {
      const key = `${document}|${pageNumber}`;
      if (!document || !pageNumber || state.pages.has(key)) return;
      state.pages.set(key, null);
      try {
        const data = await request(`${base}/source-page?document=${encodeURIComponent(document)}&page=${pageNumber}`);
        state.pages.set(key, data.png_base64);
      } catch (error) {
        state.pages.set(key, { error: error.message || "原页没有取到" });
      }
      const viewer = $("[data-fee-viewer]");
      if (viewer && state.viewer.document === document && state.viewer.page === pageNumber) viewer.outerHTML = viewerHtml(state.current);
    }

    function selectLine(line) {
      state.viewer.sig = line.sig;
      if (line.document && linePage(line)) {
        state.viewer.document = line.document;
        state.viewer.page = linePage(line);
        loadPage(line.document, state.viewer.page);
      }
    }

    function lineRowHtml(record, line, editable) {
      const changed = record.compare && line.changed;
      const missing = missingSource(line);
      const selected = state.viewer.sig === line.sig;
      const node = line.path ? get(record.content, line.path.split(".")) : null;
      const editing = editable && state.editPath && state.editPath === line.path;
      const tick = line.done
        ? `<span class="fs-done">✓ 已核对</span>${editable ? `<button class="hb-link" type="button" data-line-untick="${escapeHtml(line.sig)}">取消</button>` : ""}`
        : editable ? `<button class="ghost-btn hb-btn-sm" type="button" data-line-tick="${escapeHtml(line.sig)}" ${missing ? 'disabled title="先补出处页码"' : ""}>✓ 对</button>` : '<span class="hb-meta">未核对</span>';
      return `<li class="fs-line" data-selected="${selected}" data-done="${line.done}">
        <button class="fs-line__main" type="button" data-line-select="${escapeHtml(line.sig)}">
          <span class="fs-line__cond"><span class="fs-line__title">${changed ? '<span class="hb-badge" data-tone="pending">有变化</span>' : ""}${missing ? '<span class="hb-badge" data-tone="danger">缺出处</span>' : ""}<b>${escapeHtml(line.condition || line.text)}</b></span>
            <span class="fs-line__src"${missing ? ' data-tone="danger"' : ""}>${missing ? "待补页码" : escapeHtml(line.location)}</span></span>
          <span class="fs-line__amount">${escapeHtml(line.amount || "")}</span></button>
        <span class="fs-line__ops">${tick}${editable && line.path ? `<button class="hb-link" type="button" data-line-edit="${escapeHtml(line.path)}">${editing ? "收起" : "改"}</button>` : ""}</span>
        ${editing && node ? `<div class="fs-line__edit">${ownFields(node, line.path.split("."), true)}${sourceFields(line.path.split("."), node.source, true)}
          <p class="hb-meta">改完自动保存；改了数字或出处，这一行要重新核对。</p></div>` : ""}</li>`;
    }

    function step2(record, editable, readOnly) {
      const lines = linesOf(record);
      const ticked = lines.filter((line) => line.done).length;
      if (!lines.length) return `${stepTitle(1, record)}<div class="fs-empty">先在第 ① 步拿到规则初稿，这里才有收费表可以核对。</div>`;
      const compare = record.compare;
      const sameHidden = compare && !state.showSame ? lines.filter((line) => !line.changed && line.done) : [];
      const shown = lines.filter((line) => !sameHidden.includes(line));
      const groups = [];
      shown.forEach((line) => {
        const key = missingSource(line) ? "missing" : `${line.document}|${linePage(line)}`;
        if (!groups.length || groups[groups.length - 1].key !== key) groups.push({ key, line, lines: [] });
        groups[groups.length - 1].lines.push(line);
      });
      if (!state.viewer.sig) { const first = lines.find((line) => !line.done && line.document) || lines.find((line) => line.document); if (first) selectLine(first); }
      const can = editable && !readOnly;
      return `${stepTitle(1, record, `已核对 <b>${ticked}</b> / ${lines.length} 行${ticked < lines.length ? ` · 还差 ${lines.length - ticked} 行` : ""}`)}
        <span class="fs-bar"><span style="width:${Math.round((ticked / lines.length) * 100)}%"></span></span>
        ${compare ? `<div class="fs-banner"><div><b>与${compare.status === "APPROVED" ? "现行" : ""}版本 ${escapeHtml(compare.version_label || "")} 相比：${compare.changed} 行有变化（已标出），${compare.same} 行不变（沿用上次核对）</b>
          ${compare.removed?.length ? `<span>上一版有 ${compare.removed.length} 行已删除：${compare.removed.map(escapeHtml).join("；")}</span>` : ""}
          <span>不变的行默认收起，展开后可以改。</span></div></div>` : ""}
        <div class="fs-split">${viewerHtml(record)}
          <div class="fs-lines"><div class="fs-lines__head"><span>条件</span><span>金额</span><span>核对</span></div>
            <div class="fs-lines__body">${groups.map((group) => `<div class="fs-lines__group">${group.key === "missing" ? "缺出处 · 补上页码后才能核对" : `第 ${linePage(group.line) || "?"} 页 · ${escapeHtml(group.line.document)} · ${group.lines.length} 行`}</div>
              <ul>${group.lines.map((line) => lineRowHtml(record, line, can)).join("")}</ul>`).join("")}
            ${sameHidden.length ? `<button class="fs-lines__more" type="button" data-fee-show-same>展开其余 ${sameHidden.length} 行（不变，沿用上次核对）</button>` : compare && state.showSame ? '<button class="fs-lines__more" type="button" data-fee-show-same>收起不变的行</button>' : ""}</div></div>
        </div>
        <p class="fs-dotnote">点右侧某一行，左侧会跳到对应页；点「改」在这一行就地展开编辑。缺出处的行补上页码后才能打勾。</p>`;
    }

    // -- ③ 拍板 ---------------------------------------------------------------------------

    function step3(record, editable, readOnly) {
      const list = record.checks || [];
      const qs = questions(record);
      const answered = qs.filter((item) => item.answer != null).length;
      const can = editable && !readOnly;
      const textEditable = can && state.editQuestions;
      let number = 0;
      const cards = list.map((item, index) => {
        if (item.kind === "line") return "";
        if (!item.kind) {
          return `<section class="fs-q" data-answered="${item.done}"><div class="fs-q__head"><span class="fs-q__no">事项</span>
            ${textEditable ? `<input class="fs-q__text" data-check-text="${index}" value="${escapeHtml(item.text)}" aria-label="事项">` : `<b>${escapeHtml(item.text)}</b>`}
            <span class="hb-badge" data-tone="${item.done ? "active" : "pending"}">${item.done ? "已确认" : "未确认"}</span></div>
            <label class="fs-opt"><input type="checkbox" data-check="${index}" ${item.done ? "checked" : ""} ${can ? "" : "disabled"}><span>已确认这件事</span></label>
            ${textEditable ? `<button class="hb-link" data-tone="danger" type="button" data-fee-remove-check="${index}">删除这条</button>` : ""}</section>`;
        }
        number += 1;
        const other = String(item.answer || "").startsWith("其他");
        const done = item.answer != null && (!other || String(item.note || "").trim());
        return `<section class="fs-q" data-answered="${Boolean(done)}"><div class="fs-q__head"><span class="fs-q__no">Q${number}</span>
          ${textEditable ? `<input class="fs-q__text" data-question-text="${index}" value="${escapeHtml(item.text)}" aria-label="问题 ${number}">` : `<b>${escapeHtml(item.text)}</b>`}
          <span class="hb-badge" data-tone="${done ? "active" : "pending"}">${done ? "已回答" : "未回答"}</span></div>
          ${textEditable ? `<label class="hb-cell hb-cell--full"><span>答案选项（用 / 分隔，2–6 个；以“其他”开头的选项要写明做法）</span><input data-question-options="${index}" value="${escapeHtml((item.options || []).join(" / "))}"></label>
            <button class="hb-link" data-tone="danger" type="button" data-fee-remove-check="${index}">删除这个问题</button>` : ""}
          <div class="fs-opts" role="radiogroup" aria-label="${escapeHtml(item.text)}">${(item.options || []).map((option) => `<label class="fs-opt"><input type="radio" name="fee-q-${index}" data-answer="${index}" value="${escapeHtml(option)}" ${item.answer === option ? "checked" : ""} ${can ? "" : "disabled"}><span>${escapeHtml(option)}</span></label>`).join("")}</div>
          ${other ? `<label class="fs-other"><span class="sr-only">写明怎么做</span><textarea rows="3" maxlength="200" data-answer-note="${index}" placeholder="写明怎么做" ${can ? "" : "disabled"}>${escapeHtml(item.note || "")}</textarea></label>
            <p class="fs-warn">改了做法，记得回第 ① 步修改规则结构，否则试算结果会对不上。</p>` : item.note ? `<p class="hb-meta">说明：${escapeHtml(item.note)}</p>` : ""}
          <p class="fs-q__foot">${item.answer != null ? `已选 ${escapeHtml(item.answer)} · 点选项可改` : "选好立即保存；没有「保存」按钮。"}</p></section>`;
      }).join("");
      const empty = !qs.length && !items(record).length;
      return `${stepTitle(2, record, qs.length ? `已回答 ${answered} / ${qs.length}` : "", "手册没写清的地方，由人来定；答案会写进生效规则和机器人的回答。")}
        ${empty ? '<div class="fs-empty">这条规则没有需要拍板的问题，这一步自动算完成。</div>' : cards}
        ${can ? `<div class="fs-weak"><button class="hb-link" type="button" data-fee-add-question>＋ 添加问题</button>${qs.length || items(record).length ? `<button class="hb-link" type="button" data-edit-questions>${state.editQuestions ? "完成编辑" : "编辑问题"}</button>` : ""}</div>` : ""}
        <div class="fs-note"><b>没有问题时，这一步自动算完成</b><span>只有手册没写清、又会影响算钱的点才需要问；不要让用户替机器人做阅读理解。</span></div>`;
    }

    // -- ④ 试算 ---------------------------------------------------------------------------

    function resultHtml(record) {
      const data = state.trial;
      if (!data) return '<div class="fs-empty">填好参数后点“试算”，这里显示金额和计算过程。</div>';
      const result = data.result;
      const amount = result.outcome === "AMOUNT" ? `¥${escapeHtml(result.amount)}` : result.missing?.length ? `还缺少：${escapeHtml(result.missing.join("、"))}` : escapeHtml(Form.outcomeText(result));
      const steps = result.steps || [];
      return `<section class="fs-card fs-result"><div class="fs-result__left"><span class="hb-meta">本次结果</span>
          <b class="fs-result__amount" data-tone="${result.outcome === "AMOUNT" ? "" : "warn"}">${amount}</b>
          <span class="hb-meta">${escapeHtml(scenarioText({ values: data.values || {} }).split(" → ")[0])}</span>
          <span class="hb-meta">${escapeHtml(record.fee_name)} ${escapeHtml(result.rule_version || record.version_label)} · 按第 ⑤ 步的执行日期生效</span></div>
        <div class="fs-result__right"><div class="fs-row"><span class="hb-meta">计算过程</span>${result.reportable ? '<span class="hb-badge" data-tone="active">已生效规则</span>' : '<span class="hb-badge" data-tone="pending">未生效 · 只供核对</span>'}</div>
          ${steps.length ? `<ol class="fs-calc">${steps.map((step, index) => `<li${index === steps.length - 1 && /^合计/.test(step) ? ' class="fs-calc__total"' : ""}>${escapeHtml(step)}</li>`).join("")}</ol>` : ""}
          ${Form.resultBody({ ...result, steps: [], reason: result.missing?.length ? "" : result.reason }, { steps: false })}</div></section>`;
    }

    function boundaryHtml() {
      const data = state.trial;
      return `${data?.boundary ? `<div class="fs-table"><table class="hb-table"><thead><tr><th>参数</th><th>临界值</th><th>金额</th><th>命中档位</th></tr></thead><tbody>
          ${data.boundary.length ? data.boundary.map((row) => `<tr><td>${escapeHtml(row.param)}</td><td>${escapeHtml(row.value)}</td><td>${row.outcome === "AMOUNT" ? `¥ ${escapeHtml(row.amount)}` : "—"}</td>
            <td>${escapeHtml(Form.OUTCOME[row.outcome] || row.outcome)}${row.note ? ` · ${escapeHtml(row.note)}` : ""}</td></tr>`).join("") : '<tr><td colspan="4">这条规则没有分档或数字条件。</td></tr>'}</tbody></table></div>` : ""}`;
    }

    function step4(record, editable, readOnly) {
      const done = scenarios(record);
      const problems = record.content_problems || [];
      if (problems.length) return `${stepTitle(3, record)}<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span>规则还有问题，回第 ① 步修正后才能试算：<br>${problems.map(escapeHtml).join("<br>")}</span></div>`;
      const can = canEdit && !readOnly;
      return `${stepTitle(3, record, `已试算 <b>${Number(record.trial_count || 0)}</b> 个场景`, "用真实场景验证算出来的钱对不对；试算不对外，也不会影响线上单据。")}
        <section class="fs-card fs-form"><div data-fee-trial-form>${Form.render(record.params || [], state.trialValues)}</div>
          ${can ? '<button class="primary-btn" type="button" data-fee-trial>试算 ↵</button>' : ""}</section>
        <div data-fee-trial-result>${resultHtml(record)}</div>
        ${can ? '<div class="fs-row fs-row--start"><button class="ghost-btn hb-btn-sm" type="button" data-fee-trial="boundary">分界试算表</button><span class="hb-meta">其它参数不变，一次只改一个值，看各档临界点</span></div>' : ""}
        ${boundaryHtml()}
        <div class="fs-sub"><h4>试算过的场景</h4>${done.length ? `<ul class="fs-scenes">${done.map((item, index) => `<li><button type="button" class="hb-link" data-fee-scene="${index}"${index === 0 ? ' data-current="true"' : ""}>${escapeHtml(scenarioText(item.detail || {}))}</button><span class="hb-meta">${escapeHtml(formatTime(item.created_at))}</span></li>`).join("")}</ul>` : '<p class="hb-meta">规则改过之后还没有试算。至少试算 1 个场景才能进第 ⑤ 步。</p>'}</div>`;
    }

    // -- ⑤ 日期与提交 / ⑥ 审核 -------------------------------------------------------------

    function summaryHtml(record, { rows = 6, footer = "" } = {}) {
      const lines = linesOf(record);
      const qs = questions(record);
      const trials = scenarios(record);
      const count = Number(record.trial_count || 0);  // the Agent's count; events only give the latest time
      const ok = '<span class="hb-badge" data-tone="active">已完成</span>';
      const list = [
        ["费用名称", escapeHtml(record.fee_name), ""],
        ["版本", `${escapeHtml(record.version_label)}${record.compare?.version_label ? ` · 上一版 ${escapeHtml(record.compare.version_label)}` : ""}`, ""],
        ["来源文档", escapeHtml(sourceText(record) || "—"), ""],
        ["核对", `${lines.filter((line) => line.done).length} / ${lines.length} 行${lines.every((line) => line.done) ? "全部核对" : ""}`, lines.length && lines.every((line) => line.done) ? ok : ""],
        ["拍板", qs.length ? `${qs.length} 个问题 · ${qs.filter((item) => item.answer != null).length} 个已回答` : "没有需要拍板的问题", openDecisions(record) ? "" : ok],
        ["试算", count ? `${count} 个场景${trials.length ? ` · 最近 ${escapeHtml(formatTime(trials[0].created_at))}` : ""}` : "还没有试算", count ? ok : ""],
      ].filter((_row, index) => rows === 6 || index !== 4);
      const answers = qs.filter((item) => item.answer != null);
      return `<section class="fs-card fs-sum"><dl class="fs-sum__left">${list.map(([label, value, tag]) => `<div><dt>${label}</dt><dd>${value}</dd>${tag}</div>`).join("")}</dl>
        <div class="fs-sum__right"><h4>拍板结果</h4>${answers.length ? answers.map((item, index) => `<p><b>Q${index + 1} ${escapeHtml(item.text)}</b><br>${escapeHtml(item.answer)}${item.note ? `：${escapeHtml(item.note)}` : ""}</p>`).join("") : '<p class="hb-meta">没有需要拍板的问题。</p>'}</div>
        ${footer ? `<p class="fs-sum__foot">${footer}</p>` : ""}</section>`;
    }

    function step5(record, editable, readOnly) {
      const can = editable && !readOnly;
      const blockers = record.submit_blockers || [];
      const date = record.effective_from;
      const before = date ? new Date(`${date}T00:00:00`) : null;
      if (before) before.setDate(before.getDate() - 1);
      const pad = (value) => String(value).padStart(2, "0");
      const prev = before ? `${before.getFullYear()}-${pad(before.getMonth() + 1)}-${pad(before.getDate())}` : "";
      const old = record.compare?.status === "APPROVED" ? `仍按 ${record.compare.version_label}` : "不受影响";
      const status = !date ? '<div class="fs-state" data-tone="warn">还差执行日期</div>'
        : blockers.length ? `<div class="fs-state" data-tone="warn">还不能提交：${escapeHtml(blockers[0])}</div>`
        : '<div class="fs-state" data-tone="ok"><span aria-hidden="true">✓</span>可以提交：核对、拍板、试算都已完成</div>';
      return `${stepTitle(4, record, "", "确定从哪天开始执行，然后提交审核。")}
        <div class="fs-date"><label for="fee-date-input">执行日期 <span class="hb-meta">以总部通知为准，填了才能提交</span></label>
          <div class="fs-row fs-row--start"><input id="fee-date-input" type="date" data-meta="effective_from" value="${escapeHtml(date || "")}" ${can ? "" : "disabled"}>${status}</div></div>
        <div class="fs-banner"><div><b>${date ? `开单日期在 ${escapeHtml(date)}（含）之后的单据按这版规则计算；${escapeHtml(prev)} 及之前的单据${escapeHtml(old)}。` : "填好执行日期后，这里写明哪些单据按这版规则计算。"}</b>
          <span>提交后由超级管理员审核，审核中不可修改。</span></div></div>
        ${summaryHtml(record, { footer: "审核人：超级管理员 · 提交后只读" })}
        ${blockers.length > 1 ? `<div class="fs-sub"><h4>提交前还差</h4><ul class="fs-tips">${blockers.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>` : ""}
        <p class="fs-dotnote">确认无误后，点右下角「提交审核」。审核中不可修改。</p>`;
    }

    function linesTable(record) {
      const lines = linesOf(record);
      return `<div class="fs-table"><table class="hb-table"><thead><tr><th>条件</th><th>金额</th><th>出处</th></tr></thead><tbody>
        ${lines.length ? lines.map((line) => `<tr><td>${escapeHtml(line.condition || line.text)}</td><td>${escapeHtml(line.amount || "")}</td><td class="hb-meta">${escapeHtml(line.location || "—")}</td></tr>`).join("") : '<tr><td colspan="3">没有收费行。</td></tr>'}</tbody></table></div>`;
    }

    function step6(record) {
      if (record.status !== "PENDING") return `${stepTitle(5, record)}<div class="fs-empty">在第 ⑤ 步提交审核后，审核人在这里通过或退回。</div>`;
      const lines = linesOf(record);
      const qs = questions(record);
      const line = `提交人 ${escapeHtml(person(record, record.submitted_by))} · 核对 ${lines.filter((item) => item.done).length}/${lines.length} 行 · 拍板 ${qs.filter((item) => item.answer != null).length}/${qs.length} · 试算 ${Number(record.trial_count || 0)} 个场景 · 执行日期 ${escapeHtml(record.effective_from || "未填")}`;
      if (superAdmin) {
        const blockers = record.blockers || [];
        return `${stepTitle(5, record, `提交于 ${escapeHtml(formatTime(record.submitted_at))}`, "审核人只需要看：做对了没有、问题答得对不对。前面的步骤已锁定。")}
          <div class="fs-await"><span aria-hidden="true">?</span><div><b>${escapeHtml(record.fee_name)} ${escapeHtml(record.version_label)} 已提交，等待你审核</b><p>${line}</p></div></div>
          <div class="fs-sub"><h4>提交内容（只读）</h4>${summaryHtml(record, { rows: 5, footer: "改了做法没有？→ 回第 ① 步看规则结构" })}</div>
          <div class="fs-sub"><h4>收费表（供核对，不可改）</h4>${linesTable(record)}</div>
          ${blockers.length ? `<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span>还不能生效：${blockers.map(escapeHtml).join("；")}</span></div>` : ""}
          <div class="fs-row fs-row--start"><button class="ghost-btn" type="button" data-fee-action="withdraw">退回草稿</button>
            <button class="primary-btn" type="button" data-fee-action="approve" ${blockers.length ? "disabled" : ""}>通过并生效</button>
            <span class="hb-meta">通过后按执行日期生效，机器人和报价从那天起使用。</span></div>`;
      }
      return `${stepTitle(5, record, "", "已提交，等待超级管理员审核。审核期间不能修改。")}
        <div class="fs-wait"><span class="fs-wait__icon"><i data-feather="clock"></i></span><div><b>已提交，等待超级管理员审核</b>
          <p>提交于 ${escapeHtml(formatTime(record.submitted_at))} · ${escapeHtml(person(record, record.submitted_by))}</p><p>有结果会显示在这里，期间可以先去看别的费用。</p></div><span class="hb-badge">只读</span></div>
        <div class="fs-sub"><h4>提交内容（只读）</h4>${summaryHtml(record, { rows: 5 })}</div>
        ${canEdit ? '<div class="fs-row fs-row--start"><button class="ghost-btn" type="button" data-fee-action="withdraw">撤回为草稿</button><span class="hb-meta">撤回后回到草稿，可以继续改；审核人不会再看到这一版。</span></div>' : ""}
        <div class="fs-note"><b>审核中，前面的步骤是锁住的</b><span>点步骤条可以回看第 ①–⑤ 步当时提交的内容，但所有输入框只读；要改只能先撤回。</span></div>`;
    }

    // -- 已生效 / 已作废 overview (68) ---------------------------------------------------------

    function overviewHtml(record) {
      const lines = linesOf(record);
      const later = state.versions.filter((item) => item.fee_kind === record.fee_kind && item.status === "APPROVED" && item.rule_id !== record.rule_id
        && String(item.effective_from || "") > String(record.effective_from || "")).sort((a, b) => String(a.effective_from).localeCompare(String(b.effective_from)))[0];
      const answers = questions(record).filter((item) => item.answer != null);
      const retired = record.status === "RETIRED";
      return `<div class="fs-over__head"><div><h3>生效概览 ${retired ? '<span class="hb-badge" data-tone="retired">已作废</span>' : '<span class="hb-badge" data-tone="active">当前版本</span>'}</h3>
          <p class="hb-meta">${record.approved_at ? `生效于 ${escapeHtml(formatTime(record.approved_at, { withYear: true }))} · 审核 ${escapeHtml(person(record, record.approved_by))}` : "没有生效过"}${retired ? ` · 作废于 ${escapeHtml(formatTime(record.retired_at, { withYear: true }))}` : ""}</p></div>
          ${canEdit ? '<button class="primary-btn" type="button" data-fee-action="copy">复制为新草稿以修改</button>' : ""}</div>
        <div class="fs-tiles">
          <div><span>执行日期</span><b>${escapeHtml(record.effective_from || "—")}</b><p>从这天开始算</p></div>
          <div><span>适用范围</span><b>${escapeHtml(record.effective_from ? `${record.effective_from} 起` : "—")}</b><p>${later ? `至 ${escapeHtml(later.effective_from)}（${escapeHtml(later.version_label)}）之前` : "长期有效"}</p></div>
          <div><span>收费表</span><b>${lines.length} 行</b><p>${lines.some(missingSource) ? "有行缺出处" : "全部有手册出处"}</p></div></div>
        <div class="fs-sub"><h4>收费表 <span class="hb-meta">只读 · 来自第 ② 步核对结果</span></h4>${linesTable(record)}</div>
        <div class="fs-sub"><h4>拍板结果</h4>${answers.length ? `<div class="fs-card fs-answers">${answers.map((item, index) => `<p><b>Q${index + 1} ${escapeHtml(item.text)} → ${escapeHtml(item.answer)}${item.note ? `：${escapeHtml(item.note)}` : ""}</b></p>`).join("")}</div>` : '<p class="hb-meta">没有需要拍板的问题。</p>'}</div>
        ${canEdit && !retired ? '<div class="fs-note"><b>要改这版？复制成新草稿，只核对改动的行</b><span>新草稿直接进第 ② 步：没变化的行沿用这次核对，拍板答案沿用，试算要重新做一次；生效版本不受影响。</span></div>' : ""}
        <p class="hb-meta">来源文档 ${escapeHtml(sourceText(record) || "—")}</p>`;
    }

    // -- render --------------------------------------------------------------------------

    function bodyHtml(record) {
      if (record.status === "APPROVED" || record.status === "RETIRED") return overviewHtml(record);
      const editable = editableDraft(record);
      const open = firstOpen(record);
      const step = state.step;
      const readOnly = record.status !== "DRAFT";
      if (record.status === "DRAFT" && step > open && step < 5) {
        return `${stepTitle(step, record)}<div class="fs-empty">先完成第 ${NUM[open]} 步「${STEPS[open]}」，这一步才能做。<button class="hb-link fs-link-red" type="button" data-fee-step="${open}">去第 ${NUM[open]} 步 →</button></div>`;
      }
      const views = [() => step1(record, editable && !readOnly), () => step2(record, editable, readOnly), () => step3(record, editable, readOnly),
        () => step4(record, editable, readOnly), () => step5(record, editable, readOnly), () => step6(record)];
      return views[step]();
    }

    const SCROLLERS = [".fs-lines__body", ".fs-viewer__paper", "[data-fee-body]"];
    function captureScroll(node) {
      const inner = SCROLLERS.map((selector) => {
        const el = node.querySelector(selector);
        return el ? [selector, el.scrollTop, el.scrollLeft] : null;
      }).filter(Boolean);
      const outer = [];
      for (let el = node.parentElement; el; el = el.parentElement) {
        if (el.scrollTop || el.scrollLeft) outer.push([el, el.scrollTop, el.scrollLeft]);
      }
      return { inner, outer, x: window.scrollX, y: window.scrollY };
    }
    function restoreScroll(node, kept) {
      kept.inner.forEach(([selector, top, left]) => {
        const el = node.querySelector(selector);
        if (el) { el.scrollTop = top; el.scrollLeft = left; }
      });
      kept.outer.forEach(([el, top, left]) => { el.scrollTop = top; el.scrollLeft = left; });
      window.scrollTo(kept.x, kept.y);
    }

    function renderEditor() {
      const node = $("[data-fee-editor]");
      const record = state.current;
      root.querySelector(".fs-work")?.setAttribute("data-drawer", String(state.drawer));
      if (!record) {
        const empty = !state.versions.length;
        node.innerHTML = `<button class="ghost-btn hb-btn-sm fs-drawer-btn" type="button" data-fee-drawer><i data-feather="menu"></i><span>费用</span></button>
          ${empty ? `<div class="fs-blank"><b>还没有费用规则</b><p class="hb-meta">从手册起草，或手动新建一条。</p>${canEdit ? `<div class="fs-row fs-row--start">
            <button class="fs-mini" type="button" data-fee-draft><b>从手册起草</b><span class="hb-badge" data-tone="danger">推荐</span></button>
            <button class="fs-mini" type="button" data-fee-create><b>手动新建费用</b></button></div>` : ""}</div>`
            : '<div class="fs-blank fs-blank--line"><p class="hb-meta">从左侧选择一个费用</p></div>'}`;
        if (window.feather) window.feather.replace();
        return;
      }
      const flat = record.status === "APPROVED" || record.status === "RETIRED";
      // Re-rendering the same rule and step (e.g. after 「✓ 对」) keeps every scroll position:
      // the 收费表 list used to jump back to its first row on each tick.
      const view = `${record.rule_id}:${state.step}`;
      const kept = view === state.renderedView ? captureScroll(node) : null;
      node.innerHTML = `${headerHtml(record)}${flat ? "" : stepperHtml(record)}
        <div class="fs-body" data-fee-body>${conflictHtml()}${bodyHtml(record)}</div>${footerHtml(record)}`;
      state.renderedView = view;
      if (kept) restoreScroll(node, kept);
      state.staleBody = false;
      if (window.feather) window.feather.replace();
    }

    // Refresh what does not hold the caret (stepper, footer, list); the body waits until focus leaves it.
    function renderChrome() {
      const record = state.current;
      if (!record) return;
      const active = document.activeElement;
      const typing = active && $("[data-fee-body]")?.contains(active) && active.matches("input, textarea, select");
      if (!typing) { renderEditor(); return; }
      state.staleBody = true;
      const steps = root.querySelector(".fs-steps");
      if (steps) steps.outerHTML = stepperHtml(record);
      const foot = root.querySelector(".fs-foot");
      if (foot) foot.outerHTML = footerHtml(record);
      if (window.feather) window.feather.replace();
    }

    // -- data ----------------------------------------------------------------------------

    async function loadList() {
      state.versions = (await request(base)).versions || [];
      renderList();
    }

    async function openVersion(ruleId, { step = null } = {}) {
      const record = await request(`${base}/${encodeURIComponent(ruleId)}`);
      const switched = state.current?.rule_id !== ruleId;
      if (switched) {
        Object.assign(state, { trial: null, trialValues: {}, editQuestions: false, expanded: new Set(["formula"]), json: null, jsonError: "",
          structureOpen: false, manual: false, editPath: "", showSame: false, conflict: null, saveState: "saved", saveError: "",
          viewer: { document: "", page: 0, zoom: 100, sig: "" }, menu: false });
        if (draft) await draft.load(ruleId);
      }
      const statusChanged = state.current?.status !== record.status;
      state.current = record;
      state.dirty = false; state.checksDirty = false;
      if (step !== null) state.step = step;
      else if (switched || statusChanged) state.step = record.status === "DRAFT" || record.status === "PENDING" ? firstOpen(record) : 0;
      renderList();
      renderEditor();
    }

    async function run(progress, work, success = "") {
      if (state.busy) return;
      state.busy = true;
      const unlock = UI.lockButtons(root);
      if (progress) feedback.progress(progress);
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

    function checksPayload(record) {
      return (record.checks || []).map((item) => {
        if (item.kind === "line") return { kind: "line", sig: item.sig, done: true };
        if (item.kind === "question") {
          return { kind: "question", text: String(item.text || "").trim(), options: (item.options || []).map((option) => String(option).trim()).filter(Boolean),
            answer: item.answer ?? null, note: String(item.note || "").trim() };
        }
        return { text: String(item.text || "").trim(), done: Boolean(item.done) };
      });
    }

    // 自动保存 (spec 69 D): about a second after typing stops; one request at a time. Content
    // edits go with “save” (checks included), ticks and answers alone with “checks”. A conflict
    // never overwrites: the page offers 载入最新. A failure stays on the page with 重试.
    function schedule(kind) {
      const record = state.current;
      if (!record || !canEdit || !["DRAFT", "PENDING"].includes(record.status)) return;
      if (kind === "content") state.dirty = true; else state.checksDirty = true;
      if (state.saveState === "failed") state.saveState = "saved";
      window.clearTimeout(state.saveTimer);
      state.saveTimer = window.setTimeout(flush, kind === "content" ? SAVE_DELAY : 350);
      renderChrome();
    }

    async function flush() {
      const record = state.current;
      if (!record || state.saving || state.conflict || (!state.dirty && !state.checksDirty)) return;
      // An answer “其他…” waits for its note; the Agent refuses it without one.
      const waiting = questions(record).some((item) => String(item.answer || "").startsWith("其他") && !String(item.note || "").trim());
      const ruleId = record.rule_id;
      const content = state.dirty;
      const checks = checksPayload(record);
      if (!content && waiting) { state.checksDirty = false; renderChrome(); return; }
      const body = content ? { fee_name: String(record.fee_name || "").trim(), version_label: record.version_label, effective_from: record.effective_from || null,
        content: record.content, checks: waiting ? checks.map((item) => (item.kind === "question" && String(item.answer || "").startsWith("其他") && !item.note ? { ...item, answer: null } : item)) : checks,
        row_version: record.row_version } : { checks, row_version: record.row_version };
      state.saving = true; state.dirty = false; state.checksDirty = false;
      renderChrome();
      try {
        const data = await request(`${base}/${encodeURIComponent(ruleId)}/${content ? "save" : "checks"}`, body);
        if (state.current?.rule_id !== ruleId) return;
        state.current.row_version = data.row_version;
        state.saveState = "saved";
        if (!state.dirty && !state.checksDirty) {
          const fresh = await request(`${base}/${encodeURIComponent(ruleId)}`);
          if (state.current?.rule_id === ruleId && !state.dirty && !state.checksDirty) {
            state.current = fresh;
            if (state.editPath && !(fresh.lines || []).some((line) => line.path === state.editPath)) state.editPath = "";
            loadList().catch(() => {});
          } else if (state.current?.rule_id === ruleId) state.current.row_version = fresh.row_version;
        }
      } catch (error) {
        if (state.current?.rule_id !== ruleId) return;
        if (error.status === 409 && /别人修改/.test(error.message || "")) {
          state.saveState = "conflict";
          const fresh = await request(`${base}/${encodeURIComponent(ruleId)}`).catch(() => null);
          state.conflict = { by: fresh ? person(fresh, fresh.updated_by) : "" };
          state.saving = false;
          renderEditor();
          return;
        }
        state.saveState = "failed";
        state.saveError = error.message || "网络中断";
        if (content) state.dirty = true; else state.checksDirty = true;
      } finally {
        state.saving = false;
      }
      if ((state.dirty || state.checksDirty) && state.saveState !== "failed") flush();
      else renderChrome();
    }

    async function settle() {
      window.clearTimeout(state.saveTimer);
      await flush();
      for (let tries = 0; state.saving && tries < 100; tries += 1) await new Promise((resolve) => { window.setTimeout(resolve, 100); });
      return !state.dirty && !state.checksDirty && state.saveState === "saved" && !state.conflict;
    }

    // Scroll only the 收费表 box (never the page) so the newly selected row is in view.
    function revealSelectedLine() {
      const box = root.querySelector(".fs-lines__body");
      const row = box?.querySelector('.fs-line[data-selected="true"]');
      if (!box || !row) return;
      const top = row.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
      if (top < box.scrollTop) box.scrollTop = top - 24;
      else if (top + row.offsetHeight > box.scrollTop + box.clientHeight) box.scrollTop = top + row.offsetHeight - box.clientHeight + 8;
    }

    function setLine(record, sig, done) {
      const line = linesOf(record).find((item) => item.sig === sig);
      if (!line) return;
      line.done = done;
      record.checks = (record.checks || []).filter((item) => !(item.kind === "line" && item.sig === sig));
      if (done) record.checks.push({ kind: "line", sig, done: true });
    }

    // 看原页 enlarged: the cited manual page as a picture in a dialog.
    let pageDialog = null;
    async function showPage(document, pageNumber) {
      if (!pageDialog) {
        pageDialog = window.document.createElement("dialog");
        pageDialog.className = "hb-dialog hb-dialog--page";
        pageDialog.setAttribute("aria-labelledby", "fee-page-title");
        window.document.body.append(pageDialog);
        pageDialog.addEventListener("click", (event) => {
          const target = event.target.closest("button");
          if (event.target === pageDialog || target?.hasAttribute("data-page-close")) pageDialog.close();
          else if (target?.dataset.pageGo) showPage(pageDialog.dataset.document, Number(target.dataset.pageGo));
        });
      }
      pageDialog.dataset.document = document;
      const head = `<div class="hb-dialog__head"><h2 id="fee-page-title">${escapeHtml(document)} · 第 ${pageNumber} 页</h2>
        <span class="hb-actions">${pageNumber > 1 ? `<button class="ghost-btn hb-btn-sm" type="button" data-page-go="${pageNumber - 1}">上一页</button>` : ""}
        <button class="ghost-btn hb-btn-sm" type="button" data-page-go="${pageNumber + 1}">下一页</button>
        <button class="ghost-btn hb-btn-sm" type="button" data-page-close>关闭</button></span></div>`;
      const key = `${document}|${pageNumber}`;
      if (!state.pages.get(key)) {
        pageDialog.innerHTML = `${head}<div class="hb-page"><p class="hb-meta">正在读取手册原页……</p></div>`;
        if (!pageDialog.open) pageDialog.showModal();
        state.pages.delete(key);
        await loadPage(document, pageNumber);
      }
      const image = state.pages.get(key);
      pageDialog.innerHTML = `${head}<div class="hb-page">${image?.error ? `<p class="hb-meta" data-tone="danger">${escapeHtml(image.error)}</p>` : `<img alt="${escapeHtml(document)} 第 ${pageNumber} 页" src="data:image/png;base64,${image}">`}</div>`;
      if (!pageDialog.open) pageDialog.showModal();
    }

    function showHistory() {
      const record = state.current;
      const dialog = window.document.createElement("dialog");
      dialog.className = "hb-dialog fs-history";
      dialog.setAttribute("aria-labelledby", "fee-history-title");
      const events = record.events || [];
      dialog.innerHTML = `<div class="hb-dialog__body"><h2 id="fee-history-title">历史 · ${escapeHtml(record.fee_name)} ${escapeHtml(record.version_label)}</h2>
        ${events.length ? `<ul class="hb-events">${events.map((item) => `<li><span class="hb-meta">${escapeHtml(formatTime(item.created_at))}</span><b>${escapeHtml(eventText(item))}</b><span class="hb-meta">${escapeHtml(person(record, item.actor_id))}</span></li>`).join("")}</ul>` : '<p>还没有操作记录。</p>'}</div>
        <div class="hb-dialog__foot"><span></span><button class="ghost-btn" type="button" data-close>关闭</button></div>`;
      window.document.body.append(dialog);
      dialog.addEventListener("click", (event) => { if (event.target === dialog || event.target.closest("[data-close]")) dialog.close(); });
      dialog.addEventListener("close", () => dialog.remove(), { once: true });
      dialog.showModal();
    }

    function eventText(item) {
      const detail = item.detail || {};
      if (item.action === "TRIAL") return `${detail.boundary ? "分界试算表" : "试算"} ${scenarioText(detail)}`;
      if (item.action === "CHECK") return `核对（收费表已勾 ${detail.lines ?? 0} 行 · 已回答 ${detail.answered ?? 0} 个问题）`;
      if (item.action === "DATE") return `设执行日期 ${detail.effective_from || ""}`;
      if (item.action === "DRAFT") return `AI 起草（${detail.document || "手册"} 第 ${detail.pages || "?"} 页${detail.filled ? "" : detail.new_fee ? "，新费用" : "，存为新版本"}）`;
      if ((item.action === "SAVE" || item.action === "EDIT") && detail.fee_name) return `费用改名为“${detail.fee_name}”`;
      return ACTION_TEXT[item.action] || item.action;
    }

    // 批量设执行日期: one headquarters date for several drafts at once.
    async function batchDates() {
      const drafts = state.versions.filter((item) => item.status === "DRAFT");
      if (!drafts.length) { feedback.error("没有草稿", "只有草稿可以设执行日期。"); return; }
      const dialog = window.document.createElement("dialog");
      dialog.className = "hb-dialog";
      dialog.setAttribute("aria-labelledby", "fee-dates-title");
      dialog.innerHTML = `<form method="dialog"><div class="hb-dialog__body"><h2 id="fee-dates-title">批量设执行日期</h2>
        <p>以总部通知为准。只改执行日期，不影响已做的核对和试算。</p>
        <label class="hb-dialog__field"><span>执行日期</span><input type="date" required data-batch-date></label>
        <div class="hb-dialog__field"><span>应用到这些草稿</span><ul class="hb-batch">${drafts.map((item) => `<li><label><input type="checkbox" data-batch-rule="${escapeHtml(item.rule_id)}" ${item.effective_from ? "" : "checked"}>
          <span>${escapeHtml(item.fee_name)} <span class="hb-meta">${escapeHtml(item.version_label)} · ${item.effective_from ? `现为 ${escapeHtml(item.effective_from)}` : "未填"}</span></span></label></li>`).join("")}</ul></div></div>
        <div class="hb-dialog__foot"><button class="ghost-btn" value="cancel">取消</button><button class="primary-btn" value="ok">保存执行日期</button></div></form>`;
      window.document.body.append(dialog);
      const result = await new Promise((resolve) => {
        dialog.addEventListener("close", () => {
          const date = dialog.querySelector("[data-batch-date]").value;
          const chosen = [...dialog.querySelectorAll("[data-batch-rule]:checked")].map((box) => box.dataset.batchRule);
          resolve(dialog.returnValue === "ok" ? { date, chosen } : null);
          dialog.remove();
        }, { once: true });
        dialog.showModal();
      });
      if (!result) return;
      if (!result.date || !result.chosen.length) { feedback.error("没有保存", "请选择执行日期和至少一个草稿。"); return; }
      if (!(await settle())) { feedback.error("没有保存", "当前草稿还有没保存的修改，请先处理。"); return; }
      const versions = drafts.filter((item) => result.chosen.includes(item.rule_id)).map((item) => ({ rule_id: item.rule_id, row_version: item.row_version }));
      run("正在保存执行日期……", async () => {
        await request(`${base}/effective-dates`, { effective_from: result.date, versions });
        await loadList();
        if (state.current && result.chosen.includes(state.current.rule_id)) await openVersion(state.current.rule_id, { step: state.step });
      }, `已为 ${versions.length} 个草稿设执行日期 ${result.date}`);
    }

    function rememberTrialValues() {
      const form = $("[data-fee-trial-form]");
      if (form && state.current) state.trialValues = Form.read(form, state.current.params || []);
      return state.trialValues;
    }

    async function runTrial(boundary) {
      const record = state.current;
      const values = rememberTrialValues();
      if (!(await settle())) { feedback.error("没有试算", "规则还有没保存的修改，保存后再试算。"); return; }
      run("", async () => {
        const data = await request(`${base}/${encodeURIComponent(record.rule_id)}/trial`, { values, boundary });
        state.trial = { ...data, values };
        await openVersion(record.rule_id, { step: 3 });
      });
    }

    async function askName(title) {
      const name = await confirmAction({ title, body: "左栏和报价单上会显示这个名字，之后在第 ① 步也可以改。", confirm: "新建", input: { label: "费用名称（例如：打牛费）", maxLength: 20 } });
      if (name === false) return null;
      if (!name) { feedback.error("没有新建", "费用名称不能为空。"); return null; }
      return name;
    }

    async function createFee({ fromManual = false, nodeToken = "" } = {}) {
      if (!(await settle()) && state.current) { feedback.error("没有新建", "当前草稿还有没保存的修改，请先处理。"); return; }
      const name = await askName(fromManual ? "从手册起草：先给费用起个名字" : "新建费用");
      if (!name) return;
      run("正在新建……", async () => {
        const created = await request(base, { fee_name: name });
        await loadList();
        await openVersion(created.rule_id, { step: 0 });
        state.drawer = false;
        if (fromManual && draft) await draft.openPicker({ nodeToken });
        else renderEditor();
      }, fromManual ? "" : "已新建草稿：从手册起草，或手动填写规则");
    }

    function draftChanged(kind) {
      const record = state.current;
      if (!record) return;
      if (kind === "done") {
        run("", async () => { await openVersion(record.rule_id, { step: 0 }); await loadList(); }, "起草完成：初稿已写进这份草稿，下一步逐行核对原文");
        return;
      }
      if (kind === "manual") { state.manual = true; state.structureOpen = true; state.json = JSON.stringify(record.content, null, 2); }
      if (state.step === 0) renderEditor(); else renderChrome();
    }

    // -- events ------------------------------------------------------------------------

    document.addEventListener("handbook:draft-from", async (event) => {
      if (!canEdit) return;
      await createFee({ fromManual: true, nodeToken: event.detail?.nodeToken || "" });
    });

    root.addEventListener("focusout", (event) => {
      // Moving to a button: its click re-renders; replacing it now would swallow the click.
      if (event.relatedTarget?.closest?.("button")) return;
      window.setTimeout(() => {
        const active = document.activeElement;
        if (state.staleBody && !($("[data-fee-body]")?.contains(active) && active.matches("input, textarea, select"))) renderEditor();
      }, 0);
    });

    root.addEventListener("input", async (event) => {
      const target = event.target;
      if (target.matches("[data-fee-search]")) { state.search = target.value; renderList(); return; }
      if (draft && target.closest(".fs-picker") && await draft.handle(event)) return;
      const record = state.current;
      if (!record || target.closest("[data-fee-trial-form]") || target.matches("[data-json]") || !editableDraft(record)) return;
      if (target.dataset.f) {
        const raw = target.value.trim();
        const blank = target.dataset.blank;
        const value = raw === "" ? (blank === "delete" ? undefined : blank === "null" ? null : "") : raw;
        set(record.content, target.dataset.f.split("."), value);
      } else if (target.dataset.meta) {
        record[target.dataset.meta] = target.value || (target.dataset.meta === "effective_from" ? null : "");
        if (target.dataset.meta === "fee_name" && !String(target.value).trim()) return;  // an empty name is not saved
      } else if (target.hasAttribute("data-reminders")) {
        record.content.reminders = target.value.split("\n").map((line) => line.trim()).filter(Boolean);
      } else if (target.dataset.checkText !== undefined) {
        record.checks[Number(target.dataset.checkText)].text = target.value;
      } else if (target.dataset.questionText !== undefined) {
        record.checks[Number(target.dataset.questionText)].text = target.value;
      } else if (target.dataset.questionOptions !== undefined) {
        const item = record.checks[Number(target.dataset.questionOptions)];
        item.options = target.value.split(/[/／]/).map((option) => option.trim()).filter(Boolean);
        if (!item.options.includes(item.answer)) item.answer = null;
      } else if (target.dataset.answerNote !== undefined) {
        record.checks[Number(target.dataset.answerNote)].note = target.value;
        schedule("checks");
        return;
      } else return;
      state.json = null;
      schedule("content");
    });

    root.addEventListener("change", async (event) => {
      const target = event.target;
      if (draft && target.closest(".fs-picker") && await draft.handle(event)) return;
      const record = state.current;
      if (!record) return;
      if (target.dataset.check !== undefined && canEdit) {
        record.checks[Number(target.dataset.check)].done = target.checked;
        schedule("checks"); renderEditor();
      } else if (target.dataset.answer !== undefined && canEdit) {
        const item = record.checks[Number(target.dataset.answer)];
        item.answer = target.value;
        schedule("checks"); renderEditor();
        if (String(item.answer).startsWith("其他") && !String(item.note || "").trim()) root.querySelector(`[data-answer-note="${target.dataset.answer}"]`)?.focus();
      } else if (target.matches("select[data-f], [data-meta='effective_from'], [data-question-options]")) {
        renderEditor();
      }
    });

    root.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target.closest("[data-fee-trial-form]") && event.target.matches("input")) { event.preventDefault(); runTrial(false); }
      if (event.key === "Escape" && state.menu) { state.menu = false; renderEditor(); }
    });

    document.addEventListener("click", (event) => {
      if (state.menu && !event.target.closest(".fs-menu-wrap")) { state.menu = false; if (state.current) renderEditor(); }
    });

    async function act(action) {
      const record = state.current;
      const url = (name, id = record.rule_id) => `${base}/${encodeURIComponent(id)}/${name}`;
      if (action === "approve" && !(await confirmAction({ title: "通过并生效？", body: `生效后机器人和报价按执行日期 ${record.effective_from}（含）起使用 ${record.version_label}；之前的单据不受影响。`, confirm: "通过并生效" }))) return;
      if (action === "withdraw" && !(await confirmAction(superAdmin
        ? { title: "退回草稿？", body: "退回后由发起人修改，需要重新提交审核。", confirm: "退回草稿" }
        : { title: "撤回为草稿？", body: "撤回后回到草稿，可以继续改；审核人不会再看到这一版。", confirm: "撤回为草稿" }))) return;
      if (action === "retire" && !(await confirmAction({ title: "作废此版本？", body: "作废后不可恢复，机器人和报价都不会再使用该版本，左栏也不再显示。", confirm: "作废此版本", danger: true }))) return;
      if (action === "delete" && !(await confirmAction({ title: "删除这份草稿？", body: "删除后无法恢复，这份草稿会被彻底删掉。左栏也会同步移除。", confirm: "删除草稿", danger: true }))) return;
      if (action === "submit") {
        if (!(await settle())) { feedback.error("没有提交", "还有修改没保存，请先处理底栏的保存状态。"); return; }
        if (!(await confirmAction({ title: "提交审核？", body: "提交后进入审核，期间不可修改；超级管理员审核通过后按执行日期生效。", confirm: "提交审核" }))) return;
      }
      if (action === "copy" && record.status === "DRAFT") return;
      const labels = { submit: "已提交审核：超级管理员会收到；通过后按执行日期生效", withdraw: superAdmin ? "已退回草稿" : "已撤回为草稿", approve: "已审核生效", retire: "已作废", copy: "已复制为新草稿：只核对改动的行", delete: "已删除草稿" };
      run("正在处理……", async () => {
        if (action === "copy") {
          const created = await request(url("copy"), {});
          await loadList();
          await openVersion(created.rule_id);  // lands on the first open step: ② for a copy
          return;
        }
        await request(url(action), { row_version: record.row_version });
        await loadList();
        if (action === "retire" || action === "delete") {
          state.current = null;
          const next = state.versions.find((item) => item.fee_kind === record.fee_kind && item.status !== "RETIRED");
          if (next) await openVersion(next.rule_id); else renderEditor();
          renderList();
        } else {
          await openVersion(record.rule_id, { step: null });
        }
      }, labels[action]);
    }

    root.addEventListener("click", async (event) => {
      if (draft && event.target.closest(".fs-picker, .fs-progress, .fs-fail, [data-draft-retry], [data-draft-rerange], [data-draft-manual], [data-draft-cancel]")) {
        if (await draft.handle(event)) return;
      }
      const target = event.target.closest("button");
      if (!target || target.disabled) return;
      const record = state.current;
      if (record && target.closest("[data-fee-trial-form]") && Form.handleClick(target, record.params || [], $("[data-fee-trial-form]"))) return;
      if (state.busy && !target.matches("[data-fee-menu], [data-fee-drawer]")) return;
      if (target.hasAttribute("data-fee-create")) { createFee(); return; }
      if (target.hasAttribute("data-fee-draft")) { createFee({ fromManual: true }); return; }
      if (target.hasAttribute("data-fee-batch-dates")) { batchDates(); return; }
      if (target.hasAttribute("data-fee-drawer")) { state.drawer = !state.drawer; renderEditor(); return; }
      if (target.hasAttribute("data-fee-drawer-close")) { state.drawer = false; renderEditor(); return; }
      if (target.dataset.feeOpen) {
        if (target.dataset.feeOpen === record?.rule_id) { state.drawer = false; renderEditor(); return; }
        if (record && !(await settle())) {
          if (!(await confirmAction({ title: "还有修改没保存", body: "离开后这些修改不会保存。", confirm: "放弃并离开", cancel: "留下处理", danger: true }))) return;
        }
        state.drawer = false;
        run("", () => openVersion(target.dataset.feeOpen));
        return;
      }
      if (!record) return;
      if (target.hasAttribute("data-fee-menu")) { state.menu = !state.menu; renderEditor(); return; }
      if (target.dataset.feeMenuItem) {
        state.menu = false;
        const item = target.dataset.feeMenuItem;
        renderEditor();
        if (item === "dates") batchDates();
        else if (item === "rename") {
          const name = await confirmAction({ title: "重命名费用", body: `现在叫“${record.fee_name}”。改名会同时改这个费用的所有版本。`, confirm: "保存名称", input: { label: "新名称", maxLength: 20 } });
          if (name) { record.fee_name = name; schedule("content"); renderEditor(); }
        } else act(item);
        return;
      }
      if (target.hasAttribute("data-fee-history")) { showHistory(); return; }
      if (target.hasAttribute("data-fee-reload")) {
        state.conflict = null; state.saveState = "saved"; state.dirty = false; state.checksDirty = false;
        run("", () => openVersion(record.rule_id, { step: state.step }), "已载入最新版本");
        return;
      }
      if (target.hasAttribute("data-fee-retry")) { state.saveState = "saved"; flush(); return; }
      if (target.dataset.feeStep !== undefined) {
        if (state.step === 3) rememberTrialValues();
        state.step = Number(target.dataset.feeStep); renderEditor(); return;
      }
      if (target.hasAttribute("data-fee-prev")) { if (state.step === 3) rememberTrialValues(); state.step = Math.max(0, state.step - 1); renderEditor(); return; }
      if (target.hasAttribute("data-fee-next")) { if (state.step === 3) rememberTrialValues(); state.step = Math.min(5, state.step + 1); renderEditor(); return; }
      if (target.hasAttribute("data-fee-submit")) { act("submit"); return; }
      if (target.dataset.feeAction) { act(target.dataset.feeAction); return; }
      if (target.hasAttribute("data-fee-from-manual") && draft) {
        if (!(await settle())) { feedback.error("没有开始", "还有修改没保存，请先处理。"); return; }
        draft.openPicker(); return;
      }
      if (target.hasAttribute("data-fee-manual")) { draftChanged("manual"); return; }
      if (target.hasAttribute("data-fee-structure")) { state.structureOpen = !state.structureOpen; if (!state.structureOpen) state.manual = false; renderEditor(); return; }
      if (target.dataset.lineSelect) {
        const line = linesOf(record).find((item) => item.sig === target.dataset.lineSelect);
        if (line) { selectLine(line); renderEditor(); }
        return;
      }
      if (target.dataset.lineTick || target.dataset.lineUntick) {
        const sig = target.dataset.lineTick || target.dataset.lineUntick;
        setLine(record, sig, Boolean(target.dataset.lineTick));
        let moved = false;
        if (target.dataset.lineTick) {
          // Move on to the next unchecked row below this one (wrapping round), not the table's first.
          const lines = linesOf(record);
          const at = lines.findIndex((line) => line.sig === sig);
          const open = (line) => !line.done && line.document;
          const next = lines.slice(at + 1).find(open) || lines.slice(0, Math.max(at, 0)).find(open);
          if (next) { selectLine(next); moved = true; }
        }
        schedule("checks"); renderEditor();
        if (moved) revealSelectedLine();
        return;
      }
      if (target.dataset.lineEdit) { state.editPath = state.editPath === target.dataset.lineEdit ? "" : target.dataset.lineEdit; renderEditor(); return; }
      if (target.hasAttribute("data-fee-show-same")) { state.showSame = !state.showSame; renderEditor(); return; }
      if (target.dataset.viewerPage) { state.viewer.page = Number(target.dataset.viewerPage); loadPage(state.viewer.document, state.viewer.page); renderEditor(); return; }
      if (target.dataset.viewerZoom) { state.viewer.zoom = Math.min(200, Math.max(50, state.viewer.zoom + Number(target.dataset.viewerZoom))); renderEditor(); return; }
      if (target.hasAttribute("data-viewer-open")) { showPage(state.viewer.document, state.viewer.page); return; }
      if (target.hasAttribute("data-edit-questions")) { state.editQuestions = !state.editQuestions; renderEditor(); return; }
      if (target.hasAttribute("data-fee-add-question")) {
        state.editQuestions = true;
        record.checks = [...(record.checks || []), { kind: "question", text: "新问题（写明页码和没写清的地方）", options: ["按现在的规则", "其他（写明做法）"], answer: null, note: "" }];
        schedule("content"); renderEditor();
        const inputs = root.querySelectorAll("[data-question-text]");
        inputs[inputs.length - 1]?.select();
        return;
      }
      if (target.dataset.feeRemoveCheck !== undefined) { record.checks.splice(Number(target.dataset.feeRemoveCheck), 1); schedule("content"); renderEditor(); return; }
      if (target.hasAttribute("data-fee-trial")) { runTrial(target.dataset.feeTrial === "boundary"); return; }
      if (target.dataset.feeScene !== undefined) {
        const scene = scenarios(record)[Number(target.dataset.feeScene)];
        state.trialValues = { ...(scene?.detail?.values || {}) };
        renderEditor(); return;
      }
      if (target.dataset.fold) {
        const key = target.dataset.fold;
        if (state.expanded.has(key)) state.expanded.delete(key); else state.expanded.add(key);
        renderEditor(); return;
      }
      if (target.hasAttribute("data-fold-all")) {
        const keys = new Set();
        eachRoot((_node, path) => keys.add(path.join(".")));
        state.expanded = state.expanded.size > 1 ? new Set() : keys;
        renderEditor(); return;
      }
      if (target.dataset.sourceAdd && editableDraft(record)) {
        const path = target.dataset.sourceAdd.split(".");
        let last = null;
        eachRoot((node) => { if (filled(node.source)) last = node.source; });
        set(record.content, [...path, "source"], { document: last?.document || "", location: "" });
        state.json = null; schedule("content"); renderEditor(); return;
      }
      if (target.dataset.sourceRemove && editableDraft(record)) {
        set(record.content, [...target.dataset.sourceRemove.split("."), "source"], undefined);
        state.json = null; schedule("content"); renderEditor(); return;
      }
      if (target.hasAttribute("data-json-apply") && editableDraft(record)) {
        const text = $("[data-json]").value;
        try {
          const parsed = JSON.parse(text);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("最外层必须是一个对象 {…}");
          record.content = parsed;
          state.json = null; state.jsonError = ""; state.manual = false;
          schedule("content"); renderEditor();
        } catch (error) {
          state.json = text; state.jsonError = `JSON 格式不对：${error.message}`;
          renderEditor();
        }
        return;
      }
      if (target.hasAttribute("data-json-reset")) { state.json = null; state.jsonError = ""; renderEditor(); }
    });

    window.addEventListener("beforeunload", (event) => {
      if (state.dirty || state.checksDirty || state.saving) { flush(); event.preventDefault(); }
    });

    // Open the first version that still needs work (else the first one).
    run("正在读取……", async () => {
      await loadList();
      const first = state.versions.find((item) => item.status === "DRAFT" || item.status === "PENDING") || state.versions.find((item) => item.status !== "RETIRED");
      if (first && !state.current) await openVersion(first.rule_id); else renderEditor();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
