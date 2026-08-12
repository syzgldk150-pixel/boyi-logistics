(() => {
  "use strict";

  const root = document.querySelector("[data-llm-settings]");
  if (!root) return;
  const canEdit = root.dataset.canEdit === "true";
  const feedback = root.querySelector("[data-feedback]");
  let state = {};

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);

  function showFeedback(message, tone = "info") {
    if (!feedback) return;
    feedback.hidden = false;
    feedback.dataset.tone = tone;
    feedback.textContent = message;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok !== true) {
      throw new Error(payload.message || payload.error?.message || `请求失败（${response.status}）`);
    }
    return payload.data;
  }

  function renderStatus() {
    const runtime = state.runtime || {};
    const active = state.active || {};
    const title = root.querySelector("[data-active-name]");
    const meta = root.querySelector("[data-active-meta]");
    const health = root.querySelector("[data-health]");
    if (runtime.configured) {
      title.textContent = `${runtime.provider} / ${runtime.model}`;
      meta.textContent = runtime.source === "database"
        ? `数据库配置版本 #${runtime.config_version_id ?? active.id ?? "—"}，无需重启即可生效。`
        : "环境托管配置；激活首个数据库版本后将停止使用。";
    } else {
      title.textContent = "尚未配置可用模型";
      meta.textContent = "财务采集与确定性校验继续运行，AI 分析保持待处理。";
    }
    const tone = runtime.health === "ready" ? "success" : runtime.health === "error" ? "danger" : "warning";
    health.dataset.tone = tone;
    health.textContent = runtime.health === "ready" ? "运行正常" : runtime.health === "error" ? "调用异常" : "未配置";
    if (active.status === "active") health.title = `已激活版本 #${active.id}`;
  }

  function renderProviders() {
    const grid = root.querySelector("[data-provider-grid]");
    if (!grid) return;
    grid.innerHTML = (state.providers || []).map((item) => `
      <article class="provider-card">
        <header><h3>${item.provider === "deepseek" ? "DeepSeek" : "GLM"}</h3><span class="llm-badge" data-tone="${item.configured ? "success" : "warning"}">${item.configured ? "已配置 Key" : "未配置 Key"}</span></header>
        <dl><dt>官方 API</dt><dd>${escapeHtml(item.base_url || "固定官方地址")}</dd><dt>密钥标识</dt><dd>${escapeHtml(item.key_hint || (item.configured ? "已配置" : "—"))}</dd></dl>
        ${canEdit && item.configured ? `<button class="llm-action" data-clear-provider="${escapeHtml(item.provider)}" data-danger="true" type="button">清除该供应商密钥</button>` : ""}
      </article>`).join("");
  }

  function renderModelOptions() {
    const list = root.querySelector("[data-model-options]");
    const provider = root.querySelector("[data-provider-select]")?.value;
    if (!list || !provider) return;
    list.innerHTML = (state.models || [])
      .filter((item) => item.provider === provider)
      .map((item) => `<option value="${escapeHtml(item.model_id)}"></option>`)
      .join("");
  }

  function testSummary(version) {
    const result = version.test_result || {};
    if (!version.tested_at) return "尚未测试";
    if (result.passed) return `${version.tested_at} · ${result.latency_ms ?? "—"} ms`;
    return version.test_error_message || result.error || "测试未通过";
  }

  function renderVersions() {
    const body = root.querySelector("[data-version-list]");
    if (!body) return;
    const versions = state.versions || [];
    body.innerHTML = versions.length ? versions.map((item) => {
      const tone = item.status === "active" || item.status === "tested" ? "success" : item.status === "draft" ? "warning" : "";
      const actions = [];
      if (item.status === "draft" || item.status === "tested") {
        actions.push(`<button type="button" class="llm-action" data-action="refresh" data-id="${item.id}">刷新模型</button>`);
        actions.push(`<button type="button" class="llm-action" data-action="test" data-id="${item.id}">完整测试</button>`);
      }
      if (item.status === "tested" && item.test_result?.passed) actions.push(`<button type="button" class="llm-action" data-action="activate" data-id="${item.id}">激活</button>`);
      if (item.status === "inactive" && item.test_result?.passed) actions.push(`<button type="button" class="llm-action" data-action="rollback" data-id="${item.id}">回滚到此版本</button>`);
      return `<tr><td>#${item.id}</td><td><strong>${escapeHtml(item.provider)}</strong><br>${escapeHtml(item.model_id)}</td><td><span class="llm-badge" data-tone="${tone}">${escapeHtml(item.status)}</span></td><td>${escapeHtml(testSummary(item))}</td><td><div class="llm-table-actions">${actions.join("") || "—"}</div></td></tr>`;
    }).join("") : '<tr><td colspan="5">尚无数据库配置版本。</td></tr>';
  }

  async function loadAudit() {
    if (!canEdit) return;
    const container = root.querySelector("[data-audit-list]");
    const payload = await request("/settings/llm/audit");
    const rows = payload.items || [];
    container.innerHTML = rows.length ? rows.map((item) => `<div class="audit-row"><span>${escapeHtml(item.created_at)}</span><strong>${escapeHtml(item.changed_by)} · ${escapeHtml(item.action)}</strong><span>${item.api_key_changed ? "密钥已变更" : "密钥未变更"}</span></div>`).join("") : "<p>暂无配置变更。</p>";
  }

  async function load() {
    try {
      state = await request("/settings/llm/status");
      renderStatus(); renderProviders(); renderVersions(); renderModelOptions();
      await loadAudit();
    } catch (error) {
      showFeedback(error.message, "danger");
      root.querySelector("[data-active-name]").textContent = "模型状态不可达";
      root.querySelector("[data-health]").textContent = "读取失败";
      root.querySelector("[data-health]").dataset.tone = "danger";
    }
  }

  root.querySelector("[data-candidate-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const button = form.querySelector("[data-save-candidate]");
    button.disabled = true;
    try {
      const data = { provider: values.get("provider"), model_id: values.get("model_id") };
      const key = String(values.get("api_key") || "").trim();
      if (key) data.api_key = key;
      await request("/settings/llm/candidates", { method: "POST", body: JSON.stringify(data) });
      form.elements.api_key.value = "";
      showFeedback("候选配置已保存，当前运行配置未改变。", "success");
      await load();
    } catch (error) { showFeedback(error.message, "danger"); }
    finally { button.disabled = false; }
  });

  root.querySelector("[data-provider-select]")?.addEventListener("change", renderModelOptions);

  root.addEventListener("click", async (event) => {
    const refresh = event.target.closest("[data-refresh-page]");
    if (refresh) { await load(); return; }
    const clear = event.target.closest("[data-clear-provider]");
    const actionButton = event.target.closest("[data-action]");
    if (!clear && !actionButton) return;
    const button = clear || actionButton;
    if (clear && !window.confirm(`确认清除 ${clear.dataset.clearProvider} 的已保存 API Key？`)) return;
    button.disabled = true;
    try {
      let path; let data;
      if (clear) {
        path = "/settings/llm/credentials/clear";
        data = { provider: clear.dataset.clearProvider };
      } else {
        const action = actionButton.dataset.action;
        path = `/settings/llm/${action === "refresh" ? "models/refresh" : action}`;
        data = action === "rollback" ? { config_id: Number(actionButton.dataset.id) } : { config_id: Number(actionButton.dataset.id) };
      }
      const result = await request(path, { method: "POST", body: JSON.stringify(data) });
      showFeedback(actionButton?.dataset.action === "test" && !result.passed ? "兼容性测试未通过，不能激活。" : "操作已完成。", result?.passed === false ? "danger" : "success");
      await load();
    } catch (error) { showFeedback(error.message, "danger"); }
    finally { button.disabled = false; }
  });

  load();
})();
