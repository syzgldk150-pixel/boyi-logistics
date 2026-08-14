(() => {
  "use strict";

  const POLICY_ENDPOINT = "/automations/tasks/approval-policy";
  const REQUIRE_EACH_RUN = "REQUIRE_EACH_RUN";
  const EXACT_SCHEDULE_EXEMPT = "EXACT_SCHEDULE_EXEMPT";
  const VALID_MODES = new Set([REQUIRE_EACH_RUN, EXACT_SCHEDULE_EXEMPT]);
  const VALID_STATUSES = new Set(["ACTIVE", "STALE", "UNSUPPORTED"]);

  function parseObject(value) {
    try {
      const parsed = JSON.parse(value || "{}");
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    } catch (_) {
      return null;
    }
  }

  function responseMessage(payload, fallback) {
    if (!payload || typeof payload !== "object") return fallback;
    if (payload.error_code === "TASK_CONFIGURATION_VERSION_CONFLICT") {
      return "任务配置已发生变化，请刷新页面，重新核对当前时间、账号和参数后再保存审批策略。";
    }
    if (typeof payload.message === "string" && payload.message) return payload.message;
    if (typeof payload.error === "string" && payload.error) return payload.error;
    if (payload.error && typeof payload.error.message === "string") {
      return payload.error.message;
    }
    return fallback;
  }

  function validPolicyItem(item) {
    return Boolean(
      item
      && typeof item === "object"
      && typeof item.task_id === "string"
      && item.task_id.length > 0
      && VALID_MODES.has(item.mode)
      && item.configured_mode === item.mode
      && VALID_MODES.has(item.effective_mode)
      && VALID_STATUSES.has(String(item.effective_status || "").toUpperCase())
      && typeof item.can_exempt === "boolean"
      && Number.isInteger(item.version)
      && item.version > 0
      && Number.isInteger(item.configuration_version)
      && item.configuration_version > 0
    );
  }

  function readPolicyItem(row) {
    const item = parseObject(row.dataset.policyItem);
    return validPolicyItem(item) ? item : null;
  }

  function modeLabel(value) {
    if (value === EXACT_SCHEDULE_EXEMPT) return "固定计划自动执行";
    if (value === REQUIRE_EACH_RUN) return "每次运行审批";
    return "混合策略";
  }

  function itemStateLabel(item) {
    const status = String(item.effective_status || "").toUpperCase();
    if (status === "STALE") return "免审已失效";
    if (status === "UNSUPPORTED") return "不支持免审";
    return item.effective_mode === EXACT_SCHEDULE_EXEMPT ? "免审生效" : "需要审批";
  }

  function setFeedback(row, message, kind) {
    const feedback = row.querySelector("[data-approval-policy-feedback]");
    if (!feedback) return;
    feedback.textContent = message || "";
    feedback.dataset.kind = kind || "";
    feedback.hidden = !message;
  }

  function setButtonLabel(button, value) {
    if (Object.prototype.hasOwnProperty.call(button.dataset, "previousLabel")) {
      button.dataset.previousLabel = value;
      return;
    }
    const label = button.querySelector("[data-approval-policy-save-label]");
    if (label) label.textContent = value;
  }

  function setLoading(button, loading) {
    const label = button.querySelector("[data-approval-policy-save-label]");
    if (loading) {
      button.dataset.previousLabel = label?.textContent || "保存审批策略";
      if (label) label.textContent = "保存中…";
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      return;
    }
    if (label) label.textContent = button.dataset.previousLabel || "保存审批策略";
    button.disabled = false;
    button.removeAttribute("aria-busy");
    delete button.dataset.previousLabel;
  }

  function setMeta(panel, selector, valueSelector, value) {
    const wrapper = panel.querySelector(selector);
    const target = panel.querySelector(valueSelector);
    if (target) target.textContent = value || "";
    if (wrapper) wrapper.hidden = !value;
  }

  function oneOrMany(items, field, manyLabel) {
    const values = [...new Set(items.map(item => item[field] || "").filter(Boolean))];
    if (values.length === 1) return values[0];
    return values.length > 1 ? manyLabel : "";
  }

  function summarizePolicyRows(panel) {
    const rows = [...panel.querySelectorAll("[data-approval-policy-item]")];
    const items = rows.map(readPolicyItem);
    if (!items.length || items.some(item => !item)) return;

    const modes = new Set(items.map(item => item.mode));
    const statuses = new Set(items.map(item => String(item.effective_status).toUpperCase()));
    const effectiveModes = new Set(items.map(item => item.effective_mode));
    const invalidReasons = [...new Set(items.map(item => item.invalid_reason || "").filter(Boolean))];
    const mixed = modes.size !== 1;
    const effectiveMixed = effectiveModes.size !== 1;
    const mode = mixed ? "" : [...modes][0];
    const effectiveMode = effectiveMixed ? "" : [...effectiveModes][0];
    const stale = invalidReasons.length > 0 || statuses.has("STALE");
    const unsupported = statuses.has("UNSUPPORTED");
    const canExempt = items.every(item => item.can_exempt === true);
    const groupPrefix = items.length > 1 ? `${items.length} 条任务，` : "";

    let label = "每次运行审批";
    let summary = `${groupPrefix}每次定时运行都先进入审批。`;
    let effectiveStatus = "ACTIVE";
    if (unsupported) {
      label = items.length > 1 ? "部分计划不支持免审" : "工具不允许免审";
      summary = items.length > 1
        ? `${groupPrefix}不支持免审的计划仍需逐次审批，其余计划可分别设置。`
        : "当前工具契约不允许固定计划免审。";
      effectiveStatus = "UNSUPPORTED";
    } else if (mixed || effectiveMixed) {
      label = "混合策略";
      summary = `${groupPrefix}当前审批策略不一致，可在下方按执行时间分别设置。`;
      effectiveStatus = "MIXED";
    } else if (mode === EXACT_SCHEDULE_EXEMPT && stale) {
      label = "配置已变更需重新授权";
      summary = `${groupPrefix}已保存的免审基线与当前配置不一致，相关任务不会免审执行。`;
      effectiveStatus = "STALE";
    } else if (mode === EXACT_SCHEDULE_EXEMPT) {
      label = "固定计划自动执行";
      summary = `${groupPrefix}仅 Scheduler 按当前时间、账号、参数和工具版本执行时免审；手工运行仍需审批。`;
    }
    if (!canExempt && !unsupported) summary += " 部分计划不允许免审。";

    ["active", "stale", "unsupported", "mixed", "unavailable"].forEach(status => {
      panel.classList.remove(`auto-approval-policy--${status}`);
    });
    panel.classList.add(`auto-approval-policy--${effectiveStatus.toLowerCase()}`);
    const labelElement = panel.querySelector("[data-approval-policy-label]");
    const summaryElement = panel.querySelector("[data-approval-policy-summary]");
    if (labelElement) labelElement.textContent = label;
    if (summaryElement) summaryElement.textContent = summary;
    const configuredModeElement = panel.querySelector("[data-policy-configured-mode]");
    const effectiveModeElement = panel.querySelector("[data-policy-effective-mode]");
    if (configuredModeElement) configuredModeElement.textContent = modeLabel(mode);
    if (effectiveModeElement) effectiveModeElement.textContent = modeLabel(effectiveMode);

    const restriction = panel.querySelector("[data-approval-policy-restriction]");
    if (restriction) {
      restriction.textContent = items.length > 1 ? "部分计划不允许免审" : "工具不允许免审";
      restriction.hidden = canExempt;
    }
    setMeta(
      panel,
      "[data-policy-meta-hash]",
      "[data-policy-hash]",
      oneOrMany(items, "policy_hash_short", "多项"),
    );
    setMeta(
      panel,
      "[data-policy-meta-actor]",
      "[data-policy-approved-by]",
      oneOrMany(items, "approved_by", "多人"),
    );
    setMeta(
      panel,
      "[data-policy-meta-time]",
      "[data-policy-approved-at]",
      oneOrMany(items, "approved_at", "多次"),
    );
    setMeta(
      panel,
      "[data-policy-meta-reason]",
      "[data-policy-invalid-reason]",
      invalidReasons.join("；"),
    );
  }

  function saveLabel(row, item, selectedMode = item.mode) {
    const panel = row.closest("[data-approval-policy]");
    const grouped = panel?.querySelectorAll("[data-approval-policy-item]").length > 1;
    if (item.effective_status === "STALE" && selectedMode === EXACT_SCHEDULE_EXEMPT) {
      return grouped ? "重新授权" : "重新授权固定计划";
    }
    return grouped ? "保存" : "保存审批策略";
  }

  function renderPolicyItem(row, rawItem) {
    const current = readPolicyItem(row);
    const item = {
      ...rawItem,
      schedule_label: rawItem.schedule_label || current?.schedule_label || "计划时间未设置",
    };
    if (!validPolicyItem(item) || (current && item.task_id !== current.task_id)) {
      throw new Error("Agent 未返回当前计划的完整审批策略结果。");
    }
    row.dataset.policyItem = JSON.stringify(item);
    ["active", "stale", "unsupported"].forEach(status => {
      row.classList.remove(`auto-approval-policy-item--${status}`);
    });
    row.classList.add(`auto-approval-policy-item--${String(item.effective_status).toLowerCase()}`);

    const state = row.querySelector("[data-policy-item-state]");
    const configuredMode = row.querySelector("[data-policy-item-configured-mode]");
    const effectiveMode = row.querySelector("[data-policy-item-effective-mode]");
    const invalidReason = row.querySelector("[data-policy-item-invalid-reason]");
    if (state) state.textContent = itemStateLabel(item);
    if (configuredMode) configuredMode.textContent = modeLabel(item.configured_mode);
    if (effectiveMode) effectiveMode.textContent = modeLabel(item.effective_mode);
    if (invalidReason) {
      invalidReason.textContent = item.invalid_reason || "";
      invalidReason.hidden = !item.invalid_reason;
    }

    const select = row.querySelector("[data-approval-policy-mode]");
    if (select instanceof HTMLSelectElement) {
      const exactOption = select.querySelector(`option[value="${EXACT_SCHEDULE_EXEMPT}"]`);
      if (exactOption) exactOption.disabled = !item.can_exempt;
      select.value = item.mode;
    }
    const button = row.querySelector("[data-approval-policy-save]");
    if (button instanceof HTMLButtonElement) {
      setButtonLabel(button, saveLabel(row, item));
    }
    return item;
  }

  function exemptionConfirmation(item) {
    const schedule = item.schedule_label || "当前计划";
    return [
      `确认将“${schedule}”（${item.task_id}）当前的执行时间、账号绑定、完整参数和工具版本锁定为免审基线？`,
      "仅 Scheduler 定时触发可免审，手工运行仍需审批。任务名称改动不影响授权；时间、账号、参数或工具版本变化会立即失效。",
    ].join("\n\n");
  }

  async function savePolicy(panel, row) {
    const select = row.querySelector("[data-approval-policy-mode]");
    const comment = row.querySelector("[data-approval-policy-comment]");
    const button = row.querySelector("[data-approval-policy-save]");
    const item = readPolicyItem(row);
    if (
      !(select instanceof HTMLSelectElement)
      || !(button instanceof HTMLButtonElement)
      || !item
      || !VALID_MODES.has(select.value)
    ) {
      setFeedback(row, "审批策略数据不完整，请刷新页面后重试。", "error");
      return;
    }

    const mode = select.value;
    const taskId = item.task_id;
    if (mode === EXACT_SCHEDULE_EXEMPT && !window.confirm(exemptionConfirmation(item))) {
      select.value = item.mode;
      select.focus();
      return;
    }
    if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
      setFeedback(row, "当前浏览器无法生成安全的请求标识，审批策略未保存。", "error");
      return;
    }

    const requestId = row.dataset.policyRequestId || window.crypto.randomUUID();
    row.dataset.policyRequestId = requestId;
    setFeedback(row, "", "");
    setLoading(button, true);
    try {
      const response = await fetch(POLICY_ENDPOINT, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json; charset=UTF-8",
          "X-Browser-Request-UUID": requestId,
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({
          task_ids: [taskId],
          mode,
          comment: comment instanceof HTMLInputElement ? comment.value.trim() : "",
          request_id: requestId,
          expected_versions: { [taskId]: item.version },
          expected_configuration_versions: { [taskId]: item.configuration_version },
        }),
      });
      let payload = null;
      try {
        payload = await response.json();
      } catch (_) {
        payload = null;
      }
      if (!response.ok || !payload || payload.ok !== true) {
        throw new Error(responseMessage(payload, "审批策略保存失败，请重试。"));
      }
      const items = payload.data && Array.isArray(payload.data.items)
        ? payload.data.items
        : null;
      if (!items || items.length !== 1 || items[0]?.task_id !== taskId) {
        throw new Error("Agent 未返回当前计划的完整审批策略结果。");
      }
      renderPolicyItem(row, items[0]);
      summarizePolicyRows(panel);
      delete row.dataset.policyRequestId;
      if (comment instanceof HTMLInputElement) comment.value = "";
      setFeedback(row, payload.message || "审批策略已保存。", "success");
    } catch (error) {
      setFeedback(
        row,
        error instanceof Error ? error.message : "审批策略保存失败，请重试。",
        "error",
      );
    } finally {
      setLoading(button, false);
    }
  }

  function initializeItem(panel, row) {
    const select = row.querySelector("[data-approval-policy-mode]");
    const comment = row.querySelector("[data-approval-policy-comment]");
    const button = row.querySelector("[data-approval-policy-save]");
    const resetReplayId = () => {
      delete row.dataset.policyRequestId;
      setFeedback(row, "", "");
    };
    select?.addEventListener("change", () => {
      resetReplayId();
      const item = readPolicyItem(row);
      if (item && button instanceof HTMLButtonElement) {
        setButtonLabel(button, saveLabel(row, item, select.value));
      }
    });
    comment?.addEventListener("input", resetReplayId);
    button?.addEventListener("click", event => {
      event.preventDefault();
      void savePolicy(panel, row);
    });
  }

  function initializePanel(panel) {
    panel.querySelectorAll("[data-approval-policy-item]").forEach(row => {
      initializeItem(panel, row);
    });
    summarizePolicyRows(panel);
  }

  function initialize() {
    document.querySelectorAll("[data-approval-policy]").forEach(initializePanel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
