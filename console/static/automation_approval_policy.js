(() => {
  "use strict";

  const REQUIRE_EACH_RUN = "REQUIRE_EACH_RUN";
  const PROJECT_FULL_AUTO = "PROJECT_FULL_AUTO";
  const LEGACY_SCHEDULE_ONLY = "LEGACY_SCHEDULE_ONLY";
  const POLICY_MODES = new Set([REQUIRE_EACH_RUN, PROJECT_FULL_AUTO]);
  const EFFECTIVE_MODES = new Set([REQUIRE_EACH_RUN, PROJECT_FULL_AUTO, LEGACY_SCHEDULE_ONLY]);
  const POLICY_STATUSES = new Set([
    "ACTIVE", "RECONCILING", "UNAVAILABLE", "UNSUPPORTED", LEGACY_SCHEDULE_ONLY,
  ]);
  const RUNTIME_STATUSES = new Set(["READY", "RECONCILING", "UNAVAILABLE"]);
  const PENDING_RISKS = new Set(["LOW", "MEDIUM", "HIGH", "CRITICAL"]);
  const PENDING_HASH_PATTERN = /^[A-Za-z0-9._~-]{16,256}$/;
  const RUN_RECEIPT_ID_PATTERN = /^[A-Za-z0-9_.:@-]{1,160}$/;
  const RUN_RECEIPT_STATUSES = new Set([
    "WAITING_APPROVAL",
    "QUEUED",
    "RUNNING",
    "VERIFYING",
    "COMPLETED",
    "PARTIAL",
    "FAILED_TERMINAL",
    "CANCELLED",
  ]);
  const PLUGIN_CONFIGURATION_CONTROL_SELECTOR = [
    "[data-plugin-config-path]",
    "[data-plugin-account-role]",
    "[data-plugin-resource-role]",
    "[data-plugin-entrypoint]",
    "[data-plugin-worker-select]",
    "[data-plugin-schedule-kind]",
    "[data-automation-toggle]",
    "[data-schedule-time]",
  ].join(", ");
  const PLUGIN_CONFIGURATION_PENDING_RUNTIME_STATES = new Set([
    "BLOCKED_GENERATION",
    "REFRESH_FAILED",
  ]);
  const pendingStates = new WeakMap();

  function parseObject(value) {
    try {
      const parsed = JSON.parse(value || "{}");
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    } catch (_) {
      return null;
    }
  }

  function responseMessage(payload, fallback) {
    const code = String(payload?.error?.code || payload?.error_code || "");
    if (code === "PROJECT_POLICY_VERSION_CONFLICT" || code === "PROJECT_CONFIGURATION_VERSION_CONFLICT") {
      return "项目权限或配置已被其他管理员更新，请刷新页面后重新确认。";
    }
    if (code === "PLUGIN_RECORD_VERSION_CONFLICT") {
      return "项目实例已被其他管理员更新，请刷新页面后重新确认。";
    }
    if (typeof payload?.message === "string" && payload.message) return payload.message;
    if (typeof payload?.error === "string" && payload.error) return payload.error;
    if (typeof payload?.error?.message === "string" && payload.error.message) {
      return payload.error.message;
    }
    return fallback;
  }

  function validPolicy(policy, automationId) {
    return Boolean(
      policy
      && typeof policy === "object"
      && policy.automation_id === automationId
      && POLICY_MODES.has(policy.configured_mode)
      && EFFECTIVE_MODES.has(policy.effective_mode)
      && POLICY_STATUSES.has(String(policy.effective_status || "").toUpperCase())
      && typeof policy.can_full_auto === "boolean"
      && typeof policy.runnable === "boolean"
      && RUNTIME_STATUSES.has(String(policy.runtime_status || "").toUpperCase())
      && typeof policy.runtime_reason === "string"
      && Number.isInteger(policy.policy_version)
      && policy.policy_version > 0
      && Number.isInteger(policy.project_configuration_version)
      && policy.project_configuration_version > 0
    );
  }

  function validPending(pending, automationId) {
    if (
      !pending
      || typeof pending !== "object"
      || pending.automation_id !== automationId
      || !Number.isInteger(pending.pending_count)
      || pending.pending_count < 0
    ) return false;
    if (pending.pending_count === 0) return pending.expected_pending_set_hash === "";
    return Boolean(
      PENDING_RISKS.has(String(pending.highest_risk || "").toUpperCase())
      && typeof pending.highest_risk_label === "string"
      && pending.highest_risk_label
      && typeof pending.source_summary === "string"
      && pending.source_summary
      && typeof pending.expected_pending_set_hash === "string"
      && PENDING_HASH_PATTERN.test(pending.expected_pending_set_hash)
    );
  }

  function validApprovedRunReceipts(receipts, automationId, decidedCount) {
    if (
      !Number.isInteger(decidedCount)
      || decidedCount < 0
      || !Array.isArray(receipts)
      || receipts.length !== decidedCount
    ) return false;
    const runIds = new Set();
    const workItemIds = new Set();
    return receipts.every(receipt => {
      const runId = String(receipt?.run_id || "");
      const workItemId = String(receipt?.work_item_id || "");
      const status = String(receipt?.status || "").toUpperCase();
      const nextPollAfterMs = receipt?.next_poll_after_ms;
      if (
        receipt?.automation_id !== automationId
        || !RUN_RECEIPT_ID_PATTERN.test(runId)
        || !RUN_RECEIPT_ID_PATTERN.test(workItemId)
        || !RUN_RECEIPT_STATUSES.has(status)
        || !Number.isInteger(nextPollAfterMs)
        || nextPollAfterMs < 250
        || nextPollAfterMs > 10000
        || runIds.has(runId)
        || workItemIds.has(workItemId)
      ) return false;
      runIds.add(runId);
      workItemIds.add(workItemId);
      return true;
    });
  }

  function setBusy(button, busy, busyLabel) {
    if (!(button instanceof HTMLButtonElement)) return;
    if (busy) {
      button.dataset.previousMarkup = button.innerHTML;
      button.textContent = busyLabel;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      return;
    }
    if (button.dataset.previousMarkup) button.innerHTML = button.dataset.previousMarkup;
    delete button.dataset.previousMarkup;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }

  function setFeedback(element, message, kind = "") {
    if (!(element instanceof HTMLElement)) return;
    element.textContent = message || "";
    element.dataset.kind = kind;
    element.hidden = !message;
  }

  function pluginConfigurationContext(target) {
    if (!(target instanceof Element)) return null;
    const form = target.closest("form");
    if (!(form instanceof HTMLFormElement)) return null;
    const instance = form.querySelector("[data-plugin-instance]");
    if (!(instance instanceof HTMLElement)) return null;
    return { form, instance };
  }

  function setPluginConfigurationFeedback(instance, message, kind = "", options = {}) {
    const form = instance.closest("form");
    const summary = instance.querySelector("[data-plugin-instance-feedback]");
    const local = form?.querySelector("[data-plugin-settings-feedback]");
    if (options.summary === true) {
      setFeedback(local, "", "");
      setFeedback(summary, message, kind);
      return;
    }
    setFeedback(summary, "", "");
    if (local instanceof HTMLElement) {
      local.setAttribute("role", kind === "error" ? "alert" : "status");
      local.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
    }
    setFeedback(local, message, kind);
  }

  function markPluginConfigurationDirty(instance) {
    const form = instance.closest("form");
    const configurationSave = form?.querySelector("[data-plugin-configuration-save]");
    const runButton = form?.querySelector("[data-run-now]");
    if (configurationSave instanceof HTMLButtonElement) {
      delete configurationSave.dataset.requestId;
    }
    setPluginConfigurationFeedback(
      instance,
      "项目设置尚未保存；保存后才能按新配置运行。",
      "warning",
    );
    if (runButton instanceof HTMLButtonElement && !runButton.disabled) {
      runButton.dataset.runTitleBeforePluginConfig = runButton.getAttribute("title") || "";
      runButton.disabled = true;
      runButton.dataset.disabledForPluginConfig = "true";
      runButton.title = "请先保存项目设置";
    }
  }

  function nestedConfigHasPath(value, rawPath) {
    const parts = String(rawPath || "").split(".").filter(Boolean);
    let cursor = value;
    for (const part of parts) {
      if (
        !cursor
        || typeof cursor !== "object"
        || !Object.prototype.hasOwnProperty.call(cursor, part)
      ) return false;
      cursor = cursor[part];
    }
    return parts.length > 0;
  }

  function syncPluginConfigurationSavedState(instance, settings) {
    const form = instance.closest("form");
    form?.querySelectorAll("[data-plugin-config-path]").forEach(control => {
      control.dataset.pluginConfigPresent = String(
        nestedConfigHasPath(settings.config, control.dataset.pluginConfigPath),
      );
      control.dataset.pluginConfigTouched = "false";
    });
    const runButton = form?.querySelector("[data-run-now]");
    if (!(runButton instanceof HTMLButtonElement)) return;
    const consoleEntrypoints = [...(form?.querySelectorAll("[data-plugin-entrypoint]") || [])]
      .filter(control => (
        control instanceof HTMLInputElement
        && control.dataset.pluginEntrypointKind === "console"
      ));
    const enabledConsoleIds = new Set(
      consoleEntrypoints
        .map(control => control.value)
        .filter(entrypointId => settings.enabled_entrypoints.includes(entrypointId)),
    );
    const contribution = form?.querySelector("[name='contribution_id']");
    if (contribution instanceof HTMLSelectElement) {
      [...contribution.options].forEach(option => {
        option.disabled = !enabledConsoleIds.has(option.value);
      });
      if (!enabledConsoleIds.has(contribution.value)) {
        const nextOption = [...contribution.options].find(option => !option.disabled);
        contribution.value = nextOption?.value || "";
      }
    }
    const selectedContribution = contribution instanceof HTMLInputElement
      || contribution instanceof HTMLSelectElement
      ? contribution.value
      : "";
    const consoleEnabled = selectedContribution
      ? enabledConsoleIds.has(selectedContribution)
      : enabledConsoleIds.size > 0;
    if (!consoleEnabled) {
      runButton.disabled = true;
      delete runButton.dataset.disabledForPluginConfig;
      runButton.dataset.disabledForPluginEntrypoint = "true";
      runButton.title = "后台手动入口已关闭";
      return;
    }
    if (
      runButton.dataset.disabledForPluginConfig !== "true"
      && runButton.dataset.disabledForPluginEntrypoint !== "true"
    ) return;
    runButton.disabled = false;
    delete runButton.dataset.disabledForPluginConfig;
    delete runButton.dataset.disabledForPluginEntrypoint;
    const previousTitle = runButton.dataset.runTitleBeforePluginConfig || "";
    if (previousTitle) runButton.title = previousTitle;
    else runButton.removeAttribute("title");
    delete runButton.dataset.runTitleBeforePluginConfig;
  }

  function announce(governance, message) {
    const live = governance.querySelector("[data-governance-live]");
    if (live) live.textContent = message || "";
  }

  function setPolicyPanelOpen(governance, open) {
    const toggle = governance.querySelector("[data-project-policy-toggle]");
    const panel = governance.querySelector("[data-project-policy-panel]");
    if (!(toggle instanceof HTMLButtonElement) || !(panel instanceof HTMLElement)) return;
    toggle.setAttribute("aria-expanded", String(open));
    panel.hidden = !open;
    if (open) {
      const checked = panel.querySelector("[data-project-policy-mode]:checked");
      (checked instanceof HTMLInputElement ? checked : panel.querySelector("input, button"))?.focus();
    } else {
      toggle.focus({ preventScroll: true });
    }
  }

  function renderPolicy(governance, policy) {
    const automationId = governance.dataset.automationId || "";
    if (!validPolicy(policy, automationId)) {
      throw new Error("智能服务未返回完整的项目权限结果。");
    }
    governance.dataset.projectPolicy = JSON.stringify(policy);
    const label = governance.querySelector("[data-project-policy-label]");
    const summary = governance.querySelector("[data-project-policy-summary]");
    if (label) label.textContent = policy.label || (policy.effective_mode === PROJECT_FULL_AUTO ? "完全自动" : "每次运行审批");
    if (summary) summary.textContent = policy.summary || "";
    governance.querySelectorAll("[data-project-policy-mode]").forEach(input => {
      if (!(input instanceof HTMLInputElement)) return;
      input.checked = input.value === policy.configured_mode;
    });
    ["active", "reconciling", "unsupported", "legacy_schedule_only", "unavailable"].forEach(state => {
      governance.classList.remove(`auto-project-governance--${state}`);
    });
    governance.classList.add(`auto-project-governance--${String(policy.effective_status).toLowerCase()}`);
  }

  async function savePolicy(governance) {
    const automationId = governance.dataset.automationId || "";
    const policy = parseObject(governance.dataset.projectPolicy);
    const selected = governance.querySelector("[data-project-policy-mode]:checked");
    const button = governance.querySelector("[data-project-policy-save]");
    const feedback = governance.querySelector("[data-project-policy-feedback]");
    if (
      !validPolicy(policy, automationId)
      || !(selected instanceof HTMLInputElement)
      || !POLICY_MODES.has(selected.value)
      || !(button instanceof HTMLButtonElement)
    ) {
      setFeedback(feedback, "项目权限数据不完整，请刷新页面后重试。", "error");
      return;
    }
    if (selected.value === PROJECT_FULL_AUTO) {
      const confirmed = window.confirm(
        "确认将整个项目设为“完全自动”？\n\n项目清单允许且已启用的系统定时、后台手动、飞书和外部验签入口，都会仅按当前保存的参数、账号、资源与版本直接执行；通用接口和智能模型入口不在授权范围内。完全自动权限会持续保留；配置或扩展版本变化时先同步运行环境，同步完成前不会运行旧配置，也不会自动改回审批。",
      );
      if (!confirmed) return;
    }
    if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
      setFeedback(feedback, "当前浏览器无法生成安全请求标识，项目权限未保存。", "error");
      return;
    }
    const requestId = governance.dataset.policyRequestId || window.crypto.randomUUID();
    governance.dataset.policyRequestId = requestId;
    setFeedback(feedback, "", "");
    setBusy(button, true, "保存中…");
    try {
      const response = await fetch(
        `/automations/projects/${encodeURIComponent(automationId)}/approval-policy`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({
            mode: selected.value,
            request_id: requestId,
            comment: "",
            expected_policy_version: policy.policy_version,
            expected_project_configuration_version: policy.project_configuration_version,
          }),
        },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok || payload?.ok !== true) {
        throw new Error(responseMessage(payload, "项目权限保存失败，请重试。"));
      }
      renderPolicy(governance, payload?.data?.policy);
      delete governance.dataset.policyRequestId;
      setFeedback(feedback, payload.message || "项目权限已保存。", "success");
      announce(governance, payload.message || "项目权限已保存。");
    } catch (error) {
      setFeedback(feedback, error instanceof Error ? error.message : "项目权限保存失败，请重试。", "error");
    } finally {
      setBusy(button, false, "");
    }
  }

  function renderPending(governance, pending) {
    const automationId = governance.dataset.automationId || "";
    if (!validPending(pending, automationId)) {
      throw new Error("智能服务未返回有效的待审批集合。");
    }
    const current = pendingStates.get(governance) || {};
    pendingStates.set(governance, { ...current, pending, requestIds: {} });
    const bar = governance.querySelector("[data-project-pending]");
    if (!(bar instanceof HTMLElement)) return;
    bar.hidden = pending.pending_count === 0;
    bar.classList.remove("is-error");
    const count = bar.querySelector("[data-pending-count]");
    const risk = bar.querySelector("[data-pending-risk]");
    const source = bar.querySelector("[data-pending-source]");
    if (count) count.textContent = String(pending.pending_count);
    if (risk) risk.textContent = pending.highest_risk_label || "—";
    if (source) source.textContent = pending.source_summary || "—";
    bar.querySelectorAll("[data-pending-action]").forEach(button => {
      if (button instanceof HTMLButtonElement) button.disabled = pending.pending_count === 0;
    });
  }

  async function loadPending(governance, { quiet = false } = {}) {
    const automationId = governance.dataset.automationId || "";
    const bar = governance.querySelector("[data-project-pending]");
    const feedback = governance.querySelector("[data-pending-feedback]");
    try {
      const response = await fetch(
        `/automations/projects/${encodeURIComponent(automationId)}/pending-approvals`,
        { credentials: "same-origin", headers: { "Accept": "application/json" } },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok || payload?.ok !== true) {
        throw new Error(responseMessage(payload, "待审批集合加载失败。"));
      }
      renderPending(governance, payload?.data?.pending);
      if (!quiet) setFeedback(feedback, "", "");
      return payload.data.pending;
    } catch (error) {
      if (bar instanceof HTMLElement) {
        bar.hidden = false;
        bar.classList.add("is-error");
      }
      setFeedback(feedback, error instanceof Error ? error.message : "待审批集合加载失败。", "error");
      return null;
    }
  }

  async function actOnPending(governance, action, button) {
    const state = pendingStates.get(governance);
    const pending = state?.pending;
    const automationId = governance.dataset.automationId || "";
    const feedback = governance.querySelector("[data-pending-feedback]");
    const comment = governance.querySelector("[data-pending-comment]");
    if (!validPending(pending, automationId) || pending.pending_count === 0) {
      await loadPending(governance);
      return;
    }
    const verb = action === "approve" ? "通过" : "驳回";
    if (!window.confirm(`确认批量${verb}该项目当前 ${pending.pending_count} 项待审批？\n\n集合若已变化，系统会阻止提交并原位刷新。`)) return;
    if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
      setFeedback(feedback, "当前浏览器无法生成安全请求标识，批量操作未提交。", "error");
      return;
    }
    const commentValue = comment instanceof HTMLInputElement ? comment.value.trim() : "";
    const replayKey = `${action}:${pending.expected_pending_set_hash}:${commentValue}`;
    const requestId = state.requestIds?.[replayKey] || window.crypto.randomUUID();
    state.requestIds = { [replayKey]: requestId };
    setFeedback(feedback, "", "");
    setBusy(button, true, action === "approve" ? "通过中…" : "驳回中…");
    try {
      const response = await fetch(
        `/automations/projects/${encodeURIComponent(automationId)}/pending-approvals/${action}`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({
            expected_pending_set_hash: pending.expected_pending_set_hash,
            request_id: requestId,
            comment: commentValue,
          }),
        },
      );
      const payload = await response.json().catch(() => null);
      const changed = response.status === 409
        || String(payload?.error?.code || payload?.error_code || "") === "PENDING_SET_CHANGED";
      const returnedPending = payload?.data?.pending;
      if (validPending(returnedPending, automationId)) {
        renderPending(governance, returnedPending);
      } else {
        await loadPending(governance, { quiet: true });
      }
      if (changed) {
        const message = "待审批集合已变化，已原位刷新；请核对后重试。";
        setFeedback(feedback, message, "warning");
        announce(governance, message);
        return;
      }
      if (!response.ok || payload?.ok !== true) {
        throw new Error(responseMessage(payload, `批量${verb}失败，请重试。`));
      }
      if (action === "approve") {
        const decidedCount = payload?.data?.decided_count;
        const runReceipts = payload?.data?.run_receipts;
        if (!validApprovedRunReceipts(runReceipts, automationId, decidedCount)) {
          throw new Error("服务端未返回完整的本次批准执行记录，卡片不会推测执行状态。");
        }
        governance.dispatchEvent(new CustomEvent("automation:approved-runs", {
          bubbles: true,
          detail: {
            automation_id: automationId,
            decided_count: decidedCount,
            run_receipts: runReceipts,
          },
        }));
      }
      if (comment instanceof HTMLInputElement) comment.value = "";
      const message = payload.message || `已批量${verb}。`;
      setFeedback(feedback, message, "success");
      announce(governance, message);
    } catch (error) {
      setFeedback(feedback, error instanceof Error ? error.message : `批量${verb}失败，请重试。`, "error");
    } finally {
      setBusy(button, false, "");
    }
  }

  function initializeGovernance(governance) {
    const automationId = governance.dataset.automationId || "";
    const policy = parseObject(governance.dataset.projectPolicy);
    const toggle = governance.querySelector("[data-project-policy-toggle]");
    const cancel = governance.querySelector("[data-project-policy-cancel]");
    const save = governance.querySelector("[data-project-policy-save]");
    if (validPolicy(policy, automationId)) renderPolicy(governance, policy);
    toggle?.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      setPolicyPanelOpen(governance, open);
    });
    cancel?.addEventListener("click", () => {
      const saved = parseObject(governance.dataset.projectPolicy);
      if (validPolicy(saved, automationId)) renderPolicy(governance, saved);
      setPolicyPanelOpen(governance, false);
    });
    save?.addEventListener("click", () => void savePolicy(governance));
    governance.querySelectorAll("[data-project-policy-mode]").forEach(control => {
      control.addEventListener("input", () => {
        delete governance.dataset.policyRequestId;
        setFeedback(governance.querySelector("[data-project-policy-feedback]"), "", "");
      });
    });
    governance.querySelectorAll("[data-pending-action]").forEach(button => {
      button.addEventListener("click", () => {
        if (button instanceof HTMLButtonElement) void actOnPending(governance, button.dataset.pendingAction, button);
      });
    });
  }

  function secureRequestId(feedback) {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    setFeedback(feedback, "当前浏览器无法生成安全请求标识，操作未提交。", "error");
    return "";
  }

  async function recoverUnknownWrite(instance, button) {
    const automationId = instance.dataset.automationId || "";
    const feedback = instance.querySelector("[data-plugin-instance-feedback]");
    const fallback = "系统仍无法确认上次是否已经保存。任务会继续暂停，也没有重复执行。请先到对应业务表格核对实际结果。";
    const requestId = button.dataset.requestId || secureRequestId(feedback);
    if (!requestId) return;
    button.dataset.requestId = requestId;
    setBusy(button, true, "提交中…");
    try {
      const response = await fetch(
        `/automations/plugins/${encodeURIComponent(automationId)}/recover`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({ request_id: requestId }),
        },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok || payload?.ok !== true) {
        throw new Error(responseMessage(payload, fallback));
      }
      delete button.dataset.requestId;
      setFeedback(feedback, payload.message || "未知写入状态已重新核对。", "success");
      window.location.reload();
    } catch (error) {
      setFeedback(feedback, error instanceof Error ? error.message : fallback, "error");
    } finally {
      setBusy(button, false, "");
    }
  }
  function setNestedConfig(target, path, value) {
    const parts = String(path || "").split(".").filter(Boolean);
    if (!parts.length) throw new Error("配置字段路径无效。");
    let cursor = target;
    parts.forEach((part, index) => {
      if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(part)) {
        throw new Error("配置字段路径无效。");
      }
      if (index === parts.length - 1) {
        cursor[part] = value;
        return;
      }
      if (!cursor[part] || typeof cursor[part] !== "object" || Array.isArray(cursor[part])) {
        cursor[part] = {};
      }
      cursor = cursor[part];
    });
  }

  function pluginConfigControlValue(control) {
    const kind = control.dataset.pluginConfigKind || "text";
    const required = control.dataset.pluginConfigRequired === "true";
    const present = control.dataset.pluginConfigPresent === "true";
    const touched = control.dataset.pluginConfigTouched === "true";
    if (kind === "checkbox") {
      if (!required && !present && !touched && !control.checked) return { omitted: true };
      return { value: control.checked };
    }
    if (kind === "select") {
      if (!(control instanceof HTMLSelectElement)) throw new Error("配置选项无效。");
      const option = control.selectedOptions[0];
      if (!option || !option.dataset.pluginOptionValue) {
        if (required) throw new Error("请补齐必填配置项。");
        return { omitted: true };
      }
      try {
        return { value: JSON.parse(option.dataset.pluginOptionValue) };
      } catch (_) {
        throw new Error("配置选项值无效。");
      }
    }
    const raw = String(control.value || "");
    if (control.dataset.pluginConfigSecret === "true" && !raw) {
      if (required) throw new Error("受保护配置不会回显，请重新填写必填值。");
      return { omitted: true };
    }
    if (kind === "list") {
      const lines = raw.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
      if (!lines.length && !required && !present && !touched) return { omitted: true };
      const itemType = control.dataset.pluginConfigItemType || "string";
      if (itemType === "string") return { value: lines };
      const values = lines.map(item => Number(item));
      if (values.some(value => !Number.isFinite(value))) throw new Error("列表中包含无效数字。");
      if (itemType === "integer" && values.some(value => !Number.isInteger(value))) {
        throw new Error("列表中包含非整数值。");
      }
      return { value: values };
    }
    if (!raw && !required && !present) return { omitted: true };
    if (required && !raw) throw new Error("请补齐必填配置项。");
    if (kind === "number") {
      const value = Number(raw);
      if (!Number.isFinite(value)) throw new Error("配置中包含无效数字。");
      if (control.getAttribute("step") === "1" && !Number.isInteger(value)) {
        throw new Error("该配置项必须是整数。");
      }
      return { value };
    }
    return { value: raw };
  }

  function collectPluginConfiguration(instance) {
    const form = instance.closest("form");
    if (!(form instanceof HTMLFormElement)) throw new Error("项目设置表单不存在。");
    const config = {};
    form.querySelectorAll("[data-plugin-config-path]").forEach(control => {
      if (!(control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement || control instanceof HTMLSelectElement)) return;
      if (!control.checkValidity()) {
        control.reportValidity();
        control.focus();
        throw new Error("请检查“任务怎么运行”中的填写内容。");
      }
      const normalized = pluginConfigControlValue(control);
      if (!normalized.omitted) setNestedConfig(config, control.dataset.pluginConfigPath, normalized.value);
    });

    const accountBindings = {};
    form.querySelectorAll("[data-plugin-account-role]").forEach(control => {
      if (!(control instanceof HTMLSelectElement)) return;
      const role = control.dataset.pluginAccountRole || "";
      const selected = [...control.selectedOptions];
      const required = control.dataset.pluginAccountRequired === "true";
      const many = control.dataset.pluginAccountCardinality === "many";
      if (selected.some(option => option.disabled)) {
        control.focus();
        throw new Error("已选账号已停用或登录态无效，请重新选择。");
      }
      const selectedIds = selected.map(option => option.value).filter(Boolean);
      if (!selectedIds.length) {
        if (required) {
          control.focus();
          throw new Error("请为每个必需角色选择可用业务账号。");
        }
        return;
      }
      accountBindings[role] = many ? selectedIds : selectedIds[0];
    });

    const resourceBindings = {};
    form.querySelectorAll("[data-plugin-resource-role]").forEach(control => {
      if (!(control instanceof HTMLSelectElement)) return;
      const role = control.dataset.pluginResourceRole || "";
      const selected = control.selectedOptions[0];
      const required = control.dataset.pluginResourceRequired === "true";
      if (selected?.disabled) {
        control.focus();
        throw new Error("已保存资源不可用或类型不匹配，请重新选择。");
      }
      const resourceId = String(control.value || "").trim();
      if (!resourceId) {
        if (required) {
          control.focus();
          throw new Error("请为每个必需角色选择可用资源。");
        }
        return;
      }
      resourceBindings[role] = resourceId;
    });

    const enabledEntrypoints = [...form.querySelectorAll("[data-plugin-entrypoint]:checked")]
      .map(control => String(control.value || "").trim())
      .filter(Boolean);
    const worker = instance.querySelector("[data-plugin-worker-select]");
    let deviceId = null;
    if (worker instanceof HTMLSelectElement) {
      const selected = worker.selectedOptions[0];
      if (!worker.value) {
        worker.focus();
        throw new Error("请选择在线的工作节点。");
      }
      if (selected?.disabled) {
        worker.focus();
        throw new Error("已选工作节点当前不可用。");
      }
      deviceId = worker.value;
    }

    const kindControl = form.querySelector("[data-plugin-schedule-kind]");
    const enabledControl = form.querySelector("[data-automation-toggle]");
    const scheduleKind = kindControl instanceof HTMLSelectElement ? kindControl.value : "none";
    let times = [];
    if (scheduleKind === "daily_times") {
      times = [...form.querySelectorAll("[data-schedule-time]")]
        .map(control => String(control.value || "").trim())
        .filter(Boolean);
      if (!times.length) {
        addScheduleTimeFocus(form);
        throw new Error("每天指定时间至少需要一个有效时间。");
      }
      if (times.some(value => !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value))) {
        form.querySelector("[data-schedule-time]:invalid")?.focus();
        throw new Error("定时时间格式无效。");
      }
      times = [...new Set(times)].sort();
      const scheduleStack = form.querySelector("[data-plugin-schedule-max]");
      const maximum = Number(scheduleStack?.dataset.pluginScheduleMax || 0);
      if (!Number.isInteger(maximum) || maximum < 1 || times.length > maximum) {
        throw new Error("每天执行时间数量超过动作包允许上限。");
      }
    }
    if (!["none", "daily_times", "startup"].includes(scheduleKind)) {
      throw new Error("当前旧定时无法安全迁移，请先处理调度合同。");
    }
    const schedule = {
      kind: scheduleKind,
      times,
      enabled: scheduleKind === "none"
        ? false
        : enabledControl instanceof HTMLInputElement && enabledControl.checked,
    };
    return {
      config,
      account_bindings: accountBindings,
      resource_bindings: resourceBindings,
      enabled_entrypoints: enabledEntrypoints,
      device_id: deviceId,
      schedule,
    };
  }

  function addScheduleTimeFocus(form) {
    const addSchedule = form.querySelector("[data-add-schedule-time]");
    if (addSchedule instanceof HTMLElement) addSchedule.focus();
  }

  async function savePluginConfiguration(instance, button) {
    if (button.getAttribute("aria-busy") === "true") return;
    const automationId = instance.dataset.automationId || "";
    const configurationVersion = Number(instance.dataset.projectConfigurationVersion || 0);
    if (!Number.isInteger(configurationVersion) || configurationVersion < 1) {
      setPluginConfigurationFeedback(
        instance,
        "项目配置版本已缺失，请刷新页面后重新保存。",
        "error",
      );
      return;
    }
    let settings;
    try {
      settings = collectPluginConfiguration(instance);
    } catch (error) {
      setPluginConfigurationFeedback(
        instance,
        error instanceof Error ? error.message : "项目设置无效，请检查后重试。",
        "error",
      );
      return;
    }
    const requestId = button.dataset.requestId || secureRequestId(null);
    if (!requestId) {
      setPluginConfigurationFeedback(
        instance,
        "当前浏览器无法生成安全请求标识，设置尚未提交。请刷新页面后重试。",
        "error",
      );
      return;
    }
    button.dataset.requestId = requestId;
    setBusy(button, true, "保存中…");
    try {
      const response = await fetch(
        `/automations/plugins/${encodeURIComponent(automationId)}/configuration`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({
            ...settings,
            request_id: requestId,
            expected_project_configuration_version: configurationVersion,
          }),
        },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok || payload?.ok !== true) {
        const error = new Error(responseMessage(
          payload,
          response.status >= 500
            ? "服务器暂时无法确认设置是否保存，请使用当前页面再次点击保存。"
            : "项目设置未保存，请根据提示修正后重试。",
        ));
        error.responseConfirmed = response.status < 500;
        throw error;
      }
      const scheduleRuntimeState = String(
        payload?.data?.schedule_runtime_state || "REFRESH_FAILED",
      ).toUpperCase();
      const schedulerRefreshCompleted = payload?.data?.scheduler_refresh_completed === true;
      const stableRuntime = ["ACTIVE", "DISABLED", "ENTRYPOINT_DISABLED"].includes(
        scheduleRuntimeState,
      );
      if (
        PLUGIN_CONFIGURATION_PENDING_RUNTIME_STATES.has(scheduleRuntimeState)
        || !stableRuntime
        || !schedulerRefreshCompleted
      ) {
        setPluginConfigurationFeedback(
          instance,
          payload.message || "设置已保存，但运行中定时尚未生效；请使用同一请求重试。",
          "warning",
        );
        return;
      }
      const newVersion = Number(payload?.data?.project_configuration_version || 0);
      if (!Number.isInteger(newVersion) || newVersion < 1) {
        setPluginConfigurationFeedback(
          instance,
          "设置可能已经保存，但页面未取得新的配置版本。请刷新页面核对后再继续。",
          "warning",
        );
        return;
      }
      instance.dataset.projectConfigurationVersion = String(newVersion);
      syncPluginConfigurationSavedState(instance, settings);
      delete button.dataset.requestId;
      setPluginConfigurationFeedback(
        instance,
        payload.message || "项目设置已保存。",
        "success",
        { summary: true },
      );
      const form = instance.closest("form");
      form?.dispatchEvent(new CustomEvent("automation:plugin-configuration-saved", {
        bubbles: false,
        detail: { automationId, projectConfigurationVersion: newVersion },
      }));
    } catch (error) {
      if (error?.responseConfirmed === true) delete button.dataset.requestId;
      const message = error instanceof TypeError
        ? "未能连接服务器，设置是否保存尚未确认。请检查网络后再次点击，页面会复用同一请求标识。"
        : error instanceof Error
          ? error.message
          : "项目设置保存失败，请重试。";
      setPluginConfigurationFeedback(instance, message, "error");
    } finally {
      setBusy(button, false, "");
    }
  }

  function initializePluginInstance(instance) {
    const recoverButton = instance.querySelector("[data-plugin-recover-unknown-write]");
    recoverButton?.addEventListener("click", () => {
      if (!(recoverButton instanceof HTMLButtonElement)) return;
      void recoverUnknownWrite(instance, recoverButton);
    });

    const form = instance.closest("form");
    const scheduleKind = form?.querySelector("[data-plugin-schedule-kind]");
    const scheduleStack = form?.querySelector("[data-schedule-stack]");
    const addSchedule = form?.querySelector("[data-add-schedule-time]");
    const scheduleToggle = form?.querySelector("[data-automation-toggle]");
    const scheduleEffect = form?.querySelector("[data-plugin-schedule-effect]");
    const syncScheduleVisibility = () => {
      const showTimes = scheduleKind instanceof HTMLSelectElement && scheduleKind.value === "daily_times";
      if (scheduleStack instanceof HTMLElement) scheduleStack.hidden = !showTimes;
      if (addSchedule instanceof HTMLElement) addSchedule.hidden = !showTimes;
      const schedulerEntrypoints = [...(form?.querySelectorAll("[data-plugin-entrypoint]") || [])]
        .filter(control => (
          control instanceof HTMLInputElement
          && control.dataset.pluginEntrypointKind === "scheduler"
        ));
      const scheduleRequested = scheduleKind instanceof HTMLSelectElement
        && scheduleKind.value !== "none"
        && scheduleToggle instanceof HTMLInputElement
        && scheduleToggle.checked;
      if (scheduleEffect instanceof HTMLElement) {
        scheduleEffect.hidden = !(
          scheduleRequested
          && schedulerEntrypoints.length > 0
          && !schedulerEntrypoints.some(control => control.checked)
        );
      }
    };
    syncScheduleVisibility();
    scheduleKind?.addEventListener("change", syncScheduleVisibility);
    scheduleToggle?.addEventListener("change", syncScheduleVisibility);

    const syncEntrypointState = control => {
      if (!(control instanceof HTMLInputElement) || !control.matches("[data-plugin-entrypoint]")) return;
      const state = control.closest("label")?.querySelector("[data-plugin-entrypoint-state]");
      if (state) state.textContent = control.checked ? "开启" : "关闭";
    };
    form?.querySelectorAll("[data-plugin-entrypoint]").forEach(control => {
      syncEntrypointState(control);
      control.addEventListener("change", () => {
        syncEntrypointState(control);
        syncScheduleVisibility();
      });
    });
  }

  function initializePluginConfigurationDelegation() {
    const root = document.documentElement;
    if (root?.dataset.pluginConfigurationDelegated === "true") return;
    if (root) root.dataset.pluginConfigurationDelegated = "true";

    const markFromControl = event => {
      const control = event.target instanceof Element ? event.target : null;
      if (!control?.matches(PLUGIN_CONFIGURATION_CONTROL_SELECTOR)) return;
      if (control.dataset.pluginConfigPath) control.dataset.pluginConfigTouched = "true";
      const context = pluginConfigurationContext(control);
      if (context) markPluginConfigurationDirty(context.instance);
    };
    document.addEventListener("input", markFromControl);
    document.addEventListener("change", markFromControl);
    document.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const save = target?.closest("[data-plugin-configuration-save]");
      if (save instanceof HTMLButtonElement) {
        const context = pluginConfigurationContext(save);
        if (context) void savePluginConfiguration(context.instance, save);
        return;
      }
      const scheduleAction = target?.closest(
        "[data-add-schedule-time], [data-remove-schedule-time]",
      );
      if (!scheduleAction) return;
      const context = pluginConfigurationContext(scheduleAction);
      if (context) window.setTimeout(() => markPluginConfigurationDirty(context.instance), 0);
    });
  }

  function initializePlugins() {
    initializePluginConfigurationDelegation();
    document.querySelectorAll("[data-plugin-instance]").forEach(initializePluginInstance);
  }
  function initialize() {
    initializePlugins();
    const panels = [...document.querySelectorAll("[data-automation-project-governance]")];
    panels.forEach(initializeGovernance);
    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (!entry.isIntersecting) return;
          observer.unobserve(entry.target);
          void loadPending(entry.target);
        });
      }, { rootMargin: "240px" });
      panels.forEach(panel => observer.observe(panel));
    } else {
      panels.forEach(panel => void loadPending(panel));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
