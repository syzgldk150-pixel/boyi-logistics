(() => {
  "use strict";

  // 业务手册 → 试问 (frontend-spec 57): one question, the reply, the quoted
  // passages and the steps. The Agent runs it read-only under the person's own
  // robot permissions; this page only sends the question and shows the answer.
  function init() {
    const root = document.querySelector("[data-handbook-try]");
    if (!root || !window.HandbookUI || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const { escapeHtml, request } = window.HandbookUI;
    const $ = (selector) => root.querySelector(selector);
    const recent = [];
    let busy = false;
    let timer = null;
    let lastReply = "";

    const question = $("[data-try-question]");
    const updateCounter = () => { $("[data-try-counter]").textContent = `${question.value.length} / 500`; };

    function renderRecent() {
      $("[data-try-recent-block]").hidden = !recent.length;
      $("[data-try-recent]").innerHTML = recent.map((item, index) => `<li><button class="hb-link" type="button" data-try-recent-item="${index}">${escapeHtml(item.text)}</button><span class="hb-meta">${escapeHtml(item.time)}</span></li>`).join("");
    }

    function renderAnswer(result, seconds) {
      const quotes = result.quotes || [];
      const steps = result.steps || [];
      lastReply = result.reply || "";
      $("[data-try-copy]").hidden = !lastReply;
      const timeline = [
        { title: "理解问题", detail: "按你本人的机器人权限、当前生效的业务手册理解", tone: "执行" },
        ...steps.map((step) => ({ title: step.action, detail: step.decision === "反问" ? "已执行反问：信息不全，先问清楚" : `结果：${step.decision}`, tone: step.decision })),
        ...(quotes.length ? [{ title: "检索知识库", detail: `引用 ${quotes.length} 段原文`, tone: "执行" }] : []),
        { title: "权限检查", detail: "未执行插件 / 未写入 / 未发送", tone: "未执行" },
      ];
      $("[data-try-result]").innerHTML = `
        <p class="hb-meta">用时 ${seconds} 秒 · ${steps.length ? `${steps.length} 个查询或计算步骤` : "没有调用查询，模型直接回答"}</p>
        <div class="hb-reply">${escapeHtml(lastReply || "（机器人没有给出回复）")}</div>
        ${quotes.length ? `<div class="hb-block"><h3>引用的原文</h3><div class="hb-quotes">${quotes.map((quote) => `<div class="hb-quote-card">
          <b><i data-feather="file-text"></i>《${escapeHtml(quote.title)}》</b><span class="hb-meta">${escapeHtml(quote.location || "")}${(quote.pages || []).length ? ` · 第 ${escapeHtml(quote.pages.join("、"))} 页` : ""}</span>
          ${quote.url ? `<a class="hb-link" href="${escapeHtml(quote.url)}" target="_blank" rel="noopener noreferrer"><i data-feather="external-link"></i>查看原文</a>` : ""}</div>`).join("")}</div></div>` : ""}
        <div class="hb-block"><h3>执行步骤 <span class="hb-badge" data-tone="active">只读 · 未执行</span></h3>
          <ol class="hb-timeline">${timeline.map((item, index) => `<li><span class="hb-timeline__no">${index + 1}</span><div><b>${escapeHtml(item.title)}</b><span class="hb-meta">${escapeHtml(item.detail)}</span></div><span class="hb-meta" data-tone="${item.tone === "执行" ? "ok" : item.tone === "反问" ? "warn" : ""}">${escapeHtml(item.tone)}</span></li>`).join("")}</ol></div>
        <div class="hb-readonly"><span><i data-feather="lock"></i>未执行插件</span><span><i data-feather="edit-3"></i>未写入数据</span><span><i data-feather="arrow-right"></i>未发送消息</span></div>
        <p class="hb-meta" data-tone="ok"><i data-feather="check-circle"></i>本次试问未写入任何数据，也未发送到飞书 / 微信。</p>`;
      if (window.feather) window.feather.replace();
    }

    async function send() {
      const text = question.value.trim();
      if (!text || busy) return;
      busy = true;
      const button = $("[data-try-send]");
      button.disabled = true;
      const started = Date.now();
      const status = $("[data-try-status]");
      status.dataset.tone = "";
      status.textContent = "机器人正在回答 0s";
      timer = window.setInterval(() => { status.textContent = `机器人正在回答 ${Math.round((Date.now() - started) / 1000)}s`; }, 1000);
      try {
        const result = await request("/handbook/try", { question: text });
        const seconds = ((Date.now() - started) / 1000).toFixed(1);
        renderAnswer(result, seconds);
        recent.unshift({ text, time: new Date().toTimeString().slice(0, 5) });
        recent.splice(3);
        renderRecent();
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

    question.addEventListener("input", updateCounter);
    question.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
    });
    root.addEventListener("click", async (event) => {
      const target = event.target.closest("button");
      if (!target) return;
      if (target.hasAttribute("data-try-send")) send();
      else if (target.hasAttribute("data-try-example")) { question.value = target.textContent.trim(); updateCounter(); question.focus(); }
      else if (target.dataset.tryRecentItem) { question.value = recent[Number(target.dataset.tryRecentItem)]?.text || ""; updateCounter(); question.focus(); }
      else if (target.hasAttribute("data-try-copy") && lastReply) {
        try { await navigator.clipboard.writeText(lastReply); target.lastChild.textContent = "已复制"; window.setTimeout(() => { target.lastChild.textContent = "复制回答"; }, 2000); } catch (_error) { /* clipboard blocked */ }
      }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
