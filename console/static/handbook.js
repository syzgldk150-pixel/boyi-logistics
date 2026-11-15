(() => {
  "use strict";

  // Business handbook page (robot agent layer P2). The Agent validates every
  // compile, trial and approval; this page only shows and forwards.
  const STATUS_TEXT = { DRAFT: "草稿", ACTIVE: "已生效", PAUSED: "已暂停", RETIRED: "已作废" };
  const ACTION_TEXT = { none: "直接回答", ask_user: "反问", error: "调用失败", unknown: "未知动作" };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function init() {
    const root = document.querySelector("[data-handbook]");
    if (!root || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const base = "/settings/llm/handbook/entries";
    const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
    const $ = (selector) => root.querySelector(selector);
    const state = { entries: [], current: null, busy: false };

    function status(text, tone = "") {
      const node = $("[data-handbook-status]");
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

    async function run(label, work) {
      if (state.busy) return;
      state.busy = true;
      root.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      status(label);
      try {
        await work();
        status("");
      } catch (error) {
        status(error.message || "操作失败", "error");
      } finally {
        state.busy = false;
        root.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      }
    }

    function renderList() {
      const list = $("[data-handbook-entries]");
      list.innerHTML = state.entries.length ? state.entries.map((entry) => `<li>
        <button class="handbook__entry" type="button" data-entry="${escapeHtml(entry.entry_id)}" aria-current="${state.current?.entry_id === entry.entry_id}">
          <span>${escapeHtml(entry.title)}</span>
          <span class="handbook__badge" data-status="${escapeHtml(entry.status)}">${escapeHtml(STATUS_TEXT[entry.status] || entry.status)}</span>
        </button></li>`).join("") : '<li class="handbook__lead">还没有业务说明。</li>';
    }

    function latestVersion() {
      const entry = state.current;
      return entry ? entry.versions.find((item) => item.version === entry.latest_version) : null;
    }

    function actionTitle(id, title) {
      return ACTION_TEXT[id] || title || id;
    }

    function renderResult() {
      const node = $("[data-handbook-result]");
      const entry = state.current;
      const latest = latestVersion();
      $("[data-handbook-compile]").hidden = !entry;
      if (!entry || !latest) { node.hidden = true; node.innerHTML = ""; return; }
      const compiled = latest.compiled;
      const validation = latest.validation || {};
      const parts = [];
      if (validation.ok === false) {
        parts.push(`<div class="handbook__block"><h3>校验未通过</h3><ul>${(validation.errors || []).map((item) => `<li class="handbook__error">${escapeHtml(item)}</li>`).join("")}</ul><p>请修改说明后重新编译。</p></div>`);
      }
      if (compiled) {
        parts.push(`<div class="handbook__block"><h3>中文复述</h3><p>${escapeHtml(compiled.restatement)}</p></div>`);
        parts.push(`<div class="handbook__block"><h3>将生效的内容</h3><ul>${(latest.described || []).map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul></div>`);
        if (compiled.ambiguities?.length) {
          const acknowledged = new Set(latest.acknowledged || []);
          parts.push(`<div class="handbook__block"><h3>需要人确认的歧义</h3>
            ${compiled.ambiguities.map((item, index) => `<label class="handbook__check"><input type="checkbox" data-ack value="${index}" ${acknowledged.has(index) ? "checked" : ""}><span>${escapeHtml(item)}</span></label>`).join("")}
            <p class="handbook__lead">每一条都要么修改说明消除，要么勾选表示“已核实，按复述执行”。</p>
            <div class="handbook__actions"><button class="ghost-btn" type="button" data-handbook-ack>保存勾选</button></div></div>`);
        }
        if (compiled.unsupported?.length) {
          parts.push(`<div class="handbook__block"><h3 class="handbook__warn">开发需求（现有功能做不到，不会生效）</h3><ul>${compiled.unsupported.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`);
        }
        const trial = latest.trial;
        parts.push(`<div class="handbook__block"><h3>试跑</h3>
          <p class="handbook__lead">用说明里的说法问一遍 AI：生效前、生效后各会选择什么。只看选择，不执行任何操作。</p>
          ${trial ? `<p>${trial.passed} / ${trial.cases} 条按预期选择，其中 ${trial.improved} 条是这次说明带来的改进。</p>
            <table class="handbook__trial"><thead><tr><th>说法</th><th>应选</th><th>生效前</th><th>生效后</th></tr></thead><tbody>
            ${trial.rows.map((row) => `<tr><td>${escapeHtml(row.utterance)}</td><td>${escapeHtml(row.expected_title)}</td><td>${escapeHtml(actionTitle(row.before, row.before_title))}</td><td class="${row.pass ? "" : "handbook__error"}">${escapeHtml(actionTitle(row.after, row.after_title))}</td></tr>`).join("")}
            </tbody></table>` : ""}
          <div class="handbook__actions"><button class="ghost-btn" type="button" data-handbook-trial><i data-feather="play"></i><span>试跑</span></button></div></div>`);
      }
      const blockers = latest.blockers || [];
      const controls = [];
      if (latest.status === "COMPILED") {
        controls.push(blockers.length
          ? `<ul>${blockers.map((item) => `<li class="handbook__warn">${escapeHtml(item)}</li>`).join("")}</ul>`
          : '<button class="primary-btn" type="button" data-handbook-approve><i data-feather="check"></i><span>审核通过并生效</span></button>');
      }
      if (entry.status === "ACTIVE") controls.push('<button class="ghost-btn" type="button" data-handbook-status-action="pause">暂停</button>');
      if (entry.status === "PAUSED" && entry.active_version === entry.latest_version) controls.push('<button class="ghost-btn" type="button" data-handbook-status-action="resume">恢复生效</button>');
      controls.push('<button class="ghost-btn" type="button" data-handbook-status-action="retire">作废</button>');
      parts.push(`<div class="handbook__block"><h3>审核</h3><div class="handbook__actions">${controls.join("")}</div></div>`);
      node.innerHTML = parts.join("");
      node.hidden = false;
      if (window.feather) window.feather.replace();
    }

    function renderEditor() {
      const entry = state.current;
      const latest = latestVersion();
      $("[data-handbook-editor-title]").textContent = entry ? `${entry.title}（第 ${entry.latest_version} 版）` : "新建业务说明";
      const badge = $("[data-handbook-entry-status]");
      badge.hidden = !entry;
      if (entry) { badge.textContent = STATUS_TEXT[entry.status] || entry.status; badge.dataset.status = entry.status; }
      $("[data-handbook-title]").value = entry ? entry.title : "";
      $("[data-handbook-body]").value = latest ? latest.body : "";
      renderResult();
      renderList();
    }

    async function loadList() {
      state.entries = (await request(base)).entries || [];
      renderList();
    }

    async function openEntry(entryId) {
      state.current = await request(`${base}/${encodeURIComponent(entryId)}`);
      renderEditor();
    }

    root.addEventListener("click", (event) => {
      const target = event.target.closest("button");
      if (!target) return;
      const entry = state.current;
      const latest = latestVersion();
      if (target.dataset.entry) {
        run("正在读取……", () => openEntry(target.dataset.entry));
      } else if (target.hasAttribute("data-handbook-new")) {
        state.current = null;
        renderEditor();
      } else if (target.hasAttribute("data-handbook-save")) {
        const title = $("[data-handbook-title]").value;
        const body = $("[data-handbook-body]").value;
        if (entry?.status === "ACTIVE" && !window.confirm("这条说明正在生效，保存修改后会立即暂停，直到新版本审核通过。继续吗？")) return;
        run("正在保存……", async () => {
          if (entry) {
            await request(`${base}/${encodeURIComponent(entry.entry_id)}/save`, { title, body, row_version: entry.row_version });
            await openEntry(entry.entry_id);
          } else {
            const created = await request(base, { title, body });
            await openEntry(created.entry_id);
          }
          await loadList();
        });
      } else if (target.hasAttribute("data-handbook-compile") && entry) {
        run("AI 正在编译，通常需要几十秒……", async () => {
          await request(`${base}/${encodeURIComponent(entry.entry_id)}/compile`, {});
          await openEntry(entry.entry_id);
        });
      } else if (target.hasAttribute("data-handbook-trial") && entry) {
        run("正在试跑，每条说法会问 AI 两次……", async () => {
          await request(`${base}/${encodeURIComponent(entry.entry_id)}/trial`, {});
          await openEntry(entry.entry_id);
        });
      } else if (target.hasAttribute("data-handbook-ack") && entry && latest) {
        const indices = [...root.querySelectorAll("[data-ack]:checked")].map((input) => Number(input.value));
        run("正在保存……", async () => {
          await request(`${base}/${encodeURIComponent(entry.entry_id)}/acknowledge`, { version: latest.version, indices });
          await openEntry(entry.entry_id);
        });
      } else if (target.hasAttribute("data-handbook-approve") && entry && latest) {
        if (!window.confirm("确认按上面的中文复述生效？生效后飞书、微信和后台 AI 助手立即使用。")) return;
        run("正在生效……", async () => {
          await request(`${base}/${encodeURIComponent(entry.entry_id)}/approve`, { version: latest.version });
          await openEntry(entry.entry_id);
          await loadList();
        });
      } else if (target.dataset.handbookStatusAction && entry) {
        const action = target.dataset.handbookStatusAction;
        if (action === "retire" && !window.confirm("作废后这条说明不再显示，也不能恢复。继续吗？")) return;
        run("正在处理……", async () => {
          await request(`${base}/${encodeURIComponent(entry.entry_id)}/${action}`, { row_version: entry.row_version });
          if (action === "retire") { state.current = null; renderEditor(); } else { await openEntry(entry.entry_id); }
          await loadList();
        });
      }
    });

    run("正在读取……", loadList);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("console:navigated", init);
})();
