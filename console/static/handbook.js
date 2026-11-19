(() => {
  "use strict";

  // 业务手册 → 说明条目 (frontend-spec 52 写作与编译, 53 审核生效). The Agent
  // validates every compile, trial, answer, submission and approval and returns
  // the blockers; this page only shows them and forwards actions.
  const ACTION_TEXT = { none: "直接回答", ask_user: "反问", error: "调用失败", unknown: "未知动作" };
  const TIPS = ["一条说明只讲一件事，便于单独审核和回滚", "写出大家怎么说：术语、别名、口语", "写出什么时候必须先问，避免机器人替人决定"];
  const EVENT_TEXT = {
    CREATE: "新建说明", SAVE: "保存草稿", COMPILE: "AI 编译完成", TRIAL: "试跑", ACKNOWLEDGE: "确认歧义", SUBMIT: "提交审核",
    WITHDRAW: "撤回审核", REJECT: "退回修改", APPROVE: "审核生效", PAUSE: "暂停", RESUME: "恢复生效", RETIRE: "作废说明", DISCARD: "作废版本",
  };

  function init() {
    const root = document.querySelector("[data-handbook-entries-panel]");
    const page = document.querySelector("[data-handbook]");
    if (!root || !page || !window.HandbookUI || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const UI = window.HandbookUI;
    const { escapeHtml, badge, formatTime, request, confirmAction } = UI;
    const { canEdit, superAdmin } = UI.role(page);
    const base = "/handbook/entries";
    const $ = (selector) => root.querySelector(selector);
    const feedback = UI.feedback($("[data-handbook-status]"));
    const state = {
      entries: [], current: null, busy: false, dirty: false, query: "", filter: "all", showRetired: false,
      view: "work", allDescribed: false, trialTable: false, answering: false, compare: false, history: false,
    };

    // -- derived facts -----------------------------------------------------------------

    const latestOf = (entry) => (entry ? (entry.versions || []).find((item) => item.version === entry.latest_version) || null : null);
    const person = (entry, id) => (entry?.people || {})[String(id || "")] || (id && /^\d+$/.test(String(id)) ? `账号 ${id}` : id || "系统");

    // One status per entry for the list (spec 52: 草稿 / 待审核 / 已生效 / 已暂停 / 已作废).
    function entryStatus(entry, latestStatus = entry?.latest_status ?? latestOf(entry)?.status) {
      if (!entry) return "DRAFT";
      if (entry.status === "RETIRED") return "RETIRED";
      if (latestStatus === "PENDING") return "PENDING";
      if (entry.status === "PAUSED") return "PAUSED";
      return entry.status === "ACTIVE" ? "ACTIVE" : "DRAFT";
    }

    function versionLabel(entry, item) {
      if (item.status === "ACTIVE") return entry.status === "PAUSED" ? ["已暂停", "paused"] : ["已生效", "active"];
      return {
        DRAFT: ["草稿", "draft"], COMPILED: ["已编译", "draft"], PENDING: ["待审核", "pending"],
        SUPERSEDED: ["已替换", "retired"], VOID: ["已作废", "retired"],
      }[item.status] || [item.status, "draft"];
    }

    function versionBadge(entry, item) {
      const [text, tone] = versionLabel(entry, item);
      return `<span class="hb-badge" data-tone="${tone}">${escapeHtml(text)}</span>`;
    }

    function ambiguities(latest) {
      const items = latest?.ambiguity_items || (latest?.compiled?.ambiguities || []).map((item) => (typeof item === "string" ? { question: item, options: [] } : item));
      const answers = new Map();
      (latest?.acknowledged || []).forEach((item) => {
        if (typeof item === "number") answers.set(item, { index: item, answer: "", by: "", at: "" });
        else if (item && typeof item.index === "number") answers.set(item.index, item);
      });
      return { items, answers, open: items.filter((_item, index) => !answers.has(index)).length };
    }

    function lastCompile(entry, latest) {
      return (entry?.events || []).find((item) => item.action === "COMPILE" && item.version === latest?.version) || null;
    }

    function submitBlockers(latest) {
      if (!latest) return ["先保存说明"];
      if (latest.status === "DRAFT") return [latest.validation?.ok === false ? "校验未通过，请修改说明后重新编译" : "交给 AI 编译"];
      return latest.submit_blockers || [];
    }

    // 编写 → 编译 → 校验 → 试跑 → 审核; each step is done / current / error / todo.
    function steps(entry, latest) {
      const compiled = latest && latest.status !== "DRAFT" && latest.compiled;
      const validation = latest?.validation || {};
      const amb = ambiguities(latest);
      const live = entry && entry.status === "ACTIVE" && entry.active_version === latest?.version;
      const needsTrial = Boolean(compiled?.examples?.length);
      const compile = !latest ? "todo" : latest.status !== "DRAFT" || validation.ok === false ? "done" : "current";
      let check = "todo";
      if (validation.ok === false) check = "error";
      else if (compiled) check = amb.open ? "current" : "done";
      let trial = "todo";
      if (compiled && check === "done") trial = latest.trial?.rows?.length || !needsTrial ? "done" : "current";
      let review = "todo";
      if (live) review = "done";
      else if (latest?.status === "PENDING") review = "current";
      return [["编写", latest ? "done" : "current"], ["编译", compile], ["校验", check], ["试跑", trial], ["审核", review]];
    }

    function stepper(entry, latest) {
      return `<ol class="hb-steps">${steps(entry, latest).map(([label, tone], index) => `<li data-tone="${tone}">
        <span class="hb-steps__dot">${tone === "done" ? "✓" : tone === "error" || (tone === "current" && index === 2) ? "!" : index + 1}</span><span>${label}</span></li>`).join("")}</ol>`;
    }

    // -- list --------------------------------------------------------------------------

    function renderList() {
      const all = state.entries;
      const live = all.filter((entry) => entry.status !== "RETIRED");
      const retired = all.length - live.length;
      const pending = live.filter((entry) => ["PENDING", "PAUSED"].includes(entryStatus(entry))).length;
      const drafts = live.filter((entry) => entryStatus(entry) === "DRAFT").length;
      $("[data-entries-summary]").textContent = `${live.length} 条说明${pending ? ` · ${pending} 条待处理` : ""}`;
      UI.setTabCount("entries", live.length);
      const filters = $("[data-entries-filters]");
      filters.innerHTML = [["all", "全部", live.length], ["pending", "待处理", pending], ["draft", "草稿", drafts]]
        .map(([key, label, count]) => `<button type="button" class="hb-filter" data-entries-filter="${key}" aria-pressed="${state.filter === key && !state.showRetired}">${label} <span>${count}</span></button>`).join("");
      const query = state.query.trim().toLowerCase();
      const source = state.showRetired ? all.filter((entry) => entry.status === "RETIRED") : live.filter((entry) => {
        const status = entryStatus(entry);
        if (state.filter === "pending") return status === "PENDING" || status === "PAUSED";
        if (state.filter === "draft") return status === "DRAFT";
        return true;
      });
      const visible = source.filter((entry) => !query || entry.title.toLowerCase().includes(query) || `v${entry.latest_version}`.includes(query));
      const list = $("[data-handbook-entries]");
      if (!all.length) {
        list.innerHTML = `<li class="hb-empty hb-empty--row"><i data-feather="file-text"></i><p>还没有业务说明。</p>
          ${canEdit ? '<button class="hb-link" type="button" data-handbook-new-empty>新建第一条说明</button>' : ""}</li>`;
      } else if (!visible.length) {
        list.innerHTML = `<li class="hb-empty hb-empty--row"><p>${query ? "没有标题或版本包含这个词的说明。" : "这一栏暂时没有说明。"}</p></li>`;
      } else {
        list.innerHTML = visible.map((entry) => {
          const status = entryStatus(entry);
          const versionText = { DRAFT: "草稿", PENDING: "待审核", ACTIVE: "已生效", PAUSED: "已暂停", RETIRED: "已作废" }[status];
          return `<li><button class="hb-row hb-row--entry" type="button" data-entry="${escapeHtml(entry.entry_id)}" aria-current="${state.current?.entry_id === entry.entry_id}" data-status="${status}">
            <span class="hb-row__top"><span class="hb-row__title">${escapeHtml(entry.title)}</span>${badge(status)}</span>
            <span class="hb-row__meta">V${entry.latest_version} ${versionText}${entry.active_version && entry.active_version !== entry.latest_version ? ` · 当前生效 V${entry.active_version}` : ""}</span>
            <span class="hb-row__summary">${escapeHtml(entry.summary || "")}</span>
            <span class="hb-row__time">${escapeHtml(formatTime(entry.latest_updated_at || entry.updated_at))}${entry.latest_edited_name ? ` · ${escapeHtml(entry.latest_edited_name)}` : ""}</span></button></li>`;
        }).join("");
      }
      $("[data-entries-foot]").innerHTML = `<span>共 ${live.length} 条 · 已作废 ${retired}</span>
        ${retired || state.showRetired ? `<button class="hb-link" type="button" data-entries-retired>${state.showRetired ? "返回说明列表" : "查看已作废"}</button>` : ""}`;
      if (window.feather) window.feather.replace();
    }

    // -- editor ------------------------------------------------------------------------

    function renderEditor() {
      const entry = state.current;
      const latest = latestOf(entry);
      const pendingReview = latest?.status === "PENDING";
      const retired = entry?.status === "RETIRED";
      const editable = canEdit && !pendingReview && !retired;
      const readonly = editable ? "" : " readonly";
      let notice = "";
      if (retired) {
        notice = '<div class="hb-alert"><i data-feather="archive"></i><span><b>这条说明已作废</b><br>只能查看，机器人不再使用。</span></div>';
      } else if (pendingReview) {
        notice = `<div class="hb-alert" data-tone="warn"><i data-feather="lock"></i><span><b>V${latest.version} 正在审核</b><br>审核期间不能修改；需要修改请先撤回审核。</span></div>`;
      } else if (entry?.status === "ACTIVE" && canEdit) {
        notice = `<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span><b>这条说明正在生效中</b><br>保存后会立即暂停 V${entry.active_version}，直到新版本审核通过。</span></div>`;
      } else if (entry?.status === "PAUSED") {
        notice = '<div class="hb-alert" data-tone="danger"><i data-feather="pause-circle"></i><span><b>这条说明已暂停</b><br>机器人暂不使用它；新版本审核通过后恢复。</span></div>';
      }
      const body = latest ? latest.body : "";
      const title = entry ? entry.title : "";
      const status = latest ? versionLabel(entry, latest)[0] : "";
      $("[data-handbook-editor]").innerHTML = `
        <div class="hb-card__head">
          <div><h2 id="handbook-editor-title">${entry ? "编辑说明" : "新建说明"}${entry ? ` <span class="hb-chip hb-chip--sm">V${entry.latest_version} · ${escapeHtml(status)}</span>` : ""}</h2>
            <p class="hb-card__sub">${entry ? (entry.active_version && entry.active_version !== entry.latest_version ? `基于 V${entry.active_version} 修改` : entry.active_version ? `V${entry.active_version} 是当前线上版本` : "尚未生效的说明") + (entry.status === "ACTIVE" ? " · 保存后会暂停线上版本" : "") : "用平常的话写，保存后交给 AI 编译"}</p></div>
          ${entry ? `<button class="hb-link" type="button" data-history-toggle aria-expanded="${state.history}"><i data-feather="clock"></i>版本历史</button>` : ""}
        </div>
        ${notice}
        <label class="hb-field"><span class="hb-field__label">标题 <span class="hb-counter" data-count-for="title">${title.length} / 100</span></span>
          <input type="text" maxlength="100" data-handbook-title value="${escapeHtml(title)}" placeholder="例如：网点简称与报价说法"${readonly}></label>
        <label class="hb-field hb-field--grow"><span class="hb-field__label">说明 <span class="hb-counter" data-count-for="body">${body.length} / 4000</span></span>
          <textarea maxlength="4000" data-handbook-body placeholder="例如：大家说“岳麓店”就是长沙岳麓区网点。“报个价”“问个价”都是要三平台报价。"${readonly}>${escapeHtml(body)}</textarea>
          <span class="hb-field__hint">支持换行分段；编译时会按段落理解。</span></label>
        ${state.history && entry ? historyBlock(entry) : ""}
        ${editable ? `<div class="hb-tips"><h3>写作提示</h3><ul>${TIPS.map((tip) => `<li><i data-feather="check"></i>${escapeHtml(tip)}</li>`).join("")}</ul></div>` : ""}
        <div class="hb-card__bar">
          <span class="hb-meta" data-saved>${entry ? `<i class="hb-dot" data-tone="ok"></i>已保存 ${escapeHtml(formatTime(latest?.updated_at || entry.updated_at))}${latest?.edited_by ? ` · ${escapeHtml(person(entry, latest.edited_by))}` : ""}` : "尚未保存"}</span>
          ${editable ? `<div class="hb-actions"><button class="ghost-btn" type="button" data-handbook-save>保存说明</button>
            ${entry ? '<button class="primary-btn" type="button" data-handbook-compile><i data-feather="zap"></i><span>交给 AI 编译</span></button>' : ""}</div>` : ""}
          ${canEdit && pendingReview ? '<div class="hb-actions"><button class="ghost-btn" type="button" data-handbook-withdraw>撤回审核</button></div>' : ""}
        </div>
        ${editable ? '<p class="hb-card__foot">AI 编译通常 20–40 秒，可以离开此页；编译结果不会自动发布。</p>' : ""}`;
      state.dirty = false;
    }

    function historyBlock(entry) {
      return `<div class="hb-history"><h3>版本历史</h3><ul>${(entry.versions || []).map((item) => `<li>
        <b>V${item.version}</b><span>${versionBadge(entry, item)}${entry.active_version === item.version ? ' <span class="hb-meta">当前线上</span>' : ""}</span>
        <span class="hb-meta">${escapeHtml(formatTime(item.updated_at))}</span></li>`).join("")}</ul></div>`;
    }

    // -- result ------------------------------------------------------------------------

    function answerPanel(latest) {
      const amb = ambiguities(latest);
      return `<div class="hb-answers" data-answers>${amb.items.map((item, index) => {
        const answered = amb.answers.get(index);
        const current = answered?.answer || "";
        const custom = current && !item.options.includes(current) ? current : "";
        return `<fieldset class="hb-amb-answer" data-answer-index="${index}"><legend>${index + 1}. ${escapeHtml(item.question)}</legend>
          ${item.options.length ? `<div class="hb-amb-answer__options">${item.options.map((option) => `<label><input type="radio" name="hb-answer-${index}" value="${escapeHtml(option)}" ${current === option ? "checked" : ""}><span>${escapeHtml(option)}</span></label>`).join("")}</div>` : ""}
          <input type="text" maxlength="100" data-answer-text placeholder="${item.options.length ? "以上都不对时，写出你的答案" : "写出你的答案（可选）"}" value="${escapeHtml(custom)}">
        </fieldset>`;
      }).join("")}
        <div class="hb-actions"><button class="primary-btn hb-btn-sm" type="button" data-handbook-answer-save>保存确认</button>
          <button class="hb-link" type="button" data-handbook-answer-cancel>取消</button></div></div>`;
    }

    function renderResult() {
      const node = $("[data-handbook-result]");
      const entry = state.current;
      const latest = latestOf(entry);
      const compileEvent = lastCompile(entry, latest);
      const head = `<div class="hb-card__head"><h2 id="handbook-result-title">AI 编译结果</h2>
        ${latest?.compiled && latest.status !== "DRAFT" ? `<span class="hb-meta" data-tone="ok">✓ ${escapeHtml(formatTime(latest.updated_at))}${compileEvent?.detail?.seconds ? ` · ${compileEvent.detail.seconds} 秒` : ""}</span>` : ""}</div>`;
      if (!entry) {
        node.innerHTML = `${head}<div class="hb-empty"><i data-feather="zap"></i><p>${canEdit ? "保存说明后，交给 AI 编译，这里会显示 AI 理解了什么、哪些会生效、还差哪一步。" : "选择一条说明查看编译结果。"}</p></div>`;
        if (window.feather) window.feather.replace();
        return;
      }
      const compiled = latest && latest.status !== "DRAFT" ? latest.compiled : null;
      const validation = latest?.validation || {};
      const amb = ambiguities(latest);
      const editable = canEdit && latest?.status === "COMPILED";
      const parts = [head, stepper(entry, latest)];
      if (validation.ok === false) {
        parts.push(`<div class="hb-alert" data-tone="danger"><i data-feather="alert-triangle"></i><span><b>校验未通过 · ${(validation.errors || []).length} 个错误</b>
          <ul>${(validation.errors || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>请修改说明后重新编译。</span></div>`);
      }
      if (!compiled && validation.ok !== false) {
        parts.push(`<div class="hb-empty"><p>还没有编译${state.dirty ? "，请先保存修改" : ""}。${canEdit ? "点“交给 AI 编译”，通常 20–40 秒。" : ""}</p></div>`);
      }
      if (compiled) {
        if (amb.items.length) {
          parts.push(`<div class="hb-alert" data-tone="${amb.open ? "warn" : "ok"}"><i data-feather="${amb.open ? "alert-triangle" : "check-circle"}"></i><span>
            <b>${amb.open ? `需要你确认 ${amb.open} 项歧义` : `歧义 ${amb.items.length} / ${amb.items.length} 已确认`}</b>
            <br>${amb.open ? "确认后才会进入试跑与审核。" : "答案会随版本一起交给审核人。"}
            ${editable && !state.answering ? `<button class="hb-link hb-alert__action" type="button" data-handbook-answer>${amb.open ? "去确认 →" : "修改答案"}</button>` : ""}</span></div>`);
          if (state.answering && editable) parts.push(answerPanel(latest));
        }
        parts.push(`<div class="hb-block"><h3>AI 的理解</h3><p class="hb-quote">${escapeHtml(compiled.restatement)}</p></div>`);
        const described = latest.described || [];
        const shown = state.allDescribed ? described : described.slice(0, 3);
        parts.push(`<div class="hb-block"><h3>将生效的内容 ${described.length > 3 ? `<button class="hb-link" type="button" data-described-toggle>${state.allDescribed ? "收起" : `查看 ${described.length} 条`}</button>` : ""}</h3>
          <ol class="hb-numbered">${shown.map((line) => `<li>${escapeHtml(line)}</li>`).join("") || "<li>没有可生效的条目</li>"}</ol></div>`);
        if (compiled.unsupported?.length) {
          parts.push(`<div class="hb-alert" data-tone="warn"><i data-feather="file-text"></i><span><b>开发需求</b> <span class="hb-badge" data-tone="pending">不会生效</span>
            <ul>${compiled.unsupported.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>现有功能做不到，已整理为开发需求；不影响其余内容提交审核。</span></div>`);
        }
        const trial = latest.trial;
        const needsTrial = Boolean(compiled.examples?.length);
        parts.push(`<div class="hb-block"><h3>试跑 ${trial ? `<button class="hb-link" type="button" data-trial-toggle>${state.trialTable ? "收起试跑表" : "查看试跑表 →"}</button>` : ""}</h3>
          ${trial ? `<div class="hb-stats"><span><b data-tone="ok">${trial.passed}</b>通过</span><span><b data-tone="warn">${trial.improved}</b>改善</span><span><b>${trial.passed} / ${trial.cases}</b>全部说法</span></div>
            ${state.trialTable ? trialTable(trial) : ""}` : `<p class="hb-meta">${needsTrial ? "用说明里的说法问 AI 两次（生效前、生效后），只看选择，不执行任何操作。提交审核前需要试跑。" : "这条说明没有说法样例，不需要试跑。"}</p>`}
          ${editable && needsTrial ? `<button class="ghost-btn hb-btn-sm" type="button" data-handbook-trial><i data-feather="play"></i><span>${trial ? "重新试跑" : "试跑"}</span></button>` : ""}</div>`);
        parts.push(`<div class="hb-impact"><b>发布后会发生什么</b><span>V${latest.version} 审核通过后立即替换机器人的理解${entry.active_version && entry.active_version !== latest.version ? `，V${entry.active_version} 自动停用` : ""}；进行中的对话不受影响。</span></div>`);
      }
      parts.push(submitArea(entry, latest));
      node.innerHTML = parts.join("");
      if (window.feather) window.feather.replace();
    }

    function trialTable(trial) {
      return `<div class="hb-table-wrap"><table class="hb-table hb-table--trial">
        <colgroup><col style="width:214px"><col style="width:105px"><col style="width:120px"><col style="width:165px"><col style="width:80px"></colgroup>
        <thead><tr><th>说法</th><th>应选动作</th><th>生效前</th><th>生效后</th><th>结果</th></tr></thead><tbody>
        ${trial.rows.map((row) => {
          const improved = row.pass && row.before !== row.expected;
          return `<tr><td>${escapeHtml(row.utterance)}</td><td>${escapeHtml(row.expected_title || row.expected)}</td>
          <td>${escapeHtml(ACTION_TEXT[row.before] || row.before_title || row.before)}</td><td>${escapeHtml(ACTION_TEXT[row.after] || row.after_title || row.after)}</td>
          <td data-tone="${row.pass ? (improved ? "warn" : "ok") : "danger"}">${row.pass ? (improved ? "! 改善" : "✓ 通过") : "✕ 未通过"}</td></tr>`;
        }).join("")}</tbody></table></div>`;
    }

    function submitArea(entry, latest) {
      const live = entry.status === "ACTIVE" && entry.active_version === latest?.version;
      const controls = [];
      let line = "";
      const resumable = entry.status === "PAUSED" && entry.active_version
        && (entry.versions || []).every((item) => item.version <= entry.active_version || item.status === "VOID");
      if (entry.status === "RETIRED") {
        line = '<span class="hb-meta">已作废，只能查看</span>';
      } else if (live) {
        line = `<span class="hb-meta" data-tone="ok"><i data-feather="check-circle"></i>V${latest.version} 正在生效</span>`;
        if (canEdit) controls.push('<button class="ghost-btn" type="button" data-handbook-status-action="pause">暂停</button>');
      } else if (resumable) {
        line = `<span class="hb-meta" data-tone="danger">V${entry.active_version} 已暂停</span>`;
        if (superAdmin) controls.push('<button class="primary-btn" type="button" data-handbook-status-action="resume">恢复生效</button>');
      } else if (latest?.status === "PENDING") {
        line = `<span class="hb-meta" data-tone="warn"><i data-feather="clock"></i>V${latest.version} 已提交审核，等待超级管理员处理</span>`;
        if (superAdmin) controls.push('<button class="primary-btn" type="button" data-handbook-review>去审核 →</button>');
        else if (canEdit) controls.push('<button class="ghost-btn" type="button" data-handbook-withdraw>撤回审核</button>');
      } else {
        const blockers = submitBlockers(latest);
        if (blockers.length) {
          line = `<span class="hb-meta" data-tone="warn"><i data-feather="lock"></i>${escapeHtml(blockers[0])}${blockers.length > 1 ? `（还差 ${blockers.length} 项：${escapeHtml(blockers.slice(1).join("；"))}）` : ""}</span>`;
          if (canEdit) controls.push('<button class="primary-btn" type="button" disabled>提交审核</button>');
        } else {
          line = '<span class="hb-meta" data-tone="ok"><i data-feather="check-circle"></i>已满足提交条件</span>';
          if (canEdit) controls.push('<button class="primary-btn" type="button" data-handbook-submit>提交审核</button>');
        }
      }
      const retire = superAdmin && entry.status !== "RETIRED" && entry.active_version ? '<button class="hb-link" data-tone="danger" type="button" data-handbook-status-action="retire">作废说明</button>' : "";
      const note = latest?.status === "COMPILED" || latest?.status === "DRAFT" ? '<p class="hb-meta">确认歧义并试跑后可提交，提交后由超级管理员审核。</p>' : "";
      return `<div class="hb-submit">${line}<div class="hb-actions">${retire}${controls.join("")}</div>${note}</div>`;
    }

    // -- review (spec 53) ----------------------------------------------------------------

    function lineDiff(before, after) {
      const a = String(before || "").split("\n");
      const b = String(after || "").split("\n");
      const table = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
      for (let i = a.length - 1; i >= 0; i -= 1) for (let j = b.length - 1; j >= 0; j -= 1) {
        table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
      }
      const out = [];
      let i = 0; let j = 0;
      while (i < a.length || j < b.length) {
        if (i < a.length && j < b.length && a[i] === b[j]) { out.push(["same", a[i]]); i += 1; j += 1; }
        else if (j < b.length && (i >= a.length || table[i][j + 1] >= table[i + 1][j])) { out.push(["add", b[j]]); j += 1; }
        else { out.push(["del", a[i]]); i += 1; }
      }
      return out.map(([kind, text]) => `<div class="hb-diff__line" data-kind="${kind}"><span>${{ same: " ", add: "+", del: "−" }[kind]}</span>${escapeHtml(text) || "&nbsp;"}</div>`).join("");
    }

    function renderReview() {
      const entry = state.current;
      const latest = latestOf(entry);
      const node = $("[data-handbook-review-page]");
      if (!entry || !latest) { node.innerHTML = ""; return; }
      const compiled = latest.compiled || {};
      const amb = ambiguities(latest);
      const trial = latest.trial;
      const blockers = latest.blockers || [];
      const ready = latest.status === "PENDING" && !blockers.length;
      const submit = (entry.events || []).find((item) => item.action === "SUBMIT" && item.version === latest.version);
      const active = entry.active_version && entry.active_version !== latest.version ? (entry.versions || []).find((item) => item.version === entry.active_version) : null;
      const validationOk = latest.validation?.ok === true;
      const trialText = trial ? `${trial.passed - trial.improved} 通过 · ${trial.improved} 改善${trial.cases - trial.passed ? ` · ${trial.cases - trial.passed} 未通过` : ""}` : compiled.examples?.length ? "未试跑" : "无需试跑";
      const checks = [
        ["校验", validationOk ? "通过" : "未通过", validationOk],
        ["歧义", `${amb.items.length - amb.open} / ${amb.items.length} 已确认`, !amb.open],
        ["试跑", trialText, Boolean(trial?.rows?.length) || !compiled.examples?.length],
        ["阻塞项", blockers.length ? `${blockers.length} 项` : "无", !blockers.length],
      ];
      const described = latest.described || [];
      node.innerHTML = `
        <div class="hb-review__crumbs"><button class="hb-link" type="button" data-review-back><i data-feather="arrow-left"></i>返回说明列表</button><span>/ ${escapeHtml(entry.title)}</span>
          ${active ? `<button class="hb-link hb-review__compare" type="button" data-review-compare aria-pressed="${state.compare}"><i data-feather="git-pull-request"></i>与 V${active.version} 对比</button>` : ""}</div>
        <div class="hb-review__title"><div><h2>${escapeHtml(entry.title)} ${versionBadge(entry, latest)}</h2>
          <p class="hb-meta">V${latest.version}${submit ? ` · 提交于 ${escapeHtml(formatTime(submit.created_at))} · ${escapeHtml(person(entry, submit.actor_id))}` : ""}</p></div></div>
        ${state.compare && active ? `<section class="hb-card"><div class="hb-card__head"><h3>V${active.version} → V${latest.version} 的说明改动</h3></div><div class="hb-diff">${lineDiff(active.body, latest.body)}</div></section>` : ""}
        <div class="hb-review">
          <div class="hb-review__main">
            <div class="hb-alert" data-tone="${ready ? "ok" : "warn"}"><i data-feather="${ready ? "check-circle" : "alert-triangle"}"></i><span>
              <b>${ready ? "已满足生效条件" : latest.status === "PENDING" ? "还不能生效" : "这个版本还没有提交审核"}</b><br>
              ${escapeHtml(checks.map(([label, text]) => `${label} ${text}`).join(" · "))}
              ${blockers.length ? `<br>${escapeHtml(blockers.join("；"))}` : ""}</span>
              ${compiled.unsupported?.length ? `<span class="hb-chip hb-chip--sm" data-tone="warn">开发需求 ${compiled.unsupported.length} 项</span>` : ""}</div>
            <section class="hb-card"><div class="hb-card__head"><h3>AI 的中文复述</h3>${amb.open ? "" : '<span class="hb-badge" data-tone="active">已确认</span>'}</div>
              <p class="hb-quote">${escapeHtml(compiled.restatement || "")}</p>
              <details class="hb-review__source"><summary>查看原文</summary><p>${escapeHtml(latest.body)}</p></details></section>
            <section class="hb-card"><div class="hb-card__head"><h3>需要确认的歧义</h3><span class="hb-badge" data-tone="${amb.open ? "pending" : "active"}">${amb.items.length - amb.open} / ${amb.items.length} 已确认</span></div>
              ${amb.items.length ? `<ol class="hb-review__amb">${amb.items.map((item, index) => {
                const answer = amb.answers.get(index);
                return `<li data-done="${Boolean(answer)}"><div><b>${escapeHtml(item.question)}</b>
                  <span class="hb-meta" data-tone="${answer ? "ok" : "warn"}">${answer ? `已选：${escapeHtml(answer.answer || "已核实，按复述执行")}` : "未确认"}</span></div>
                  ${answer ? `<span class="hb-meta">${escapeHtml(person(entry, answer.by))}${answer.at ? ` · ${escapeHtml(formatTime(answer.at))}` : ""}</span>` : ""}</li>`;
              }).join("")}</ol><p class="hb-meta">全部歧义确认后，版本才可提交审核。</p>` : '<p class="hb-meta">AI 没有发现需要确认的歧义。</p>'}</section>
            <section class="hb-card"><div class="hb-card__head"><h3>将生效的内容</h3><span class="hb-meta">${described.length} 条</span></div>
              <ol class="hb-numbered">${described.map((line) => `<li>${escapeHtml(line)}</li>`).join("") || "<li>没有可生效的条目</li>"}</ol></section>
            <section class="hb-card"><div class="hb-card__head"><h3>试跑结果</h3>${trial ? `<span class="hb-badge" data-tone="${trial.passed === trial.cases ? "active" : "pending"}">${escapeHtml(trialText)}</span>` : ""}</div>
              ${trial ? `${trialTable(trial)}<p class="hb-meta">按已保存的 V${latest.version} 内容试跑，只看机器人会选哪个动作，不执行任何操作；“改善”表示生效前选错、生效后选对。</p>`
                : `<p class="hb-meta">${compiled.examples?.length ? "还没有试跑。" : "这条说明没有说法样例，不需要试跑。"}</p>`}</section>
          </div>
          <aside class="hb-review__side">
            <section class="hb-card"><div class="hb-card__head"><div><h3>审核决定</h3><p class="hb-card__sub">仅超级管理员可审核</p></div>${versionBadge(entry, latest)}</div>
              <ul class="hb-review__checks">${checks.map(([label, text, ok]) => `<li data-ok="${ok}"><i data-feather="${ok ? "check" : "x"}"></i><span>${label}</span><b>${escapeHtml(text)}</b></li>`).join("")}</ul>
              <div class="hb-impact"><b>生效后会发生什么</b><span>V${latest.version} 立即替换机器人对这条说明的理解${active ? `，V${active.version} 自动停用` : ""}；进行中的对话不受影响。</span></div>
              ${superAdmin && latest.status === "PENDING" ? `<div class="hb-actions hb-actions--split">
                  <button class="ghost-btn" type="button" data-review-reject>退回修改</button>
                  <button class="primary-btn" type="button" data-review-approve ${ready ? "" : "disabled"}><i data-feather="check"></i><span>通过并生效</span></button></div>
                ${ready ? "" : `<p class="hb-meta" data-tone="warn">${escapeHtml(blockers.join("；"))}</p>`}` : ""}
              ${superAdmin && ["PENDING", "COMPILED", "DRAFT"].includes(latest.status) ? `<div class="hb-danger-row"><button class="hb-btn-danger-text" type="button" data-review-discard><i data-feather="trash-2"></i>作废此版本</button><span class="hb-meta">作废不可恢复，需二次确认</span></div>` : ""}
              <p class="hb-meta">审核记录会写入操作记录，并通知说明维护人。</p></section>
            <section class="hb-card"><div class="hb-card__head"><h3>版本历史</h3></div>
              <ul class="hb-review__versions">${(entry.versions || []).map((item) => `<li aria-current="${item.version === latest.version}"><b>V${item.version}</b>
                <span>${item.version === latest.version ? "当前审核版本" : entry.active_version === item.version ? "当前线上版本" : item.status === "SUPERSEDED" ? "已被替换" : ""}</span>
                <span class="hb-meta">${escapeHtml(formatTime(item.updated_at))}</span>${versionBadge(entry, item)}</li>`).join("")}</ul></section>
            <section class="hb-card"><div class="hb-card__head"><h3>操作记录</h3></div>
              <ul class="hb-review__events">${(entry.events || []).slice(0, 12).map((item) => `<li><span class="hb-meta">${escapeHtml(formatTime(item.created_at))}</span>
                <span>${escapeHtml(eventText(item))}</span><span class="hb-meta">${escapeHtml(person(entry, item.actor_id))}</span></li>`).join("") || '<li class="hb-meta">暂无记录</li>'}</ul></section>
          </aside>
        </div>`;
      if (window.feather) window.feather.replace();
    }

    function eventText(item) {
      const detail = item.detail || {};
      const version = item.version ? `V${item.version} ` : "";
      if (item.action === "COMPILE") return `${version}${detail.ok === false ? "编译未通过校验" : "AI 编译完成"}${detail.seconds ? ` · ${detail.seconds} 秒` : ""}`;
      if (item.action === "TRIAL") return `${version}试跑 ${detail.cases ?? ""} 条${detail.cases ? `（${detail.passed} 通过）` : ""}`;
      if (item.action === "ACKNOWLEDGE") return `${version}确认 ${detail.count ?? ""} 项歧义`;
      if (item.action === "REJECT") return `${version}退回修改${detail.reason ? `：${detail.reason}` : ""}`;
      return `${version}${EVENT_TEXT[item.action] || item.action}`;
    }

    function renderAll() {
      renderList();
      const review = state.view === "review" && state.current;
      $("[data-entries-work]").hidden = Boolean(review);
      $("[data-handbook-review-page]").hidden = !review;
      if (review) { renderReview(); return; }
      renderEditor();
      renderResult();
    }

    // -- data --------------------------------------------------------------------------

    async function loadList() {
      state.entries = (await request(`${base}?include_retired=1`)).entries || [];
      renderList();
    }

    async function openEntry(entryId, { keepView = false } = {}) {
      state.current = await request(`${base}/${encodeURIComponent(entryId)}`);
      state.allDescribed = false;
      state.answering = false;
      if (!keepView) { state.view = "work"; state.compare = false; }
      renderAll();
    }

    async function run(progress, work, success = "") {
      if (state.busy) return;
      state.busy = true;
      const unlock = UI.lockButtons(root);
      feedback.progress(...(Array.isArray(progress) ? progress : [progress]));
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

    async function leaveOk() {
      if (!state.dirty) return true;
      return confirmAction({ title: "有未保存的修改", body: "离开后本次编辑不会保存，已有的编译结果也不会更新。", confirm: "放弃并离开", cancel: "继续编辑", danger: true });
    }

    function collectAnswers(latest) {
      const amb = ambiguities(latest);
      const answers = [];
      for (const fieldset of root.querySelectorAll("[data-answer-index]")) {
        const index = Number(fieldset.dataset.answerIndex);
        const typed = fieldset.querySelector("[data-answer-text]").value.trim();
        const picked = fieldset.querySelector("input[type=radio]:checked")?.value || "";
        const answer = typed || picked;
        if (amb.items[index].options.length && !answer) continue;
        answers.push({ index, answer });
      }
      return answers;
    }

    // -- events ------------------------------------------------------------------------

    root.addEventListener("input", (event) => {
      const target = event.target;
      if (target.matches("[data-entries-search]")) { state.query = target.value; renderList(); return; }
      if (target.matches("[data-handbook-title], [data-handbook-body]")) {
        state.dirty = true;
        const key = target.matches("[data-handbook-title]") ? "title" : "body";
        const counter = root.querySelector(`[data-count-for="${key}"]`);
        if (counter) counter.textContent = `${target.value.length} / ${key === "title" ? 100 : 4000}`;
        const saved = $("[data-saved]");
        if (saved) saved.innerHTML = '<i class="hb-dot" data-tone="warn"></i>有未保存的修改';
      }
    });

    root.addEventListener("click", async (event) => {
      const target = event.target.closest("button");
      if (!target || target.disabled || state.busy) return;
      const entry = state.current;
      const latest = latestOf(entry);
      const url = (action) => `${base}/${encodeURIComponent(entry.entry_id)}/${action}`;
      const refresh = async (keepView = false) => { await openEntry(entry.entry_id, { keepView }); await loadList(); };
      if (target.dataset.entry) {
        if (target.dataset.entry === entry?.entry_id && state.view === "work") return;
        if (!(await leaveOk())) return;
        run("正在读取……", () => openEntry(target.dataset.entry));
      } else if (target.dataset.entriesFilter) {
        state.filter = target.dataset.entriesFilter; state.showRetired = false; renderList();
      } else if (target.hasAttribute("data-entries-retired")) {
        state.showRetired = !state.showRetired; renderList();
      } else if (target.hasAttribute("data-handbook-new") || target.hasAttribute("data-handbook-new-empty")) {
        if (!(await leaveOk())) return;
        state.current = null; state.view = "work";
        renderAll();
        root.querySelector("[data-handbook-title]")?.focus();
      } else if (target.hasAttribute("data-history-toggle")) {
        state.history = !state.history; renderEditor();
      } else if (target.hasAttribute("data-described-toggle")) {
        state.allDescribed = !state.allDescribed; renderResult();
      } else if (target.hasAttribute("data-trial-toggle")) {
        state.trialTable = !state.trialTable; renderResult();
      } else if (target.hasAttribute("data-handbook-answer")) {
        state.answering = true; renderResult();
        root.querySelector("[data-answers] input")?.focus();
      } else if (target.hasAttribute("data-handbook-answer-cancel")) {
        state.answering = false; renderResult();
      } else if (target.hasAttribute("data-handbook-answer-save") && entry && latest) {
        const answers = collectAnswers(latest);
        if (!answers.length) { feedback.error("还没有选择答案", "为每项歧义选择一个候选答案，或写出你的答案。"); return; }
        run("正在保存确认……", async () => {
          await request(url("acknowledge"), { version: latest.version, answers });
          await openEntry(entry.entry_id);
        }, "已保存确认");
      } else if (target.hasAttribute("data-handbook-save")) {
        const title = $("[data-handbook-title]").value.trim();
        const body = $("[data-handbook-body]").value;
        if (!title || !body.trim()) { feedback.error("还不能保存", "标题和说明都要填写。"); return; }
        if (entry?.status === "ACTIVE" && !(await confirmAction({ title: "保存并暂停线上版本？", body: `这条说明正在生效。保存后 V${entry.active_version} 会立即暂停，直到新版本审核通过。`, confirm: "保存并暂停" }))) return;
        run("正在保存……", async () => {
          if (entry) {
            await request(url("save"), { title, body, row_version: entry.row_version });
            await openEntry(entry.entry_id);
          } else {
            const created = await request(base, { title, body });
            await openEntry(created.entry_id);
          }
          await loadList();
        }, "已保存");
      } else if (target.hasAttribute("data-handbook-compile") && entry) {
        if (state.dirty) { feedback.error("请先保存说明", "编译使用已保存的内容；保存后再交给 AI 编译。"); return; }
        run(["AI 正在编译", "通常需要 20–40 秒；可以离开此页，完成后结果留在右侧。"], async () => {
          await request(url("compile"), {});
          await refresh();
        }, "编译完成");
      } else if (target.hasAttribute("data-handbook-trial") && entry) {
        run(["正在试跑", "每条说法会问 AI 两次（生效前、生效后），不执行任何操作。"], async () => {
          await request(url("trial"), {});
          await openEntry(entry.entry_id);
        }, "试跑完成");
      } else if (target.hasAttribute("data-handbook-submit") && entry && latest) {
        if (!(await confirmAction({ title: `提交 V${latest.version} 审核？`, body: "提交后由超级管理员审核；审核期间不能修改，需要修改可以撤回。", confirm: "提交审核" }))) return;
        run("正在提交……", async () => { await request(url("submit"), { version: latest.version }); await refresh(); }, `V${latest.version} 已提交审核`);
      } else if (target.hasAttribute("data-handbook-withdraw") && entry && latest) {
        if (!(await confirmAction({ title: "撤回审核？", body: `撤回后 V${latest.version} 回到已编译状态，可以继续修改，修改后要重新编译和提交。`, confirm: "撤回审核" }))) return;
        run("正在撤回……", async () => { await request(url("withdraw"), { version: latest.version }); await refresh(); }, "已撤回审核");
      } else if (target.hasAttribute("data-handbook-review") && entry) {
        state.view = "review"; state.compare = false; renderAll();
      } else if (target.hasAttribute("data-review-back")) {
        state.view = "work"; renderAll();
      } else if (target.hasAttribute("data-review-compare")) {
        state.compare = !state.compare; renderReview();
      } else if (target.hasAttribute("data-review-approve") && entry && latest) {
        const replaced = entry.active_version && entry.active_version !== latest.version ? `，V${entry.active_version} 自动停用` : "";
        if (!(await confirmAction({ title: `通过并生效 V${latest.version}？`, body: `生效后，飞书、微信和后台 AI 助手立即按 V${latest.version} 理解这条说明${replaced}；进行中的对话不受影响。`, confirm: "通过并生效" }))) return;
        run("正在生效……", async () => { await request(url("approve"), { version: latest.version }); state.view = "work"; await refresh(); }, `V${latest.version} 已生效`);
      } else if (target.hasAttribute("data-review-reject") && entry && latest) {
        const reason = await confirmAction({ title: `退回 V${latest.version} 修改？`, body: "退回后版本回到已编译状态，由维护人修改后重新提交；会通知说明维护人。", confirm: "退回修改", input: { label: "退回原因（选填，会发给维护人）", maxLength: 200 } });
        if (reason === false) return;
        run("正在退回……", async () => { await request(url("reject"), { version: latest.version, reason }); state.view = "work"; await refresh(); }, "已退回修改");
      } else if (target.hasAttribute("data-review-discard") && entry && latest) {
        const consequence = entry.active_version ? `线上的 V${entry.active_version} 保持暂停，可以恢复生效。` : "这条说明没有已生效的版本，作废后整条说明一起作废。";
        if (!(await confirmAction({ title: `作废 V${latest.version}？`, body: `作废后这个版本不能恢复，也不能再审核。${consequence}`, confirm: "确认作废", danger: true }))) return;
        run("正在作废……", async () => {
          const result = await request(url("discard"), { version: latest.version });
          state.view = "work";
          if (result.entry_status === "RETIRED") { state.current = null; renderAll(); await loadList(); } else { await refresh(); }
        }, `V${latest.version} 已作废`);
      } else if (target.dataset.handbookStatusAction && entry) {
        const action = target.dataset.handbookStatusAction;
        const dialogs = {
          retire: { title: "作废这条说明？", body: "作废后机器人不再使用这条说明，只能在“查看已作废”里查看，无法恢复。", confirm: "确认作废", danger: true },
          pause: { title: "暂停这条说明？", body: "暂停后机器人立即停止使用这条说明；超级管理员可以恢复。", confirm: "暂停" },
          resume: { title: "恢复生效？", body: `恢复后机器人立即按 V${entry.active_version} 理解这条说明。`, confirm: "恢复生效" },
        };
        if (!(await confirmAction(dialogs[action]))) return;
        run("正在处理……", async () => {
          await request(url(action), { row_version: entry.row_version });
          if (action === "retire") { state.current = null; renderAll(); } else { await openEntry(entry.entry_id); }
          await loadList();
        }, { retire: "已作废", pause: "已暂停", resume: "已恢复生效" }[action]);
      }
    });

    renderAll();
    run("正在读取……", loadList);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
