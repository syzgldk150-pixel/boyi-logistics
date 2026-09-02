(() => {
  "use strict";
  const bridgeSession = new URLSearchParams(location.search).get("bridge_session") || "";
  const form = document.querySelector("[data-settings-form]");
  const configRoot = document.querySelector("[data-config-fields]");
  const accountRoot = document.querySelector("[data-account-fields]");
  const resourceRoot = document.querySelector("[data-resource-fields]");
  const saveButton = document.querySelector("[data-save-settings]");
  const feedback = document.querySelector("[data-settings-feedback]");
  const pending = new Map();
  let context = null;

  const labels = {
    sitecode: "网点代码", sitefbcode: "分拨代码", sitename: "网点名称", sitefbname: "分拨名称",
    first_type: "第一次打卡类型", second_type: "第二次打卡类型", delay_seconds: "两次操作间隔（秒）",
    include_daxiang_s_self_pickup: "同时检查大祥 S 站自提货物", limit: "单次最多处理数量",
    refresh_disabled: "不刷新已有统计", target_date: "业务日期", scan_window_days: "扫描日期范围",
    scan_codes_retention_days: "扫描记录保留天数", pending_sheet_disabled: "不处理未齐清单",
    missing_limit: "未齐清单上限", export_limit: "导出数量上限", child_count_limit: "子单数量上限",
    archive_snapshot: "保存归档快照", dry_run: "仅预览不写入", child_item_limit: "子单读取上限",
    batch_size: "每批数量", max_batches: "最多批次", skip_bill_codes: "跳过的运单号",
    operator: "运行账号", account_id: "运行账号", daxiang_s_account_id: "大祥 S 站账号",
    arrival_stats_tms: "到货统计账号", self_pickup_source_sheet: "自提货物来源表",
    split_pending_source_sheet: "分批未到来源表", split_pending_target_sheet: "分批未到结果表",
    arrival_stats_primary_sheet: "每日到货主表", arrival_stats_secondary_sheet: "每日到货明细表",
    arrival_stats_pending_sheet: "未齐货物表", arrival_stats_archive_sheet: "统计归档表",
    arrival_stats_split_pending_sheet: "分批未到结果表",
  };

  function setFeedback(message, kind = "") {
    if (!(feedback instanceof HTMLElement)) return;
    feedback.textContent = String(message || "");
    feedback.dataset.kind = kind;
  }

  function request(operation, payload) {
    const requestId = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      pending.set(requestId, { resolve, reject });
      parent.postMessage({
        type: "boyi.settings.request",
        bridge_session: bridgeSession,
        request_id: requestId,
        operation,
        payload,
      }, "*");
      setTimeout(() => {
        if (!pending.has(requestId)) return;
        pending.delete(requestId);
        reject(new Error("设置服务响应超时，请稍后重试。"));
      }, 15000);
    });
  }

  window.addEventListener("message", event => {
    if (event.source !== parent) return;
    const response = event.data;
    if (!response || response.type !== "boyi.settings.response" || response.bridge_session !== bridgeSession) return;
    const waiter = pending.get(response.request_id);
    if (!waiter) return;
    pending.delete(response.request_id);
    if (response.ok === true) waiter.resolve(response.data);
    else waiter.reject(new Error(response.message || "设置服务暂时不可用。"));
  });

  function fieldLabel(name) { return labels[name] || name.replaceAll("_", " "); }
  function addEmpty(root, text) {
    const node = document.createElement("p");
    node.className = "settings-empty";
    node.textContent = text;
    root.append(node);
  }
  function wrapper(name, hint = "") {
    const label = document.createElement("label");
    label.className = "settings-field";
    const title = document.createElement("span");
    title.textContent = fieldLabel(name);
    label.append(title);
    if (hint) {
      const help = document.createElement("small");
      help.textContent = hint;
      label.append(help);
    }
    return label;
  }

  function renderConfig(settings) {
    configRoot.replaceChildren();
    const schema = settings.config_schema || {};
    const properties = schema.properties || {};
    const required = new Set(schema.required || []);
    if (!Object.keys(properties).length) {
      addEmpty(configRoot, "这个插件没有额外业务参数。");
      return;
    }
    Object.entries(properties).forEach(([name, spec]) => {
      const label = wrapper(name, required.has(name) ? "必填" : "可选");
      let input;
      if (spec.type === "boolean") {
        label.classList.add("settings-check");
        input = document.createElement("input");
        input.type = "checkbox";
        input.checked = settings.config?.[name] === true || (settings.config?.[name] === undefined && spec.default === true);
        label.prepend(input);
      } else {
        input = ["array", "object"].includes(spec.type)
          ? document.createElement("textarea")
          : document.createElement("input");
        input.type = ["integer", "number"].includes(spec.type) ? "number" : "text";
        if (spec.type === "integer") input.step = "1";
        if (typeof spec.minimum === "number") input.min = String(spec.minimum);
        if (typeof spec.maximum === "number") input.max = String(spec.maximum);
        if (typeof spec.maxLength === "number") input.maxLength = spec.maxLength;
        input.required = required.has(name);
        const current = settings.config?.[name];
        if (spec.type === "array") {
          const values = current === undefined ? (spec.default || []) : current;
          input.value = Array.isArray(values) ? values.join("\n") : "";
          input.placeholder = "每行填写一项";
        } else if (spec.type === "object") {
          const value = current === undefined ? (spec.default || {}) : current;
          input.value = JSON.stringify(value, null, 2);
          input.placeholder = "填写 JSON 对象";
        } else {
          input.value = current === undefined || current === null ? String(spec.default ?? "") : String(current);
        }
        label.append(input);
      }
      input.dataset.configField = name;
      input.dataset.valueType = spec.type || "string";
      configRoot.append(label);
    });
  }

  function renderAccounts(settings, accounts, available) {
    accountRoot.replaceChildren();
    if (!settings.account_roles.length) {
      addEmpty(accountRoot, "这个插件不需要业务账号。");
      return;
    }
    settings.account_roles.forEach(role => {
      const label = wrapper(role.role, role.required ? "必选；只显示脱敏名称和登录状态" : "可选");
      const select = document.createElement("select");
      select.dataset.accountRole = role.role;
      select.required = role.required === true;
      select.append(new Option("请选择账号", ""));
      accounts.filter(item => role.allowed_systems.includes(item.system)).forEach(item => {
        const option = new Option(`${item.name} · ${item.status_label}`, item.account_ref);
        option.disabled = item.available !== true;
        select.append(option);
      });
      const saved = settings.account_bindings?.[role.role];
      select.value = Array.isArray(saved) ? String(saved[0] || "") : String(saved || "");
      if (!available) select.disabled = true;
      label.append(select);
      accountRoot.append(label);
    });
  }

  function renderResources(settings, resources, available) {
    resourceRoot.replaceChildren();
    if (!settings.resource_roles.length) {
      addEmpty(resourceRoot, "这个插件不需要飞书业务资源。");
      return;
    }
    settings.resource_roles.forEach(role => {
      const label = wrapper(role.role, role.required ? "必选；仅保存不透明引用" : "可选");
      const select = document.createElement("select");
      select.dataset.resourceRole = role.role;
      select.required = role.required === true;
      select.append(new Option("请选择飞书资源", ""));
      resources.filter(item => role.allowed_kinds.includes(item.kind)).forEach(item => {
        const suffix = item.status === "available" ? "" : ` · ${item.problem_label}`;
        const option = new Option(`${item.display_name}${suffix}`, item.resource_id);
        option.disabled = item.status !== "available";
        select.append(option);
      });
      select.value = String(settings.resource_bindings?.[role.role] || "");
      if (!available) select.disabled = true;
      label.append(select);
      resourceRoot.append(label);
    });
  }

  function collect() {
    if (!(form instanceof HTMLFormElement) || !form.reportValidity()) throw new Error("请先完成必填设置。");
    const config = {};
    configRoot.querySelectorAll("[data-config-field]").forEach(input => {
      const type = input.dataset.valueType;
      if (type === "boolean") config[input.dataset.configField] = input.checked;
      else if (input.value !== "") {
        if (type === "integer") config[input.dataset.configField] = Number.parseInt(input.value, 10);
        else if (type === "number") config[input.dataset.configField] = Number(input.value);
        else if (type === "array") config[input.dataset.configField] = input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
        else if (type === "object") {
          const value = JSON.parse(input.value);
          if (!value || Array.isArray(value) || typeof value !== "object") throw new Error(`${fieldLabel(input.dataset.configField)}必须是 JSON 对象。`);
          config[input.dataset.configField] = value;
        } else config[input.dataset.configField] = input.value;
      }
    });
    const accountBindings = {};
    accountRoot.querySelectorAll("[data-account-role]").forEach(select => { if (select.value) accountBindings[select.dataset.accountRole] = select.value; });
    const resourceBindings = {};
    resourceRoot.querySelectorAll("[data-resource-role]").forEach(select => { if (select.value) resourceBindings[select.dataset.resourceRole] = select.value; });
    return { config, account_bindings: accountBindings, resource_bindings: resourceBindings };
  }

  async function load() {
    if (!bridgeSession || !crypto?.randomUUID) throw new Error("设置桥不可用，请返回自动化页面后重新打开。");
    const data = await request("context", {});
    context = data.settings;
    renderConfig(context);
    renderAccounts(context, data.accounts || [], data.account_catalog_available === true);
    renderResources(context, data.resources || [], data.resource_catalog_available === true);
    saveButton.disabled = false;
    setFeedback("设置已连接。", "success");
  }

  saveButton?.addEventListener("click", async () => {
    if (!context) return;
    try {
      saveButton.disabled = true;
      setFeedback("正在保存…");
      const values = collect();
      const data = await request("save", {
        ...values,
        request_id: crypto.randomUUID(),
        expected_project_configuration_version: context.project_configuration_version,
      });
      const nextVersion = Number(data?.project_configuration_version);
      if (!Number.isInteger(nextVersion) || nextVersion < 1) throw new Error("设置可能已保存，请刷新页面核对。 ");
      context.project_configuration_version = nextVersion;
      setFeedback("插件设置已保存。", "success");
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "插件设置保存失败。", "error");
    } finally {
      saveButton.disabled = false;
    }
  });

  load().catch(error => setFeedback(error instanceof Error ? error.message : "插件设置暂时不可用。", "error"));
})();
