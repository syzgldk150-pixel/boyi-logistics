(() => {
  "use strict";
  const fetch = window.ConsoleUI.pageRequest();
  const form = document.querySelector(".main-content:not([hidden]) [data-default-plugin-settings]");
  if (!form || form.dataset.initialized) return;
  form.dataset.initialized = "true";
  const fields = form.querySelector("[data-account-fields]");
  const configFields = form.querySelector("[data-config-fields]");
  const feedback = form.querySelector("[data-settings-feedback]");
  const submit = form.querySelector("button[type=submit]");
  let snapshot;
  async function request(operation, payload) {
    const response = await fetch(form.dataset.endpoint, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
      body: JSON.stringify({bridge_session: form.dataset.session, operation, payload}),
    });
    const result = await response.json();
    if (!response.ok || result.ok !== true) {
      throw new Error(result.message || result.error?.message || "设置暂时不可用，请重试。");
    }
    return result.data;
  }
  async function load() {
    const data = await request("context", {});
    snapshot = data.settings;
    if (snapshot.account_roles.length && data.account_catalog_available !== true) throw new Error("账号目录暂时不可用，请稍后重试。");
    fields.replaceChildren();
    if (!snapshot.account_roles.length && !snapshot.default_config_fields?.length) fields.textContent = "无需填写额外参数，保存后即可使用。";
    for (const role of snapshot.account_roles) {
      const group = document.createElement("div");
      group.className = "form-group";
      const label = document.createElement("label");
      const select = document.createElement("select");
      select.className = "review-input";
      select.id = `plugin-account-${role.role}`;
      select.name = role.role;
      select.required = role.required === true;
      select.multiple = role.binding_cardinality === "many" || role.collection === true;
      label.htmlFor = select.id;
      label.textContent = role.label || role.title || "业务账号";
      if (!select.multiple) select.add(new Option("请选择账号", ""));
      const bound = snapshot.account_bindings[role.role];
      const selected = Array.isArray(bound) ? bound : bound ? [bound] : [];
      for (const account of data.accounts) {
        const systems = role.allowed_systems || role.systems || (role.system ? [role.system] : []);
        if (systems.length && !systems.includes(account.system)) continue;
        const option = new Option(`${account.name} · ${account.status_label}`, account.account_ref);
        option.selected = selected.includes(account.account_ref);
        select.add(option);
      }
      for (const reference of selected) {
        if (Array.from(select.options).some(option => option.value === reference)) continue;
        const unavailable = new Option("原账号当前不可用，请明确更换或清除", reference, true, true);
        unavailable.disabled = true;
        select.add(unavailable);
      }
      group.append(label, select);
      if (select.multiple) {
        const hint = document.createElement("p");
        hint.textContent = "此任务可以使用多个账号，请保留全部需要的账号。";
        group.append(hint);
      }
      fields.append(group);
    }
    configFields.replaceChildren();
    for (const field of snapshot.default_config_fields || []) {
      const group = document.createElement("div");
      group.className = "form-group";
      const label = document.createElement("label");
      const choices = field.enum.length ? field.enum : field.kind === "checkbox" ? [true, false] : null;
      const control = document.createElement(choices ? "select" : "input");
      control.className = "review-input";
      control.id = `plugin-config-${field.path}`;
      control.dataset.configKey = field.path;
      control.required = field.required;
      label.htmlFor = control.id;
      label.textContent = field.label;
      if (choices) {
        control.add(new Option("请选择", ""));
        choices.forEach((value, index) => {
          const directionLabels = {received: "收到的问题件", published: "发出的问题件", both: "收到和发出的全部问题件"};
          const text = field.path === "direction" ? directionLabels[value] || String(value) : typeof value === "boolean" ? value ? "启用" : "关闭" : String(value);
          const option = new Option(text, String(index));
          option.selected = field.present && field.value === value;
          control.add(option);
        });
        control._configChoices = choices;
      } else {
        control.type = field.kind === "number" ? "number" : "text";
        control.value = field.present ? String(field.value) : "";
        if (field.kind === "number") control.step = field.step;
        for (const [attribute, value] of Object.entries({min: field.minimum, max: field.maximum, minlength: field.min_length, maxlength: field.max_length})) {
          if (value !== null) control.setAttribute(attribute, String(value));
        }
      }
      group.append(label, control);
      if (field.hint) { const hint = document.createElement("p"); hint.textContent = field.hint; group.append(hint); }
      configFields.append(group);
    }
    fields.setAttribute("aria-busy", "false");
    submit.disabled = false;
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!snapshot || !form.reportValidity()) return;
    submit.disabled = true;
    feedback.textContent = "正在保存设置…";
    const bindings = {};
    const config = {...snapshot.config};
    fields.querySelectorAll("select").forEach(select => {
      const selected = Array.from(select.selectedOptions, option => option.value).filter(Boolean);
      if (selected.length) bindings[select.name] = select.multiple ? selected : selected[0];
    });
    configFields.querySelectorAll("[data-config-key]").forEach(control => {
      const key = control.dataset.configKey;
      if (control.value === "") delete config[key];
      else config[key] = control._configChoices ? control._configChoices[Number(control.value)] : control.type === "number" ? Number(control.value) : control.value;
    });
    try {
      await request("save", {
        config, account_bindings: bindings,
        resource_bindings: snapshot.resource_bindings,
        request_id: crypto.randomUUID(),
        expected_project_configuration_version: snapshot.project_configuration_version,
      });
      await load();
      feedback.textContent = "设置已保存，新运行会使用当前配置。";
    } catch (error) {
      feedback.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
  load().catch(error => {
    fields.setAttribute("aria-busy", "false");
    fields.textContent = "账号设置未能加载。";
    feedback.textContent = error.message;
  });
})();
