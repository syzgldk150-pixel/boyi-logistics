(() => {
  "use strict";

  // 业务手册 → 知识来源: lists documents and reminders; scans and checks are decided by the Agent.
  const KIND_TEXT = { CHANGED: "文档有改动", NEW: "新文档" };

  function formatTime(value) {
    const text = String(value || "");
    if (/^\d{10,13}$/.test(text)) {
      const date = new Date(Number(text.length === 10 ? text + "000" : text));
      return Number.isNaN(date.getTime()) ? text : date.toLocaleString("zh-CN", { hour12: false });
    }
    return text.slice(0, 16).replace("T", " ");
  }

  function init() {
    const root = document.querySelector("[data-handbook-sources]");
    const page = document.querySelector("[data-handbook]");
    if (!root || !page || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const canEdit = page.dataset.canEdit === "true";
    const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
    const $ = (selector) => root.querySelector(selector);
    let loaded = false;
    let busy = false;

    function status(text, tone = "") {
      const node = $("[data-sources-status]");
      node.textContent = text || "";
      node.dataset.tone = tone;
    }

    async function request(url, post) {
      const options = post ? { method: "POST", headers: { "Content-Type": "application/json",
        "X-Browser-Request-UUID": window.crypto.randomUUID() }, body: "{}" } : { headers: { Accept: "application/json" } };
      const response = await pageFetch(url, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) throw new Error(data.message || data.error?.message || `HTTP ${response.status}`);
      return data.data || {};
    }

    function cell(row, text) {
      const td = document.createElement("td");
      td.textContent = text;
      row.append(td);
    }

    function render(data) {
      const last = data.last_scan;
      $("[data-sources-last]").textContent = last
        ? `上次检查：${formatTime(last.at)}，${last.documents} 篇文档，${last.baseline ? "首次检查只记录现状" : `新提醒 ${last.reminders} 条`}`
        : "本次服务启动后还没有检查过；每天 08:30 自动检查。";
      const list = $("[data-sources-reminders]");
      list.replaceChildren();
      (data.reminders || []).forEach((item) => {
        const li = document.createElement("li");
        li.className = "sources__reminder";
        const text = document.createElement("span");
        text.textContent = `${KIND_TEXT[item.kind] || item.kind}《${item.title}》：${item.detail}（${formatTime(item.detected_at)}）`;
        li.append(text);
        if (item.status === "OPEN" && canEdit) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "ghost-btn";
          button.textContent = "已检查";
          button.dataset.check = String(item.reminder_id);
          li.append(button);
        } else {
          const badge = document.createElement("span");
          badge.className = "handbook__badge";
          badge.textContent = item.status === "OPEN" ? "待检查" : "已检查";
          li.append(badge);
        }
        list.append(li);
      });
      if (!list.children.length) {
        const li = document.createElement("li");
        li.className = "handbook__empty";
        li.textContent = "没有提醒。";
        list.append(li);
      }
      const body = $("[data-sources-documents]");
      body.replaceChildren();
      (data.documents || []).forEach((doc) => {
        const row = document.createElement("tr");
        cell(row, doc.title || "（无标题）");
        cell(row, doc.space || "");
        cell(row, formatTime(doc.edit_time));
        cell(row, (doc.cited_by || []).join("、") || "—");
        body.append(row);
      });
      if (!body.children.length) {
        const row = document.createElement("tr");
        cell(row, "还没有检查记录，点“立即检查”或等每天 08:30 自动检查。");
        body.append(row);
      }
    }

    async function run(label, work) {
      if (busy) return;
      busy = true;
      root.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      status(label);
      try {
        await work();
        status("");
      } catch (error) {
        status(error.message || "操作失败", "error");
      } finally {
        busy = false;
        root.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      }
    }

    const load = () => run("正在读取……", async () => render(await request("/handbook/sources")));
    root.addEventListener("click", (event) => {
      const target = event.target.closest("button");
      if (!target) return;
      if (target.hasAttribute("data-sources-scan")) {
        run("正在检查知识库，可能需要十几秒……", async () => {
          await request("/handbook/sources/scan", true);
          render(await request("/handbook/sources"));
        });
      } else if (target.dataset.check) {
        run("正在保存……", async () => {
          await request(`/handbook/sources/reminders/${encodeURIComponent(target.dataset.check)}/check`, true);
          render(await request("/handbook/sources"));
        });
      }
    });
    // Load when the tab is first shown, so other tabs do not wait on the knowledge base.
    const panelObserver = new MutationObserver(() => {
      if (!root.hidden && !loaded) { loaded = true; load(); }
    });
    panelObserver.observe(root, { attributes: true, attributeFilter: ["hidden"] });
    if (!root.hidden) { loaded = true; load(); }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
