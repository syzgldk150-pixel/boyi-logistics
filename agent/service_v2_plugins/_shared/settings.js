(() => {
  "use strict";
  // Bridge client 2: longer save wait, coded errors, reload-latest, and height reports.
  const BRIDGE_CLIENT = 2;
  const CONTEXT_TIMEOUT_MS = 30000;
  // The host waits up to 90 s for the save and then reads it back once.
  const SAVE_TIMEOUT_MS = 150000;
  const RELOAD_CODES = new Set([
    "PLUGIN_SETTINGS_VERSION_CHANGED", "PLUGIN_SETTINGS_REQUEST_REUSED", "PLUGIN_SETTINGS_SAVE_UNCONFIRMED",
    "SETTINGS_BRIDGE_TIMEOUT",
  ]);
  const bridgeSession = new URLSearchParams(location.search).get("bridge_session") || "";
  const form = document.querySelector("[data-settings-form]");
  const configRoot = document.querySelector("[data-config-fields]");
  const accountRoot = document.querySelector("[data-account-fields]");
  const resourceRoot = document.querySelector("[data-resource-fields]");
  const saveButton = document.querySelector("[data-save-settings]");
  const feedback = document.querySelector("[data-settings-feedback]");
  const pending = new Map();
  let context = null;
  let busy = false;
  let dirty = false;
  let reloadButton = null;

  const labels = {
    sitecode: "网点代码", sitefbcode: "分拨代码", sitename: "网点名称", sitefbname: "分拨名称",
    first_type: "第一次打卡类型", second_type: "第二次打卡类型", delay_seconds: "两次操作间隔（秒）",
    include_daxiang_s_self_pickup: "同时检查大祥 S 站自提货物", limit: "单次最多处理数量",
    refresh_disabled: "不刷新已有统计", target_date: "业务日期", scan_window_days: "扫描日期范围",
    scan_codes_retention_days: "扫描记录保留天数", pending_sheet_disabled: "不处理未齐清单",
    missing_limit: "未齐清单上限", export_limit: "导出数量上限", child_count_limit: "子单数量上限",
    archive_snapshot: "保存归档快照", dry_run: "仅预览不写入", child_item_limit: "子单读取上限",
    batch_size: "每批数量", max_batches: "最多批次", skip_bill_codes: "跳过的运单号",
    start_date: "补拉开始日期", end_date: "补拉结束日期",
    sync_nas: "写入 NAS 寄件表格", sync_sql: "同步后台运单表", sync_sheet: "同步飞书电子表格",
    sql_only: "只同步后台运单表", ensure_fields: "自动补齐飞书表字段",
    page_size: "每页条数", max_pages: "最多读取页数", list_limit: "每页读取飞书记录数",
    query_batch_size: "每批查询运单数", max_per_run: "每轮最多处理件数", days: "查询天数",
    dest_brch: "目的网点编码", direction: "查询方向", bill_codes: "运单号", record_ids: "飞书记录 ID",
    BILL_CODE: "运单号（旧参数）", RECORD_ID: "飞书记录 ID（旧参数）", flow: "处理流程",
    recheck_items: "复核事项", arrive_list_request_body: "到货清单请求参数", enrich_addresses: "补齐地址",
    browser_batch_size: "补查每批数量", exact_historical_sign_limit: "历史签收补查上限",
    exact_sign_conflict_limit: "签收冲突补查上限", problem_max_pages: "问题件最多页数",
    problem_page_size: "问题件每页条数", problem_retry_attempts: "问题件重试次数",
    problem_timeout_sec: "问题件超时（秒）", sign_chunk_days: "签收分段天数", sign_max_pages: "签收最多页数",
    sign_page_size: "签收每页条数", sign_retry_attempts: "签收重试次数", sign_site_code: "签收网点编码",
    sign_timeout_sec: "签收超时（秒）", source_start: "来源开始时间", source_end: "来源结束时间",
    waybill_timeout_sec: "运单查询超时（秒）",
    operator: "运行账号", account_id: "运行账号", daxiang_s_account_id: "大祥 S 站账号",
    arrival_stats_tms: "到货统计账号", daily_send_source: "融辉寄件账号", yunda_send_source: "韵达账号",
    arrive_list_ronghui: "融辉账号", customer_service_source: "客服账号", daily_sign_r13: "R13 账号",
    daily_sign_tms: "融辉账号（历史配置）", delivery_source: "签收查询账号", scan_ronghui: "融辉扫描账号",
    self_pickup_primary: "自提部账号", self_pickup_daxiang_s: "大祥 S 站账号", site_send_ronghui: "融辉账号",
    split_pending_ronghui: "融辉账号", yunda_dispatch_source: "韵达账号",
    self_pickup_source_sheet: "自提货物来源表", split_pending_source_sheet: "分批未到来源表",
    split_pending_target_sheet: "分批未到结果表", arrival_stats_primary_sheet: "每日到货主表",
    arrival_stats_secondary_sheet: "每日到货明细表", arrival_stats_pending_sheet: "未齐货物表",
    arrival_stats_archive_sheet: "统计归档表", arrival_stats_split_pending_sheet: "分批未到结果表",
    send_order_bitable: "寄件多维表", send_waybills_bitable: "韵达寄件多维表",
    send_waybills_sheet: "韵达寄件电子表格", arrive_primary_sheet: "到货清单主表",
    arrive_secondary_sheet: "到货清单明细表", daily_sign_bitable: "每日应签多维表", daily_sign_sheet: "每日应签表",
    delivery_status_bitable: "签收状态多维表", dispatch_forecast_bitable: "派件预测多维表",
    site_send_bitable: "出港清单多维表", site_send_sheet: "出港清单表",
  };
  const enumLabels = { received: "发布给我的", published: "我发布的", both: "两者都要" };
  // Parameters that rarely change are folded under “高级设置”.
  const advancedNames = new Set([
    "page_size", "max_pages", "list_limit", "query_batch_size", "ensure_fields", "sql_only", "dry_run",
    "batch_size", "max_batches", "child_count_limit", "child_item_limit", "export_limit", "missing_limit",
    "browser_batch_size", "recheck_items", "arrive_list_request_body", "BILL_CODE", "RECORD_ID", "flow",
    "scan_codes_retention_days",
  ]);
  const leadingOrder = ["target_date", "start_date", "end_date"];
  const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

  function setFeedback(message, kind = "") {
    if (!(feedback instanceof HTMLElement)) return;
    feedback.textContent = String(message || "");
    feedback.dataset.kind = kind;
    reportHeight();
  }

  function showReload(show) {
    if (!(saveButton instanceof HTMLElement)) return;
    if (!reloadButton) {
      reloadButton = document.createElement("button");
      reloadButton.type = "button";
      reloadButton.className = "settings-secondary";
      reloadButton.textContent = "载入最新设置";
      reloadButton.dataset.reloadSettings = "";
      reloadButton.addEventListener("click", () => {
        showReload(false);
        setFeedback("正在载入最新设置…");
        load().catch(error => setFeedback(messageOf(error, "插件设置暂时不可用。"), "error"));
      });
      saveButton.after(reloadButton);
    }
    reloadButton.hidden = !show;
  }

  function messageOf(error, fallback) {
    return error instanceof Error && error.message ? error.message : fallback;
  }

  function bridgeError(message, code = "") {
    const error = new Error(message);
    error.code = code;
    return error;
  }

  function request(operation, payload, timeoutMs) {
    const requestId = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      pending.set(requestId, { resolve, reject });
      parent.postMessage({
        type: "boyi.settings.request",
        bridge_session: bridgeSession,
        bridge_client: BRIDGE_CLIENT,
        request_id: requestId,
        operation,
        payload,
      }, "*");
      setTimeout(() => {
        if (!pending.has(requestId)) return;
        pending.delete(requestId);
        reject(operation === "save"
          ? bridgeError("保存结果还没有确认。请稍后点“载入最新设置”核对，不要重复保存。", "SETTINGS_BRIDGE_TIMEOUT")
          : bridgeError("设置服务响应超时，请稍后重试。"));
      }, timeoutMs);
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
    else waiter.reject(bridgeError(response.message || "设置服务暂时不可用。", String(response.code || "")));
  });

  // The frame reports its height so the Console page scrolls instead of a box inside it.
  let heightFrame = 0;
  function reportHeight() {
    if (heightFrame) return;
    heightFrame = requestAnimationFrame(() => {
      heightFrame = 0;
      parent.postMessage({
        type: "boyi.settings.resize",
        bridge_session: bridgeSession,
        // The body's own box, not the viewport: a frame already made taller must be able to shrink.
        height: Math.ceil(document.body.getBoundingClientRect().height),
      }, "*");
    });
  }
  if (typeof ResizeObserver === "function") new ResizeObserver(reportHeight).observe(document.body);
  document.addEventListener("toggle", reportHeight, true);

  function fieldLabel(name, spec = {}) {
    const title = typeof spec.title === "string" ? spec.title.trim() : "";
    return title || labels[name] || name.replaceAll("_", " ");
  }

  function fieldHint(spec) {
    return String(spec.description || "").trim().replace(/^(可选|必填)[，,：:\s]*/, "");
  }

  function defaultHint(spec) {
    if (spec.default !== undefined && spec.default !== null && typeof spec.default !== "object") return `默认 ${spec.default}`;
    const match = /默认\s*([0-9]+(?:\.[0-9]+)?)/.exec(String(spec.description || ""));
    return match ? `默认 ${match[1]}` : "";
  }

  function isDateField(name, spec) {
    return spec.type === "string" && (spec.format === "date" || /(^|_)date$/.test(name));
  }

  function isAdvanced(name, spec, required) {
    return !required && (advancedNames.has(name) || /^历史配置保留/.test(String(spec.description || "")));
  }

  // A switch the plugin has never saved shows the plugin's own default. Manifests
  // state it in the description ("默认开启" / "默认关闭"), since the Host schema
  // checks do not accept a "default" keyword in config fields.
  function switchDefault(spec) {
    if (typeof spec.default === "boolean") return spec.default;
    const text = String(spec.description || "");
    if (/默认\s*(?:true|开启|打开)/i.test(text)) return true;
    return false;
  }

  function addEmpty(root, text) {
    const node = document.createElement("p");
    node.className = "settings-empty";
    node.textContent = text;
    root.append(node);
  }

  function heading(text, required) {
    const head = document.createElement("span");
    head.className = "settings-field__title";
    head.textContent = text;
    if (required) {
      const badge = document.createElement("em");
      badge.className = "settings-required";
      badge.textContent = "必填";
      head.append(badge);
    }
    return head;
  }

  // Notes go under the control so the inputs of one row line up.
  function note(text, className = "") {
    const help = document.createElement("small");
    if (className) help.className = className;
    help.textContent = text;
    return help;
  }

  function switchField(name, spec, current) {
    const label = document.createElement("label");
    label.className = "settings-switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.setAttribute("role", "switch");
    input.checked = current === undefined ? switchDefault(spec) : current === true;
    const text = document.createElement("span");
    text.className = "settings-switch__text";
    text.append(heading(fieldLabel(name, spec), false));
    if (fieldHint(spec)) text.append(note(fieldHint(spec)));
    label.append(input, text);
    return { node: label, input };
  }

  function valueField(name, spec, current, required) {
    const label = document.createElement("label");
    label.className = "settings-field";
    const hint = fieldHint(spec);
    let input;
    let warning = "";
    if (Array.isArray(spec.enum) && spec.enum.length) {
      input = document.createElement("select");
      if (!required) input.append(new Option("不指定", ""));
      spec.enum.forEach(value => input.append(new Option(enumLabels[value] || String(value), String(value))));
      input.value = current === undefined || current === null ? String(spec.default ?? "") : String(current);
    } else if (["array", "object"].includes(spec.type)) {
      input = document.createElement("textarea");
      input.rows = spec.type === "array" ? 3 : 5;
      if (spec.type === "array") {
        const values = current === undefined ? (spec.default || []) : current;
        input.value = Array.isArray(values) ? values.join("\n") : "";
        input.placeholder = "每行填写一项";
      } else {
        input.value = JSON.stringify(current === undefined ? (spec.default || {}) : current, null, 2);
        input.placeholder = "填写 JSON 对象";
      }
    } else {
      input = document.createElement("input");
      const value = current === undefined || current === null ? "" : String(current);
      if (isDateField(name, spec)) {
        input.type = "date";
        if (ISO_DATE.test(value)) input.value = value;
        else if (value) warning = `原来保存的“${value}”不是有效日期，保存时会清空。`;
      } else {
        input.type = ["integer", "number"].includes(spec.type) ? "number" : "text";
        input.value = value || (spec.default !== undefined && spec.default !== null ? String(spec.default) : "");
        if (!input.value) input.placeholder = defaultHint(spec);
      }
      if (spec.type === "integer") input.step = "1";
      if (typeof spec.minimum === "number") input.min = String(spec.minimum);
      if (typeof spec.maximum === "number") input.max = String(spec.maximum);
      if (typeof spec.maxLength === "number") input.maxLength = spec.maxLength;
    }
    input.required = required;
    label.append(heading(fieldLabel(name, spec), required), input);
    if (hint) label.append(note(hint));
    if (warning) label.append(note(warning, "settings-warning"));
    return { node: label, input };
  }

  function orderedNames(properties) {
    return Object.keys(properties).sort((left, right) => {
      const a = leadingOrder.indexOf(left);
      const b = leadingOrder.indexOf(right);
      if (a !== b) return (a < 0 ? leadingOrder.length : a) - (b < 0 ? leadingOrder.length : b);
      return fieldLabel(left, properties[left]).localeCompare(fieldLabel(right, properties[right]), "zh-CN");
    });
  }

  function renderConfig(settings) {
    configRoot.replaceChildren();
    configRoot.classList.add("settings-config");
    const schema = settings.config_schema || {};
    const properties = schema.properties || {};
    const required = new Set(schema.required || []);
    if (!Object.keys(properties).length) {
      addEmpty(configRoot, "这个插件没有额外业务参数。");
      return;
    }
    const values = document.createElement("div");
    values.className = "settings-grid";
    const switches = document.createElement("div");
    switches.className = "settings-switches";
    const advanced = document.createElement("details");
    advanced.className = "settings-advanced";
    const summary = document.createElement("summary");
    summary.textContent = "高级设置（一般不用改）";
    const advancedValues = document.createElement("div");
    advancedValues.className = "settings-grid";
    const advancedSwitches = document.createElement("div");
    advancedSwitches.className = "settings-switches";
    advanced.append(summary, advancedValues, advancedSwitches);
    orderedNames(properties).forEach(name => {
      const spec = properties[name] || {};
      const isRequired = required.has(name);
      const current = settings.config?.[name];
      const field = spec.type === "boolean" ? switchField(name, spec, current) : valueField(name, spec, current, isRequired);
      field.input.dataset.configField = name;
      field.input.dataset.valueType = spec.type || "string";
      const folded = isAdvanced(name, spec, isRequired);
      if (spec.type === "boolean") (folded ? advancedSwitches : switches).append(field.node);
      else (folded ? advancedValues : values).append(field.node);
    });
    [values, switches].forEach(node => { if (node.childElementCount) configRoot.append(node); });
    if (advancedValues.childElementCount || advancedSwitches.childElementCount) configRoot.append(advanced);
  }

  function renderAccounts(settings, accounts, available) {
    accountRoot.replaceChildren();
    if (!settings.account_roles.length) {
      addEmpty(accountRoot, "这个插件不需要业务账号。");
      return;
    }
    settings.account_roles.forEach(role => {
      const label = document.createElement("label");
      label.className = "settings-field";
      const select = document.createElement("select");
      select.dataset.accountRole = role.role;
      select.required = role.required === true;
      select.multiple = role.collection === true || role.binding_cardinality === "many";
      label.append(heading(fieldLabel(role.role), role.required === true));
      if (select.multiple) select.size = 4;
      else select.append(new Option("请选择账号", ""));
      accounts.filter(item => role.allowed_systems.includes(item.system)).forEach(item => {
        const option = new Option(`${item.name} · ${item.status_label}`, item.account_ref);
        option.disabled = item.available !== true;
        select.append(option);
      });
      const saved = settings.account_bindings?.[role.role];
      if (select.multiple) {
        const selected = new Set(Array.isArray(saved) ? saved : []);
        Array.from(select.options).forEach(option => { option.selected = selected.has(option.value); });
      } else select.value = String(saved || "");
      if (!available) select.disabled = true;
      label.append(select);
      if (select.multiple) label.append(note("可选择多个账号；按住 Ctrl（Mac 为 Command）增减选择。"));
      accountRoot.append(label);
    });
    if (!available) addEmpty(accountRoot, "账号列表暂时读不到，账号选择先保持不变。");
  }

  function renderResources(settings, resources, available) {
    resourceRoot.replaceChildren();
    if (!settings.resource_roles.length) {
      addEmpty(resourceRoot, "这个插件不需要飞书业务资源。");
      return;
    }
    settings.resource_roles.forEach(role => {
      const label = document.createElement("label");
      label.className = "settings-field";
      label.append(heading(fieldLabel(role.role), role.required === true));
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
    if (!available) addEmpty(resourceRoot, "飞书资源列表暂时读不到，资源选择先保持不变。");
  }

  function collect() {
    if (!(form instanceof HTMLFormElement)) throw new Error("设置页面不完整，请刷新后重试。");
    const invalid = form.querySelector(":invalid");
    const folded = invalid?.closest("details");
    if (folded instanceof HTMLDetailsElement) folded.open = true;
    if (!form.reportValidity()) throw new Error("请先完成必填设置。");
    const config = {};
    configRoot.querySelectorAll("[data-config-field]").forEach(input => {
      const name = input.dataset.configField;
      const type = input.dataset.valueType;
      if (type === "boolean") config[name] = input.checked;
      else if (input.value !== "") {
        if (type === "integer") config[name] = Number.parseInt(input.value, 10);
        else if (type === "number") config[name] = Number(input.value);
        else if (type === "array") config[name] = input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
        else if (type === "object") {
          let value;
          try { value = JSON.parse(input.value); } catch { throw new Error(`${fieldLabel(name)}不是有效的 JSON。`); }
          if (!value || Array.isArray(value) || typeof value !== "object") throw new Error(`${fieldLabel(name)}必须是 JSON 对象。`);
          config[name] = value;
        } else config[name] = input.value;
      }
    });
    const accountBindings = {};
    accountRoot.querySelectorAll("[data-account-role]").forEach(select => {
      if (select.multiple) {
        const values = Array.from(select.selectedOptions, option => option.value);
        if (values.length) accountBindings[select.dataset.accountRole] = values;
      } else if (select.value) accountBindings[select.dataset.accountRole] = select.value;
    });
    const resourceBindings = {};
    resourceRoot.querySelectorAll("[data-resource-role]").forEach(select => { if (select.value) resourceBindings[select.dataset.resourceRole] = select.value; });
    return { config, account_bindings: accountBindings, resource_bindings: resourceBindings };
  }

  async function load() {
    if (!bridgeSession || !crypto?.randomUUID) throw new Error("设置桥不可用，请返回自动化页面后重新打开。");
    saveButton.disabled = true;
    const data = await request("context", {}, CONTEXT_TIMEOUT_MS);
    context = data.settings;
    renderConfig(context);
    renderAccounts(context, data.accounts || [], data.account_catalog_available === true);
    renderResources(context, data.resources || [], data.resource_catalog_available === true);
    dirty = false;
    saveButton.disabled = false;
    setFeedback("已载入当前设置。", "success");
  }

  form?.addEventListener("input", () => {
    if (!context || busy || dirty) return;
    dirty = true;
    setFeedback("有未保存的修改。", "warn");
  });
  form?.addEventListener("change", () => form.dispatchEvent(new Event("input")));
  form?.addEventListener("submit", event => event.preventDefault());

  saveButton?.addEventListener("click", async () => {
    if (!context || busy) return;
    let values;
    try {
      values = collect();
    } catch (error) {
      setFeedback(messageOf(error, "插件设置保存失败。"), "error");
      return;
    }
    const label = saveButton.textContent;
    busy = true;
    saveButton.disabled = true;
    saveButton.textContent = "保存中…";
    showReload(false);
    setFeedback("正在保存，通常需要半分钟左右，请不要离开本页…");
    try {
      const data = await request("save", {
        ...values,
        request_id: crypto.randomUUID(),
        expected_project_configuration_version: context.project_configuration_version,
      }, SAVE_TIMEOUT_MS);
      const nextVersion = Number(data?.project_configuration_version);
      if (!Number.isInteger(nextVersion) || nextVersion < 1) {
        throw bridgeError("设置可能已保存，请点“载入最新设置”核对。", "PLUGIN_SETTINGS_SAVE_UNCONFIRMED");
      }
      context.project_configuration_version = nextVersion;
      dirty = false;
      const time = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
      setFeedback(`插件设置已保存（${time}）${data?.transition_pending ? "，后台还在切换到新设置" : ""}。`, "success");
    } catch (error) {
      setFeedback(messageOf(error, "插件设置保存失败。"), "error");
      if (RELOAD_CODES.has(error?.code)) showReload(true);
    } finally {
      busy = false;
      saveButton.disabled = false;
      saveButton.textContent = label;
    }
  });

  load().catch(error => setFeedback(messageOf(error, "插件设置暂时不可用。"), "error"));
})();
