(() => {
  "use strict";

  // 业务手册 → 试问: shows what the robot would reply. The Agent runs it read-only
  // under the person's own robot permissions; this page only sends the question.
  function init() {
    const root = document.querySelector("[data-handbook-try]");
    if (!root || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
    const $ = (selector) => root.querySelector(selector);
    let busy = false;

    function status(text, tone = "") {
      const node = $("[data-try-status]");
      node.textContent = text || "";
      node.dataset.tone = tone;
    }

    async function send() {
      const question = $("[data-try-question]").value.trim();
      if (!question || busy) return;
      busy = true;
      $("[data-try-send]").disabled = true;
      status("机器人正在回答，可能需要几十秒……");
      try {
        const response = await pageFetch("/handbook/try", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Browser-Request-UUID": window.crypto.randomUUID() },
          body: JSON.stringify({ question }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) throw new Error(data.message || data.error?.message || `HTTP ${response.status}`);
        const result = data.data || {};
        $("[data-try-reply]").textContent = result.reply || "";
        const steps = $("[data-try-steps]");
        steps.replaceChildren();
        (result.quotes || []).forEach((quote) => {
          const item = document.createElement("li");
          item.textContent = `引用原文：《${quote.title}》${quote.location || ""}`;
          steps.append(item);
        });
        (result.steps || []).forEach((step) => {
          const item = document.createElement("li");
          item.textContent = `${step.action}：${step.decision}`;
          steps.append(item);
        });
        if (!steps.children.length) {
          const item = document.createElement("li");
          item.textContent = "没有调用任何查询，模型直接回答。";
          steps.append(item);
        }
        $("[data-try-result]").hidden = false;
        status("");
      } catch (error) {
        status(error.message || "试问失败", "error");
      } finally {
        busy = false;
        $("[data-try-send]").disabled = false;
      }
    }

    $("[data-try-send]").addEventListener("click", send);
    $("[data-try-question]").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) send();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
