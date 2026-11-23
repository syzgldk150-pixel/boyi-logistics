(() => {
  "use strict";

  // 业务手册 shared parts (frontend-spec 58): permission chip, tabs with counts,
  // status badges, in-page feedback and the confirmation dialog. The Console and
  // the Agent enforce every permission; this file only decides what to show.
  if (window.HandbookUI) return;

  const ENTRY_STATUS = {
    DRAFT: ["草稿", "draft"], PENDING: ["待审核", "pending"], ACTIVE: ["已生效", "active"],
    PAUSED: ["已暂停", "paused"], RETIRED: ["已作废", "retired"],
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function badge(status, text) {
    const [label, tone] = ENTRY_STATUS[status] || [text || status, "draft"];
    return `<span class="hb-badge" data-tone="${tone}">${escapeHtml(text || label)}</span>`;
  }

  function formatTime(value, { withYear = false } = {}) {
    const text = String(value || "");
    if (!text) return "";
    let date = null;
    if (/^\d{10,13}$/.test(text)) date = new Date(Number(text.length === 10 ? `${text}000` : text));
    else if (/T|Z|\+/.test(text)) date = new Date(text.endsWith("Z") || /[+-]\d\d:?\d\d$/.test(text) ? text : `${text}Z`);
    if (!date || Number.isNaN(date.getTime())) return text.slice(0, 16).replace("T", " ");
    const pad = (number) => String(number).padStart(2, "0");
    const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
    return withYear ? `${date.getFullYear()}-${day}` : day;
  }

  function pageFetch() {
    return window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
  }

  async function request(url, payload) {
    const options = payload === undefined ? { headers: { Accept: "application/json" } } : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Browser-Request-UUID": window.crypto.randomUUID() },
      body: JSON.stringify(payload),
    };
    const response = await pageFetch()(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const error = new Error(data.message || data.error?.message || (response.status === 403 ? "当前身份没有这项权限。" : `请求失败（${response.status}）`));
      error.status = response.status;
      throw error;
    }
    return data.data || {};
  }

  // In-page feedback bar: progress stays until done, success fades after 3 s,
  // errors stay on the page until the next action (spec 58 操作反馈).
  function feedback(node) {
    let timer = null;
    let started = 0;
    function show(tone, title, detail = "", action = "") {
      window.clearInterval(timer);
      timer = null;
      if (!node) return;
      if (!title) { node.hidden = true; node.innerHTML = ""; return; }
      node.hidden = false;
      node.dataset.tone = tone;
      node.innerHTML = `<i data-feather="${{ progress: "loader", success: "check-circle", error: "alert-triangle", info: "lock" }[tone] || "info"}"></i>
        <div class="hb-feedback__text"><b>${escapeHtml(title)}</b>${detail ? `<span>${escapeHtml(detail)}</span>` : ""}
        ${tone === "progress" ? '<span class="hb-feedback__bar"><span></span></span>' : ""}</div>
        ${tone === "progress" ? '<span class="hb-feedback__timer" data-timer>0s</span>' : ""}${action}`;
      if (window.feather) window.feather.replace();
      if (tone === "progress") {
        started = Date.now();
        timer = window.setInterval(() => {
          const out = node.querySelector("[data-timer]");
          if (out) out.textContent = `${Math.round((Date.now() - started) / 1000)}s`;
        }, 1000);
      } else if (tone === "success") {
        timer = window.setTimeout(() => { if (node.dataset.tone === "success") show("", ""); }, 3000);
      }
    }
    return {
      progress: (title, detail) => show("progress", title, detail),
      success: (title, detail) => show("success", title, detail),
      error: (title, detail) => show("error", title, detail),
      info: (title, detail) => show("info", title, detail),
      clear: () => show("", ""),
    };
  }

  // One shared confirmation dialog. Buttons say what happens; never a bare “确定”.
  // With ``input`` it also asks for a short text and resolves to that text (or false when cancelled).
  let dialog = null;
  function confirmAction({ title, body, confirm, danger = false, cancel = "取消", input = null }) {
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.className = "hb-dialog";
      dialog.setAttribute("aria-labelledby", "hb-dialog-title");
      document.body.append(dialog);
    }
    const field = input ? `<label class="hb-dialog__field"><span>${escapeHtml(input.label || "")}</span>
        <textarea rows="3" maxlength="${Number(input.maxLength) || 200}" data-dialog-input></textarea></label>` : "";
    dialog.innerHTML = `<form method="dialog">
      <div class="hb-dialog__body"><h2 id="hb-dialog-title">${escapeHtml(title)}</h2><p>${escapeHtml(body)}</p>${field}</div>
      <div class="hb-dialog__foot"><button class="ghost-btn" value="cancel">${escapeHtml(cancel)}</button>
        <button class="${danger ? "hb-btn-danger" : "primary-btn"}" value="ok">${escapeHtml(confirm)}</button></div></form>`;
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        const ok = dialog.returnValue === "ok";
        if (!input) { resolve(ok); return; }
        resolve(ok ? String(dialog.querySelector("[data-dialog-input]")?.value || "").trim() : false);
      }, { once: true });
      dialog.returnValue = "";
      if (typeof dialog.showModal === "function") dialog.showModal();
      else if (input) { const value = window.prompt(`${title}\n${body}`, ""); resolve(value === null ? false : value.trim()); }
      else resolve(window.confirm(`${title}\n${body}`));
    });
  }

  function setTabCount(tab, count, tone = "") {
    const node = document.querySelector(`[data-handbook-tab="${tab}"] [data-tab-count]`);
    if (!node) return;
    node.hidden = count === null || count === undefined;
    node.textContent = String(count ?? "");
    node.dataset.tone = tone;
  }

  function initTabs(page) {
    if (page.dataset.tabsBound === "true") return;
    page.dataset.tabsBound = "true";
    const tabs = [...page.querySelectorAll("[data-handbook-tab]")];
    function select(tab) {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
        document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
      });
      // 说明条目 sits under “更多” (2026-11-22): close the menu and mark it while one of its tabs is open.
      const more = page.querySelector("[data-handbook-more]");
      if (more) { more.open = false; more.dataset.selected = String(more.contains(tab)); }
      try { window.localStorage.setItem("handbook.tab", tab.dataset.handbookTab); } catch (_error) { /* optional */ }
    }
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => select(tab));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        const next = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        select(next);
        next.focus();
      });
    });
    let remembered = null;
    try { remembered = window.localStorage.getItem("handbook.tab"); } catch (_error) { /* optional */ }
    const initial = tabs.find((tab) => tab.dataset.handbookTab === remembered);
    if (initial) select(initial);
  }

  // Disables every button while an action runs and restores only those it
  // disabled; buttons rendered during the action keep their own state.
  function lockButtons(root) {
    const locked = [...root.querySelectorAll("button:not([disabled])")];
    locked.forEach((button) => { button.disabled = true; });
    return () => locked.forEach((button) => { if (button.isConnected) button.disabled = false; });
  }

  function role(page) {
    return { canEdit: page.dataset.canEdit === "true", superAdmin: page.dataset.superAdmin === "true" };
  }

  window.HandbookUI = { escapeHtml, badge, formatTime, request, feedback, confirmAction, setTabCount, initTabs, role, lockButtons };

  function boot() {
    const page = document.querySelector("[data-handbook]");
    if (page) initTabs(page);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
  document.addEventListener("console:navigated", boot);
})();
