(() => {
  "use strict";
  const fetch = window.ConsoleUI.pageRequest();
  const root = document.querySelector(".main-content:not([hidden]) [data-module-source-history]");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  const table = root.querySelector("tbody");
  const message = root.querySelector("[role=status]");
  const choices = JSON.parse(root.dataset.producers || "[]");
  async function load() {
    const response = await fetch(`/module-data-sources/${root.dataset.module}`, {credentials: "same-origin"});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.message || "数据源记录暂时无法读取。");
    table.replaceChildren();
    for (const source of payload.data.sources) {
      const row = document.createElement("tr");
      const cell = text => {const item = document.createElement("td"); item.textContent = text; row.append(item); return item;};
      const name = cell("");
      const link = document.createElement("a");
      link.dataset.consoleNavigate = "";
      link.textContent = source.display_name;
      link.href = `${root.dataset.queryUrl}?source_id=${encodeURIComponent(source.source_id)}`;
      name.append(link);
      cell(source.status === "history_only" ? "保留历史 · 未连接采集器" : source.status === "legacy_unassigned" ? "保留历史 · 待验证接续" : source.producer_instance_id ? "已连接采集器" : "来源状态待核实");
      cell(source.producer_instance_id ? (choices.find(item => item.automation_id === source.producer_instance_id)?.name || "采集实例暂不可用") : "无");
      const actions = cell("");
      if (root.dataset.canManage === "true") {
        const select = document.createElement("select");
        select.className = "review-input compact-input";
        select.setAttribute("aria-label", `${source.display_name}的采集器`);
        select.add(new Option("选择接续采集器", ""));
        let choicesLoaded = false;
        const prepareChoices = () => {
          if (choicesLoaded) return;
          choicesLoaded = true;
          choices.filter(item => item.automation_id !== source.producer_instance_id).forEach(item => select.add(new Option(item.name, item.automation_id)));
        };
        select.addEventListener("focus", prepareChoices);
        select.addEventListener("pointerdown", prepareChoices);
        const button = document.createElement("button");
        button.className = "ghost-btn";
        button.textContent = "接续来源";
        button.type = "button";
        button.addEventListener("click", async () => {
          if (!select.value) { message.textContent = "请先选择已配置的采集器。"; return; }
          button.disabled = true;
          try {
            const requestId = crypto.randomUUID();
            const result = await fetch(`/module-data-sources/${encodeURIComponent(source.source_id)}/producer`, {
              method: "POST", credentials: "same-origin",
              headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "X-Browser-Request-UUID": requestId},
              body: JSON.stringify({producer_instance_id: select.value, expected_revision: source.revision, expected_producer_instance_id: source.producer_instance_id, request_id: requestId}),
            });
            const data = await result.json();
            if (!result.ok || !data.ok) throw new Error(data.message || data.error?.message || "接续未完成，请确认原采集器已暂停且新账号属于同一来源。");
            await load();
            message.textContent = "来源已接续，历史数据继续保留。";
          } catch (error) {message.textContent = error.message;}
          finally {button.disabled = false;}
        });
        actions.append(select, button);
      }
      table.append(row);
    }
    message.textContent = payload.data.sources.length ? "暂停或卸载采集器后，仍可从来源名称查看历史数据。" : "尚无已验证的数据源。配置采集器并完成首次同步后会显示在这里。";
  }
  load().catch(error => {message.textContent = error.message;});
})();
