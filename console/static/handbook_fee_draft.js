(() => {
  "use strict";

  // 业务手册 → 计算规则 → ① 起草 → 从手册起草 (frontend-spec 59–61, 2026-11-23). Inside one fee's
  // draft: pick a knowledge-base PDF and a chapter (or pages), the Agent asks the AI to draft only
  // this fee and writes the result into the draft when it passes the checks
  // (shared/fee_rule_drafting.py). This file renders the picker, the four-stage progress (60) and
  // the failure (61); progress comes from polling the Agent's job, never from a local timer.
  const BASE = "/handbook/fee-drafts";
  const MAX_PAGES = 8;
  const STAGES = ["读取手册", "生成页面图片", "AI 起草", "检查结果"];
  const STAGE_NOTE = { 读取手册: "从飞书知识库下载这份 PDF", 生成页面图片: "把选中的页面转成图片，表格以图片为准", "AI 起草": "AI 按页读出收费条件与金额", 检查结果: "每个数字都要在原文里找得到" };

  function create({ UI, onChange }) {
    const { escapeHtml, request, formatTime } = UI;
    const state = { ruleId: "", job: null, dismissed: "", picking: false, pick: "doc", documents: null, document: "",
      outline: null, chapter: null, from: "", to: "", loading: "", error: "", timer: 0 };

    const pagesOf = (item) => item.end - item.start + 1;
    const range = (job) => (job?.start && job?.end ? `第 ${job.start}–${job.end} 页` : "");

    function reset(ruleId) {
      window.clearTimeout(state.timer);
      Object.assign(state, { ruleId, job: null, dismissed: "", picking: false, pick: "doc", outline: null, chapter: null,
        from: "", to: "", loading: "", error: "" });
    }

    async function load(ruleId) {
      if (ruleId !== state.ruleId) reset(ruleId);
      try {
        const data = await request(`${BASE}/jobs?rule_id=${encodeURIComponent(ruleId)}`);
        if (state.ruleId !== ruleId) return;
        state.job = data.job || null;
      } catch (_error) {
        state.job = null;  // drafting unavailable: ① still offers 手动填写
      }
      if (state.job?.status === "RUNNING") poll();
    }

    function poll() {
      window.clearTimeout(state.timer);
      if (state.job?.status !== "RUNNING") return;
      const ruleId = state.ruleId;
      state.timer = window.setTimeout(async () => {
        if (state.ruleId !== ruleId || !state.job) return;
        try {
          state.job = await request(`${BASE}/jobs/${encodeURIComponent(state.job.job_id)}`);
          state.error = "";
        } catch (error) {
          state.error = error.message || "起草进度读取失败";
        }
        if (state.ruleId !== ruleId) return;
        onChange(state.job?.status === "DONE" ? "done" : state.job?.status === "FAILED" ? "failed" : "progress");
        poll();
      }, 3000);
    }

    // -- views ----------------------------------------------------------------------------

    function progressHtml(job) {
      const current = Math.max(0, STAGES.indexOf(job.stage));
      const rows = STAGES.map((label, index) => {
        const status = index < current ? "done" : index === current ? "current" : "todo";
        const mark = status === "done" ? "✓" : status === "current" ? '<span class="fs-ring" aria-hidden="true"></span>' : index + 1;
        return `<li class="fs-stage" data-state="${status}"><span class="fs-stage__dot">${mark}</span>
          <span class="fs-stage__text"><b>${label}</b><span>${status === "current" ? `进行中 · 已用 ${Number(job.elapsed || 0)} 秒` : status === "done" ? "已完成" : escapeHtml(STAGE_NOTE[label])}</span>
          ${status === "current" ? '<span class="fs-bar fs-bar--busy" aria-hidden="true"><span></span></span>' : ""}</span></li>`;
      }).join("");
      return `<section class="fs-card fs-progress" aria-live="polite">
          <div class="fs-progress__head"><div><b>${escapeHtml(job.title || job.document_title || "手册")}</b>
            <span class="hb-meta">${escapeHtml([job.chapter, range(job)].filter(Boolean).join(" · "))} · 飞书知识库</span></div>
            <span class="hb-badge" data-tone="pending">进行中</span></div>
          <ol class="fs-stages">${rows}</ol>
          ${state.error ? `<p class="hb-meta" data-tone="warn">${escapeHtml(state.error)}（会继续重试）</p>` : ""}
        </section>
        <div class="fs-row"><span class="hb-meta">完成或失败都会显示在这里；可以先离开这一页，回来时按进度继续。</span>
          <button class="ghost-btn hb-btn-sm" type="button" data-draft-cancel>取消起草</button></div>`;
    }

    function failureHtml(job, editable) {
      return `<section class="fs-fail"><span class="fs-fail__icon" aria-hidden="true">!</span>
          <div><b>起草失败</b><p>${escapeHtml(job.error || "原因未知")}</p>
          <span class="hb-meta">耗时 ${Number(job.elapsed || 0)} 秒</span></div></section>
        ${editable ? `<div class="hb-actions"><button class="primary-btn hb-btn-sm" type="button" data-draft-retry>重新起草</button>
          <button class="ghost-btn hb-btn-sm" type="button" data-draft-rerange>换个范围</button></div>` : ""}
        <div class="fs-sub"><h4>可以这样试</h4><ul class="fs-tips">
          <li>缩小页码范围，只选写着这项收费的几页。</li>
          <li>扫描件读不出表格时，先在飞书里把 PDF 转成可复制的文字版，再回来起草。</li>
          <li>或者用手动填写，第 ② 步再补手册出处。</li></ul></div>
        <div class="fs-sub"><h4>上次尝试</h4><dl class="fs-kv">
          <div><dt>文档</dt><dd>${escapeHtml(job.title || job.document_title || "—")}</dd></div>
          <div><dt>页码</dt><dd>${escapeHtml([job.chapter, range(job)].filter(Boolean).join(" · ") || "—")}</dd></div>
          <div><dt>结果</dt><dd>没有生成初稿</dd></div></dl></div>
        ${editable ? '<button class="hb-link fs-link-red" type="button" data-draft-manual>也可以先手动填写，之后再从手册补出处 →</button>' : ""}`;
    }

    function documentsHtml() {
      const list = state.documents || [];
      if (!list.length) return '<p class="hb-meta">知识来源里还没有 PDF。把手册放进飞书知识库后，到“知识来源”页签点“立即检查”。</p>';
      return `<p class="hb-meta">选择要起草的文件（来自飞书知识库，只列 PDF）。</p>
        <ul class="fs-docs">${list.map((item) => `<li><button type="button" class="fs-doc" data-draft-doc="${escapeHtml(item.node_token)}">
          <i data-feather="file-text"></i><span><b>${escapeHtml(item.title)}</b><span class="hb-meta">${escapeHtml(item.space || "")}</span></span><i data-feather="chevron-right"></i></button></li>`).join("")}</ul>`;
    }

    function chapterHtml(feeName) {
      const outline = state.outline;
      const custom = state.chapter === "custom";
      const from = Number(state.from);
      const to = Number(state.to);
      const customOk = custom && Number.isInteger(from) && Number.isInteger(to) && from >= 1 && to >= from && to <= outline.page_count && to - from < MAX_PAGES;
      const chosen = outline.chapters[state.chapter];
      const ready = custom ? customOk : Boolean(chosen && pagesOf(chosen) <= MAX_PAGES);
      return `<div class="fs-row"><span><span class="hb-meta">文档</span> <b>${escapeHtml(outline.title)}</b> <span class="hb-meta">共 ${outline.page_count} 页</span></span>
          <button class="hb-link" type="button" data-draft-docs>换一个</button></div>
        <p class="hb-meta">选写着“${escapeHtml(feeName)}”的章节（按 PDF 书签），或按页码。一次最多 ${MAX_PAGES} 页。</p>
        ${outline.chapters.length ? `<ul class="fs-chapters">${outline.chapters.map((item, index) => {
          const tooLong = pagesOf(item) > MAX_PAGES;
          return `<li style="--level:${Math.min(item.level, 4)}"><label data-disabled="${tooLong}"><input type="radio" name="fee-draft-chapter" data-draft-chapter="${index}" ${state.chapter === index ? "checked" : ""} ${tooLong ? "disabled" : ""}>
            <span>${escapeHtml(item.title)}</span><span class="hb-meta">第 ${item.start}${item.end > item.start ? `–${item.end}` : ""} 页${tooLong ? ` · ${pagesOf(item)} 页，太长` : ""}</span></label></li>`;
        }).join("")}</ul>` : '<p class="hb-meta">这份 PDF 没有书签，请按页码起草。</p>'}
        <div class="fs-pages"><label><input type="radio" name="fee-draft-chapter" data-draft-chapter="custom" ${custom ? "checked" : ""}><span>按页码</span></label>
          <input type="number" min="1" max="${outline.page_count}" inputmode="numeric" data-draft-from value="${escapeHtml(state.from)}" aria-label="起始页"><span>到</span>
          <input type="number" min="1" max="${outline.page_count}" inputmode="numeric" data-draft-to value="${escapeHtml(state.to)}" aria-label="结束页"><span class="hb-meta">页</span>
          ${custom && state.from && state.to && !customOk ? `<span class="hb-meta" data-tone="warn">页码要在 1–${outline.page_count} 之间，最多 ${MAX_PAGES} 页</span>` : ""}</div>
        <div class="hb-actions"><button class="primary-btn hb-btn-sm" type="button" data-draft-start ${ready ? "" : "disabled"}><i data-feather="zap"></i><span>开始起草</span></button>
          <button class="hb-link" type="button" data-draft-close>取消</button>
          <span class="hb-meta">只起草“${escapeHtml(feeName)}”这一项，通常 1–3 分钟。</span></div>`;
    }

    function pickerHtml(feeName) {
      const body = state.loading ? `<p class="hb-meta">${escapeHtml(state.loading)}</p>` : state.pick === "chapter" && state.outline ? chapterHtml(feeName) : documentsHtml();
      return `<section class="fs-card fs-picker"><div class="fs-row"><b>从手册起草</b>${state.pick === "doc" ? '<button class="hb-link" type="button" data-draft-close>取消</button>' : ""}</div>
        ${state.error ? `<p class="hb-meta" data-tone="warn">${escapeHtml(state.error)}</p>` : ""}${body}</section>`;
    }

    // What ① shows for drafting: "progress" | "failed" | "picker" | "" (nothing to show).
    function mode() {
      if (state.job?.status === "RUNNING") return "progress";
      if (state.picking) return "picker";
      if (state.job?.status === "FAILED" && state.dismissed !== state.job.job_id) return "failed";
      return "";
    }

    function html(feeName, editable) {
      const current = mode();
      if (current === "progress") return progressHtml(state.job);
      if (current === "failed") return failureHtml(state.job, editable);
      if (current === "picker") return pickerHtml(feeName);
      return "";
    }

    // -- actions ----------------------------------------------------------------------------

    async function guarded(work) {
      state.error = "";
      try { await work(); } catch (error) { state.loading = ""; state.error = error.message || "操作没有完成"; }
      onChange("render");
    }

    async function openDocuments() {
      state.picking = true; state.pick = "doc"; state.loading = "正在读取知识来源里的 PDF……"; onChange("render");
      await guarded(async () => { state.documents = (await request(`${BASE}/documents`)).documents || []; state.loading = ""; });
    }

    async function openDocument(nodeToken) {
      state.picking = true; state.document = nodeToken; state.pick = "chapter"; state.chapter = null; state.outline = null;
      state.loading = "正在读取手册目录……第一次需要十几秒。"; onChange("render");
      await guarded(async () => { state.outline = await request(`${BASE}/outline?node_token=${encodeURIComponent(nodeToken)}`); state.loading = ""; });
      if (!state.outline) { state.pick = "doc"; if (!state.documents) await openDocuments(); }
    }

    async function start(body) {
      await guarded(async () => {
        const started = await request(`${BASE}/jobs`, { ...body, rule_id: state.ruleId });
        state.job = { job_id: started.job_id, status: "RUNNING", stage: STAGES[0], elapsed: 0, title: state.outline?.title || state.job?.title || "",
          ...body, rule_id: state.ruleId };
        state.picking = false;
      });
      poll();
    }

    function startChosen() {
      const outline = state.outline;
      const custom = state.chapter === "custom";
      const chapter = custom ? null : outline.chapters[state.chapter];
      return start({ node_token: state.document, start: custom ? Number(state.from) : chapter.start, end: custom ? Number(state.to) : chapter.end,
        chapter: custom ? "" : chapter.title });
    }

    async function handle(event) {
      const target = event.target.closest("button, input");
      if (!target) return false;
      const click = event.type === "click";
      if (target.dataset.draftDoc && click) await openDocument(target.dataset.draftDoc);
      else if (target.hasAttribute("data-draft-docs") && click) await openDocuments();
      else if (target.dataset.draftChapter !== undefined && event.type === "change") {
        state.chapter = target.dataset.draftChapter === "custom" ? "custom" : Number(target.dataset.draftChapter); onChange("render");
      } else if ((target.matches("[data-draft-from]") || target.matches("[data-draft-to]")) && event.type === "input") {
        const key = target.matches("[data-draft-from]") ? "from" : "to";
        state[key] = target.value; state.chapter = "custom"; onChange("render");
        const again = document.querySelector(`[data-draft-${key}]`); again?.focus();
      } else if (target.hasAttribute("data-draft-start") && click) await startChosen();
      else if (target.hasAttribute("data-draft-retry") && click) {
        const job = state.job;
        await start({ node_token: job.node_token, start: job.start, end: job.end, chapter: job.chapter || "" });
      } else if (target.hasAttribute("data-draft-rerange") && click) {
        state.dismissed = state.job?.job_id || ""; await openDocument(state.job?.node_token || "");
      } else if (target.hasAttribute("data-draft-manual") && click) {
        state.dismissed = state.job?.job_id || ""; state.picking = false; onChange("manual");
      } else if (target.hasAttribute("data-draft-close") && click) {
        state.picking = false; state.error = ""; onChange("render");
      } else if (target.hasAttribute("data-draft-cancel") && click) {
        await guarded(async () => { state.job = await request(`${BASE}/jobs/${encodeURIComponent(state.job.job_id)}/cancel`, {}); });
        state.dismissed = state.job?.job_id || "";
        window.clearTimeout(state.timer);
      } else return false;
      return true;
    }

    return {
      load, handle, html, mode, reset,
      job: () => state.job,
      openPicker: ({ nodeToken = "" } = {}) => (nodeToken ? openDocument(nodeToken) : openDocuments()),
      lastAttemptText: (job) => (job ? `${job.title || job.document_title || ""} ${range(job)}`.trim() : ""),
      formatTime,
    };
  }

  window.FeeDraft = { create };
})();
