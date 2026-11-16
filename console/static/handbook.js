(() => {
  "use strict";

  // 业务手册 → 说明条目 (frontend-spec 52 写作与编译, 53 审核生效). The Agent
  // validates every compile, trial and approval; this page only shows and forwards.
  const ACTION_TEXT = { none: "直接回答", ask_user: "反问", error: "调用失败", unknown: "未知动作" };
  const VERSION_TEXT = { DRAFT: "草稿", COMPILED: "已编译", ACTIVE: "已生效", SUPERSEDED: "已被替换" };

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
    const state = { entries: [], current: null, busy: false, dirty: false, query: "", allDescribed: false, trialTable: false };

    // -- derived facts -----------------------------------------------------------------

    function latestOf(entry) {
      return entry ? (entry.versions || []).find((item) => item.version === entry.latest_version) || null : null;
    }

    function ambiguityState(latest) {
      const list = latest?.compiled?.ambiguities || [];
      const acknowledged = new Set(latest?.acknowledged || []);
      return { list, acknowledged, open: list.filter((_item, index) => !acknowledged.has(index)).length };
    }

    // 编写 → 编译 → 校验 → 试跑 → 审核; each step is done / current / error / todo.
    function steps(entry, latest) {
      const compiled = latest && latest.status !== "DRAFT" && latest.compiled;
      const validation = latest?.validation || {};
      const amb = ambiguityState(latest);
      const live = entry && entry.status === "ACTIVE" && entry.active_version === latest?.version;
      const write = latest ? "done" : "current";
      const compile = !latest ? "todo" : (compiled || validation.ok === false) && latest.status !== "DRAFT" ? "done" : "current";
      let check = "todo";
      if (validation.ok === false) check = "error";
      else if (compiled) check = amb.open ? "current" : "done";
      // 试跑 is optional for the Agent; 审核 opens once approval_blockers is empty.
      const trial = latest?.trial ? "done" : "todo";
      let review = "todo";
      if (live) review = "done";
      else if (latest?.status === "COMPILED" && !(latest.blockers || []).length) review = "current";
      return [["编写", write], ["编译", compile], ["校验", check], ["试跑", trial], ["审核", review]];
    }

    function stepper(entry, latest) {
      return `<ol class="hb-steps">${steps(entry, latest).map(([label, tone], index) => `<li data-tone="${tone}">
        <span class="hb-steps__dot">${tone === "done" ? "✓" : tone === "error" || (tone === "current" && index === 2) ? "!" : index + 1}</span><span>${label}</span></li>`).join("")}</ol>`;
    }

    // -- list --------------------------------------------------------------------------

    function renderList() {
      const entries = state.entries;
      $("[data-entries-summary]").textContent = `${entries.length} 条说明`;
      UI.setTabCount("entries", entries.length);
      const query = state.query.trim().toLowerCase();
      const visible = entries.filter((entry) => !query || entry.title.toLowerCase().includes(query));
      const list = $("[data-handbook-entries]");
      if (!entries.length) {
        list.innerHTML = `<li class="hb-empty hb-empty--row"><i data-feather="file-text"></i><p>还没有业务说明。</p>
          ${canEdit ? '<button class="hb-link" type="button" data-handbook-new-empty>新建第一条说明</button>' : ""}</li>`;
      } else if (!visible.length) {
        list.innerHTML = '<li class="hb-empty hb-empty--row"><p>没有标题包含这个词的说明。</p></li>';
      } else {
        list.innerHTML = visible.map((entry) => `<li><button class="hb-row" type="button" data-entry="${escapeHtml(entry.entry_id)}" aria-current="${state.current?.entry_id === entry.entry_id}">
          <span class="hb-row__top"><span class="hb-row__title">${escapeHtml(entry.title)}</span>${badge(entry.status)}</span>
          <span class="hb-row__meta">V${entry.latest_version} · ${entry.active_version ? `当前生效 V${entry.active_version}` : "尚未生效"}</span>
          <span class="hb-row__time">${escapeHtml(formatTime(entry.updated_at))}</span></button></li>`).join("");
      }
      $("[data-entries-foot]").textContent = "已作废的说明不在列表中";
      if (window.feather) window.feather.replace();
    }

    // -- editor ------------------------------------------------------------------------

    function renderEditor() {
      const entry = state.current;
      const latest = latestOf(entry);
      const readonly = canEdit ? "" : " readonly";
      let notice = "";
      if (entry?.status === "ACTIVE" && canEdit) {
        notice = `<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span><b>这条说明正在生效中</b><br>保存后会立即暂停 V${entry.active_version}，直到新版本审核通过。</span></div>`;
      } else if (entry?.status === "PAUSED") {
        notice = '<div class="hb-alert" data-tone="danger"><i data-feather="pause-circle"></i><span><b>这条说明已暂停</b><br>机器人暂不使用它；新版本审核通过后恢复，超级管理员也可直接恢复原版本。</span></div>';
      }
      const body = latest ? latest.body : "";
      const title = entry ? entry.title : "";
      $("[data-handbook-editor]").innerHTML = `
        <div class="hb-card__head">
          <div><h2 id="handbook-editor-title">${entry ? "编辑说明" : "新建说明"}${entry ? ` <span class="hb-chip hb-chip--sm">V${entry.latest_version} · ${escapeHtml(VERSION_TEXT[latest?.status] || "")}</span>` : ""}</h2>
            <p class="hb-card__sub">${entry ? (entry.active_version ? `基于 V${entry.active_version} 修改${entry.status === "ACTIVE" ? " · 保存后会暂停线上版本" : ""}` : "尚未生效的说明") : "用平常的话写，保存后交给 AI 编译"}</p></div>
          ${entry && (entry.versions || []).length > 1 ? '<button class="hb-link" type="button" data-history-toggle><i data-feather="clock"></i>版本历史</button>' : ""}
        </div>
        ${notice}
        <label class="hb-field"><span class="hb-field__label">标题 <span class="hb-counter" data-count-for="title">${title.length} / 100</span></span>
          <input type="text" maxlength="100" data-handbook-title value="${escapeHtml(title)}" placeholder="例如：网点简称与报价说法"${readonly}></label>
        <label class="hb-field hb-field--grow"><span class="hb-field__label">说明 <span class="hb-counter" data-count-for="body">${body.length} / 4000</span></span>
          <textarea maxlength="4000" data-handbook-body placeholder="例如：大家说“岳麓店”就是长沙岳麓区网点。“报个价”“问个价”都是要三平台报价。"${readonly}>${escapeHtml(body)}</textarea>
          <span class="hb-field__hint">支持换行分段；编译时会按段落理解。</span></label>
        <div class="hb-history" data-history hidden><h3>版本历史</h3><ul>${(entry?.versions || []).map((item) => `<li><b>V${item.version}</b><span>${escapeHtml(VERSION_TEXT[item.status] || item.status)}${entry.active_version === item.version ? " · 当前线上" : ""}</span><span class="hb-meta">${escapeHtml(formatTime(item.updated_at))}</span></li>`).join("")}</ul></div>
        <div class="hb-card__bar">
          <span class="hb-meta" data-saved>${entry ? `<i class="hb-dot" data-tone="ok"></i>已保存 ${escapeHtml(formatTime(entry.updated_at))}` : "尚未保存"}</span>
          ${canEdit ? `<div class="hb-actions"><button class="ghost-btn" type="button" data-handbook-save>保存说明</button>
            ${entry ? '<button class="primary-btn" type="button" data-handbook-compile><i data-feather="zap"></i><span>交给 AI 编译</span></button>' : ""}</div>` : ""}
        </div>
        ${canEdit ? '<p class="hb-card__foot">AI 编译通常 20–40 秒，可以离开此页；编译结果不会自动发布。</p>' : ""}`;
      state.dirty = false;
    }

    // -- result ------------------------------------------------------------------------

    function blockerList(entry, latest) {
      if (!latest) return ["先保存说明"];
      if (latest.status === "DRAFT") return [latest.validation?.ok === false ? "校验未通过，请修改说明后重新编译" : "交给 AI 编译"];
      return latest.blockers || [];
    }

    function renderResult() {
      const node = $("[data-handbook-result]");
      const entry = state.current;
      const latest = latestOf(entry);
      const head = `<div class="hb-card__head"><h2 id="handbook-result-title">AI 编译结果</h2>
        ${latest?.compiled && latest.status !== "DRAFT" ? `<span class="hb-meta" data-tone="ok">✓ ${escapeHtml(formatTime(latest.updated_at))}</span>` : ""}</div>`;
      if (!entry) {
        node.innerHTML = `${head}<div class="hb-empty"><i data-feather="zap"></i><p>${canEdit ? "保存说明后，交给 AI 编译，这里会显示 AI 理解了什么、哪些会生效、还差哪一步。" : "选择一条说明查看编译结果。"}</p></div>`;
        if (window.feather) window.feather.replace();
        return;
      }
      const compiled = latest && latest.status !== "DRAFT" ? latest.compiled : null;
      const validation = latest?.validation || {};
      const amb = ambiguityState(latest);
      const parts = [head, stepper(entry, latest)];
      if (validation.ok === false) {
        parts.push(`<div class="hb-alert" data-tone="danger"><i data-feather="alert-triangle"></i><span><b>校验未通过 · ${(validation.errors || []).length} 个错误</b>
          <ul>${(validation.errors || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>请修改说明后重新编译。</span></div>`);
      }
      if (!compiled && validation.ok !== false) {
        parts.push(`<div class="hb-empty"><p>还没有编译${state.dirty ? "，请先保存修改" : ""}。${canEdit ? "点“交给 AI 编译”，通常 20–40 秒。" : ""}</p></div>`);
      }
      if (compiled) {
        if (amb.list.length) {
          parts.push(`<div class="hb-alert" data-tone="${amb.open ? "warn" : "ok"}"><i data-feather="${amb.open ? "alert-triangle" : "check-circle"}"></i><span>
            <b>${amb.open ? `需要你确认 ${amb.open} 项歧义` : `歧义 ${amb.list.length} / ${amb.list.length} 已确认`}</b>
            ${amb.open ? "<br>每一条都要么修改说明消除，要么勾选表示“已核实，按复述执行”；确认后才能审核。" : ""}
            <span class="hb-checks">${amb.list.map((item, index) => `<label><input type="checkbox" data-ack value="${index}" ${amb.acknowledged.has(index) ? "checked" : ""} ${canEdit ? "" : "disabled"}><span>${escapeHtml(item)}</span></label>`).join("")}</span>
            ${canEdit ? '<button class="ghost-btn hb-btn-sm" type="button" data-handbook-ack>保存确认</button>' : ""}</span></div>`);
        }
        parts.push(`<div class="hb-block"><h3>AI 的理解</h3><p class="hb-quote">${escapeHtml(compiled.restatement)}</p></div>`);
        const described = latest.described || [];
        const shown = state.allDescribed ? described : described.slice(0, 3);
        parts.push(`<div class="hb-block"><h3>将生效的内容 ${described.length > 3 ? `<button class="hb-link" type="button" data-described-toggle>${state.allDescribed ? "收起" : `查看 ${described.length} 条`}</button>` : ""}</h3>
          <ol class="hb-numbered">${shown.map((line) => `<li>${escapeHtml(line)}</li>`).join("") || "<li>没有可生效的条目</li>"}</ol></div>`);
        if (compiled.unsupported?.length) {
          parts.push(`<div class="hb-alert" data-tone="warn"><i data-feather="file-text"></i><span><b>开发需求</b> <span class="hb-badge" data-tone="pending">不会生效</span>
            <ul>${compiled.unsupported.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>现有功能做不到，已整理为开发需求；系统要求先从说明里删去这部分才能审核。</span></div>`);
        }
        const trial = latest.trial;
        parts.push(`<div class="hb-block"><h3>试跑 ${trial ? `<button class="hb-link" type="button" data-trial-toggle>${state.trialTable ? "收起试跑表" : "查看试跑表"}</button>` : ""}</h3>
          ${trial ? `<div class="hb-stats"><span><b data-tone="ok">${trial.passed}</b>通过</span><span><b data-tone="warn">${trial.improved}</b>改善</span><span><b>${trial.passed} / ${trial.cases}</b>全部说法</span></div>
            ${state.trialTable ? trialTable(trial) : ""}` : '<p class="hb-meta">用说明里的说法问 AI 两次（生效前、生效后），只看选择，不执行任何操作。</p>'}
          ${canEdit && validation.ok !== false ? `<button class="ghost-btn hb-btn-sm" type="button" data-handbook-trial><i data-feather="play"></i><span>${trial ? "重新试跑" : "试跑"}</span></button>` : ""}</div>`);
        parts.push(`<div class="hb-impact"><b>发布后会发生什么</b><span>V${latest.version} 审核通过后立即替换机器人的理解${entry.active_version && entry.active_version !== latest.version ? `，V${entry.active_version} 自动停用` : ""}；进行中的对话不受影响。</span></div>`);
      }
      parts.push(submitArea(entry, latest));
      node.innerHTML = parts.join("");
      if (window.feather) window.feather.replace();
    }

    function trialTable(trial) {
      return `<div class="hb-table-wrap"><table class="hb-table hb-table--trial"><thead><tr><th>说法</th><th>应选动作</th><th>生效前</th><th>生效后</th><th>结果</th></tr></thead><tbody>
        ${trial.rows.map((row) => `<tr><td>${escapeHtml(row.utterance)}</td><td>${escapeHtml(row.expected_title)}</td>
          <td>${escapeHtml(ACTION_TEXT[row.before] || row.before_title || row.before)}</td><td>${escapeHtml(ACTION_TEXT[row.after] || row.after_title || row.after)}</td>
          <td data-tone="${row.pass ? "ok" : "danger"}">${row.pass ? "✓ 通过" : "✕ 未通过"}</td></tr>`).join("")}</tbody></table></div>`;
    }

    function submitArea(entry, latest) {
      const blockers = blockerList(entry, latest);
      const live = entry.status === "ACTIVE" && entry.active_version === latest?.version;
      const controls = [];
      let line = "";
      if (live) {
        line = `<span class="hb-meta" data-tone="ok"><i data-feather="check-circle"></i>V${latest.version} 正在生效</span>`;
        if (canEdit) controls.push('<button class="ghost-btn" type="button" data-handbook-status-action="pause">暂停</button>');
      } else if (entry.status === "PAUSED" && entry.active_version === entry.latest_version) {
        line = `<span class="hb-meta" data-tone="danger">V${entry.active_version} 已暂停</span>`;
        if (superAdmin) controls.push('<button class="primary-btn" type="button" data-handbook-status-action="resume">恢复生效</button>');
      } else if (blockers.length) {
        line = `<span class="hb-meta" data-tone="warn"><i data-feather="lock"></i>还差 ${blockers.length} 项：${escapeHtml(blockers.join("；"))}</span>`;
        if (superAdmin) controls.push('<button class="primary-btn" type="button" disabled>审核生效</button>');
      } else {
        line = '<span class="hb-meta" data-tone="ok"><i data-feather="check-circle"></i>已满足生效条件</span>';
        controls.push(superAdmin ? '<button class="primary-btn" type="button" data-handbook-approve><i data-feather="check"></i><span>审核通过并生效</span></button>'
          : '<span class="hb-meta">等待超级管理员审核生效</span>');
      }
      const retire = superAdmin ? '<button class="hb-link" data-tone="danger" type="button" data-handbook-status-action="retire">作废</button>' : "";
      return `<div class="hb-submit">${line}<div class="hb-actions">${retire}${controls.join("")}</div></div>`;
    }

    function renderAll() {
      renderList();
      renderEditor();
      renderResult();
    }

    // -- data --------------------------------------------------------------------------

    async function loadList() {
      state.entries = (await request(base)).entries || [];
      renderList();
    }

    async function openEntry(entryId) {
      state.current = await request(`${base}/${encodeURIComponent(entryId)}`);
      state.allDescribed = false;
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
      if (target.dataset.entry) {
        if (target.dataset.entry === entry?.entry_id || !(await leaveOk())) return;
        run("正在读取……", () => openEntry(target.dataset.entry));
      } else if (target.hasAttribute("data-handbook-new") || target.hasAttribute("data-handbook-new-empty")) {
        if (!(await leaveOk())) return;
        state.current = null;
        renderAll();
        root.querySelector("[data-handbook-title]")?.focus();
      } else if (target.hasAttribute("data-history-toggle")) {
        const node = $("[data-history]"); node.hidden = !node.hidden;
      } else if (target.hasAttribute("data-described-toggle")) {
        state.allDescribed = !state.allDescribed; renderResult();
      } else if (target.hasAttribute("data-trial-toggle")) {
        state.trialTable = !state.trialTable; renderResult();
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
          await openEntry(entry.entry_id);
          await loadList();
        }, "编译完成");
      } else if (target.hasAttribute("data-handbook-trial") && entry) {
        run(["正在试跑", "每条说法会问 AI 两次（生效前、生效后）。"], async () => {
          await request(url("trial"), {});
          await openEntry(entry.entry_id);
        }, "试跑完成");
      } else if (target.hasAttribute("data-handbook-ack") && entry && latest) {
        const indices = [...root.querySelectorAll("[data-ack]:checked")].map((input) => Number(input.value));
        run("正在保存确认……", async () => {
          await request(url("acknowledge"), { version: latest.version, indices });
          await openEntry(entry.entry_id);
        }, "已保存确认");
      } else if (target.hasAttribute("data-handbook-approve") && entry && latest) {
        const replaced = entry.active_version && entry.active_version !== latest.version ? `，V${entry.active_version} 自动停用` : "";
        if (!(await confirmAction({ title: "审核通过并生效？", body: `生效后，飞书、微信和后台 AI 助手立即按 V${latest.version} 理解这条说明${replaced}。`, confirm: "通过并生效" }))) return;
        run("正在生效……", async () => {
          await request(url("approve"), { version: latest.version });
          await openEntry(entry.entry_id);
          await loadList();
        }, `V${latest.version} 已生效`);
      } else if (target.dataset.handbookStatusAction && entry) {
        const action = target.dataset.handbookStatusAction;
        const dialogs = {
          retire: { title: "作废这条说明？", body: "作废后机器人不再使用这条说明，列表中也不再显示，无法恢复。", confirm: "确认作废", danger: true },
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
