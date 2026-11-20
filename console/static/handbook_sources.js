(() => {
  "use strict";

  // 业务手册 → 知识来源 (frontend-spec 56): change checks and rule citations.
  // Scans and checks are decided by the Agent; the system only reminds and never edits rules.
  const KIND = { CHANGED: ["文档有改动", "warn"], NEW: ["新文档", "muted"] };

  function init() {
    const root = document.querySelector("[data-handbook-sources]");
    const page = document.querySelector("[data-handbook]");
    if (!root || !page || !window.HandbookUI || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const UI = window.HandbookUI;
    const { escapeHtml, formatTime, request } = UI;
    const { canEdit } = UI.role(page);
    const $ = (selector) => root.querySelector(selector);
    const feedback = UI.feedback($("[data-sources-status]"));
    const state = { data: null, busy: false, query: "" };

    function render() {
      const data = state.data || { documents: [], reminders: [] };
      const documents = data.documents || [];
      const reminders = data.reminders || [];
      const open = reminders.filter((item) => item.status === "OPEN");
      const byToken = new Map(documents.map((doc) => [doc.node_token, doc]));
      const cited = documents.filter((doc) => (doc.cited_by || []).length);
      const citations = new Set(documents.flatMap((doc) => doc.cited_by || []));
      const last = data.last_scan;
      UI.setTabCount("sources", open.length || null, open.length ? "warn" : "");

      $("[data-sources-last]").innerHTML = last
        ? `<span data-tone="${last.reminders ? "warn" : "ok"}">${last.reminders ? "!" : "✓"} 上次检查 ${escapeHtml(formatTime(last.at))} · ${last.baseline ? "首次检查只记录现状" : last.reminders ? `发现 ${last.reminders} 项变化` : "全部正常"}</span>`
        : "本次服务启动后还没有检查过；每天 08:30 自动检查";
      $("[data-sources-metrics]").innerHTML = [
        [cited.length, "篇", "引用文档", ""],
        [last && !last.baseline ? last.reminders : "—", "项", "本次变化", last?.reminders ? "warn" : ""],
        [open.length, "项", "待处理提醒", open.length ? "warn" : ""],
      ].map(([value, unit, label, tone]) => `<div class="hb-metric"><b data-tone="${tone}">${escapeHtml(value)}</b><span>${unit}</span><p>${label}</p></div>`).join("");
      $("[data-sources-scope]").textContent = `检查范围：${documents.length} 篇可读文档 · ${citations.size} 条规则引用`;

      const count = $("[data-reminder-count]");
      count.hidden = !open.length;
      const checkAll = $("[data-sources-check-all]");
      if (checkAll) checkAll.hidden = !open.length;
      count.textContent = `• ${open.length}`;
      const list = $("[data-sources-reminders]");
      list.innerHTML = reminders.length ? reminders.map((item) => {
        const [kindText, kindTone] = KIND[item.kind] || [item.kind, "muted"];
        const doc = byToken.get(item.node_token);
        const rules = doc?.cited_by || [];
        const checked = item.status !== "OPEN";
        return `<li class="hb-card hb-reminder" data-checked="${checked}">
          <div class="hb-reminder__top"><span class="hb-chip hb-chip--sm" data-tone="${kindTone}">${escapeHtml(kindText)}</span>
            <span class="hb-badge" data-tone="${checked ? "retired" : "pending"}">${checked ? "已检查" : "未检查"}</span></div>
          <h3>《${escapeHtml(item.title)}》</h3>
          <p>${escapeHtml(item.detail)}</p>
          <p class="hb-meta">${doc?.space ? `空间：${escapeHtml(doc.space)} · ` : ""}${escapeHtml(formatTime(item.detected_at))} 检查${checked && item.checked_at ? ` · ${escapeHtml(formatTime(item.checked_at))} 已检查` : ""}</p>
          <p class="hb-meta">${rules.length ? `<i class="hb-dot" data-tone="ok"></i>关联规则：${escapeHtml(rules.join("、"))}` : "尚未被规则引用"}</p>
          ${!checked && canEdit ? `<div class="hb-actions"><button class="ghost-btn hb-btn-sm" type="button" data-check="${item.reminder_id}">标记已检查</button>
            ${/\.pdf$/i.test(item.title || "") ? `<button class="ghost-btn hb-btn-sm" type="button" data-draft-from="${escapeHtml(item.node_token)}"><i data-feather="zap"></i><span>从这份文件起草</span></button>` : ""}</div>` : ""}</li>`;
      }).join("") : `<li class="hb-card hb-empty hb-empty--wide"><i data-feather="check-circle"></i><p>没有需要处理的变化。</p>${last ? `<span class="hb-meta">上次检查 ${escapeHtml(formatTime(last.at))}</span>` : ""}</li>`;

      const query = state.query.trim().toLowerCase();
      const visible = documents.filter((doc) => !query || `${doc.title} ${doc.space}`.toLowerCase().includes(query));
      $("[data-sources-doc-count]").textContent = `共 ${documents.length} 篇 · 机器人只读取有权限的飞书文档`;
      const newTokens = new Set(reminders.filter((item) => item.kind === "NEW" && item.status === "OPEN").map((item) => item.node_token));
      $("[data-sources-documents]").innerHTML = visible.length ? visible.map((doc) => {
        const rules = doc.cited_by || [];
        return `<tr><td><span class="hb-doc"><i data-feather="file-text"></i>《${escapeHtml(doc.title || "无标题")}》</span></td><td>${escapeHtml(doc.space || "")}</td>
          <td class="hb-num">${escapeHtml(formatTime(doc.edit_time))}</td>
          <td>${rules.length ? `<i class="hb-dot" data-tone="ok"></i>${escapeHtml(rules.join("、"))}` : '<span class="hb-meta">未被规则引用</span>'}${newTokens.has(doc.node_token) ? ' <span class="hb-badge" data-tone="pending">新</span>' : ""}</td></tr>`;
      }).join("") : `<tr><td colspan="4" class="hb-meta">${documents.length ? "没有符合的文档。" : "还没有检查记录：等每天 08:30 自动检查，或点“立即检查”。"}</td></tr>`;
      if (window.feather) window.feather.replace();
    }

    async function run(progress, work, success = "") {
      if (state.busy) return;
      state.busy = true;
      const unlock = UI.lockButtons(root);
      const scan = $("[data-sources-scan] span");
      if (scan && progress.startsWith("正在检查")) scan.textContent = "检查中…";
      feedback.progress(progress);
      try {
        await work();
        if (success) feedback.success(success); else feedback.clear();
      } catch (error) {
        feedback.error("没有完成", `${error.message || "操作失败"}。可以稍后重试。`);
      } finally {
        state.busy = false;
        if (scan) scan.textContent = "立即检查";
        unlock();
      }
    }

    const reload = async () => { state.data = await request("/handbook/sources"); render(); };

    root.addEventListener("input", (event) => {
      if (event.target.matches("[data-sources-search]")) { state.query = event.target.value; render(); }
    });
    root.addEventListener("click", (event) => {
      const target = event.target.closest("button");
      if (!target || target.disabled) return;
      if (target.dataset.draftFrom) {
        // 改版只看变化 (2026-11-20): open 计算规则 → 从手册起草 with this document chosen.
        document.querySelector("[data-handbook-tab='fees']")?.click();
        document.dispatchEvent(new CustomEvent("handbook:draft-from", { detail: { nodeToken: target.dataset.draftFrom } }));
        return;
      }
      if (target.hasAttribute("data-sources-scan")) {
        run("正在检查知识库，可能需要十几秒……", async () => { await request("/handbook/sources/scan", {}); await reload(); }, "检查完成");
      } else if (target.hasAttribute("data-sources-check-all")) {
        run("正在保存……", async () => { await request("/handbook/sources/reminders/check-all", {}); await reload(); }, "已全部标记已检查");
      } else if (target.dataset.check) {
        run("正在保存……", async () => { await request(`/handbook/sources/reminders/${encodeURIComponent(target.dataset.check)}/check`, {}); await reload(); }, "已标记已检查");
      }
    });
    // The list is read from the database only, so it also loads up front for the tab count.
    run("正在读取……", reload);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
