(() => {
  function initCustomerServiceWorkbench() {
    document.querySelectorAll("[data-customer-service-workbench]").forEach((root) => initCustomerServiceRoot(root));
  }

  function initCustomerServiceRoot(root) {
    if (!root || root.dataset.bound === "true") return;
    root.dataset.bound = "true";

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
    const state = { rows: [], selected: null, selectedDetails: [], querying: false, replying: false, errors: [] };
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

    async function postJson(url, payload) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.message || data.error || `HTTP ${response.status}`);
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
        tableBody.innerHTML = '<tr><td colspan="8" class="customer-service-empty">无数据</td></tr>';
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
              <td><span class="customer-service-problem-text">${escapeHtml(row.problem_text || "")}</span></td>
              <td>${escapeHtml(row.updated_at || row.created_at || "")}</td>
              <td><button class="ghost-btn customer-service-row-btn" type="button" data-cs-open-row="${index}">${actionText}</button></td>
            </tr>`;
        })
        .join("");
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

    async function runQuery() {
      if (state.querying) return;
      const accountIds = selectedAccounts();
      if (!accountIds.length) {
        setStatus("请先选择账号", "warning");
        return;
      }
      state.querying = true;
      setQueryBusy(true);
      setStatus("正在查询问题件...", "loading");
      try {
        const data = await postJson("/customer-service/problems/query", {
          platforms: selectedPlatforms(),
          account_ids: accountIds,
          filters: filters(),
        });
        renderRows(data.rows || []);
        refreshBadges(data.rows || [], data.errors || []);
        const errorText = data.errors && data.errors.length ? `，${data.errors.length} 个账号异常` : "";
        setStatus(`查询完成：已返回 ${(data.rows || []).length} 条${errorText}`, data.ok ? "success" : "warning");
      } catch (error) {
        renderRows([]);
        refreshBadges([], [{ message: error.message }]);
        setStatus(`查询失败：${error.message || "未知错误"}`, "error");
      } finally {
        state.querying = false;
        setQueryBusy(false);
      }
    }

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
      if (!problemModal) return;
      problemModal.hidden = true;
    }

    function openImageViewer(href, label) {
      if (!imageViewer || !imageViewerImg || !href) return;
      imageViewerImg.src = href;
      imageViewerImg.alt = label || "附件图片预览";
      imageViewer.hidden = false;
      if (window.feather) window.feather.replace();
    }

    function closeImageViewer() {
      if (!imageViewer || !imageViewerImg) return;
      imageViewer.hidden = true;
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
      if (problemModal) problemModal.hidden = false;
      const replyTextNode = $("[data-cs-reply-text]");
      const replyStatusNode = $("[data-cs-reply-status]");
      if (replyTextNode) replyTextNode.value = "";
      if (replyStatusNode) replyStatusNode.value = DEFAULT_REPLY_STATUS;
      state.replying = false;
      setReplySubmitting(false);
      renderProblemModal(row, [], { loading: true });
      seen.add(problemKey(row));
      localStorage.setItem(seenKey, JSON.stringify(Array.from(seen).slice(-2000)));
      refreshBadges(state.rows, state.errors);
      try {
        const data = await postJson("/customer-service/problems/detail", {
          platform: row.platform,
          account_id: row.account_id,
          item: row,
        });
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
        const data = await postJson("/customer-service/problems/reply", {
          platform: row.platform,
          account_id: row.account_id,
          item: row,
          payload: {
            reply_text: replyText,
            prob_status: status,
            old_prob_status: row.status || "",
            REVERSION: replyText,
          },
        });
        assertActionSucceeded(data, "回复失败");
        row.status = status;
        row.reply_text = replyText;
        row.reply_content = replyText;
        row.REVERSION = replyText;
        renderProblemModal(row, state.selectedDetails || []);
        const replyTextNode = $("[data-cs-reply-text]");
        if (replyTextNode) replyTextNode.value = "";
        setStatus("回复已提交，正在刷新...", "success");
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
        await postJson("/customer-service/problems/publish", {
          platform,
          account_id: accountId,
          payload,
        });
        publishModal.hidden = true;
        setStatus("问题件已提交发布", "success");
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
      if (event.target.closest("[data-cs-publish-close]")) publishModal.hidden = true;
    });
    root.addEventListener("dblclick", handleProblemRowOpen);
    problemModal?.addEventListener("click", (event) => {
      if (event.target === problemModal) closeProblemModal();
    });
    imageViewer?.addEventListener("click", (event) => {
      if (event.target === imageViewer) closeImageViewer();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && imageViewer && !imageViewer.hidden) {
        closeImageViewer();
        return;
      }
      if (event.key === "Escape" && problemModal && !problemModal.hidden) closeProblemModal();
    });
    document.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target : event.target?.parentElement;
      if (!target) return;
      if (!isRootActive()) return;
      if (target.closest("[data-cs-query]")) {
        event.preventDefault();
        runQuery();
        return;
      }
      if (target.closest("[data-cs-publish-open]")) {
        event.preventDefault();
        updatePublishAccounts();
        publishModal.hidden = false;
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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCustomerServiceWorkbench);
  } else {
    initCustomerServiceWorkbench();
  }
})();
