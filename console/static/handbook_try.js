(() => {
  "use strict";

  // 业务手册 → 试问: shows what the robot would reply. The Agent runs it read-only
  // under the person's own robot permissions and returns the reply, the quoted
  // passages and the steps it took; this page only sends the question and shows them.
  function init() {
    const root = document.querySelector("[data-handbook-try]");
    if (!root || !window.HandbookUI || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const { escapeHtml, request } = window.HandbookUI;
    const $ = (selector) => root.querySelector(selector);
    const question = $("[data-try-question]");
    let busy = false;

    function renderAnswer(result) {
      const quotes = result.quotes || [];
      const steps = result.steps || [];
      $("[data-try-result]").innerHTML = `
        <div class="hb-reply">${escapeHtml(result.reply || "（机器人没有给出回复）")}</div>
        ${quotes.length ? `<div class="hb-block"><h3>引用的原文</h3><div class="hb-quotes">${quotes.map((quote) => `<div class="hb-quote-card">
          <b><i data-feather="file-text"></i>《${escapeHtml(quote.title)}》</b><span class="hb-meta">${escapeHtml(quote.location || "")}${(quote.pages || []).length ? ` · 第 ${escapeHtml(quote.pages.join("、"))} 页` : ""}</span>
          ${quote.url ? `<a class="hb-link" href="${escapeHtml(quote.url)}" target="_blank" rel="noopener noreferrer"><i data-feather="external-link"></i>查看原文</a>` : ""}</div>`).join("")}</div></div>` : ""}
        <div class="hb-block"><h3>执行步骤</h3>
          ${steps.length ? `<ol class="hb-timeline">${steps.map((step, index) => `<li><span class="hb-timeline__no">${index + 1}</span><div><b>${escapeHtml(step.action)}</b></div>
            <span class="hb-meta" data-tone="${step.decision === "执行" ? "ok" : step.decision === "反问" ? "warn" : ""}">${escapeHtml(step.decision)}</span></li>`).join("")}</ol>`
            : '<p class="hb-meta">没有调用查询或计算，模型直接回答。</p>'}</div>`;
      if (window.feather) window.feather.replace();
    }

    async function send() {
      const text = question.value.trim();
      if (!text || busy) return;
      busy = true;
      const button = $("[data-try-send]");
      const status = $("[data-try-status]");
      button.disabled = true;
      status.dataset.tone = "";
      status.textContent = "机器人正在回答，可能需要几十秒……";
      try {
        renderAnswer(await request("/handbook/try", { question: text }));
        status.textContent = "";
      } catch (error) {
        status.dataset.tone = "danger";
        status.textContent = `试问失败：${error.message || "请稍后重试"}`;
      } finally {
        busy = false;
        button.disabled = false;
      }
    }

    question.addEventListener("input", () => { $("[data-try-counter]").textContent = `${question.value.length} / 500`; });
    question.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
    });
    $("[data-try-send]").addEventListener("click", send);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
