(() => {
  "use strict";
  const fetch = window.ConsoleUI.pageRequest();
  const root = document.querySelector(".main-content:not([hidden]) [data-plugin-settings-history]");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  const select = root.querySelector("select");
  const status = root.querySelector("[role=status]");
  const button = root.querySelector("button");
  let loaded = false;
  async function request(operation, payload) {
    const response = await fetch(root.dataset.endpoint, {
      method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
      body: JSON.stringify({bridge_session: root.dataset.session, operation, payload}),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || data.error?.message || "历史设置暂时不可用。");
    return data.data;
  }
  root.addEventListener("toggle", async () => {
    if (!root.open || loaded) return;
    try {
      const data = await request("history", {});
      for (const version of data.versions) select.add(new Option(`${version.created_at} · ${version.plugin_version}`, version.generation));
      status.textContent = data.versions.length ? "" : "暂无兼容且成功应用的历史设置。";
      loaded = true;
    } catch (error) {status.textContent = error.message;}
  });
  button.addEventListener("click", async () => {
    if (!select.value) {status.textContent = "请先选择历史设置。"; return;}
    button.disabled = true;
    try {
      const context = await request("context", {});
      await request("restore", {generation: Number(select.value), request_id: crypto.randomUUID(), expected_project_configuration_version: context.settings.project_configuration_version});
      status.textContent = "设置已恢复，请重新打开设置页查看。";
    } catch (error) {status.textContent = error.message;}
    finally {button.disabled = false;}
  });
})();
