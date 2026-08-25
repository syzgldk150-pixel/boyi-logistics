(() => {
  const root = document.querySelector("[data-module-manager]");
  if (!root || root.dataset.canWrite !== "true") return;
  const status = root.querySelector("[data-module-status]");
  const dialog = document.querySelector("[data-module-dialog]");
  const form = dialog?.querySelector("[data-module-form]");
  const reasonInput = dialog?.querySelector("[data-module-reason]");
  const dialogStatus = dialog?.querySelector("[data-module-dialog-status]");
  const dialogCopy = dialog?.querySelector("[data-module-dialog-copy]");
  const audit = root.querySelector("[data-module-audit-list]");
  const actionLabels = { install: "安装", enable: "启用", disable: "停用", upgrade: "升级", uninstall: "卸载" };
  const actionNotes = {
    install: "安装后模块保持停用，需要再单独启用。",
    enable: "启用后，菜单、页面和新业务请求将恢复。",
    disable: "停用后将拒绝该模块的新业务请求；已经受理的运行不会被中断。",
    upgrade: "升级会把已安装版本切换到当前代码版本，并保留现有启停状态。",
    uninstall: "卸载会移除运行状态并保留历史数据和审计记录。",
  };
  let pending = null;
  const requestId = () => crypto.randomUUID ? crypto.randomUUID() : "";
  root.addEventListener("click", async (event) => {
    const auditButton = event.target.closest("[data-module-audit-trigger]");
    if (auditButton) {
      const row = auditButton.closest("[data-module-row]");
      if (!row || !audit) return;
      audit.textContent = "正在加载审计记录…";
      try {
        const response = await fetch(`/settings/modules/${encodeURIComponent(row.dataset.moduleCode)}/audit`, { credentials: "same-origin" });
        const payload = await response.json();
        const items = payload?.data?.items;
        audit.replaceChildren();
        if (!response.ok || !Array.isArray(items)) throw new Error(payload?.error?.message || "审计记录不可用。");
        if (!items.length) { audit.textContent = "暂无审计记录。"; return; }
        const list = document.createElement("ul");
        items.forEach((item) => { const line = document.createElement("li"); line.textContent = `${item.created_at || ""} · ${actionLabels[item.action] || item.action || ""} · ${item.actor_id || ""} · ${item.reason || ""}`; list.append(line); });
        audit.append(list);
      } catch (error) { audit.textContent = error.message || "审计记录加载失败。"; }
      return;
    }
    const button = event.target.closest("[data-module-action]");
    if (!button || !button.dataset.moduleAction) return;
    const row = button.closest("[data-module-row]");
    if (!dialog || !form || !reasonInput) return;
    pending = { button, row, action: button.dataset.moduleAction };
    const actionLabel = actionLabels[pending.action] || pending.action;
    reasonInput.value = ""; dialogStatus.textContent = ""; dialogCopy.textContent = `将执行“${actionLabel}”操作。${actionNotes[pending.action] || ""} 请说明原因。`;
    dialog.showModal(); reasonInput.focus();
  });
  dialog?.querySelector("[data-module-cancel]")?.addEventListener("click", () => dialog.close());
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!pending || !reasonInput?.value.trim()) { dialogStatus.textContent = "请填写变更原因。"; return; }
    const id = requestId();
    if (!id) { dialogStatus.textContent = "浏览器无法生成请求标识，未提交变更。"; return; }
    const { button, row, action } = pending; button.disabled = true; dialogStatus.textContent = "正在提交模块变更…";
    try {
      const response = await fetch("/settings/modules/lifecycle", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ module_code: row.dataset.moduleCode, action, reason: reasonInput.value.trim(), request_id: id, expected_record_version: Number(row.dataset.recordVersion) }) });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload?.error?.message || payload?.message || "模块变更未应用。");
      dialog.close(); status.textContent = "模块变更已记录，正在刷新页面。"; window.location.reload();
    } catch (error) { dialogStatus.textContent = error.message || "模块变更失败。"; button.disabled = false; }
  });
})();
