(() => {
  "use strict";

  // 业务手册 → 试问 (frontend-spec 57): shows what the robot would reply. The Agent runs it
  // read-only under the person's own robot permissions and returns the reply, the quoted
  // passages, the six checkable steps it derived from what the turn did, and the time taken.
  // Recent questions (the question text only, never answers) stay in this browser.
  const RECENT_KEY = "handbook.try.recent";
  const STATE_TEXT = { done: "执行", skipped: "未触发", blocked: "未执行" };

  function init() {
    const root = document.querySelector("[data-handbook-try]");
    if (!root || !window.HandbookUI || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const { escapeHtml, request } = window.HandbookUI;
    const $ = (selector) => root.querySelector(selector);
    const question = $("[data-try-question]");
    let busy = false;
    let lastReply = "";

    function recent() {
      try { return JSON.parse(window.localStorage.getItem(RECENT_KEY) || "[]").filter((item) => typeof item === "string").slice(0, 3); } catch (_error) { return []; }
    }

    function remember(text) {
      const list = [text, ...recent().filter((item) => item !== text)].slice(0, 3);
      try { window.localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (_error) { /* optional */ }
      renderRecent();
    }

    function renderRecent() {
      const list = recent();
      $("[data-try-recent-block]").hidden = !list.length;
      $("[data-try-recent]").innerHTML = list.map((item) => `<li><button class="hb-link" type="button" data-try-recent-item>${escapeHtml(item)}</button></li>`).join("");
    }

    function renderAnswer(result) {
      const quotes = result.quotes || [];
      const steps = result.steps || [];
      lastReply = result.reply || "";
      $("[data-try-copy]").hidden = !lastReply;
      const meta = [result.handbook_entries != null ? `基于 ${result.handbook_entries} 条已生效说明` : "", quotes.length ? `引用原文 ${quotes.length} 段` : "",
        result.seconds != null ? `用时 ${result.seconds} 秒` : ""].filter(Boolean).join(" · ");
      $("[data-try-result]").innerHTML = `
        ${meta ? `<p class="hb-meta">${escapeHtml(meta)}</p>` : ""}
        <div class="hb-reply">${escapeHtml(result.reply || "（机器人没有给出回复）")}</div>
        ${quotes.length ? `<div class="hb-block"><h3>引用的原文</h3><div class="hb-quotes">${quotes.map((quote) => `<div class="hb-quote-card">
          <b><i data-feather="file-text"></i>《${escapeHtml(quote.title)}》</b><span class="hb-meta">${escapeHtml(quote.location || "")}${(quote.pages || []).length ? ` · 第 ${escapeHtml(quote.pages.join("、"))} 页` : ""}</span>
          ${quote.url ? `<a class="hb-link" href="${escapeHtml(quote.url)}" target="_blank" rel="noopener noreferrer"><i data-feather="external-link"></i>查看原文</a>` : ""}</div>`).join("")}</div></div>` : ""}
        <div class="hb-block"><h3>执行步骤</h3>
          <ol class="hb-timeline">${steps.map((step, index) => `<li data-state="${escapeHtml(step.state)}"><span class="hb-timeline__no">${index + 1}</span>
            <div><b>${escapeHtml(step.title)}</b><span class="hb-meta">${escapeHtml(step.result)}</span></div>
            <span class="hb-meta" data-tone="${step.state === "done" ? "ok" : step.state === "blocked" ? "warn" : ""}">${STATE_TEXT[step.state] || escapeHtml(step.state)}</span></li>`).join("")}</ol></div>
        <div class="hb-try-summary"><span><i data-feather="slash"></i>未执行插件</span><span><i data-feather="slash"></i>未写入数据</span><span><i data-feather="slash"></i>未发送消息</span></div>
        <p class="hb-alert" data-tone="ok"><i data-feather="check-circle"></i><span>本次试问未写入任何数据，也未发送到飞书 / 微信。</span></p>`;
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
      const started = Date.now();
      const tick = () => { status.textContent = `机器人正在回答… ${Math.round((Date.now() - started) / 1000)} 秒`; };
      tick();
      const timer = window.setInterval(tick, 1000);
      try {
        renderAnswer(await request("/handbook/try", { question: text }));
        remember(text);
        status.textContent = "";
      } catch (error) {
        status.dataset.tone = "danger";
        status.textContent = `试问失败：${error.message || "请稍后重试"}`;
      } finally {
        window.clearInterval(timer);
        busy = false;
        button.disabled = false;
      }
    }

    function fill(text) {
      question.value = text;
      $("[data-try-counter]").textContent = `${question.value.length} / 500`;
      question.focus();
    }

    question.addEventListener("input", () => { $("[data-try-counter]").textContent = `${question.value.length} / 500`; });
    question.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
    });
    root.addEventListener("click", async (event) => {
      const target = event.target.closest("button");
      if (!target) return;
      if (target.hasAttribute("data-try-send")) send();
      else if (target.hasAttribute("data-try-example") || target.hasAttribute("data-try-recent-item")) fill(target.textContent.trim());
      else if (target.hasAttribute("data-try-copy") && lastReply) {
        try { await navigator.clipboard.writeText(lastReply); $("[data-try-status]").textContent = "已复制回答"; } catch (_error) { $("[data-try-status]").textContent = "浏览器不允许复制，请手动选中文字复制"; }
      }
    });
    renderRecent();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
