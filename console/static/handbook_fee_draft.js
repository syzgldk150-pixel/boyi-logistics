(() => {
  "use strict";

  // 业务手册 → 计算规则 → 从手册起草 (方案 B, 2026-11-20). A four-step panel in the rule editor:
  // pick a knowledge-base PDF, pick a chapter (or pages), let the AI draft (the Agent runs it as a
  // job and this page polls), then keep the fees wanted as drafts. Kept drafts open in 核对; nothing
  // here takes effect. The Agent checks every draft (shared/fee_rule_drafting.py) before showing it.
  const BASE = "/handbook/fee-drafts";
  const MAX_PAGES = 8;
  const STEPS = ["选文档", "选章节", "AI 起草", "挑选保存"];

  function create({ UI, onSaved }) {
    const { escapeHtml, request } = UI;
    const state = { step: 0, documents: null, document: null, outline: null, chapter: null, from: "", to: "",
      job: null, picked: new Set(), open: new Set(), error: "", loading: "", timer: 0, node: null };

    const stepper = () => `<ol class="hb-draft__steps">${STEPS.map((label, index) => `<li data-state="${index < state.step ? "done" : index === state.step ? "current" : "todo"}"><span>${index < state.step ? "✓" : index + 1}</span>${label}</li>`).join("")}</ol>`;
    const pagesOf = (item) => item.end - item.start + 1;

    function documentsStep() {
      if (state.loading) return `<p class="hb-meta hb-draft__loading">${escapeHtml(state.loading)}</p>`;
      const list = state.documents || [];
      if (!list.length) return '<div class="hb-empty"><i data-feather="folder"></i><p>知识来源里还没有 PDF。把手册或总部通知放进飞书知识库后，到“知识来源”页签点“立即检查”。</p></div>';
      return `<p class="hb-meta">选择要起草的文件（来自飞书知识库，只列 PDF）。</p>
        <ul class="hb-draft__docs">${list.map((item) => `<li><button type="button" class="hb-draft__doc" data-draft-doc="${escapeHtml(item.node_token)}">
          <i data-feather="file-text"></i><span><b>${escapeHtml(item.title)}</b><span class="hb-meta">${escapeHtml(item.space || "")}</span></span><i data-feather="chevron-right"></i></button></li>`).join("")}</ul>`;
    }

    function chapterStep() {
      if (state.loading) return `<p class="hb-meta hb-draft__loading">${escapeHtml(state.loading)}</p>`;
      const outline = state.outline;
      const custom = state.chapter === "custom";
      const from = Number(state.from);
      const to = Number(state.to);
      const customOk = custom && Number.isInteger(from) && Number.isInteger(to) && from >= 1 && to >= from && to <= outline.page_count && to - from < MAX_PAGES;
      const chosen = outline.chapters[state.chapter];
      const ready = custom ? customOk : Boolean(chosen && pagesOf(chosen) <= MAX_PAGES);
      return `<div class="hb-draft__picked"><span class="hb-meta">文档</span><b>${escapeHtml(outline.title)}</b><span class="hb-meta">共 ${outline.page_count} 页</span>
          <button class="hb-link" type="button" data-draft-back="0">换一个</button></div>
        <p class="hb-meta">选一章（按 PDF 书签）。一次最多 ${MAX_PAGES} 页；章节太长时选它下面的小节，或按页码起草。</p>
        ${outline.chapters.length ? `<ul class="hb-draft__chapters">${outline.chapters.map((item, index) => {
          const tooLong = pagesOf(item) > MAX_PAGES;
          return `<li style="--level:${Math.min(item.level, 4)}"><label data-disabled="${tooLong}"><input type="radio" name="fee-draft-chapter" data-draft-chapter="${index}" ${state.chapter === index ? "checked" : ""} ${tooLong ? "disabled" : ""}>
            <span>${escapeHtml(item.title)}</span><span class="hb-meta">第 ${item.start}${item.end > item.start ? `–${item.end}` : ""} 页${tooLong ? ` · ${pagesOf(item)} 页，太长` : ""}</span></label></li>`;
        }).join("")}</ul>` : '<p class="hb-meta">这份 PDF 没有书签，请按页码起草。</p>'}
        <div class="hb-draft__custom"><label><input type="radio" name="fee-draft-chapter" data-draft-chapter="custom" ${custom ? "checked" : ""}><span>按页码</span></label>
          <input type="number" min="1" max="${outline.page_count}" inputmode="numeric" data-draft-from value="${escapeHtml(state.from)}" aria-label="起始页"><span>到</span>
          <input type="number" min="1" max="${outline.page_count}" inputmode="numeric" data-draft-to value="${escapeHtml(state.to)}" aria-label="结束页"><span class="hb-meta">页</span>
          ${custom && state.from && state.to && !customOk ? `<span class="hb-meta" data-tone="warn">页码要在 1–${outline.page_count} 之间，最多 ${MAX_PAGES} 页</span>` : ""}</div>
        <div class="hb-actions hb-draft__go"><button class="primary-btn" type="button" data-draft-start ${ready ? "" : "disabled"}><i data-feather="zap"></i><span>开始起草</span></button>
          <span class="hb-meta">通常 1–3 分钟。起草期间可以去看别的规则，回来点“从手册起草”继续。</span></div>`;
    }

    function progressStep() {
      const job = state.job || {};
      const stages = job.stages || ["读取手册", "生成页面图片", "AI 起草", "检查结果"];
      const current = stages.indexOf(job.stage);
      if (job.status === "FAILED") {
        return `<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span><b>没有起草成功</b><br>${escapeHtml(job.error || "原因未知")}</span></div>
          <div class="hb-actions"><button class="primary-btn" type="button" data-draft-retry>重新起草</button><button class="ghost-btn" type="button" data-draft-back="1">换个范围</button></div>`;
      }
      return `<p>正在起草 <b>${escapeHtml(job.chapter || `第 ${job.start}–${job.end} 页`)}</b></p>
        <ul class="hb-draft__stages">${stages.map((label, index) => `<li data-state="${index < current ? "done" : index === current ? "current" : "todo"}"><span>${index < current ? "✓" : index === current ? '<i data-feather="loader"></i>' : "·"}</span>${label}</li>`).join("")}</ul>
        <p class="hb-meta">已用 ${Number(job.elapsed || 0)} 秒。AI 会把每个数字对照原文检查，不合格会自动重做一次。</p>`;
    }

    function feeCard(fee) {
      const picked = state.picked.has(fee.index);
      const open = state.open.has(fee.index);
      const lines = fee.lines || [];
      const shown = open ? lines : lines.slice(0, 5);
      const saved = (state.job.saved || {})[String(fee.index)];
      return `<li class="hb-draft__fee" data-picked="${picked}">
        <div class="hb-draft__fee-head"><label><input type="checkbox" data-draft-pick="${fee.index}" ${picked ? "checked" : ""} ${saved ? "disabled" : ""}><b>${escapeHtml(fee.name)}</b></label>
          ${saved ? '<span class="hb-badge" data-tone="active">已保存</span>' : fee.existing ? '<span class="hb-badge" data-tone="pending">已有同名 · 存为新版本</span>' : '<span class="hb-badge">新费用</span>'}</div>
        ${fee.params?.length ? `<p class="hb-meta">要填：${escapeHtml(fee.params.join("、"))}</p>` : ""}
        <ul class="hb-draft__lines">${shown.map((line) => `<li><span>${escapeHtml(line.text)}</span><span class="hb-meta">${escapeHtml(line.location || "")}</span></li>`).join("")}</ul>
        ${lines.length > 5 ? `<button class="hb-link" type="button" data-draft-more="${fee.index}">${open ? "收起" : `展开全部 ${lines.length} 行`}</button>` : ""}
        ${fee.questions?.length ? `<div class="hb-draft__questions"><b>需要拍板</b><ul>${fee.questions.map((item) => `<li>${escapeHtml(item.text)}</li>`).join("")}</ul></div>` : ""}</li>`;
    }

    function resultStep() {
      const result = state.job.result || { fees: [], skipped: [] };
      const count = [...state.picked].filter((index) => !(state.job.saved || {})[String(index)]).length;
      return `<p>从 <b>${escapeHtml(state.job.document_title || "")}</b> ${escapeHtml(state.job.chapter || `第 ${state.job.start}–${state.job.end} 页`)} 起草了 ${result.fees.length} 项费用（用时 ${Number(state.job.elapsed || 0)} 秒）。勾选要保留的，保存成草稿后逐行核对。</p>
        ${result.skipped?.length ? `<div class="hb-alert"><i data-feather="info"></i><span>AI 没有整理的内容：<br>${result.skipped.map(escapeHtml).join("<br>")}</span></div>` : ""}
        <ul class="hb-draft__fees">${result.fees.map(feeCard).join("")}</ul>
        <div class="hb-actions hb-draft__go"><button class="primary-btn" type="button" data-draft-save ${count ? "" : "disabled"}><i data-feather="save"></i><span>保存为草稿（${count} 项）</span></button>
          <button class="ghost-btn" type="button" data-draft-back="1">换个范围再起草</button>
          <span class="hb-meta">草稿不会生效；还要核对、试算、审核。</span></div>`;
    }

    function render() {
      if (!state.node) return;
      const bodies = [documentsStep, chapterStep, progressStep, resultStep];
      state.node.innerHTML = `<div class="hb-card__head"><div><h2 id="fee-editor-title">从手册起草</h2>
          <p class="hb-card__sub">AI 读几页手册，把收费整理成规则草稿；每个数字都要在原文里找得到。保存后在“核对”里对照原页确认，不会直接生效。</p></div>
          <button class="hb-link" type="button" data-draft-close>关闭</button></div>
        ${stepper()}
        ${state.error ? `<div class="hb-alert" data-tone="warn"><i data-feather="alert-triangle"></i><span>${escapeHtml(state.error)}</span></div>` : ""}
        <div class="hb-draft__body">${bodies[state.step]()}</div>`;
      if (window.feather) window.feather.replace();
    }

    async function guarded(work) {
      state.error = "";
      try { await work(); } catch (error) { state.loading = ""; state.error = error.message || "操作没有完成"; }
      render();
    }

    function poll() {
      window.clearTimeout(state.timer);
      if (!state.job || state.job.status !== "RUNNING") return;
      state.timer = window.setTimeout(async () => {
        if (!state.node?.isConnected || !state.node.querySelector(".hb-draft__steps")) return;  // panel closed; resumes on reopen
        try {
          state.job = await request(`${BASE}/jobs/${encodeURIComponent(state.job.job_id)}`);
          if (state.job.status === "DONE") {
            state.step = 3;
            state.picked = new Set(state.job.result.fees.map((fee) => fee.index));
          }
        } catch (error) {
          state.error = error.message || "起草进度读取失败";
        }
        render();
        poll();
      }, 3000);
    }

    async function start() {
      const outline = state.outline;
      const custom = state.chapter === "custom";
      const chapter = custom ? null : outline.chapters[state.chapter];
      const body = { node_token: state.document, start: custom ? Number(state.from) : chapter.start, end: custom ? Number(state.to) : chapter.end,
        chapter: custom ? "" : chapter.title };
      await guarded(async () => {
        const started = await request(`${BASE}/jobs`, body);
        state.job = { job_id: started.job_id, status: "RUNNING", stage: "读取手册", elapsed: 0, chapter: body.chapter, start: body.start, end: body.end };
        state.step = 2;
      });
      poll();
    }

    async function handle(event) {
      const target = event.target.closest("button, input");
      if (!target || !state.node?.contains(target)) return false;
      if (target.dataset.draftDoc && event.type === "click") {
        state.document = target.dataset.draftDoc; state.step = 1; state.chapter = null; state.outline = null;
        state.loading = "正在读取手册目录……第一次需要十几秒。"; render();
        await guarded(async () => { state.outline = await request(`${BASE}/outline?node_token=${encodeURIComponent(state.document)}`); state.loading = ""; });
        if (!state.outline) { state.step = 0; render(); }
      } else if (target.dataset.draftChapter !== undefined && event.type === "change") {
        state.chapter = target.dataset.draftChapter === "custom" ? "custom" : Number(target.dataset.draftChapter); render();
      } else if ((target.matches("[data-draft-from]") || target.matches("[data-draft-to]")) && event.type === "input") {
        state[target.matches("[data-draft-from]") ? "from" : "to"] = target.value; state.chapter = "custom";
        const caret = target.matches("[data-draft-from]") ? "[data-draft-from]" : "[data-draft-to]";
        render(); const again = state.node.querySelector(caret); again?.focus(); again?.setSelectionRange?.(again.value.length, again.value.length);
      } else if (target.hasAttribute("data-draft-start") && event.type === "click") {
        await start();
      } else if (target.hasAttribute("data-draft-retry") && event.type === "click") {
        await start();
      } else if (target.dataset.draftBack !== undefined && event.type === "click") {
        state.step = Number(target.dataset.draftBack); state.error = "";
        if (state.step === 0 && !state.documents) await guarded(async () => { state.documents = (await request(`${BASE}/documents`)).documents; });
        render();
      } else if (target.dataset.draftPick !== undefined && event.type === "change") {
        const index = Number(target.dataset.draftPick);
        if (target.checked) state.picked.add(index); else state.picked.delete(index);
        render();
      } else if (target.dataset.draftMore !== undefined && event.type === "click") {
        const index = Number(target.dataset.draftMore);
        if (state.open.has(index)) state.open.delete(index); else state.open.add(index);
        render();
      } else if (target.hasAttribute("data-draft-save") && event.type === "click") {
        const indices = [...state.picked].filter((index) => !(state.job.saved || {})[String(index)]).sort((a, b) => a - b);
        let created = null;
        await guarded(async () => {
          created = (await request(`${BASE}/jobs/${encodeURIComponent(state.job.job_id)}/save`, { indices })).created;
          state.job = await request(`${BASE}/jobs/${encodeURIComponent(state.job.job_id)}`);
        });
        if (created) await onSaved(created);  // opens the first kept draft in 核对 (replaces this panel)
      } else if (target.hasAttribute("data-draft-close") && event.type === "click") {
        await onSaved([]);
      } else return false;
      return true;
    }

    return {
      async mount(node) {
        state.node = node;
        state.error = "";
        if (state.job && state.job.status === "RUNNING") { state.step = 2; render(); poll(); return; }
        if (state.job && state.job.status === "DONE" && state.step === 3) { render(); return; }
        state.step = 0; state.loading = "正在读取知识来源里的 PDF……";
        render();
        await guarded(async () => { state.documents = (await request(`${BASE}/documents`)).documents; state.loading = ""; });
      },
      handle,
      active: () => Boolean(state.node?.isConnected && state.node.querySelector(".hb-draft__steps")),
      reset() { state.job = null; state.step = 0; state.picked = new Set(); state.open = new Set(); },
    };
  }

  window.FeeDraft = { create };
})();
