(() => {
  function initCustomerServiceWorkbench() {
    document.querySelectorAll("[data-customer-service-workbench]").forEach((root) => initCustomerServiceRoot(root));
  }

  function initCustomerServiceRoot(root) {
    if (!root || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const fetch = window.ConsoleUI.pageRequest();

    const parseData = (name, fallback) => {
      try {
        return JSON.parse(root.dataset[name] || "");
      } catch {
        return fallback;
      }
    };
    const DEFAULT_REPLY_STATUS = "已处理";
    const ATTACHMENT_ORIGINS = {
      ronghui: "https://tms.ronghuiwl.com",
      yunda: "https://kyproblem.yunda56.com",
    };
    const YUNDA_ATTACHMENT_PUBLIC_ROOT = "https://kyproblem.yunda56.com/ky_problem/public/index.php";
    const settings = parseData("settings", {});
    const accounts = parseData("accounts", []);
    const accountById = new Map(accounts.map((account) => [String(account.account_id || ""), account]));
    const state = { rows: [], selected: null, selectedDetails: [], querying: false, queryRequest: 0, replying: false, errors: [],
      ai: { request: 0, statusRequest: 0, suggestionId: null, suggestion: null, status: new Map() } };
    const seenKey = "shipnow.customerService.seenKeys";
    const seen = new Set(JSON.parse(localStorage.getItem(seenKey) || "[]"));

    const $ = (selector) => root.querySelector(selector);
    const $$ = (selector) => Array.from(root.querySelectorAll(selector));
    const tableBody = $("[data-cs-table-body]");
    const statusNode = $("[data-cs-status]");
    const queryButton = document.querySelector("[data-cs-query]");
    const queryButtonLabel = queryButton?.querySelector("span");
    const totalNode = $("[data-cs-total]");
    const newNode = $("[data-cs-new]");
    const errorsNode = $("[data-cs-errors]");
    const problemModal = $("[data-cs-problem-modal]");
    const problemTitle = $("[data-cs-problem-title]");
    const problemPlatform = $("[data-cs-problem-platform]");
    const problemContent = $("[data-cs-problem-content]");
    const problemAttachments = $("[data-cs-problem-attachments]");
    const problemMeta = $("[data-cs-problem-meta]");
    const imageViewer = $("[data-cs-image-viewer]");
    const imageViewerImg = $("[data-cs-image-viewer-img]");
    const publishModal = $("[data-cs-publish-modal]");
    const settingsPanel = $("[data-cs-settings-panel]");
    const settingsToggle = $("[data-cs-settings-toggle]");
    const accountSummary = $("[data-cs-account-summary]");
    const errorToggle = $("[data-cs-error-toggle]");
    const errorLabel = $("[data-cs-error-label]");
    const errorPanel = $("[data-cs-error-panel]");
    const errorList = $("[data-cs-error-list]");
    const settingsAutosave = $("[data-cs-settings-autosave]");
    const dateRange = $("[data-cs-date-range]");
    const dateRangeToggle = $("[data-cs-date-range-toggle]");
    const dateRangeLabel = $("[data-cs-date-range-label]");
    const datePopover = $("[data-cs-date-popover]");
    const dateFromInput = $("[data-cs-date-from]");
    const dateToInput = $("[data-cs-date-to]");
    const calendarTitle = $("[data-cs-calendar-title]");
    const calendarGrid = $("[data-cs-calendar-grid]");
    const calendarPrev = $("[data-cs-calendar-prev]");
    const calendarNext = $("[data-cs-calendar-next]");
    let settingsSaveTimer = null;

    function formatDate(date) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    }

    function parseDateKey(value) {
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
      if (!match) return null;
      const year = Number(match[1]);
      const month = Number(match[2]) - 1;
      const day = Number(match[3]);
      const date = new Date(year, month, day);
      if (date.getFullYear() !== year || date.getMonth() !== month || date.getDate() !== day) return null;
      return date;
    }

    const addDays = (date, days) => new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
    const startOfMonth = (date) => new Date(date.getFullYear(), date.getMonth(), 1);
    let calendarMonth = startOfMonth(parseDateKey(dateFromInput?.value) || parseDateKey(dateToInput?.value) || new Date());

    function setStatus(text, tone = "") {
      if (!statusNode) return;
      statusNode.textContent = text;
      statusNode.dataset.tone = tone;
    }

    function setQueryBusy(isBusy) {
      if (!queryButton) return;
      queryButton.disabled = isBusy;
      queryButton.setAttribute("aria-busy", String(isBusy));
      const icon = queryButton.querySelector("[data-feather]");
      if (queryButtonLabel) {
        queryButtonLabel.dataset.originalText = queryButtonLabel.dataset.originalText || queryButtonLabel.textContent || "查询";
        queryButtonLabel.textContent = isBusy ? "查询中" : queryButtonLabel.dataset.originalText;
      }
      if (icon) {
        icon.setAttribute("data-feather", isBusy ? "loader" : "refresh-cw");
        if (window.feather) window.feather.replace();
      }
    }

    function selectedAccounts() {
      const platform = $("[data-cs-platform]")?.value || "all";
      return $$("[data-cs-account]:checked")
        .filter((input) => platform === "all" || input.dataset.system === platform)
        .map((input) => input.value);
    }

    function selectedAccountTotal() {
      return $$("[data-cs-account]:checked").length;
    }

    function updateAccountSummary() {
      if (!accountSummary) return;
      const count = selectedAccountTotal();
      const interval = Number($("[data-cs-poll-interval]")?.value || settings.poll_interval_sec || 60);
      accountSummary.textContent = `账号 ${count} 个 · ${interval} 秒`;
    }

    function setSettingsAutosave(text, tone = "") {
      if (!settingsAutosave) return;
      settingsAutosave.textContent = text;
      settingsAutosave.dataset.tone = tone;
    }

    function renderDateRangeLabel() {
      if (!dateRangeLabel) return;
      const from = dateFromInput?.value || "";
      const to = dateToInput?.value || "";
      dateRangeLabel.textContent = from && to ? `${from} 至 ${to}` : "选择日期范围";
    }

    function closeDatePopover() {
      if (!datePopover) return;
      datePopover.hidden = true;
      dateRangeToggle?.setAttribute("aria-expanded", "false");
    }

    function openDatePopover() {
      if (!datePopover) return;
      datePopover.hidden = false;
      dateRangeToggle?.setAttribute("aria-expanded", "true");
      renderCalendar();
    }

    function setDateRange(from, to, options = {}) {
      let nextFrom = from || "";
      let nextTo = to || "";
      if (nextFrom && nextTo && nextTo < nextFrom) {
        [nextFrom, nextTo] = [nextTo, nextFrom];
      }
      if (dateFromInput) dateFromInput.value = nextFrom;
      if (dateToInput) dateToInput.value = nextTo;
      const baseDate = parseDateKey(nextFrom || nextTo);
      if (baseDate && options.syncMonth !== false) {
        calendarMonth = startOfMonth(baseDate);
      }
      renderDateRangeLabel();
      renderCalendar();
      if (options.closeOnComplete && nextFrom && nextTo) closeDatePopover();
    }

    function handleCalendarDayClick(dateKey) {
      const from = dateFromInput?.value || "";
      const to = dateToInput?.value || "";
      if (!from || to) {
        setDateRange(dateKey, "");
        return;
      }
      setDateRange(from, dateKey, { closeOnComplete: true, syncMonth: false });
    }

    function renderCalendar() {
      if (!calendarGrid) return;
      const monthStart = startOfMonth(calendarMonth);
      const gridStart = addDays(monthStart, -((monthStart.getDay() + 6) % 7));
      const from = dateFromInput?.value || "";
      const to = dateToInput?.value || "";
      if (calendarTitle) calendarTitle.textContent = `${calendarMonth.getMonth() + 1}月 ${calendarMonth.getFullYear()}`;
      calendarGrid.innerHTML = "";
      for (let index = 0; index < 42; index += 1) {
        const date = addDays(gridStart, index);
        const key = formatDate(date);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "customer-service-calendar-day";
        button.textContent = String(date.getDate());
        button.setAttribute("data-cs-calendar-day", key);
        button.setAttribute("aria-label", key);
        if (date.getMonth() !== calendarMonth.getMonth()) button.classList.add("is-outside");
        if (from && to && key > from && key < to) button.classList.add("is-in-range");
        if (from && key === from) button.classList.add("is-start");
        if (to && key === to) button.classList.add("is-end");
        button.addEventListener("click", () => handleCalendarDayClick(key));
        calendarGrid.appendChild(button);
      }
    }

    function selectedPlatforms() {
      const platform = $("[data-cs-platform]")?.value || "all";
      if (platform !== "all") return [platform];
      return ["ronghui", "yunda"];
    }

    function isRootActive() {
      if (!root.isConnected) return false;
      if (root.closest("[hidden]")) return false;
      const page = root.closest(".main-content");
      return !page || !page.hidden;
    }

    function normalizeDirectionFilter(value) {
      const direction = String(value || "").trim();
      if (["registered", "published", "publish", "issue"].includes(direction)) return "my_published";
      if (["received", "receive", "inbox", "query"].includes(direction)) return "published_to_me";
      return direction || "published_to_me";
    }

    function filters() {
      const q = $("[data-cs-q]")?.value.trim() || "";
      return {
        direction: normalizeDirectionFilter($("[data-cs-direction]")?.value),
        q,
        waybill_no: q,
        date_from: $("[data-cs-date-from]")?.value || "",
        date_to: $("[data-cs-date-to]")?.value || "",
        page: 1,
        rows: 80,
        page_size: 80,
      };
    }

    function newBrowserRequestUuid() {
      if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
        throw new Error("当前浏览器无法生成安全的请求标识，本次操作未执行。");
      }
      return window.crypto.randomUUID();
    }

    async function postJson(url, payload, options = {}) {
      const headers = { "Content-Type": "application/json" };
      if (options.command === true) {
        headers["X-Browser-Request-UUID"] = options.requestUuid || newBrowserRequestUuid();
      }
      const response = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(payload || {}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.message || data.error?.message || data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    function assertActionSucceeded(data, fallbackMessage) {
      if (data && data.ok === false) {
        throw new Error(data.message || data.error || fallbackMessage || "操作失败");
      }
      return data;
    }

    function problemKey(row) {
      return [row.platform, row.account_id, row.source_direction, row.external_id].filter(Boolean).join(":");
    }

    function escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function displayAccount(row) {
      const account = accountById.get(String(row.account_id || "")) || {};
      return (
        row.account_login ||
        row.login_account ||
        account.login_account ||
        row.account_no ||
        account.account_no ||
        row.account_id ||
        row.account_label ||
        ""
      );
    }

    function rowProblemType(row) {
      return (
        sourceValue(row, ["problem_type", "prod_typ", "TYPE", "OWNER_PROBELM_TYPE", "damage_type", "classes_type"]) ||
        sourceValue(row.raw || {}, [
          "problem_type",
          "prod_typ",
          "TYPE",
          "OWNER_PROBELM_TYPE",
          "damage_type",
          "classes_type",
          "problem_type_classes",
        ]) ||
        "-"
      );
    }

    function problemStatusText(row) {
      return [
        cleanText(row?.status),
        sourceValue(row, ["prob_status", "reply_status", "BL_CHECKOK", "BL_CHECKOK_STR", "check_status", "rd_status"]),
        sourceValue(row.raw || {}, [
          "prob_status",
          "reply_status",
          "BL_CHECKOK",
          "BL_CHECKOK_STR",
          "check_status",
          "rd_status",
          "is_read",
          "send_comp",
        ]),
      ]
        .filter(Boolean)
        .join(" ");
    }

    function problemReplyText(row) {
      return [
        sourceValue(row, ["reply_text", "reply_content", "reply_status", "reply_by", "reply_time", "REVERSION"]),
        sourceValue(row.raw || {}, [
          "reply_text",
          "reply_content",
          "reply_status",
          "reply_by",
          "reply_time",
          "reply_count",
          "REVERSION",
          "REVERSION_STATUS",
        ]),
      ]
        .filter(Boolean)
        .join(" ");
    }

    function problemHasReply(row) {
      const replyText = problemReplyText(row);
      const replyCount = Number(sourceValue(row, ["reply_count"]) || sourceValue(row.raw || {}, ["reply_count"]) || 0);
      if (Number.isFinite(replyCount) && replyCount > 0) return true;
      if (!replyText || /^(0|-|无|暂无)$/i.test(replyText)) return false;
      if (/(未回复|未回|待回复|暂无回复|无回复)/.test(replyText)) return false;
      if (/(已回复|正常回复|回复人|回复日期|已回)/.test(replyText)) return true;
      return Boolean(sourceValue(row, ["reply_text", "reply_content", "REVERSION"]) || sourceValue(row.raw || {}, ["reply_text", "reply_content", "REVERSION"]));
    }

    function problemNeedsAttention(row) {
      const statusText = problemStatusText(row);
      if (problemHasReply(row)) return false;
      if (/(已完结|已结束|已关闭|已处理|处理完毕|正常回复|已回复|已签收|无需回复|不用回复)/.test(statusText)) {
        return false;
      }
      return /(未回复|未处理|待处理|待回复|需回复|需要回复|未查看)/.test(statusText);
    }

    function renderRows(rows) {
      state.rows = rows || [];
      if (!tableBody) return;
      if (!state.rows.length) {
        tableBody.innerHTML = '<tr><td colspan="9" class="customer-service-empty">无数据</td></tr>';
        return;
      }
      tableBody.innerHTML = state.rows
        .map((row, index) => {
          const key = problemKey(row);
          const needsAttention = problemNeedsAttention(row);
          const actionText = needsAttention ? "处理" : "查看";
          return `
            <tr data-cs-row="${index}" class="${needsAttention ? "is-attention" : ""}" tabindex="0">
              <td><span class="status status-${row.platform === "yunda" ? "uploaded" : "ready"}">${row.platform === "yunda" ? "韵达" : "融辉"}</span></td>
              <td class="customer-service-account-code">${escapeHtml(displayAccount(row))}</td>
              <td class="customer-service-strong">${escapeHtml(row.waybill_no || "")}</td>
              <td>${escapeHtml(rowProblemType(row))}</td>
              <td>${escapeHtml(row.status || "")}</td>
              <td data-cs-ai-cell="${index}">${aiCellHtml(row)}</td>
              <td><span class="customer-service-problem-text">${escapeHtml(row.problem_text || "")}</span></td>
              <td>${escapeHtml(row.updated_at || row.created_at || "")}</td>
              <td><button class="ghost-btn customer-service-row-btn" type="button" data-cs-open-row="${index}">${actionText}</button></td>
            </tr>`;
        })
        .join("");
      refreshAiStatus();
    }

    // ---- Customer-service AI: advice only; the reply is still sent by the person. ----
    const AI_TIER_TEXT = { L0: "新问题", L1: "AI 建议", L2: "按模板", L3: "自动处理" };

    function aiKey(row) {
      return row && row.source_id && row.external_id && row.source_direction
        ? [row.source_id, row.external_id, row.source_direction].join("|") : "";
    }

    function aiBadgeHtml(scenario, { long = false } = {}) {
      if (!scenario) return "";
      if (scenario.locked) {
        return `<span class="customer-service-ai-badge" title="${escapeHtml(scenario.name)}"><i data-feather="lock"></i>${long ? `${escapeHtml(scenario.name)} · 必须人工确认` : "必须人工"}</span>`;
      }
      const tier = scenario.tier === "L0" ? "new" : "advice";
      const label = AI_TIER_TEXT[scenario.tier] || scenario.tier;
      return `<span class="customer-service-ai-badge customer-service-ai-badge--${tier}" title="${escapeHtml(scenario.name)}"><i data-feather="${scenario.tier === "L0" ? "alert-circle" : "cpu"}"></i>${long ? `${escapeHtml(scenario.name)} · ${label}` : label}</span>`;
    }

    function aiCellHtml(row) {
      const status = state.ai.status.get(aiKey(row));
      return status ? aiBadgeHtml(status.scenario) : '<span class="customer-service-ai-meta">—</span>';
    }

    async function refreshAiStatus() {
      const problems = state.rows.filter((row) => aiKey(row)).slice(0, 200)
        .map((row) => ({ source_id: row.source_id, external_id: row.external_id, source_direction: row.source_direction }));
      if (!problems.length) return;
      const request = ++state.ai.statusRequest;
      try {
        const data = await postJson("/customer-service/problems/ai-status", { problems });
        if (request !== state.ai.statusRequest) return;
        (data.data?.problems || []).forEach((item) => {
          state.ai.status.set([item.source_id, item.external_id, item.source_direction].join("|"), item);
        });
        state.rows.forEach((row, index) => {
          const cell = root.querySelector(`[data-cs-ai-cell="${index}"]`);
          if (cell) cell.innerHTML = aiCellHtml(row);
        });
        if (window.feather) window.feather.replace();
      } catch (_error) {
        // The AI column is supplementary; the list stays usable without it.
      }
    }

    function aiNodes() {
      return { block: $("[data-cs-ai-block]"), body: $("[data-cs-ai-body]"), run: $("[data-cs-ai-run]"),
        tier: $("[data-cs-ai-tier]"), hint: $("[data-cs-ai-hint]"), reply: $("[data-cs-reply-text]") };
    }

    function resetAi() {
      state.ai.request += 1;
      state.ai.suggestionId = null;
      state.ai.suggestion = null;
      const nodes = aiNodes();
      if (nodes.block) nodes.block.hidden = true;
      if (nodes.body) nodes.body.innerHTML = "";
      if (nodes.run) nodes.run.hidden = true;
      if (nodes.tier) { nodes.tier.hidden = true; nodes.tier.innerHTML = ""; }
      if (nodes.hint) { nodes.hint.hidden = true; nodes.hint.textContent = ""; nodes.hint.classList.remove("is-new"); }
    }

    function aiNote(kind, icon, text) {
      return `<div class="customer-service-ai-note${kind ? ` customer-service-ai-note--${kind}` : ""}"><i data-feather="${icon}"></i><span>${escapeHtml(text)}</span></div>`;
    }

    function renderAiSuggestion(data) {
      const nodes = aiNodes();
      const scenario = data.scenario || {};
      if (nodes.tier) { nodes.tier.innerHTML = aiBadgeHtml(scenario, { long: true }); nodes.tier.hidden = false; }
      const evidence = (data.evidence || []).map((item) => `<li class="${item.cited ? "is-cited" : ""}"><span>${escapeHtml(item.source)}${item.cited ? " · 已引用" : ""}</span><span>${escapeHtml(item.text)}</span></li>`).join("");
      const knowledge = (data.knowledge || []).map((item) => `<li class="${item.cited ? "is-cited" : ""}"><span>知识库${item.cited ? " · 已引用" : ""}</span><span>${escapeHtml(item.source)}\n${escapeHtml(item.text)}</span></li>`).join("");
      const cases = data.cases || [];
      const casesHtml = cases.length ? `
        <details class="customer-service-ai-cases" ${cases.some((item) => item.cited) ? "open" : ""}>
          <summary>AI 参考了 ${cases.length} 个相似案例</summary>
          <ul class="customer-service-ai-evidence">${cases.map((item) => `<li class="${item.cited ? "is-cited" : ""}"><span>${escapeHtml(item.created_at || "")}${item.cited ? " · 已参考" : ""}</span><span>${escapeHtml(item.scenario)}｜${escapeHtml(item.problem)}\n回复：${escapeHtml(item.reply)}</span></li>`).join("")}</ul>
        </details>` : "";
      const notes = [];
      if (scenario.locked) notes.push(aiNote("", "lock", "这类问题涉及赔付或责任认定，AI 只给参考，必须由你确认后提交。"));
      if (scenario.tier === "L0") notes.push(aiNote("warn", "alert-circle", `这类问题 AI 还在学习（已有 ${scenario.case_count || 0} 个案例，满 3 个后开始代写回复），请你填写回复，提交后会存为案例。`));
      if ((data.unavailable || []).length) notes.push(aiNote("", "info", `以下信息这次没有取到：${data.unavailable.join("；")}`));
      nodes.body.innerHTML = `
        <p class="customer-service-ai-meta">判断为「${escapeHtml(scenario.name || "")}」，把握 ${Math.round(Number(data.confidence || 0) * 100)}%。${escapeHtml(data.reason || "")}</p>
        <ul class="customer-service-ai-evidence">${evidence}${knowledge}</ul>
        ${casesHtml}
        ${notes.join("")}`;
      if (nodes.hint) {
        if (data.reply) {
          if (nodes.reply && !nodes.reply.value.trim()) nodes.reply.value = data.reply;
          nodes.hint.textContent = "AI 已起草回复，可以直接修改后提交；你的修改也会被记录学习。";
          nodes.hint.classList.remove("is-new");
        } else {
          nodes.hint.textContent = scenario.tier === "L0" ? "请你填写回复，提交后会存为案例。" : "证据不足，AI 没有起草回复，请你填写。";
          nodes.hint.classList.toggle("is-new", scenario.tier === "L0");
        }
        nodes.hint.hidden = false;
      }
      if (nodes.run) { nodes.run.hidden = false; nodes.run.textContent = "重新生成"; }
      if (window.feather) window.feather.replace();
    }

    async function runAiSuggestion(row) {
      const nodes = aiNodes();
      if (!nodes.block || !nodes.body) return;
      nodes.block.hidden = false;
      if (!aiKey(row)) {
        nodes.body.innerHTML = aiNote("", "info", "这条问题件来自原平台实时查询，没有本地记录，AI 建议需要先查询已发布数据。");
        if (window.feather) window.feather.replace();
        return;
      }
      const request = ++state.ai.request;
      state.ai.suggestionId = null;
      if (nodes.run) nodes.run.hidden = true;
      nodes.body.innerHTML = '<p class="customer-service-ai-meta" aria-busy="true">AI 正在查运单、扫描、轨迹和知识库，约需 10–40 秒……</p>';
      try {
        const data = await postJson("/customer-service/problems/ai-suggest", { item: {
          source_id: row.source_id, external_id: row.external_id, source_direction: row.source_direction } }, { command: true });
        if (request !== state.ai.request || state.selected !== row) return;
        state.ai.suggestion = data.data;
        state.ai.suggestionId = data.data?.suggestion_id || null;
        state.ai.status.set(aiKey(row), { scenario: data.data?.scenario });
        renderAiSuggestion(data.data || {});
      } catch (error) {
        if (request !== state.ai.request || state.selected !== row) return;
        nodes.body.innerHTML = aiNote("error", "alert-triangle", `${error.message || "AI 建议暂不可用"}。不影响你直接处理。`);
        if (nodes.run) { nodes.run.hidden = false; nodes.run.textContent = "重试"; }
        if (window.feather) window.feather.replace();
      }
    }

    async function loadAiProgress() {
      const node = $("[data-cs-ai-progress]");
      if (!node) return;
      node.innerHTML = '<p class="customer-service-ai-meta" aria-busy="true">正在读取……</p>';
      try {
        const response = await fetch("/customer-service/problems/ai-stats", { headers: { Accept: "application/json" } });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.message || "学习进展暂不可用");
        const stats = data.data || {};
        const rate = (value) => (value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`);
        const rows = (stats.scenarios || []).map((row) => `
          <tr><td>${escapeHtml(row.name)}</td><td>${aiBadgeHtml({ name: row.name, tier: row.tier, locked: row.locked })}</td>
            <td class="num">${row.case_count}</td><td class="num">${row.adopted} / ${row.edited} / ${row.unused}</td>
            <td class="num">${rate(row.adoption_rate)}</td></tr>`).join("");
        const totals = stats.totals || {};
        node.innerHTML = `
          <p class="customer-service-ai-meta">近 30 天共 ${totals.cases || 0} 个案例；AI 草稿被直接采纳的比例 ${rate(totals.adoption_rate)}。</p>
          <div class="customer-service-ai-progress-table"><table>
            <thead><tr><th>问题类型</th><th>现在</th><th>累计案例</th><th>直接采纳 / 修改后用 / 未用</th><th>直接采纳率</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
        if (window.feather) window.feather.replace();
      } catch (error) {
        node.innerHTML = aiNote("", "info", error.message || "学习进展暂不可用");
        if (window.feather) window.feather.replace();
      }
    }

    async function openRequestedProblem() {
      const wanted = root.dataset.openProblem || "";
      if (!wanted) return;
      root.dataset.openProblem = "";
      const index = state.rows.findIndex((row) => aiKey(row) === wanted);
      if (index >= 0) {
        openRow(index);
      } else {
        setStatus("没有在当前结果中找到这条问题件，可能已被处理或不在所选账号中。", "warning");
      }
    }

    function refreshBadges(rows, errors) {
      const newCount = (rows || []).filter((row) => problemNeedsAttention(row) && !seen.has(problemKey(row))).length;
      if (totalNode) totalNode.textContent = String((rows || []).length);
      if (newNode) newNode.textContent = String(newCount);
      renderErrors(errors || []);
    }

    function renderErrors(errors) {
      state.errors = errors || [];
      const count = state.errors.length;
      if (errorsNode) errorsNode.textContent = String(count);
      if (errorLabel) errorLabel.textContent = count ? "账号异常，点击查看" : "异常";
      if (errorToggle) {
        errorToggle.hidden = count === 0;
        errorToggle.setAttribute("aria-expanded", "false");
      }
      if (errorPanel) errorPanel.hidden = true;
      if (!errorList) return;
      errorList.innerHTML = count
        ? state.errors
            .map((item) => {
              const platform = item.platform === "yunda" ? "韵达" : "融辉";
              const code = item.error_code ? ` · ${item.error_code}` : "";
              return `
                <div class="customer-service-error-item">
                  <strong>${escapeHtml(platform)} / ${escapeHtml(item.account_label || item.account_id || "")}${escapeHtml(code)}</strong>
                  <span>${escapeHtml(item.message || "账号查询失败")}</span>
                </div>`;
            })
            .join("")
        : "";
    }

    async function runQuery(live = false) {
      live = live === true;
      const request = ++state.queryRequest;
      const accountIds = selectedAccounts();
      const sourceId = $("[data-cs-source]")?.value || "";
      if ((!accountIds.length && !sourceId) || (live && !accountIds.length)) {
        state.querying = false;
        setQueryBusy(false);
        setStatus("请先选择账号", "warning");
        return;
      }
      state.querying = true;
      setQueryBusy(true);
      setStatus("正在查询问题件...", "loading");
      try {
        const data = await postJson(live ? "/customer-service/problems/live-query" : "/customer-service/problems/query", {
          platforms: selectedPlatforms(),
          account_ids: live ? accountIds : sourceId ? [] : accountIds,
          source_ids: !live && sourceId ? [sourceId] : [],
          filters: filters(),
        }, { command: true });
        if (request !== state.queryRequest || !root.isConnected) return;
        renderRows(data.rows || []);
        refreshBadges(data.rows || [], data.errors || []);
        const errorText = data.errors && data.errors.length ? `，${data.errors.length} 个账号异常` : "";
        setStatus(`查询完成：已返回 ${(data.rows || []).length} 条${errorText}`, data.ok ? "success" : "warning");
      } catch (error) {
        if (request !== state.queryRequest || !root.isConnected) return;
        renderRows([]);
        refreshBadges([], [{ message: error.message }]);
        setStatus(`查询失败：${error.message || "未知错误"}`, "error");
      } finally {
        if (request === state.queryRequest) {
          state.querying = false;
          setQueryBusy(false);
        }
      }
    }
    $("[data-cs-source]")?.addEventListener("module-source-change", runQuery);

    function buildSettingsPayload() {
      return {
        ronghui_account_ids: $$("[data-cs-account][data-system='ronghui']:checked").map((input) => input.value),
        yunda_account_ids: $$("[data-cs-account][data-system='yunda']:checked").map((input) => input.value),
        poll_interval_sec: Number($("[data-cs-poll-interval]")?.value || settings.poll_interval_sec || 60),
      };
    }

    async function saveSettings(options = {}) {
      const payload = buildSettingsPayload();
      try {
        setSettingsAutosave("保存中...", "saving");
        await postJson("/customer-service/problem-settings", payload);
        updateAccountSummary();
        setSettingsAutosave("设置已生效", "success");
        if (!options.silent) setStatus("设置已生效", "success");
      } catch (error) {
        setSettingsAutosave(error.message || "保存失败", "error");
        if (!options.silent) setStatus(error.message || "保存失败", "error");
      }
    }

    function autoSaveSettings() {
      updateAccountSummary();
      setSettingsAutosave("等待保存...", "saving");
      if (settingsSaveTimer) window.clearTimeout(settingsSaveTimer);
      settingsSaveTimer = window.setTimeout(() => {
        settingsSaveTimer = null;
        saveSettings({ silent: true });
      }, 250);
    }

    function cleanText(value) {
      if (value === undefined || value === null) return "";
      if (Array.isArray(value)) return value.map(cleanText).filter(Boolean).join("、");
      if (typeof value === "object") return "";
      return String(value)
        .replace(/<br\s*\/?>/gi, " ")
        .replace(/<[^>]+>/g, "")
        .replace(/\s+/g, " ")
        .trim();
    }

    function cleanMultiline(value) {
      if (value === undefined || value === null || typeof value === "object") return "";
      return String(value)
        .replace(/<br\s*\/?>/gi, "\n")
        .replace(/<[^>]+>/g, "")
        .replace(/\r\n/g, "\n")
        .trim();
    }

    function mergeDetailSource(details) {
      const merged = {};
      const visit = (value) => {
        if (!value) return;
        if (Array.isArray(value)) {
          value.forEach(visit);
          return;
        }
        if (typeof value !== "object") return;
        Object.entries(value).forEach(([key, item]) => {
          if (item !== undefined && item !== null && typeof item !== "object" && cleanText(item)) {
            merged[key] = item;
          }
        });
      };
      visit(details);
      return merged;
    }

    function sourceValue(source, keys) {
      if (!source || typeof source !== "object") return "";
      for (const key of keys) {
        const variants = [key, key.toLowerCase(), key.toUpperCase()];
        for (const variant of variants) {
          if (Object.prototype.hasOwnProperty.call(source, variant)) {
            const value = cleanText(source[variant]);
            if (value) return value;
          }
        }
      }
      return "";
    }

    function problemField(row, detailSource, keys) {
      return sourceValue(row, keys) || sourceValue(row.raw || {}, keys) || sourceValue(detailSource, keys);
    }

    function renderProblemMetaGroups(node, groups) {
      if (!node) return;
      const visibleGroups = groups
        .map((group) => ({
          ...group,
          items: group.items.filter((item) => cleanText(item.value)),
        }))
        .filter((group) => group.items.length);
      node.innerHTML = visibleGroups.length
        ? visibleGroups
            .map(
              (group) => `
                <section class="customer-service-problem-meta-group">
                  <h5>${escapeHtml(group.title)}</h5>
                  <div class="customer-service-problem-meta-list">
                    ${group.items
                      .map(
                        (item) => `
                          <div class="customer-service-problem-meta-row">
                            <span>${escapeHtml(item.label)}</span>
                            <strong>${escapeHtml(item.value)}</strong>
                          </div>`,
                      )
                      .join("")}
                  </div>
                </section>`,
            )
            .join("")
        : '<div class="customer-service-problem-muted">暂无补充信息</div>';
    }

    function normalizeAttachmentHref(value, platform) {
      const text = cleanText(value)
        .replace(/&amp;/g, "&")
        .replace(/\\\//g, "/")
        .replace(/\\/g, "/");
      if (!text || /[\s"'<>]/.test(text)) return "";
      if (/^\/\//.test(text)) return `https:${text}`;
      if (/^https?:\/\//i.test(text)) return text;
      const origin = ATTACHMENT_ORIGINS[platform] || ATTACHMENT_ORIGINS.yunda;
      if (platform === "yunda" && (text.startsWith("/base/") || /^(base|query|issue)\//i.test(text))) {
        return `${YUNDA_ATTACHMENT_PUBLIC_ROOT}/${text.replace(/^\/+/, "")}`;
      }
      if (text.startsWith("/")) return `${origin}${text}`;
      if (/^(ky_problem|kyproblem)\//i.test(text)) return `${ATTACHMENT_ORIGINS.yunda}/${text}`;
      if (/^(static|file|upload|uploads|image|images|problem\/image)\//i.test(text)) return `${origin}/${text}`;
      return "";
    }

    function attachmentPreviewHref(sourceHref, row) {
      if (!sourceHref || !row?.platform || !row?.account_id) return sourceHref || "";
      const params = new URLSearchParams({
        platform: row.platform,
        account_id: row.account_id,
        src: sourceHref,
        request_uuid: newBrowserRequestUuid(),
      });
      return `/customer-service/problems/attachments/preview?${params.toString()}`;
    }

    function filenameFromHref(href) {
      const text = cleanText(href).split("?", 1)[0];
      const filename = text.split("/").pop() || "";
      try {
        return decodeURIComponent(filename).trim();
      } catch {
        return filename.trim();
      }
    }

    function truthyAttachmentFlag(value) {
      const text = cleanText(value).toLowerCase();
      return ["1", "true", "yes", "y", "是"].includes(text);
    }

    function isYundaAttachmentIconOnly(href) {
      const text = cleanText(href);
      if (!text) return false;
      let pathname = text;
      try {
        pathname = new URL(text, ATTACHMENT_ORIGINS.yunda).pathname;
      } catch {
        pathname = text.split("?", 1)[0];
      }
      return /\/ky_problem\/public\/static\/problem\/image\/(?:bl_attachment\d+|query-icon|empty-icon|preservation-icon|delete-icon|export-icon|cancel-icon)\.png$/i.test(
        pathname,
      );
    }

    function pushAttachmentCandidate(candidates, item, row) {
      const href = normalizeAttachmentHref(item.href, row.platform);
      if (!href || isYundaAttachmentIconOnly(href)) return;
      const label = cleanText(item.label) || filenameFromHref(href) || "附件";
      candidates.push({ href, label, isImage: Boolean(item.isImage) });
    }

    function collectStructuredAttachments(value, row, candidates) {
      if (value === undefined || value === null) return;
      if (Array.isArray(value)) {
        value.forEach((item) => collectStructuredAttachments(item, row, candidates));
        return;
      }
      if (typeof value !== "object") return;

      const href = sourceValue(value, ["attachment_path", "file_path", "path", "url", "src", "href"]);
      const label = sourceValue(value, ["old_name", "file_name", "filename", "display_name", "name", "new_name"]);
      if (href) {
        pushAttachmentCandidate(
          candidates,
          {
            href,
            label,
            isImage: truthyAttachmentFlag(sourceValue(value, ["is_image", "isImage", "image"])),
          },
          row,
        );
      }

      ["attachment", "attachments", "attach", "file_arr", "files", "images", "pics"].forEach((key) => {
        if (Object.prototype.hasOwnProperty.call(value, key)) {
          collectStructuredAttachments(value[key], row, candidates);
        }
      });
    }

    function isImageAttachment(item) {
      return Boolean(
        item.href &&
          (item.isImage ||
            /\.(?:png|jpe?g|gif|webp|bmp)(?:$|\?)/i.test(item.href || "") ||
            /\.(?:png|jpe?g|gif|webp|bmp)(?:$|\?)/i.test(item.label || "")),
      );
    }

    function collectProblemAttachments(row, details) {
      const candidates = [];
      collectStructuredAttachments(row.raw || {}, row, candidates);
      collectStructuredAttachments(details || {}, row, candidates);

      const visit = (value, key = "") => {
        if (value === undefined || value === null) return;
        if (Array.isArray(value)) {
          value.forEach((item) => visit(item, key));
          return;
        }
        if (typeof value === "object") {
          Object.entries(value).forEach(([itemKey, itemValue]) => visit(itemValue, itemKey));
          return;
        }
        const text = String(value).replace(/\\"/g, '"');
        if (!/(attach|attachment|image|img|pic|photo|scan|附件|图片)/i.test(key) && !/<img/i.test(text)) return;
        const imgSrcPattern = /<img[^>]+src=["']([^"']+)["']/gi;
        const urlPattern = /(https?:\/\/[^\s"'<>]+|\/[^\s"'<>]+?\.(?:png|jpg|jpeg|gif|webp)(?:\?[^\s"'<>]*)?)/gi;
        for (const pattern of [imgSrcPattern, urlPattern]) {
          let match = pattern.exec(text);
          while (match) {
            pushAttachmentCandidate(
              candidates,
              {
                href: match[1],
                label: filenameFromHref(match[1]) || "附件",
                isImage: /\.(?:png|jpe?g|gif|webp|bmp)(?:$|\?)/i.test(match[1]),
              },
              row,
            );
            match = pattern.exec(text);
          }
        }
      };
      visit(row.raw || {});
      visit(details || {});
      const seenHref = new Set();
      return candidates.filter((item) => {
        const key = item.href || item.label;
        if (seenHref.has(key)) return false;
        seenHref.add(key);
        return true;
      });
    }

    function renderProblemAttachments(row, details) {
      if (!problemAttachments) return;
      const attachments = collectProblemAttachments(row, details);
      if (!attachments.length) {
        problemAttachments.innerHTML = '<div class="customer-service-problem-muted">无附件</div>';
        return;
      }
      const imageItems = attachments.filter(isImageAttachment);
      const fileItems = attachments.filter((item) => !isImageAttachment(item));
      const imageHtml = imageItems.length
        ? `<div class="customer-service-attachment-gallery">
            ${imageItems
              .map((item, index) => {
                const label = escapeHtml(item.label || `图片 ${index + 1}`);
                const href = escapeHtml(attachmentPreviewHref(item.href, row));
                return `
                  <a class="customer-service-attachment-thumb" href="${href}" data-cs-image-preview="${href}" data-cs-image-label="${label}">
                    <img src="${href}" alt="${label}" loading="lazy">
                    <span>${label}</span>
                  </a>`;
              })
              .join("")}
          </div>`
        : "";
      const fileHtml = fileItems.length
        ? `<div class="customer-service-attachment-files">
            ${fileItems
              .map((item, index) => {
                const label = escapeHtml(item.label || `附件 ${index + 1}`);
                if (!item.href) return `<span class="customer-service-attachment-pill">${label}</span>`;
                return `
                  <a class="customer-service-attachment-pill" href="${escapeHtml(item.href)}" target="_blank" rel="noreferrer">
                    <i data-feather="paperclip"></i>
                    <span>${label}</span>
                  </a>`;
              })
              .join("")}
          </div>`
        : "";
      problemAttachments.innerHTML = `${imageHtml}${fileHtml}`;
      if (window.feather) window.feather.replace();
    }

    function renderProblemModal(row, details, options = {}) {
      const detailSource = mergeDetailSource(details);
      const platformLabel = row.platform === "yunda" ? "韵达问题件" : "融辉问题件";
      const waybillNo = problemField(row, detailSource, ["waybill_no", "ship_no", "BILL_CODE", "bill_code"]) || row.external_id || "";
      const status = problemField(row, detailSource, ["status", "prob_status", "PROB_STATUS", "BL_CHECKOK"]) || "未标注";
      const problemText =
        cleanMultiline(row.problem_text) ||
        cleanMultiline(sourceValue(row.raw || {}, ["prob_text", "PROBLEM_CAUSE", "problem_cause", "prob_title"])) ||
        cleanMultiline(sourceValue(detailSource, ["prob_text", "PROBLEM_CAUSE", "problem_cause", "prob_title"]));
      const replyText = cleanMultiline(
        problemField(row, detailSource, ["reply_text", "REVERSION", "reply_status", "reply_content", "reply"]),
      );
      const createdAt = problemField(row, detailSource, ["created_at", "created_time", "REGISTER_DATE", "register_date"]);
      const updatedAt = problemField(row, detailSource, ["updated_at", "modified_time", "reply_time", "MODIFY_DATE"]);
      const weight = problemField(row, detailSource, ["cash_wei", "actual_weight", "WEIGHT", "weight"]);
      const volume = problemField(row, detailSource, ["obj_vol", "volume", "VOLUME"]);
      const weightVolume = [weight && `${weight} kg`, volume && `${volume} 方`].filter(Boolean).join(" / ");
      const publishSite = problemField(row, detailSource, [
        "REGISTER_SITE",
        "register_site",
        "site_id",
        "site_name",
        "publish_site",
        "publisher_site",
      ]);
      const publishSiteCode = problemField(row, detailSource, [
        "REGISTER_SITE_CODE",
        "register_site_code",
        "site_id_bm",
        "site_code",
        "publish_site_code",
        "publisher_site_code",
      ]);
      const notifiedSite = problemField(row, detailSource, [
        "SEND_SITE",
        "recv_site_id",
        "notice_site",
        "notify_site",
        "notified_site",
        "rec_comp",
        "inform_site_name",
      ]);
      const notifiedSiteCode = problemField(row, detailSource, [
        "SEND_SITE_CODE",
        "recv_site_nm_arr",
        "recv_site_id_arr",
        "notice_site_code",
        "notify_site_code",
        "notified_site_code",
        "inform_site",
        "inform_site_id",
        "rec_comp_code",
      ]);
      const destinationSite = problemField(row, detailSource, [
        "recv_comp",
        "destination_site",
        "dest_site",
        "DESTINATION_SITE",
        "RECV_COMP",
      ]);
      const senderSite = problemField(row, detailSource, [
        "send_comp",
        "sender_site",
        "shipper_site",
        "SEND_COMP",
      ]);

      if (problemPlatform) problemPlatform.textContent = platformLabel;
      if (problemTitle) problemTitle.textContent = waybillNo || "处理问题件";
      if (problemContent) {
        problemContent.innerHTML = `
          <div class="customer-service-problem-message-main">
            <p>${escapeHtml(problemText || (options.loading ? "正在获取详情..." : "无问题内容"))}</p>
          </div>
          ${
            replyText
              ? `<div class="customer-service-problem-reply-note"><span>已有回复</span><strong class="customer-service-problem-reply-body">${escapeHtml(replyText)}</strong></div>`
              : ""
          }`;
      }
      renderProblemAttachments(row, details);
      renderProblemMetaGroups(problemMeta, [
        {
          title: "站点",
          items: [
            { label: "发布网点", value: publishSite },
            { label: "发布网点编码", value: publishSiteCode },
            { label: "通知网点", value: notifiedSite },
            { label: "通知网点编码", value: notifiedSiteCode },
            { label: "目的网点", value: destinationSite },
            { label: "寄件站点", value: senderSite },
            { label: "收货地址", value: problemField(row, detailSource, ["recv_addr", "recv_address", "RECV_ADDR"]) },
          ],
        },
        {
          title: "人员与货物",
          items: [
            { label: "登记人", value: problemField(row, detailSource, ["created_by", "register_person", "REGISTER_PERSON"]) },
            { label: "回复人", value: problemField(row, detailSource, ["reply_by", "modified_by", "REPLY_MAN"]) },
            { label: "货物", value: problemField(row, detailSource, ["obj_name", "goods_name", "CARGO_NAME"]) },
            { label: "件数", value: problemField(row, detailSource, ["obj_qty", "piece_count", "PIECE_NUMBER"]) },
            { label: "重量/体积", value: weightVolume },
          ],
        },
        {
          title: "时间",
          items: [
            { label: "创建时间", value: createdAt },
            { label: "最后更新", value: updatedAt || createdAt },
          ],
        },
      ]);
    }

    function closeProblemModal() {
      if (problemModal?.open) problemModal.close();
    }

    function openPublishModal() {
      if (publishModal && !publishModal.open) publishModal.showModal();
    }

    function closePublishModal() {
      if (publishModal?.open) publishModal.close();
    }

    function openImageViewer(href, label) {
      if (!imageViewer || !imageViewerImg || !href) return;
      imageViewerImg.src = href;
      imageViewerImg.alt = label || "附件图片预览";
      if (!imageViewer.open) imageViewer.showModal();
      if (window.feather) window.feather.replace();
    }

    function closeImageViewer() {
      if (!imageViewer || !imageViewerImg) return;
      if (imageViewer.open) imageViewer.close();
      imageViewerImg.src = "";
    }

    function setReplySubmitting(isSubmitting) {
      const button = $("[data-cs-reply-submit]");
      const label = button?.querySelector("span");
      if (!button) return;
      button.disabled = isSubmitting;
      button.setAttribute("aria-busy", String(Boolean(isSubmitting)));
      if (!label) return;
      if (isSubmitting) {
        label.dataset.originalText = label.dataset.originalText || label.textContent || "回复处理";
        label.textContent = "提交中";
      } else {
        label.textContent = label.dataset.originalText || "回复处理";
      }
    }

    async function openRow(index) {
      const row = state.rows[index];
      if (!row) return;
      state.selected = row;
      state.selectedDetails = [];
      if (problemModal && !problemModal.open) problemModal.showModal();
      const replyTextNode = $("[data-cs-reply-text]");
      const replyStatusNode = $("[data-cs-reply-status]");
      if (replyTextNode) replyTextNode.value = "";
      if (replyStatusNode) replyStatusNode.value = DEFAULT_REPLY_STATUS;
      state.replying = false;
      setReplySubmitting(false);
      resetAi();
      renderProblemModal(row, [], { loading: true });
      if (problemNeedsAttention(row)) {
        runAiSuggestion(row);
      } else {
        const nodes = aiNodes();
        if (nodes.block && aiKey(row)) { nodes.block.hidden = false; nodes.run.hidden = false; nodes.run.textContent = "生成 AI 建议"; }
      }
      seen.add(problemKey(row));
      localStorage.setItem(seenKey, JSON.stringify(Array.from(seen).slice(-2000)));
      refreshBadges(state.rows, state.errors);
      try {
        const data = await postJson("/customer-service/problems/detail", {
          platform: row.platform,
          account_id: row.account_id,
          item: row,
        }, { command: true });
        state.selectedDetails = data.details || data.detail || [];
        renderProblemModal(row, state.selectedDetails);
      } catch (error) {
        setStatus(error.message || "详情获取失败", "warning");
      }
    }

    async function submitReply(event) {
      event.preventDefault();
      if (state.replying) return;
      const row = state.selected;
      if (!row) return;
      const replyText = $("[data-cs-reply-text]")?.value.trim() || "";
      const status = $("[data-cs-reply-status]")?.value || DEFAULT_REPLY_STATUS;
      if (!replyText) {
        setStatus("回复内容不能为空", "warning");
        return;
      }
      state.replying = true;
      setReplySubmitting(true);
      setStatus("回复提交中...");
      try {
        const data = await postJson(
          "/customer-service/problems/reply",
          {
            platform: row.platform,
            account_id: row.account_id,
            item: row,
            ai_suggestion_id: state.ai.suggestionId,
            payload: {
              reply_text: replyText,
              prob_status: status,
              old_prob_status: row.status || "",
              REVERSION: replyText,
            },
          },
          { command: true },
        );
        assertActionSucceeded(data, "回复失败");
        row.status = status;
        row.reply_text = replyText;
        row.reply_content = replyText;
        row.REVERSION = replyText;
        renderProblemModal(row, state.selectedDetails || []);
        const replyTextNode = $("[data-cs-reply-text]");
        if (replyTextNode) replyTextNode.value = "";
        const aiCase = data.ai_case || {};
        setStatus(aiCase.recorded ? "回复已提交，并已存入案例库，正在刷新..." : `回复已提交，正在刷新...${aiCase.message ? `（${aiCase.message}）` : ""}`, "success");
        await runQuery();
      } catch (error) {
        setStatus(error.message || "回复失败", "error");
      } finally {
        state.replying = false;
        setReplySubmitting(false);
      }
    }

    function updatePublishAccounts() {
      const platform = $("[data-cs-publish-platform]")?.value || "ronghui";
      const select = $("[data-cs-publish-account]");
      if (!select) return;
      select.innerHTML = accounts
        .filter((account) => account.system === platform)
        .map((account) => {
          const label = [account.login_account, account.name || account.account_id].filter(Boolean).join(" · ");
          return `<option value="${escapeHtml(account.account_id)}">${escapeHtml(label)}</option>`;
        })
        .join("");
    }

    async function submitPublish(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const platform = form.platform.value;
      const accountId = form.account_id.value;
      const payload =
        platform === "yunda"
          ? {
              ship_no: form.bill_code.value.trim(),
              classes_type: form.problem_type.value.trim(),
              prob_text: form.problem_cause.value.trim(),
              site_id: form.notice_site_code.value.trim() ? [form.notice_site_code.value.trim()] : [],
            }
          : {
              bill_code: form.bill_code.value.trim(),
              problem_type: form.problem_type.value.trim(),
              owner_problem_type: form.owner_problem_type.value.trim(),
              notice_site_code: form.notice_site_code.value.trim(),
              notice_site: form.notice_site.value.trim(),
              problem_cause: form.problem_cause.value.trim(),
            };
      try {
        const data = await postJson(
          "/customer-service/problems/publish",
          {
            platform,
            account_id: accountId,
            payload,
          },
          { command: true },
        );
        assertActionSucceeded(data, "发布失败");
        closePublishModal();
        setStatus(
          data.message || "问题件发布完成。",
          "success",
        );
      } catch (error) {
        setStatus(error.message || "发布失败", "error");
      }
    }

    function handleProblemRowOpen(event) {
      const target = event.target instanceof Element ? event.target : event.target?.parentElement;
      if (!target) return false;
      const openButton = target.closest("[data-cs-open-row]");
      if (openButton) {
        event.preventDefault();
        openRow(Number(openButton.dataset.csOpenRow || 0));
        return true;
      }
      const row = target.closest("[data-cs-row]");
      if (event.type === "dblclick" && row && !target.closest("button, a, input, select, textarea")) {
        event.preventDefault();
        openRow(Number(row.dataset.csRow || 0));
        return true;
      }
      return false;
    }

    root.addEventListener("click", (event) => {
      if (handleProblemRowOpen(event)) return;
      const imagePreview = event.target.closest("[data-cs-image-preview]");
      if (imagePreview) {
        event.preventDefault();
        openImageViewer(imagePreview.getAttribute("href") || "", imagePreview.dataset.csImageLabel || "附件图片预览");
        return;
      }
      if (event.target.closest("[data-cs-image-close]")) {
        closeImageViewer();
        return;
      }
      const replySubmit = event.target.closest("[data-cs-reply-submit]");
      const replyForm = replySubmit?.closest("[data-cs-reply-form]");
      if (replySubmit && replyForm && typeof replyForm.requestSubmit === "function") {
        event.preventDefault();
        replyForm.requestSubmit(replySubmit);
        return;
      }
      if (event.target.closest("[data-cs-settings-toggle]")) {
        const willOpen = Boolean(settingsPanel?.hidden);
        if (settingsPanel) settingsPanel.hidden = !willOpen;
        settingsToggle?.setAttribute("aria-expanded", String(willOpen));
      }
      if (event.target.closest("[data-cs-error-toggle]")) {
        const willOpen = Boolean(errorPanel?.hidden);
        if (errorPanel) errorPanel.hidden = !willOpen;
        errorToggle?.setAttribute("aria-expanded", String(willOpen));
      }
      if (event.target.closest("[data-cs-problem-close]")) closeProblemModal();
      if (event.target.closest("[data-cs-ai-run]") && state.selected) runAiSuggestion(state.selected);
      if (event.target.closest("[data-cs-ai-help-close]")) $("[data-cs-ai-help]")?.close();
      if (event.target.matches?.("[data-cs-ai-help]")) event.target.close();
      if (event.target.closest("[data-cs-publish-close]")) closePublishModal();
    });
    root.addEventListener("dblclick", handleProblemRowOpen);
    problemModal?.addEventListener("click", (event) => {
      if (event.target === problemModal) closeProblemModal();
    });
    imageViewer?.addEventListener("click", (event) => {
      if (event.target === imageViewer) closeImageViewer();
    });
    imageViewer?.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeImageViewer();
    });
    document.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target : event.target?.parentElement;
      if (!target) return;
      if (!isRootActive()) return;
      if (target.closest("[data-cs-live-query]")) {
        event.preventDefault();
        runQuery(true);
        return;
      }
      if (target.closest("[data-cs-query]")) {
        event.preventDefault();
        runQuery();
        return;
      }
      if (target.closest("[data-cs-ai-help-open]")) {
        const help = $("[data-cs-ai-help]");
        if (help && !help.open) { help.showModal(); if (window.feather) window.feather.replace(); loadAiProgress(); }
        return;
      }
      if (target.closest("[data-cs-publish-open]")) {
        event.preventDefault();
        updatePublishAccounts();
        openPublishModal();
      }
    });
    $("[data-cs-filter-form]")?.addEventListener("submit", (event) => {
      event.preventDefault();
      runQuery();
    });
    $("[data-cs-reply-form]")?.addEventListener("submit", submitReply);
    $("[data-cs-publish-form]")?.addEventListener("submit", submitPublish);
    $("[data-cs-publish-platform]")?.addEventListener("change", updatePublishAccounts);
    root.addEventListener("change", (event) => {
      if (event.target.closest("[data-cs-account], [data-cs-poll-interval]")) {
        autoSaveSettings();
        return;
      }
      if (event.target.closest("[data-cs-platform]")) {
        updateAccountSummary();
      }
    });
    root.addEventListener("input", (event) => {
      if (event.target.closest("[data-cs-poll-interval]")) autoSaveSettings();
    });
    renderDateRangeLabel();
    renderCalendar();
    dateRangeToggle?.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!datePopover) return;
      if (datePopover.hidden) {
        openDatePopover();
      } else {
        closeDatePopover();
      }
    });
    dateRange?.addEventListener("click", (event) => {
      event.stopPropagation();
    });
    document.addEventListener("click", closeDatePopover);
    calendarPrev?.addEventListener("click", () => {
      calendarMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() - 1, 1);
      renderCalendar();
    });
    calendarNext?.addEventListener("click", () => {
      calendarMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + 1, 1);
      renderCalendar();
    });
    $$("[data-cs-quick-range]").forEach((button) => {
      button.addEventListener("click", () => {
        const range = button.dataset.csQuickRange || "";
        if (range === "clear") {
          setDateRange("", "");
          return;
        }
        const days = Number(range);
        if (!Number.isFinite(days) || days < 1) return;
        const end = new Date();
        const start = new Date();
        start.setDate(end.getDate() - days + 1);
        setDateRange(formatDate(start), formatDate(end), { closeOnComplete: true });
      });
    });
    updateAccountSummary();
    updatePublishAccounts();
    // Opened from the overview's “待我确认”: query, then open that problem's dialog.
    if (root.dataset.openProblem) runQuery().then(openRequestedProblem);
    // The tab shell reuses an open customer-service tab and only reports the new query string.
    root.closest(".main-content")?.addEventListener("console:querychange", (event) => {
      let url;
      try { url = new URL(event.detail?.url || "", window.location.href); } catch (_error) { return; }
      const wanted = url.searchParams.get("open");
      if (!wanted) return;
      const keyword = $("[data-cs-q]");
      if (keyword) keyword.value = url.searchParams.get("q") || "";
      root.dataset.openProblem = wanted;
      runQuery().then(openRequestedProblem);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCustomerServiceWorkbench);
  } else {
    initCustomerServiceWorkbench();
  }
})();
