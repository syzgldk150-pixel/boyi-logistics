(() => {
  "use strict";

  // 业务手册 → 计算规则 (frontend-spec 54 规则编辑, 55 试算与审核). A rule is block-built
  // (shared/fee_engine): parameters people type plus a formula from a fixed set of blocks.
  // Here numbers, sources, names and reminders are edited in place; the structure itself is
  // edited as JSON under “高级”. The Agent validates every save, submission, approval and
  // trial; this page only edits the form, shows results and forwards actions.
  const STATUS = { DRAFT: "DRAFT", PENDING: "PENDING", APPROVED: "ACTIVE", RETIRED: "RETIRED" };
  const ACTION_TEXT = { CREATE: "新建费用", COPY: "复制为草稿", SAVE: "保存草稿", CHECK: "勾选核对", SUBMIT: "提交审核", WITHDRAW: "退回草稿", APPROVE: "审核生效", RETIRE: "作废", TRIAL: "试算" };
  const TYPE = { fixed: "固定金额", rate: "单价×数值", amount: "按数值计金额", sum: "相加", max: "取大", tiers: "分档", when: "按条件",
    lookup: "按选项", clamp: "最低/最高", each_piece: "逐件计费", use: "引用公共部分", none: "无金额" };
  const NO_AMOUNT = { NEGOTIATE: "需协商", NOT_PROVIDED: "可不提供服务", MANUAL: "需人工判断", ACTUAL: "实报实销" };
  const OPS = { eq: "=", ne: "≠", gt: ">", ge: "≥", lt: "<", le: "≤", in: "属于" };
  const PARAM_TYPE = { number: "数字", choice: "单选", bool: "是/否", pieces: "逐件明细" };
  const LEAVES = new Set(["fixed", "rate", "amount", "none"]);

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
    const state = { versions: [], current: null, busy: false, dirty: false, tab: "content", expanded: new Set(), trial: null,
      trialValues: {}, json: null, jsonError: "" };

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

    // Every block on its path, for the sources check and “全部展开”.
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

    function sourceStats() {
      let total = 0;
      let ok = 0;
      eachRoot((node, _path, covered) => { if (LEAVES.has(node.type)) { total += 1; if (covered) ok += 1; } });
      return { total, ok };
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

    const person = (record, id) => (record?.people || {})[String(id || "")] || (id && /^\d+$/.test(String(id)) ? `账号 ${id}` : id || "系统");

    // -- list --------------------------------------------------------------------------

    function renderList() {
      const list = $("[data-fee-list]");
      const drafts = state.versions.filter((item) => item.status === "DRAFT").length;
      const pending = state.versions.filter((item) => item.status === "PENDING").length;
      const live = state.versions.filter((item) => item.status !== "RETIRED").length;
      $("[data-fee-summary]").textContent = `${live} 个版本${drafts ? ` · ${drafts} 个草稿` : ""}${pending ? ` · ${pending} 个待审核` : ""}`;
      UI.setTabCount("fees", live);
      if (!state.versions.length) {
        list.innerHTML = '<li class="hb-empty hb-empty--row"><i data-feather="file-text"></i><p>还没有规则版本。</p></li>';
        if (window.feather) window.feather.replace();
        return;
      }
      let html = "";
      let group = "";
      state.versions.forEach((item) => {
        if (item.fee_name !== group) { group = item.fee_name; html += `<li class="hb-rows__group"><b>${escapeHtml(group)}</b></li>`; }
        const when = item.status === "APPROVED" ? `生效于 ${formatTime(item.approved_at)}` : item.status === "PENDING" ? `提交于 ${formatTime(item.submitted_at)}`
          : item.status === "RETIRED" ? `作废于 ${formatTime(item.retired_at)}` : `保存于 ${formatTime(item.updated_at)}`;
        html += `<li><button class="hb-row" type="button" data-fee-open="${escapeHtml(item.rule_id)}" aria-current="${state.current?.rule_id === item.rule_id}">
          <span class="hb-row__top"><span class="hb-row__title">${escapeHtml(item.version_label)}</span>${badge(STATUS[item.status])}</span>
          <span class="hb-row__meta"${item.effective_from ? "" : ' data-tone="warn"'}>执行日期 ${item.effective_from ? escapeHtml(item.effective_from) : "未填"}</span>
          <span class="hb-row__time">${escapeHtml(when)}</span></button>
          ${item.status === "APPROVED" && canEdit ? `<button class="hb-link hb-row__aside" type="button" data-fee-copy="${escapeHtml(item.rule_id)}">复制</button>` : ""}</li>`;
      });
      list.innerHTML = html;
    }

    // -- detail ------------------------------------------------------------------------

    // 生效检查 (spec 54): basic info, content and sources, 待核对, trial since the last save. The
    // Agent decides with the same four (shared/handbook_fee_rules.submit_blockers); this only shows them.
    function checks(record) {
      const stats = sourceStats();
      const problems = record.content_problems || [];
      const open = (record.checks || []).filter((item) => !item.done).length;
      const trials = Number(record.trial_count || 0);
      const basic = Boolean(String(record.fee_name || "").trim() && String(record.version_label || "").trim() && record.effective_from);
      const contentOk = !problems.length && stats.total > 0 && stats.ok === stats.total;
      return [
        { label: "基本信息", ok: basic, detail: basic ? `执行日期 ${record.effective_from}` : record.effective_from ? "缺费用或版本名称" : "未填执行日期" },
        { label: "规则内容", ok: contentOk, detail: problems.length ? `${problems.length} 个问题` : `${stats.ok} / ${stats.total} 出处完整`, tab: "content" },
        { label: "待核对", ok: open === 0, detail: open ? `${open} 项未勾选` : "全部已勾选", tab: "checks" },
        { label: "试算", ok: trials > 0, detail: trials ? `保存后已试算 ${trials} 个场景` : "保存后还没有试算", tab: "trial" },
      ];
    }

    function input(path, value, { editable, mode = "decimal", wide = false, placeholder = "", blank = "keep", label = "" } = {}) {
      return `<input ${editable ? "" : "disabled"} inputmode="${mode}" data-f="${escapeHtml(path.join("."))}" data-blank="${blank}"${wide ? ' class="hb-input--wide"' : ""} value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(placeholder)}"${label ? ` aria-label="${escapeHtml(label)}"` : ""}>`;
    }

    const cell = (label, path, value, opts = {}) => `<label class="hb-cell"><span>${label}</span>${input(path, value, opts)}</label>`;

    function sourceFields(path, source, editable) {
      if (!source) {
        return editable ? `<button class="hb-link" type="button" data-source-add="${escapeHtml(path.join("."))}">+ 添加出处</button>` : "";
      }
      return `<div class="hb-fee__source">
        <label class="hb-cell hb-cell--doc"><span>出处文档</span>${input([...path, "source", "document"], source.document, { editable, mode: "text", placeholder: "例如：融辉物流网络管理手册 V6.1" })}</label>
        <label class="hb-cell"><span>页码或条款</span>${input([...path, "source", "location"], source.location, { editable, mode: "text", placeholder: "第五篇第五章 第 50 页" })}</label>
        <label class="hb-cell hb-cell--doc"><span>原文摘句（选填）</span>${input([...path, "source", "quote"], source.quote, { editable, mode: "text", blank: "delete" })}</label>
        <label class="hb-cell"><span>链接（选填）</span>${input([...path, "source", "link"], source.link, { editable, mode: "url", placeholder: "https://", blank: "delete" })}</label>
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
      const title = node.label || TYPE[node.type];
      return `<section class="hb-fold hb-fold--node" data-open="${open}">
        <button class="hb-fold__head" type="button" data-fold="${escapeHtml(key)}" aria-expanded="${open}">
          <i data-feather="${open ? "chevron-down" : "chevron-right"}"></i><b>${escapeHtml(title)}</b><span class="hb-fold__summary">${escapeHtml(summary(node))}</span>${sourceBadge}</button>
        ${open ? `<div class="hb-fold__body">
          <div class="hb-fee__cells"><label class="hb-cell hb-cell--doc"><span>名称（选填，显示在计算过程里）</span>${input([...path, "label"], node.label, { editable, mode: "text", blank: "delete", placeholder: TYPE[node.type] })}</label></div>
          ${ownFields(node, path, editable)}${sourceFields(path, node.source, editable)}
          ${children(node, path, editable, covered)}</div>` : ""}</section>`;
    }

    function renderParams(editable) {
      const list = params();
      const rows = list.map((param) => `<tr><td>${escapeHtml(param.label || param.key)}</td><td class="hb-meta">${escapeHtml(param.key)}</td><td>${PARAM_TYPE[param.type] || escapeHtml(param.type)}</td>
        <td>${escapeHtml(param.unit || "")}</td><td>${param.type === "choice" ? escapeHtml((param.options || []).map((item) => item.label).join(" / ")) : param.type === "pieces" ? escapeHtml((param.fields || []).map((item) => item.label).join("、")) : escapeHtml([param.min != null ? `≥${param.min}` : "", param.max != null ? `≤${param.max}` : ""].filter(Boolean).join(" "))}</td>
        <td>${param.required === false ? "选填" : param.default != null ? `默认 ${escapeHtml(String(param.default))}` : "必填"}</td></tr>`).join("");
      return `<section class="hb-card hb-card--inset"><div class="hb-card__head"><h3>要填的参数</h3><span class="hb-meta">${list.length} 个</span></div>
        ${list.length ? `<div class="hb-table-wrap"><table class="hb-table"><thead><tr><th>名称</th><th>标识</th><th>类型</th><th>单位</th><th>范围 / 选项</th><th>必填</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="hb-meta">还没有参数。</p>'}
        <p class="hb-meta">${editable ? "参数的增删改在下方“高级：结构 JSON”中进行。" : ""}</p></section>`;
    }

    function renderContentTab(record, editable) {
      const data = content();
      const problems = record.content_problems || [];
      const definitions = Object.entries(data.definitions || {});
      const jsonText = state.json ?? JSON.stringify(data, null, 2);
      return `${problems.length && !state.dirty ? `<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span>已保存的规则有 ${problems.length} 个问题，修正后才能试算和提交：<br>${problems.map(escapeHtml).join("<br>")}</span></div>` : ""}
        ${renderParams(editable)}
        <section class="hb-card hb-card--inset"><div class="hb-card__head"><h3>计算结构</h3></div>${nodeHtml(data.formula, ["formula"], editable)}</section>
        ${definitions.length ? `<section class="hb-card hb-card--inset"><div class="hb-card__head"><h3>公共部分</h3><span class="hb-meta">被“引用公共部分”使用</span></div>
          ${definitions.map(([name, node]) => `<p class="hb-branch__title">${escapeHtml(name)}</p>${nodeHtml(node, ["definitions", name], editable)}`).join("")}</section>` : ""}
        <section class="hb-card hb-card--inset"><div class="hb-card__head"><h3>互斥与提醒</h3></div>
          <div class="hb-fee__cells">${cell("互斥组（同组费用不同时收取，取大）", ["group"], data.group, { editable, mode: "text", blank: "delete", placeholder: "例如：上楼卸货" })}</div>
          <label class="hb-cell hb-cell--full"><span>提醒：一行一条，随计算结果一起返回（最多 10 条）</span><textarea ${editable ? "" : "disabled"} rows="3" data-reminders>${escapeHtml((data.reminders || []).join("\n"))}</textarea></label></section>
        <details class="hb-card hb-card--inset hb-advanced" ${state.json != null ? "open" : ""}><summary>高级：结构 JSON</summary>
          <p class="hb-meta">增删参数或积木、改条件在这里进行。格式见 shared/fee_engine.py；“应用到草稿”后再保存，保存时会完整校验并一次列出全部问题。</p>
          <textarea class="hb-json" rows="18" spellcheck="false" ${editable ? "" : "readonly"} data-json>${escapeHtml(jsonText)}</textarea>
          ${state.jsonError ? `<p class="hb-meta" data-tone="danger">${escapeHtml(state.jsonError)}</p>` : ""}
          ${editable ? '<div class="hb-actions"><button class="ghost-btn hb-btn-sm" type="button" data-json-apply>应用到草稿</button><button class="hb-link" type="button" data-json-reset>恢复为当前内容</button></div>' : ""}</details>`;
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
      if (!canEdit) return '<div class="hb-empty"><i data-feather="lock"></i><p>试算需要“业务手册”的可操作权限。</p></div>';
      if ((record.content_problems || []).length) return `<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span>已保存的规则还有问题，修正并保存后才能试算：<br>${record.content_problems.map(escapeHtml).join("<br>")}</span></div>`;
      return `<section class="hb-card hb-card--inset"><h3>试算</h3><p class="hb-meta">按已保存的 ${escapeHtml(record.version_label)} 内容计算${record.status === "APPROVED" ? "" : "；未生效版本只供核对"}${state.dirty ? "；有未保存的修改，试算的是保存前的内容" : ""}。</p>
        <div data-fee-trial-form>${Form.render(record.params || [], state.trialValues)}</div>
        <div class="hb-actions"><button class="primary-btn" type="button" data-fee-trial>试算</button><button class="ghost-btn" type="button" data-fee-trial="boundary">分界试算表</button><span class="hb-meta">↵ 回车即可试算</span></div></section>
        <div data-fee-trial-result>${state.trial ? trialHtml(state.trial, record) : ""}</div>`;
    }

    function trialHtml(data, record) {
      const result = data.result;
      let html = `<section class="hb-card hb-card--inset hb-trial">
        <div class="hb-card__head"><h3>试算结果</h3>${result.reportable ? '<span class="hb-badge" data-tone="active">已生效规则</span>' : '<span class="hb-badge" data-tone="pending">未生效 · 只供核对</span>'}</div>
        <div class="hb-trial__amount"><b>${Form.outcomeText(result)}</b><span class="hb-meta">按 ${escapeHtml(result.rule_version || record.version_label)} 规则计算</span></div>
        ${result.missing?.length ? `<p class="hb-meta" data-tone="warn">还缺少：${result.missing.map(escapeHtml).join("、")}</p>` : ""}
        ${Form.resultBody({ ...result, reason: result.missing?.length ? "" : result.reason })}</section>`;
      if (data.boundary) {
        html += `<section class="hb-card hb-card--inset"><div class="hb-card__head"><h3>分界试算表</h3><span class="hb-meta">其余参数按本次填写，一次只改一个值</span></div>
          ${data.boundary.length ? `<div class="hb-table-wrap"><table class="hb-table hb-table--boundary"><thead><tr><th>参数</th><th>取值</th><th>金额</th><th>结果</th></tr></thead><tbody>
          ${data.boundary.map((row) => `<tr><td>${escapeHtml(row.param)}</td><td>${escapeHtml(row.value)}</td><td>${row.outcome === "AMOUNT" ? `¥ ${escapeHtml(row.amount)}` : "—"}</td>
            <td data-tone="${row.outcome === "AMOUNT" ? "ok" : row.outcome === "INVALID" ? "danger" : "warn"}" title="${escapeHtml(row.note || "")}">${escapeHtml(Form.OUTCOME[row.outcome] || row.outcome)}${row.note ? `<br><small class="hb-meta">${escapeHtml(row.note)}</small>` : ""}</td></tr>`).join("")}</tbody></table></div>`
          : '<p class="hb-meta">这条规则没有分档或数字条件。</p>'}</section>`;
      }
      return html;
    }

    function renderEventsTab(record) {
      const events = record.events || [];
      return events.length ? `<ul class="hb-events">${events.map((item) => `<li><span class="hb-meta">${escapeHtml(formatTime(item.created_at))}</span><b>${escapeHtml(eventText(item))}</b><span class="hb-meta">${escapeHtml(person(record, item.actor_id))}</span></li>`).join("")}</ul>`
        : '<p class="hb-meta">还没有操作记录。</p>';
    }

    function eventText(item) {
      const detail = item.detail || {};
      if (item.action === "TRIAL") {
        const what = detail.boundary ? "分界试算表" : "试算";
        const shown = Object.entries(detail.values || {}).slice(0, 4).map(([key, value]) => `${paramLabel(key)} ${value === true ? "是" : value === false ? "否" : value}`).join(" · ");
        return `${what}${shown ? ` ${shown}` : ""}${detail.amount ? ` → ¥ ${detail.amount}` : detail.outcome ? ` → ${Form.OUTCOME[detail.outcome] || detail.outcome}` : ""}`;
      }
      if (item.action === "CHECK") return `勾选核对（已核对 ${detail.done ?? 0} 项）`;
      if (item.action === "SAVE" && detail.fee_name) return `保存草稿（费用改名为“${detail.fee_name}”）`;
      if (item.action === "RETIRE" && detail.reason) return `作废（${detail.reason}）`;
      return ACTION_TEXT[item.action] || item.action;
    }

    function renderSide(record) {
      const list = checks(record);
      const missing = list.filter((item) => !item.ok);
      const passed = list.length - missing.length;
      const items = list.map((item) => `<li data-tone="${item.ok ? "ok" : "warn"}">${item.tab ? `<button class="hb-link" type="button" data-fee-tab="${item.tab}">${item.label}</button>` : `<b>${item.label}</b>`}<span>${escapeHtml(item.detail)}</span></li>`).join("");
      const progress = `<div class="hb-progress"><span class="hb-meta">${passed} / ${list.length} 项通过</span><span class="hb-progress__bar"><span style="width:${Math.round((passed / list.length) * 100)}%"></span></span></div>`;
      const recent = (record.events || []).slice(0, 3).map((item) => `<li><span class="hb-meta">${escapeHtml(formatTime(item.created_at))}</span><span>${escapeHtml(eventText(item))}</span><span class="hb-meta">${escapeHtml(person(record, item.actor_id))}</span></li>`).join("");
      if (record.status === "DRAFT") {
        const need = record.submit_blockers || [];
        return `<section class="hb-card hb-side"><div class="hb-card__head"><h3>生效检查</h3><span class="hb-meta" data-tone="${missing.length ? "warn" : "ok"}">${missing.length ? `还差 ${missing.length} 项` : "已满足"}</span></div>
          ${progress}
          <ul class="hb-checklist hb-checklist--stack">${items}</ul>
          ${need.length ? `<div class="hb-block"><h3>提交前还需</h3><ul class="hb-need">${need.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>` : ""}
          <p class="hb-alert" data-tone="info"><i data-feather="lock"></i><span>草稿状态仅限内部核对 · 不会用于报价，也不影响线上单据。</span></p></section>`;
      }
      if (record.status === "PENDING") {
        const date = record.effective_from || "（未填执行日期）";
        const blockers = record.blockers || [];
        return `<section class="hb-card hb-side"><div class="hb-card__head"><h3>审核</h3>${badge("PENDING")}</div>
          <p class="hb-meta" data-tone="${blockers.length ? "warn" : "ok"}">${blockers.length ? escapeHtml(blockers.join("；")) : "已满足生效条件"}</p>
          ${progress}
          <ul class="hb-checklist hb-checklist--stack">${items}</ul>
          <div class="hb-impact"><b>通过并生效后</b><span>开单日期在 ${escapeHtml(date)}（含）之后的单据按这版规则计算；之前的单据不受影响。</span>
            <span>生效时点：${escapeHtml(record.effective_from ? `${record.effective_from} 00:00` : "未填")}，以总部通知为准。</span></div>
          ${recent ? `<div class="hb-block"><h3>操作记录</h3><ul class="hb-events hb-events--compact">${recent}</ul></div>` : ""}
          ${canEdit ? `<div class="hb-actions hb-actions--split"><button class="ghost-btn" type="button" data-fee-action="withdraw">退回草稿</button>
            ${superAdmin ? `<button class="primary-btn" type="button" data-fee-action="approve" ${blockers.length ? "disabled" : ""}><i data-feather="check"></i><span>通过并生效</span></button>` : ""}</div>` : ""}
          ${superAdmin ? '<div class="hb-danger-row"><button class="hb-btn-danger-text" type="button" data-fee-action="retire"><i data-feather="trash-2"></i>作废此版本</button><span class="hb-meta">不可恢复，需二次确认</span></div>' : '<p class="hb-meta">等待超级管理员审核。</p>'}</section>`;
      }
      if (record.status === "RETIRED") {
        return `<section class="hb-card hb-side"><div class="hb-card__head"><h3>已作废</h3>${badge("RETIRED")}</div>
          <p class="hb-meta">作废于 ${escapeHtml(formatTime(record.retired_at))}${record.retired_by ? ` · ${escapeHtml(person(record, record.retired_by))}` : ""}；机器人不再使用这个版本。</p>
          ${canEdit ? '<button class="ghost-btn" type="button" data-fee-action="copy"><i data-feather="copy"></i><span>复制为新草稿</span></button>' : ""}</section>`;
      }
      return `<section class="hb-card hb-side"><div class="hb-card__head"><h3>已生效</h3>${badge("ACTIVE")}</div>
        <div class="hb-impact"><b>适用范围</b><span>开单日期在 ${escapeHtml(record.effective_from || "")}（含）之后、下一版执行日期之前的单据按这版计算。</span></div>
        <p class="hb-meta">已生效的版本不能修改；要改单价或档位，请复制为新草稿。</p>
        ${canEdit ? '<button class="primary-btn" type="button" data-fee-action="copy"><i data-feather="copy"></i><span>复制为新草稿</span></button>' : ""}
        ${superAdmin ? '<div class="hb-danger-row"><button class="hb-btn-danger-text" type="button" data-fee-action="retire"><i data-feather="trash-2"></i>作废此版本</button><span class="hb-meta">不可恢复</span></div>' : ""}</section>`;
    }

    function renderBar(record) {
      if (record.status === "DRAFT") {
        const saved = state.dirty ? '<i class="hb-dot" data-tone="warn"></i>有未保存的修改'
          : `<i class="hb-dot" data-tone="ok"></i>已保存 ${escapeHtml(formatTime(record.updated_at))}${record.updated_by ? ` · ${escapeHtml(person(record, record.updated_by))}` : ""}`;
        if (!canEdit) return `<div class="hb-card__bar"><span class="hb-meta">${saved}</span></div>`;
        const need = record.submit_blockers || [];
        const reason = state.dirty ? "请先保存草稿" : need.length ? `还差 ${need.length} 项，补齐后可提交` : "";
        return `<div class="hb-card__bar"><span class="hb-meta" data-fee-saved>${saved}</span><div class="hb-actions">
          ${superAdmin ? '<button class="hb-btn-danger-text" type="button" data-fee-action="retire">作废</button>' : ""}
          <button class="ghost-btn" type="button" data-fee-action="save">保存草稿</button>
          <span class="hb-submit-wrap"><button class="primary-btn" type="button" data-fee-action="submit" ${reason ? "disabled" : ""}>提交审核</button>${reason ? `<span class="hb-meta" data-tone="warn" data-fee-submit-reason>${escapeHtml(reason)}</span>` : ""}</span></div></div>`;
      }
      if (record.status === "RETIRED") return `<div class="hb-card__bar"><span class="hb-meta"><i data-feather="lock"></i>已作废版本只读</span></div>`;
      if (record.status === "PENDING") return `<div class="hb-card__bar"><span class="hb-meta">已提交 ${escapeHtml(formatTime(record.submitted_at))}${record.submitted_by ? ` · ${escapeHtml(person(record, record.submitted_by))}` : ""}<br><i data-feather="lock"></i>审核中，规则内容不可修改；退回草稿后可继续编辑</span></div>`;
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
        : record.status === "APPROVED" ? `执行日期 ${record.effective_from} · 生效于 ${formatTime(record.approved_at)}`
        : record.status === "RETIRED" ? `已作废 · ${formatTime(record.retired_at)}` : "计算方式由积木组成：数字、出处可直接改，结构在“高级”中调整";
      const tabs = [["content", "规则内容"], ["checks", `待核对${openChecks ? ` <span class="hb-tab__count" data-tone="warn">${openChecks}</span>` : ""}`], ["trial", "试算"], ["events", "操作记录"]];
      const bodies = { content: () => renderContentTab(record, editable), checks: () => renderChecksTab(record), trial: () => renderTrialTab(record), events: () => renderEventsTab(record) };
      node.innerHTML = `
        <div class="hb-card__head"><div><h2 id="fee-editor-title">${escapeHtml(record.fee_name)} <span class="hb-chip hb-chip--sm">${escapeHtml(record.version_label)}</span> ${badge(STATUS[record.status])}</h2>
          <p class="hb-card__sub">${escapeHtml(sub)}</p></div></div>
        <div class="hb-fee__basic">
          <label class="hb-cell"><span>费用名称</span><input ${editable ? "" : "disabled"} maxlength="40" data-meta="fee_name" value="${escapeHtml(record.fee_name)}"></label>
          <label class="hb-cell"><span>版本名称</span><input ${editable ? "" : "disabled"} maxlength="60" data-meta="version_label" value="${escapeHtml(record.version_label)}"></label>
          <label class="hb-cell"><span>执行日期（以总部通知为准）</span><input ${editable ? "" : "disabled"} type="date" data-meta="effective_from" value="${escapeHtml(record.effective_from || "")}"></label>
          ${editable ? (record.effective_from ? "" : '<span class="hb-meta" data-tone="warn"><i data-feather="alert-triangle"></i>未填执行日期，暂不能生效</span>')
            : `<span class="hb-meta"><i data-feather="lock"></i>${record.status === "PENDING" ? "审核中，字段锁定" : record.status === "RETIRED" ? "已作废，只读" : "已生效，只读"}</span>`}
        </div>
        <div class="hb-subtabs" role="tablist" aria-label="规则详情">
          ${tabs.map(([key, label]) => `<button type="button" role="tab" aria-selected="${state.tab === key}" data-fee-tab="${key}">${label}</button>`).join("")}
          ${state.tab === "content" ? `<button class="hb-link hb-subtabs__aside" type="button" data-fold-all>${state.expanded.size > 1 ? "全部收起" : "全部展开"}</button>` : ""}
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
      if (state.current?.rule_id !== ruleId) { state.trial = null; state.tab = record.status === "PENDING" ? "trial" : "content"; state.expanded = new Set(["formula"]); state.json = null; state.jsonError = ""; state.trialValues = {}; }
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
      return { fee_name: String(record.fee_name || "").trim(), version_label: String(record.version_label || "").trim(),
        effective_from: record.effective_from || null, content: record.content,
        checks: (record.checks || []).map((item) => ({ text: String(item.text || "").trim(), done: Boolean(item.done) })) };
    }

    function rememberTrialValues() {
      const form = $("[data-fee-trial-form]");
      if (form && state.current) state.trialValues = Form.read(form, state.current.params || []);
      return state.trialValues;
    }

    function runTrial(boundary) {
      const record = state.current;
      const values = rememberTrialValues();
      run("正在试算……", async () => {
        const data = await request(`${base}/${encodeURIComponent(record.rule_id)}/trial`, { values, boundary });
        state.trial = data;
        // The Agent logged this trial; reload so 生效检查 and 操作记录 show it (unsaved edits are not trialled).
        if (!state.dirty) await openVersion(record.rule_id); else renderEditor();
      });
    }

    async function createFee() {
      const name = window.prompt("新费用的名称（例如：进仓服务费）");
      if (name == null) return;
      if (!name.trim()) { feedback.error("没有新建", "费用名称不能为空。"); return; }
      run("正在新建……", async () => {
        const created = await request(base, { fee_name: name.trim() });
        await loadList();
        await openVersion(created.rule_id);
        state.tab = "content";
        state.json = JSON.stringify(state.current.content, null, 2);
        renderEditor();
      }, "已新建草稿：先在“高级：结构 JSON”里写参数和积木，保存后再核对、试算");
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
      const reason = $("[data-fee-submit-reason]");
      if (reason) reason.textContent = "请先保存草稿";
    }

    function allPaths() {
      const keys = new Set();
      eachRoot((_node, path) => keys.add(path.join(".")));
      return keys;
    }

    root.addEventListener("input", (event) => {
      const target = event.target;
      const record = state.current;
      if (!record || target.closest("[data-fee-trial-form]") || target.matches("[data-json]")) return;
      if (target.dataset.f) {
        const raw = target.value.trim();
        const blank = target.dataset.blank;
        const value = raw === "" ? (blank === "delete" ? undefined : blank === "null" ? null : "") : raw;
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
      state.json = null;
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
      if (event.key === "Enter" && event.target.closest("[data-fee-trial-form]") && event.target.matches("input")) { event.preventDefault(); runTrial(false); }
    });

    root.addEventListener("click", async (event) => {
      const target = event.target.closest("button");
      if (!target || target.disabled || state.busy) return;
      const record = state.current;
      const url = (action, id = record?.rule_id) => `${base}/${encodeURIComponent(id)}/${action}`;
      if (record && target.closest("[data-fee-trial-form]") && Form.handleClick(target, record.params || [], $("[data-fee-trial-form]"))) return;
      if (target.hasAttribute("data-fee-create")) {
        if (!(await leaveOk())) return;
        createFee();
      } else if (target.dataset.feeOpen) {
        if (target.dataset.feeOpen === record?.rule_id || !(await leaveOk())) return;
        run("正在读取……", () => openVersion(target.dataset.feeOpen));
      } else if (target.dataset.feeCopy) {
        if (!(await leaveOk())) return;
        run("正在复制……", async () => { const created = await request(url("copy", target.dataset.feeCopy), {}); await loadList(); await openVersion(created.rule_id); }, "已复制为新草稿");
      } else if (target.dataset.feeTab && record) {
        if (state.tab === "trial") rememberTrialValues();
        state.tab = target.dataset.feeTab; renderEditor();
      } else if (target.dataset.fold && record) {
        const key = target.dataset.fold;
        if (state.expanded.has(key)) state.expanded.delete(key); else state.expanded.add(key);
        renderEditor();
      } else if (target.hasAttribute("data-fold-all") && record) {
        state.expanded = state.expanded.size > 1 ? new Set() : allPaths();
        renderEditor();
      } else if (target.dataset.sourceAdd && record) {
        const path = target.dataset.sourceAdd.split(".");
        const last = (() => { let found = null; eachRoot((node) => { if (filled(node.source)) found = node.source; }); return found; })();
        set(record.content, [...path, "source"], { document: last?.document || "", location: "" });
        state.json = null; markDirty(); renderEditor();
      } else if (target.dataset.sourceRemove && record) {
        set(record.content, [...target.dataset.sourceRemove.split("."), "source"], undefined);
        state.json = null; markDirty(); renderEditor();
      } else if (target.hasAttribute("data-json-apply") && record) {
        const text = $("[data-json]").value;
        try {
          const parsed = JSON.parse(text);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("最外层必须是一个对象 {…}");
          record.content = parsed;
          state.json = null; state.jsonError = "";
          markDirty(); renderEditor();
          feedback.success("已应用到草稿，记得保存");
        } catch (error) {
          state.json = text; state.jsonError = `JSON 格式不对：${error.message}`;
          renderEditor();
        }
      } else if (target.hasAttribute("data-json-reset") && record) {
        state.json = null; state.jsonError = ""; renderEditor();
      } else if (target.hasAttribute("data-boundary-toggle")) {
        renderEditor();
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
        if (action === "retire" && !(await confirmAction({ title: "作废这个版本？", body: "作废后不可恢复，机器人和报价都不会再使用该版本。", confirm: "确认作废", danger: true }))) return;
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
