(function () {
  "use strict";

  const page = document.querySelector("[data-harness-page]");
  if (!page) return;

  const form = page.querySelector("[data-harness-form]");
  const messageInput = page.querySelector("[data-harness-message]");
  const submitButton = page.querySelector("[data-harness-submit]");
  const resetButton = page.querySelector("[data-harness-reset]");
  const feedback = page.querySelector("[data-harness-feedback]");
  const modelSettingsLink = page.querySelector("[data-harness-model-settings]");
  const stateLabel = page.querySelector("[data-harness-state-label]");
  const sessionNote = page.querySelector("[data-harness-session]");
  const thread = page.querySelector("[data-harness-thread]");
  const welcome = page.querySelector("[data-harness-welcome]");
  const toolsCount = page.querySelector("[data-harness-tools-count]");
  const toolsList = page.querySelector("[data-harness-tools]");

  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const MAX_MESSAGE_CHARS = 4000;

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

  function canonicalUuid(value) {
    const candidate = typeof value === "string" ? value : "";
    return UUID_PATTERN.test(candidate) ? candidate : "";
  }

  function requestUuid() {
    const generator = window.crypto && window.crypto.randomUUID;
    if (typeof generator !== "function") {
      throw new HarnessRequestError(
        "BROWSER_UUID_UNAVAILABLE",
        "当前浏览器无法生成安全请求标识，消息未发送。",
        0,
      );
    }
    const normalized = canonicalUuid(generator.call(window.crypto));
    if (!normalized) {
      throw new HarnessRequestError(
        "BROWSER_UUID_INVALID",
        "浏览器生成的请求标识无效，消息未发送。",
        0,
      );
    }
    return normalized;
  }

  function asObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  }

  async function postJson(path, body) {
    let response;
    try {
      response = await window.fetch(path, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(body),
      });
    } catch (_error) {
      throw new HarnessRequestError(
        "HARNESS_UNREACHABLE",
        "AI 助手服务暂时无法连接，未执行任何业务操作。",
        0,
      );
    }

    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      throw new HarnessRequestError(
        "INVALID_HARNESS_RESPONSE",
        "AI 助手返回了无法读取的响应。",
        response.status,
      );
    }
    if (!asObject(payload) || payload.ok !== true || !asObject(payload.data)) {
      const error = asObject(payload && payload.error);
      const code = error && typeof error.code === "string" ? error.code : "HARNESS_UPSTREAM_ERROR";
      const message = error && typeof error.message === "string" ? error.message.trim() : "";
      throw new HarnessRequestError(code, message || "AI 助手请求未成功。", response.status);
    }
    return payload.data;
  }

  function setFeedback(message, kind) {
    if (!feedback) return;
    feedback.classList.remove("is-error", "is-success", "is-info");
    if (!message) {
      feedback.hidden = true;
      feedback.textContent = "";
      return;
    }
    feedback.hidden = false;
    feedback.classList.add(`is-${kind || "info"}`);
    feedback.textContent = message;
  }

  function showModelSettings(visible) {
    if (modelSettingsLink) modelSettingsLink.hidden = !visible;
  }

  function setState(label) {
    if (stateLabel) stateLabel.textContent = label;
  }

  function setBusy(value) {
    busy = value;
    if (submitButton) {
      submitButton.disabled = value;
      submitButton.setAttribute("aria-busy", value ? "true" : "false");
    }
    if (resetButton) {
      resetButton.disabled = value || !sessionId;
      resetButton.setAttribute("aria-disabled", resetButton.disabled ? "true" : "false");
    }
  }

  function setSession(value) {
    sessionId = value;
    if (resetButton) {
      resetButton.disabled = !value || busy;
      resetButton.setAttribute("aria-disabled", resetButton.disabled ? "true" : "false");
    }
    if (sessionNote) {
      sessionNote.textContent = value
        ? "当前为只读会话，回复请结合原始业务系统复核。"
        : "AI 助手仅使用已开放的只读能力，回复请结合原始业务系统复核。";
    }
  }

  function scrollConversation() {
    if (!thread) return;
    window.requestAnimationFrame(() => {
      thread.scrollTop = thread.scrollHeight;
    });
  }

  function appendMessage(role, message, kind) {
    if (!thread) return;
    if (welcome) welcome.hidden = true;
    const article = createElement("article", `harness-message harness-message--${role}`);
    if (kind === "error") article.classList.add("harness-message--error");
    const body = createElement("div", "harness-message-body");
    appendText(body, "span", "harness-message-label", role === "user" ? "你" : "AI 助手");
    appendText(body, "p", "harness-message-copy", message);
    article.append(body);
    thread.append(article);
    scrollConversation();
  }

  function renderTools(tools) {
    if (!toolsList || !toolsCount) return;
    toolsList.replaceChildren();
    const items = Array.isArray(tools) ? tools : [];
    toolsCount.textContent = items.length ? `${items.length} 项` : "暂无可用查询";
    items.forEach((value) => {
      const tool = asObject(value) || {};
      const title = typeof tool.title === "string" ? tool.title.trim() : "";
      if (!title) return;
      const button = appendText(toolsList, "button", "harness-tool-prompt", title);
      button.type = "button";
      button.addEventListener("click", () => {
        if (!messageInput || busy) return;
        const examples = {
          "knowledge.search": "帮我查询业务知识：",
          "waybill.lookup": "帮我查一下这个运单：",
          "tracking.lookup": "帮我查一下物流轨迹：",
          "work_items.list_open": "帮我看看现在有哪些待处理事项",
          "runs.get_summary": "帮我查看这个任务的运行结果：",
          "artifact.inspect": "帮我查看这条运行证据：",
        };
        messageInput.value = examples[String(tool.tool_id || "")] || `帮我${title}`;
        resizeComposer();
        messageInput.focus();
      });
    });
  }

  function unavailableStatus(data) {
    const response = asObject(data) || {};
    if (String(response.status || "").toUpperCase() !== "CAPABILITY_UNAVAILABLE") return "";
    return String(response.blocked_reason || response.availability || "CAPABILITY_UNAVAILABLE");
  }

  function responseText(response) {
    const value = response.result !== undefined ? response.result : response.assistant_message;
    if (typeof value === "string" && value.trim()) return value.trim();
    if (value == null) return "查询已完成，但没有可展示的结果。";
    try {
      return JSON.stringify(value, null, 2);
    } catch (_error) {
      return "查询已完成，结果无法展示。";
    }
  }

  function renderResponse(data) {
    const response = asObject(data) || {};
    const returnedSessionId = canonicalUuid(response.session_id);
    if (returnedSessionId && sessionId && returnedSessionId !== sessionId) {
      throw new HarnessRequestError("INVALID_HARNESS_RESPONSE", "智能服务返回了不匹配的会话。", 502);
    }
    if (returnedSessionId) setSession(returnedSessionId);
    if (response.tools !== undefined) renderTools(response.tools);
    appendMessage("assistant", responseText(response));
    setState("可以继续提问");
    showModelSettings(false);
  }

  function describeError(error) {
    const code = String(error && error.code || "").toUpperCase();
    if (code.includes("MODEL_NOT_CONFIGURED")) {
      return "尚未启用智能模型，请先打开“智能模型”完成配置。";
    }
    if (code.includes("MODEL_UNAVAILABLE") || code.includes("TIMEOUT")) {
      return "智能模型暂时无法连接，请稍后重试。";
    }
    if (code.includes("CAPABILITY_UNAVAILABLE") || code.includes("SIDECAR") || code.includes("UNREACHABLE")) {
      return "AI 助手当前无法启动只读会话，请稍后重试或联系系统管理员。未执行任何业务操作。";
    }
    if (code.includes("LIMIT_EXCEEDED")) {
      return "这次问题需要查询的内容过多，请缩小范围后重试。";
    }
    return String(error && error.message || "AI 助手请求失败，未执行任何业务操作。");
  }

  async function createSession() {
    const data = await postJson("/harness/sessions", { request_uuid: requestUuid() });
    const createdSessionId = canonicalUuid(data.session_id);
    if (!createdSessionId) {
      throw new HarnessRequestError("INVALID_HARNESS_RESPONSE", "智能服务未返回有效会话。", 502);
    }
    setSession(createdSessionId);
    renderTools(data.tools);
    const unavailable = unavailableStatus(data);
    if (unavailable) {
      throw new HarnessRequestError(unavailable, "AI 助手暂时无法连接。", 503);
    }
    setState("可以开始提问");
    showModelSettings(false);
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

  function resetConversation() {
    if (thread) thread.querySelectorAll(".harness-message").forEach((item) => item.remove());
    if (welcome) welcome.hidden = false;
    if (toolsList) toolsList.replaceChildren();
    if (toolsCount) toolsCount.textContent = "未加载";
    setSession("");
    setState("等待提问");
    setFeedback("", "info");
    initializeSession();
  }

  async function initializeSession() {
    if (busy || sessionId) return;
    setBusy(true);
    setState("正在连接");
    showModelSettings(false);
    try {
      await createSession();
    } catch (error) {
      const code = String(error && error.code || "").toUpperCase();
      setState(code.includes("MODEL_NOT_CONFIGURED") ? "模型未启用" : "暂时无法连接");
      setFeedback(describeError(error), "error");
      showModelSettings(code.includes("MODEL_NOT_CONFIGURED"));
    } finally {
      setBusy(false);
    }
  }

  function resizeComposer() {
    if (!messageInput) return;
    messageInput.style.height = "auto";
    messageInput.style.height = `${Math.min(messageInput.scrollHeight, 180)}px`;
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
    appendMessage("user", message);
    messageInput.value = "";
    resizeComposer();
    setBusy(true);
    setState(sessionId ? "正在查询" : "正在建立会话");
    setFeedback("", "info");
    try {
      await sendMessage(message);
    } catch (error) {
      const readable = describeError(error);
      appendMessage("assistant", readable, "error");
      const code = String(error && error.code || "").toUpperCase();
      setState(code.includes("MODEL_NOT_CONFIGURED") ? "模型未启用" : "暂时无法连接");
      showModelSettings(code.includes("MODEL_NOT_CONFIGURED"));
      setFeedback("", "error");
    } finally {
      setBusy(false);
      messageInput.focus();
    }
  });

  messageInput?.addEventListener("input", resizeComposer);
  messageInput?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    form?.requestSubmit();
  });

  resetButton?.addEventListener("click", () => {
    if (busy) return;
    resetConversation();
    messageInput?.focus();
  });

  setSession("");
  setBusy(false);
  resizeComposer();
  initializeSession();
})();
