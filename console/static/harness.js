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
  let pendingMessage = null;
  const pluginCards = new Map();

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
        "暂时无法读取响应。请读取原请求结果，或到自动化页面查看执行记录，勿重复触发。",
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
        ? "插件按已保存设置执行；需要确认的操作会先展示预览。"
        : "可查询业务信息，也可描述要执行的插件。";
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
    (response.plugin_invocations || []).forEach((item) => renderPlugin(item, sessionId));
    setState("可以继续提问");
    showModelSettings(false);
  }

  function describeError(error) {
    const code = String(error && error.code || "").toUpperCase();
    if (code.includes("MODEL_NOT_CONFIGURED")) {
      return "尚未启用智能模型，请先打开“智能模型”完成配置。";
    }
    if (code.includes("MODEL_UNAVAILABLE") || code.includes("TIMEOUT")) {
      return "请求响应暂未返回。请读取原请求结果，勿重复发起业务操作。";
    }
    if (code.includes("CAPABILITY_UNAVAILABLE") || code.includes("SIDECAR") || code.includes("UNREACHABLE")) {
      return "AI 助手暂时无法返回结果。已有执行可在自动化页面查看，请勿重复触发。";
    }
    if (code.includes("LIMIT_EXCEEDED")) {
      return "这次问题需要查询的内容过多，请缩小范围后重试。";
    }
    return String(error && error.message || "AI 助手请求结果暂时无法读取。");
  }

  async function createSession() {
    const data = await postJson("/harness/sessions", { request_uuid: requestUuid() });
    const createdSessionId = canonicalUuid(data.session_id);
    if (!createdSessionId) {
      throw new HarnessRequestError("INVALID_HARNESS_RESPONSE", "智能服务未返回有效会话。", 502);
    }
    setSession(createdSessionId);
    for (const message of Array.isArray(data.messages) ? data.messages : []) {
      if ((message.role === "user" || message.role === "assistant") && typeof message.content === "string") {
        appendMessage(message.role, message.content);
      }
    }
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
    pendingMessage = pendingMessage || {
      request_uuid: requestUuid(),
      session_id: activeSessionId,
      message,
    };
    const data = await postJson("/harness/messages", pendingMessage);
    renderResponse(data);
    pendingMessage = null;
  }

  function renderPlugin(item, boundSession) {
    if (!item.invocation_id) return;
    let card = pluginCards.get(item.invocation_id);
    if (!card) {
      const element = createElement("article", "harness-message harness-message--assistant");
      const body = createElement("div", "harness-message-body");
      element.append(body);
      thread.append(element);
      card = { element, body, session: boundSession, title: item.title || "插件执行", timer: null };
      pluginCards.set(item.invocation_id, card);
    }
    window.clearTimeout(card.timer);
    card.body.replaceChildren();
    appendText(card.body, "strong", "harness-message-label", card.title);
    const labels = { RUNNING: "正在执行", STARTING: "正在启动", COMPLETED: "已完成", FAILED: "执行失败", CANCELLED: "已取消", CANCELLING: "正在停止", WRITE_OUTCOME_UNKNOWN: "写入结果待核验" };
    appendText(card.body, "p", "harness-message-copy", labels[item.status] || item.status);
    if (item.summary) appendText(card.body, "p", "harness-message-copy", item.summary);
    if (item.message) appendText(card.body, "p", "harness-message-copy", item.message);
    if (item.result_text) {
      const details = createElement("details");
      appendText(details, "summary", "harness-message-copy", "查看返回数据");
      const data = appendText(details, "pre", "harness-message-copy", item.result_text);
      data.style.whiteSpace = "pre-wrap";
      data.style.overflowWrap = "anywhere";
      card.body.append(details);
    }
    const controls = createElement("div", "harness-composer-actions");
    const notice = appendText(card.body, "p", "harness-message-copy", "");
    const selections = [];
    const preview = item.preview;
    if (preview) {
      appendText(card.body, "p", "harness-message-copy", typeof preview.summary === "string" ? preview.summary : "请核对本次候选清单。");
      const state = { CONSUMED: "本次预览已使用，请查看正式执行结果。", EXPIRED: "本次预览已过期，需要重新读取。" };
      if (state[preview.state]) appendText(card.body, "p", "harness-message-copy", state[preview.state]);
      (preview.candidates || []).forEach((candidate) => {
        const label = createElement("label", "harness-message-copy");
        const input = createElement("input");
        input.type = "checkbox";
        input.disabled = !preview.can_confirm;
        label.append(input, document.createTextNode(` ${candidate.label}`));
        card.body.append(label, document.createElement("br"));
        selections.push({ input, index: candidate.index });
      });
    }
    async function action(name, indices = [], stableRequest = requestUuid()) {
      const data = await postJson("/harness/plugin-actions", {
        session_id: card.session, invocation_id: item.invocation_id,
        request_uuid: stableRequest, action: name, selected_indices: indices,
      });
      const next = data.plugin_invocations && data.plugin_invocations[0];
      if (!next || data.session_id !== card.session) throw new Error("执行结果响应不匹配");
      if (!card.element.isConnected) return;
      if (next.invocation_id !== item.invocation_id) {
        notice.textContent = "已提交正式执行。";
        controls.replaceChildren();
      }
      renderPlugin({ ...next, title: card.title }, card.session);
    }
    function button(title, name, getIndices) {
      const control = appendText(controls, "button", "harness-tool-prompt", title);
      control.type = "button";
      let request = null;
      let indices = null;
      control.addEventListener("click", async () => {
        if (!request) {
          indices = getIndices ? getIndices() : [];
          if (preview?.kind === "selection" && name === "confirm" && !indices.length) {
            notice.textContent = "请先选择本次要处理的运单。";
            return;
          }
          request = requestUuid();
          selections.forEach(({ input }) => { input.disabled = true; });
        }
        control.disabled = true;
        try { await action(name, indices, request); }
        catch (error) {
          notice.textContent = describeError(error);
          control.textContent = name === "status" ? "重新读取状态" : "读取本次操作结果";
          control.disabled = false;
        }
      });
    }
    const running = ["RUNNING", "STARTING", "CANCELLING"].includes(item.status);
    if (running && item.status !== "CANCELLING") button("取消本次执行", "cancel");
    if (preview?.can_confirm) button("确认执行", "confirm", () => selections.filter(({ input }) => input.checked).map(({ index }) => index));
    button("刷新结果", "status");
    const link = appendText(controls, "a", "harness-tool-prompt", "查看自动化记录");
    link.href = "/automations";
    link.target = "_blank";
    link.rel = "noopener";
    card.body.append(controls);
    // Reading a result never re-submits the plugin. A failed read leaves the
    // last known state visible and continues reading independently.
    if (running || !("summary" in item)) {
      card.timer = window.setTimeout(async () => {
        if (!card.element.isConnected) return;
        try { await action("status"); }
        catch (error) {
          notice.textContent = describeError(error);
          button("重新读取状态", "status");
        }
      }, 2000);
    }
    scrollConversation();
  }

  async function resetConversation() {
    setBusy(true);
    try {
      await postJson("/harness/messages", {
        session_id: sessionId, request_uuid: requestUuid(), message: "清空会话",
      });
    } catch (error) {
      setFeedback(describeError(error), "error");
      return;
    } finally {
      setBusy(false);
    }
    pluginCards.forEach((card) => window.clearTimeout(card.timer));
    pluginCards.clear();
    pendingMessage = null;
    if (thread) thread.querySelectorAll(".harness-message").forEach((item) => item.remove());
    if (welcome) welcome.hidden = false;
    if (toolsList) toolsList.replaceChildren();
    if (toolsCount) toolsCount.textContent = "未加载";
    setState("等待提问");
    setFeedback("已清空会话。已发起的插件继续执行，结果可在自动化记录中查看。", "info");
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
    if (pendingMessage) {
      setFeedback("上次响应尚未确认，请先读取原请求结果。", "info");
      return;
    }
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
      if (pendingMessage) {
        const retry = appendText(thread, "button", "harness-tool-prompt harness-message", "读取原请求结果");
        retry.type = "button";
        retry.addEventListener("click", async () => {
          if (busy) return;
          setBusy(true);
          try { await sendMessage(pendingMessage.message); retry.remove(); }
          catch (error) { setFeedback(describeError(error), "error"); }
          finally { setBusy(false); }
        });
      }
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
