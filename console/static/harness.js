(function () {
  "use strict";

  const page = document.querySelector("[data-harness-page]");
  if (!page) return;

  const form = page.querySelector("[data-harness-form]");
  const messageInput = page.querySelector("[data-harness-message]");
  const submitButton = page.querySelector("[data-harness-submit]");
  const submitLabel = submitButton?.querySelector("span");
  const resetButton = page.querySelector("[data-harness-reset]");
  const feedback = page.querySelector("[data-harness-feedback]");
  const stateLabel = page.querySelector("[data-harness-state-label]");
  const stateBadge = page.querySelector("[data-harness-state]");
  const sessionNote = page.querySelector("[data-harness-session]");
  const outputState = page.querySelector("[data-harness-output-state]");
  const toolsCount = page.querySelector("[data-harness-tools-count]");
  const toolsList = page.querySelector("[data-harness-tools]");
  const processContent = page.querySelector("[data-harness-process]");
  const evidenceContent = page.querySelector("[data-harness-evidence]");
  const resultContent = page.querySelector("[data-harness-result]");
  const toolSummariesContent = page.querySelector("[data-harness-tool-summaries]");

  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const MAX_MESSAGE_CHARS = 4000;
  const HIDDEN_KEYS = new Set([
    "account_id",
    "actor",
    "actor_roles",
    "automation_id",
    "command_type",
    "contribution_id",
    "contract_hash",
    "file_path",
    "filename",
    "operation",
    "path",
    "plan_hash",
    "provider_id",
    "resource_id",
    "service",
    "source_code",
    "task_id",
    "tool_name",
  ]);
  const KEY_LABELS = Object.freeze({
    assistant_message: "助手摘要",
    availability: "可用性",
    blocked_reason: "受限原因",
    created_at: "时间",
    description: "说明",
    message_id: "消息标识",
    next_poll_after_ms: "下次检查",
    persistence_status: "会话保存",
    read_only: "只读状态",
    result: "结果",
    status: "状态",
    summary: "摘要",
    title: "标题",
    tool_calls: "工具调用次数",
  });

  let sessionId = "";
  let busy = false;

  class HarnessRequestError extends Error {
    constructor(code, message, status) {
      super(message);
      this.name = "HarnessRequestError";
      this.code = code;
      this.status = status;
    }
  }

  function createElement(tagName, className) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    return element;
  }

  function appendText(parent, tagName, className, value) {
    const element = createElement(tagName, className);
    element.textContent = String(value == null ? "" : value);
    parent.append(element);
    return element;
  }

  function clearAndAppendEmpty(container, message) {
    if (!container) return;
    container.replaceChildren();
    appendText(container, "p", "harness-empty", message);
  }

  function canonicalUuid(value) {
    const candidate = typeof value === "string" ? value : "";
    return UUID_PATTERN.test(candidate) ? candidate : "";
  }

  function requestUuid() {
    const generator = window.crypto && window.crypto.randomUUID;
    if (typeof generator !== "function") {
      throw new HarnessRequestError(
        "BROWSER_UUID_UNAVAILABLE",
        "当前浏览器无法生成安全请求标识，操作未提交。",
        0,
      );
    }
    const value = generator.call(window.crypto);
    const normalized = canonicalUuid(value);
    if (!normalized) {
      throw new HarnessRequestError(
        "BROWSER_UUID_INVALID",
        "浏览器生成的请求标识无效，操作未提交。",
        0,
      );
    }
    return normalized;
  }

  function asObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  }

  function errorMessage(payload, fallback) {
    const error = asObject(payload && payload.error);
    const message = error && typeof error.message === "string" ? error.message.trim() : "";
    return message || fallback;
  }

  async function postJson(path, body) {
    let response;
    try {
      response = await window.fetch(path, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        credentials: "same-origin",
        body: JSON.stringify(body),
      });
    } catch (_error) {
      throw new HarnessRequestError(
        "HARNESS_UNAVAILABLE",
        "Harness 服务暂时不可达，未执行任何业务写入。",
        0,
      );
    }

    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      throw new HarnessRequestError(
        "INVALID_HARNESS_RESPONSE",
        "Harness 返回了无法读取的响应，未显示为成功。",
        response.status,
      );
    }
    if (!asObject(payload) || payload.ok !== true || !asObject(payload.data)) {
      const error = asObject(payload && payload.error);
      const code = error && typeof error.code === "string" ? error.code : "HARNESS_UPSTREAM_ERROR";
      throw new HarnessRequestError(
        code,
        errorMessage(payload, "Harness 请求未成功，未显示为成功。"),
        response.status,
      );
    }
    return payload.data;
  }

  function setFeedback(message, kind) {
    if (!feedback) return;
    feedback.replaceChildren();
    feedback.classList.remove("is-error", "is-success", "is-info");
    if (!message) {
      feedback.hidden = true;
      return;
    }
    feedback.hidden = false;
    feedback.classList.add(`is-${kind || "info"}`);
    feedback.textContent = message;
  }

  function setState(label, stateClass) {
    if (stateLabel) stateLabel.textContent = label;
    if (stateBadge) {
      stateBadge.classList.remove("is-ready", "is-busy", "is-error", "is-gated");
      if (stateClass) stateBadge.classList.add(`is-${stateClass}`);
    }
  }

  function setOutputState(label, stateClass) {
    if (!outputState) return;
    outputState.textContent = label;
    outputState.classList.remove("is-ready", "is-error", "is-gated");
    if (stateClass) outputState.classList.add(`is-${stateClass}`);
  }

  function setBusy(value) {
    busy = value;
    if (!submitButton) return;
    submitButton.disabled = value;
    submitButton.setAttribute("aria-busy", value ? "true" : "false");
    if (submitLabel) submitLabel.textContent = value ? "处理中…" : sessionId ? "发送只读查询" : "创建会话并发送";
    if (resetButton) {
      resetButton.disabled = value || !sessionId;
      resetButton.setAttribute("aria-disabled", resetButton.disabled ? "true" : "false");
    }
  }

  function setSession(session) {
    sessionId = session;
    if (sessionNote) {
      sessionNote.textContent = session
        ? "会话状态：已建立（内存保存）；生产数据和真实模型均未声明可用。"
        : "会话状态：尚未建立；生产数据和真实模型均未声明可用。";
    }
    if (resetButton) {
      resetButton.disabled = !session || busy;
      resetButton.setAttribute("aria-disabled", resetButton.disabled ? "true" : "false");
    }
    if (submitLabel && !busy) submitLabel.textContent = session ? "发送只读查询" : "创建会话并发送";
  }

  function displayLabel(key) {
    return KEY_LABELS[key] || key.replaceAll("_", " ");
  }

  function isHiddenKey(key) {
    return HIDDEN_KEYS.has(String(key));
  }

  function renderValue(container, value, depth) {
    const level = depth || 0;
    if (level > 5) {
      appendText(container, "p", "harness-value", "内容层级过深，已停止展开。 ");
      return;
    }
    if (value == null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      appendText(container, "p", "harness-value", value == null ? "无数据" : String(value));
      return;
    }
    if (Array.isArray(value)) {
      if (!value.length) {
        appendText(container, "p", "harness-empty", "无条目。 ");
        return;
      }
      const list = createElement("ul", "harness-value-list");
      value.forEach((item) => {
        const row = createElement("li");
        renderValue(row, item, level + 1);
        list.append(row);
      });
      container.append(list);
      return;
    }
    const objectValue = asObject(value);
    if (!objectValue) {
      appendText(container, "p", "harness-value", "无可展示数据。 ");
      return;
    }
    const entries = Object.entries(objectValue).filter(([key]) => !isHiddenKey(key));
    if (!entries.length) {
      appendText(container, "p", "harness-empty", "无可展示数据。 ");
      return;
    }
    const definitionList = createElement("dl", "harness-value-list harness-value-list--definition");
    entries.forEach(([key, item]) => {
      const wrapper = createElement("div", "harness-value-definition");
      appendText(wrapper, "dt", "", displayLabel(key));
      const definition = createElement("dd");
      renderValue(definition, item, level + 1);
      wrapper.append(definition);
      definitionList.append(wrapper);
    });
    container.append(definitionList);
  }

  function renderOutput(container, value, emptyMessage) {
    if (!container) return;
    container.replaceChildren();
    if (value == null || value === "" || (Array.isArray(value) && !value.length)) {
      appendText(container, "p", "harness-empty", emptyMessage);
      return;
    }
    renderValue(container, value, 0);
  }

  function renderTools(tools) {
    if (!toolsList || !toolsCount) return;
    toolsList.replaceChildren();
    const items = Array.isArray(tools) ? tools : [];
    toolsCount.textContent = items.length ? `${items.length} 项` : "不可用";
    if (!items.length) {
      appendText(toolsList, "p", "harness-empty", "当前没有可展示的受限工具。 ");
      return;
    }
    items.forEach((value) => {
      const tool = asObject(value) || {};
      const item = createElement("article", "harness-tool");
      const title = typeof tool.title === "string" && tool.title.trim() ? tool.title : "只读工具";
      appendText(item, "h4", "", title);
      if (typeof tool.description === "string" && tool.description.trim()) {
        appendText(item, "p", "", tool.description);
      }
      appendText(item, "span", "harness-tool-effect", "只读");
      toolsList.append(item);
    });
  }

  function productionGated(data) {
    const values = [data && data.status, data && data.availability, data && data.blocked_reason];
    return values.some((value) => {
      if (typeof value === "string") return value.toUpperCase().includes("PRODUCTION_GATED");
      const objectValue = asObject(value);
      return objectValue && Object.values(objectValue).some((nested) => (
        typeof nested === "string" && nested.toUpperCase().includes("PRODUCTION_GATED")
      ));
    });
  }

  function renderResponse(data) {
    const response = asObject(data) || {};
    const returnedSessionId = canonicalUuid(response.session_id);
    if (returnedSessionId && sessionId && returnedSessionId !== sessionId) {
      throw new HarnessRequestError(
        "INVALID_HARNESS_RESPONSE",
        "Agent 返回了不匹配的会话，结果未显示。",
        502,
      );
    }
    if (returnedSessionId) setSession(returnedSessionId);
    if (response.tools !== undefined) renderTools(response.tools);
    renderOutput(processContent, response.process || response.status || response.availability, "当前没有可展示的过程摘要。 ");
    renderOutput(evidenceContent, response.evidence, "当前没有可展示的证据。 ");
    renderOutput(resultContent, response.result || response.assistant_message, "当前没有可展示的结果。 ");
    renderOutput(toolSummariesContent, response.tool_summaries || response.tool_calls, "本次响应尚无工具摘要。 ");

    if (productionGated(response)) {
      setState("生产能力未开放", "gated");
      setOutputState("PRODUCTION_GATED", "gated");
      setFeedback("当前 Harness 仅返回受限状态，生产能力尚未开放。", "info");
    } else {
      setState("会话可用", "ready");
      setOutputState("已返回受限结果", "ready");
    }
  }

  function clearOutput() {
    clearAndAppendEmpty(processContent, "发送消息后，这里显示受限处理摘要。 ");
    clearAndAppendEmpty(evidenceContent, "当前没有可展示的证据。 ");
    clearAndAppendEmpty(resultContent, "查询结果会显示在这里。 ");
    clearAndAppendEmpty(toolSummariesContent, "本次响应尚无工具摘要。 ");
    if (toolsList) {
      toolsList.replaceChildren();
      appendText(toolsList, "p", "harness-empty", "建立会话后显示受限工具摘要。 ");
    }
    if (toolsCount) toolsCount.textContent = "未加载";
    setOutputState("等待查询", "");
  }

  function describeError(error) {
    const code = String(error && error.code || "").toUpperCase();
    if (code.includes("PRODUCTION_GATED")) return "当前生产能力尚未开放，Harness 未执行任何生产操作。";
    if (code.includes("SANDBOX") || code.includes("UNAVAILABLE") || code.includes("UNREACHABLE")) {
      return "受限 Harness 当前不可用，未执行任何业务写入。";
    }
    return String(error && error.message || "Harness 请求失败，未显示为成功。");
  }

  async function createSession() {
    const data = await postJson("/harness/sessions", { request_uuid: requestUuid() });
    const createdSessionId = canonicalUuid(data.session_id);
    if (!createdSessionId) {
      throw new HarnessRequestError(
        "INVALID_HARNESS_RESPONSE",
        "Agent 未返回有效会话，结果未显示。",
        502,
      );
    }
    setSession(createdSessionId);
    renderResponse(data);
    return createdSessionId;
  }

  async function sendMessage(message) {
    const activeSessionId = sessionId || await createSession();
    const data = await postJson("/harness/messages", {
      request_uuid: requestUuid(),
      session_id: activeSessionId,
      message,
    });
    renderResponse(data);
  }

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !messageInput) return;
    const message = String(messageInput.value || "").trim();
    if (!message || message.length > MAX_MESSAGE_CHARS) {
      setFeedback("请输入消息，且长度不得超过 4000 个字符。", "error");
      messageInput.focus();
      return;
    }
    setBusy(true);
    setState(sessionId ? "正在处理" : "正在建立会话", "busy");
    setOutputState("处理中…", "");
    setFeedback("正在通过受限 Harness 处理，请稍候。", "info");
    try {
      await sendMessage(message);
      setFeedback("响应已返回；以上内容仅代表受限只读投影。", "success");
    } catch (error) {
      setState(
        String(error && error.code || "").toUpperCase().includes("PRODUCTION_GATED")
          ? "生产能力未开放"
          : "当前不可用",
        String(error && error.code || "").toUpperCase().includes("PRODUCTION_GATED") ? "gated" : "error",
      );
      setOutputState("未返回结果", "error");
      setFeedback(describeError(error), "error");
    } finally {
      setBusy(false);
    }
  });

  resetButton?.addEventListener("click", () => {
    if (busy) return;
    setSession("");
    clearOutput();
    setState("尚未建立会话", "");
    setFeedback("会话已在浏览器中清空；没有发送清理请求。", "info");
    messageInput?.focus();
  });

  setSession("");
  setBusy(false);
})();
