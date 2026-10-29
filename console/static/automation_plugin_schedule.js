(() => {
  "use strict";
  const pageRoot = document.querySelector(".main-content:not([hidden]) .auto-page");
  if (!pageRoot || pageRoot.dataset.approvalInitialized) return;
  pageRoot.dataset.approvalInitialized = "true";
  const fetch = window.ConsoleUI.pageRequest();

  const PLUGIN_CONFIGURATION_CONTROL_SELECTOR = [
    "[data-plugin-schedule-kind]",
    "[data-automation-toggle]",
    "[data-schedule-time]",
  ].join(", ");
  const PLUGIN_CONFIGURATION_PENDING_RUNTIME_STATES = new Set([
    "BLOCKED_GENERATION",
    "REFRESH_FAILED",
  ]);

  function responseMessage(payload, fallback) {
    const code = String(payload?.error?.code || payload?.error_code || "");
    if (code === "PROJECT_CONFIGURATION_VERSION_CONFLICT") {
      return "项目配置已被其他管理员更新，请刷新页面后重新确认。";
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

  function markPluginConfigurationDirty(instance, { keepFeedback = false } = {}) {
    const form = instance.closest("form");
    const configurationSave = form?.querySelector("[data-plugin-configuration-save]");
    const runButton = form?.querySelector("[data-run-now]");
    // An unconfirmed save keeps its request ID and message so the retry is idempotent.
    if (!keepFeedback) {
      if (configurationSave instanceof HTMLButtonElement) {
        delete configurationSave.dataset.requestId;
      }
      setPluginConfigurationFeedback(
        instance,
        "定时计划尚未保存；保存后才能按新计划运行。",
        "warning",
      );
    }
    if (runButton instanceof HTMLButtonElement && !runButton.disabled) {
      runButton.dataset.runTitleBeforePluginConfig = runButton.getAttribute("title") || "";
      runButton.disabled = true;
      runButton.dataset.disabledForPluginConfig = "true";
      runButton.title = "请先保存项目设置";
    }
  }

  function restoreRunButtonAfterPluginConfig(form) {
    const runButton = form?.querySelector("[data-run-now]");
    if (!(runButton instanceof HTMLButtonElement) || runButton.dataset.disabledForPluginConfig !== "true") return;
    const previousTitle = runButton.dataset.runTitleBeforePluginConfig || "";
    if (previousTitle) runButton.title = previousTitle;
    else runButton.removeAttribute("title");
    delete runButton.dataset.disabledForPluginConfig;
    delete runButton.dataset.runTitleBeforePluginConfig;
    runButton.disabled = false;
  }

  function secureRequestId(feedback) {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    setFeedback(feedback, "当前浏览器无法生成安全请求标识，操作未提交。", "error");
    return "";
  }

  function collectPluginSchedule(instance) {
    const form = instance.closest("form");
    if (!(form instanceof HTMLFormElement)) throw new Error("项目设置表单不存在。");
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
    return {
      kind: scheduleKind,
      times,
      enabled: scheduleKind === "none"
        ? false
        : enabledControl instanceof HTMLInputElement && enabledControl.checked,
    };
  }

  function expectedScheduleField(instance) {
    // Legacy rows have no Agent digest and keep the full configuration path.
    const digest = instance.dataset.projectScheduleSha256;
    return typeof digest === "string" && /^[0-9a-f]{64}$/.test(digest)
      ? { expected_schedule_sha256: digest }
      : {};
  }

  function addScheduleTimeFocus(form) {
    const addSchedule = form.querySelector("[data-add-schedule-time]");
    if (addSchedule instanceof HTMLElement) addSchedule.focus();
  }

  // Returns "saved", "pending" (stored, scheduler not yet refreshed),
  // "rejected" (nothing stored) or "unconfirmed" (outcome unknown).
  async function savePluginConfiguration(instance, button, { fromToggle = false } = {}) {
    if (button.getAttribute("aria-busy") === "true") return "rejected";
    const feedback = (message, kind, success = false) => setPluginConfigurationFeedback(
      instance, message, kind, { summary: fromToggle || success },
    );
    const automationId = instance.dataset.automationId || "";
    const configurationVersion = Number(instance.dataset.projectConfigurationVersion || 0);
    if (!Number.isInteger(configurationVersion) || configurationVersion < 1) {
      feedback("项目配置版本已缺失，请刷新页面后重新保存。", "error");
      return "rejected";
    }
    let settings;
    try {
      settings = collectPluginSchedule(instance);
    } catch (error) {
      feedback(error instanceof Error ? error.message : "项目设置无效，请检查后重试。", "error");
      return "rejected";
    }
    const requestId = button.dataset.requestId || secureRequestId(null);
    if (!requestId) {
      feedback("当前浏览器无法生成安全请求标识，设置尚未提交。请刷新页面后重试。", "error");
      return "rejected";
    }
    button.dataset.requestId = requestId;
    setBusy(button, true, "保存中…");
    try {
      // Document-level delegation outlives the page that installed it. Bind
      // this save to the page active when the operator clicks the button.
      const saveRequest = window.ConsoleUI.pageRequest();
      const response = await saveRequest(
        `/automations/plugins/${encodeURIComponent(automationId)}/schedule`,
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
            schedule: settings,
            request_id: requestId,
            expected_project_configuration_version: configurationVersion,
            ...expectedScheduleField(instance),
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
        feedback(
          payload.message || "设置已保存，但运行中定时尚未生效；请使用同一请求重试。",
          "warning",
        );
        return "pending";
      }
      const newVersion = Number(payload?.data?.project_configuration_version || 0);
      const scheduleSha256 = payload?.data?.schedule_sha256;
      if (!Number.isInteger(newVersion) || newVersion < 1
          || typeof scheduleSha256 !== "string" || !/^[0-9a-f]{64}$/.test(scheduleSha256)) {
        feedback("设置可能已经保存，但页面未取得新的配置版本。请刷新页面核对后再继续。", "warning");
        return "unconfirmed";
      }
      instance.dataset.projectConfigurationVersion = String(newVersion);
      // The server's committed digest is the next times-only CAS base.
      instance.dataset.projectScheduleSha256 = scheduleSha256;
      delete button.dataset.requestId;
      feedback(payload.message || "定时计划已保存。", "success", true);
      const form = instance.closest("form");
      restoreRunButtonAfterPluginConfig(form);
      form?.dispatchEvent(new CustomEvent("automation:plugin-configuration-saved", {
        bubbles: false,
        detail: { automationId, projectConfigurationVersion: newVersion, fromToggle },
      }));
      return "saved";
    } catch (error) {
      if (error?.responseConfirmed === true) delete button.dataset.requestId;
      const message = error instanceof TypeError
        ? "未能连接服务器，设置是否保存尚未确认。请检查网络后再次点击，页面会复用同一请求标识。"
        : error instanceof Error
          ? error.message
          : "项目设置保存失败，请重试。";
      feedback(message, "error");
      return error?.responseConfirmed === true ? "rejected" : "unconfirmed";
    } finally {
      setBusy(button, false, "");
    }
  }

  async function savePluginScheduleToggle(instance, toggle) {
    const form = instance.closest("form");
    const button = form?.querySelector("[data-plugin-configuration-save]");
    const enabled = toggle.checked;
    const revert = message => {
      toggle.checked = !enabled;
      toggle.dispatchEvent(new CustomEvent("automation:schedule-toggle-reverted", { bubbles: true }));
      if (message) setPluginConfigurationFeedback(instance, message, "error", { summary: true });
    };
    if (button instanceof HTMLButtonElement && button.getAttribute("aria-busy") === "true") {
      revert("上一次保存尚未完成，请稍后再拨动开关。");
      return;
    }
    if (!(button instanceof HTMLButtonElement) || button.disabled) {
      revert("当前账号或插件状态不允许修改定时。");
      return;
    }
    const scheduleKind = form.querySelector("[data-plugin-schedule-kind]");
    if (enabled && (!(scheduleKind instanceof HTMLSelectElement) || scheduleKind.value === "none")) {
      revert("还没有设置运行时间，请展开设置选择运行时间并保存定时计划。");
      return;
    }
    toggle.disabled = true;
    setPluginConfigurationFeedback(instance, enabled ? "正在开启定时…" : "正在关闭定时…", "", { summary: true });
    try {
      const outcome = await savePluginConfiguration(instance, button, { fromToggle: true });
      if (outcome === "rejected") revert("");
      // The stored state is unknown: keep the operator's choice and require
      // an explicit retry through the save button with the same request ID.
      if (outcome === "unconfirmed") markPluginConfigurationDirty(instance, { keepFeedback: true });
    } finally {
      toggle.disabled = false;
    }
  }

  function initializePluginInstance(instance) {
    const form = instance.closest("form");
    const scheduleKind = form?.querySelector("[data-plugin-schedule-kind]");
    const scheduleStack = form?.querySelector("[data-schedule-stack]");
    const addSchedule = form?.querySelector("[data-add-schedule-time]");
    const syncScheduleVisibility = () => {
      const showTimes = scheduleKind instanceof HTMLSelectElement && scheduleKind.value === "daily_times";
      if (scheduleStack instanceof HTMLElement) scheduleStack.hidden = !showTimes;
      if (addSchedule instanceof HTMLElement) addSchedule.hidden = !showTimes;
    };
    syncScheduleVisibility();
    scheduleKind?.addEventListener("change", syncScheduleVisibility);
  }

  function initializePluginConfigurationDelegation() {
    const root = document.documentElement;
    if (root?.dataset.pluginConfigurationDelegated === "true") return;
    if (root) root.dataset.pluginConfigurationDelegated = "true";

    const markFromControl = event => {
      const control = event.target instanceof Element ? event.target : null;
      if (!control?.matches(PLUGIN_CONFIGURATION_CONTROL_SELECTOR)) return;
      const context = pluginConfigurationContext(control);
      if (!context) return;
      // The card's schedule switch saves itself; other controls wait for "保存".
      if (control.matches("[data-automation-toggle]")) {
        if (event.type === "change") void savePluginScheduleToggle(context.instance, control);
        return;
      }
      markPluginConfigurationDirty(context.instance);
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
    pageRoot.querySelectorAll("[data-plugin-instance]").forEach(initializePluginInstance);
  }
  function initialize() {
    initializePlugins();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
