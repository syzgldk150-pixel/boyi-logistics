(() => {
  "use strict";

  // Review requests page (robot agent layer P4). The Agent re-checks and runs; this page only forwards.
  const STATUS_TEXT = { PENDING: "待处理", APPROVED: "执行中", REJECTED: "已拒绝", EXPIRED: "已过期", EXECUTED: "已执行", FAILED: "未执行成功" };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function init() {
    const root = document.querySelector("[data-reviews]");
    if (!root || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
    const $ = (selector) => root.querySelector(selector);
    let busy = false;

    function status(text, tone = "") {
      const node = $("[data-reviews-status]");
      node.textContent = text || "";
      node.dataset.tone = tone;
    }

    async function request(url, payload) {
      const options = payload === undefined ? { headers: { Accept: "application/json" } } : {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Browser-Request-UUID": window.crypto.randomUUID() },
        body: JSON.stringify(payload),
      };
      const response = await pageFetch(url, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) throw new Error(data.message || data.error?.message || `HTTP ${response.status}`);
      return data.data || {};
    }

    function render(reviews) {
      $("[data-reviews-rows]").innerHTML = reviews.length ? reviews.map((item) => `<tr>
        <td>${escapeHtml(item.short_code)}</td>
        <td>${escapeHtml(item.requester_label)}</td>
        <td>${escapeHtml(item.summary)}<br><small>${escapeHtml((item.created_at || "").replace("T", " ").slice(0, 16))} 提出</small></td>
        <td>${escapeHtml(STATUS_TEXT[item.status] || item.status)}</td>
        <td>${escapeHtml(item.result_text || item.decision_note || "")}</td>
        <td>${item.status === "PENDING" ? `<div class="handbook__actions">
          <button class="primary-btn" type="button" data-approve="${escapeHtml(item.request_id)}">同意并执行</button>
          <button class="ghost-btn" type="button" data-reject="${escapeHtml(item.request_id)}">拒绝</button></div>` : ""}</td>
      </tr>`).join("") : '<tr><td colspan="6">暂无复核请求。</td></tr>';
    }

    async function load() {
      render((await request("/settings/llm/reviews/items")).reviews || []);
    }

    async function run(label, work) {
      if (busy) return;
      busy = true;
      status(label);
      try { status((await work()) || ""); } catch (error) { status(error.message || "操作失败", "error"); } finally { busy = false; }
    }

    root.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      if (button.hasAttribute("data-reviews-refresh")) run("正在读取……", async () => { await load(); });
      if (button.dataset.approve) {
        if (!window.confirm("同意后会立即以发起人的身份执行一次，确认吗？")) return;
        run("正在执行……", async () => { const result = await request(`/settings/llm/reviews/${button.dataset.approve}/approve`, {}); await load(); return result.result_text || "已处理"; });
      }
      if (button.dataset.reject) {
        const note = window.prompt("拒绝原因（可留空）", "");
        if (note === null) return;
        run("正在处理……", async () => { await request(`/settings/llm/reviews/${button.dataset.reject}/reject`, { note }); await load(); });
      }
    });
    run("正在读取……", async () => { await load(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
