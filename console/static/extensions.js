(() => {
  "use strict";

  const MAX_NAME_LENGTH = 120;
  const MAX_SCHEDULE_TIMES = 24;
  const SAFE_IDENTIFIER = /^[A-Za-z][A-Za-z0-9_.:-]{0,159}$/;
  const SAFE_BINDING_ID = /^[A-Za-z0-9_.:@/-]{1,160}$/;
  const SAFE_ENTRYPOINT = /^[a-z][a-z0-9_.-]{0,127}$/;
  const SAFE_KINDS = new Set([
    "console",
    "scheduler",
    "webhook",
    "feishu",
    "events",
    "module_slots",
  ]);
  const SAFE_CONFIG_TYPES = new Set(["string", "integer", "number", "boolean", "array"]);
  const SAFE_SCHEMA_KEYSETS = {
    object: new Set(["type", "additionalProperties", "properties", "required", "title", "description"]),
    string: new Set(["type", "title", "description", "enum", "default", "minLength", "maxLength"]),
    integer: new Set(["type", "title", "description", "enum", "default", "minimum", "maximum"]),
    number: new Set(["type", "title", "description", "enum", "default", "minimum", "maximum"]),
    boolean: new Set(["type", "title", "description", "enum", "default"]),
    array: new Set(["type", "title", "description", "items", "default", "minItems", "maxItems"]),
  };

  const secureRequestId = () => window.crypto?.randomUUID?.() || "";

  const setFeedback = (box, message = "", kind = "") => {
    if (!(box instanceof HTMLElement)) return;
    box.textContent = message;
    box.hidden = !message;
    if (kind) box.dataset.kind = kind;
    else delete box.dataset.kind;
  };

  const responseMessage = (payload, fallback) => (
    payload?.error?.message || payload?.message || fallback
  );

  const createNode = (tag, className = "", text = undefined) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };

  const clearNode = (node) => {
    if (!(node instanceof Node)) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  };

  const safeText = (value, fallback = "") => {
    if (typeof value === "string") return value.trim();
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    return fallback;
  };

  const safeRole = (value) => {
    const role = safeText(value);
    return SAFE_IDENTIFIER.test(role) ? role : "";
  };

  const safeEntryPoint = (value) => {
    const id = safeText(value);
    return SAFE_ENTRYPOINT.test(id) ? id : "";
  };

  const hasOnlySchemaKeys = (value, type) => Object.keys(value).every(
    (key) => SAFE_SCHEMA_KEYSETS[type]?.has(key),
  );

  const schemaValueMatches = (type, value, itemSchema = null) => {
    if (value === null) return false;
    if (type === "string") return typeof value === "string";
    if (type === "boolean") return typeof value === "boolean";
    if (type === "integer") return typeof value === "number"
      && Number.isFinite(value) && Number.isInteger(value) && Number.isSafeInteger(value);
    if (type === "number") return typeof value === "number" && Number.isFinite(value);
    if (type === "array") return Array.isArray(value)
      && itemSchema !== null
      && value.every((item) => schemaValueMatches(itemSchema.type, item, itemSchema.items || null));
    return false;
  };

  const schemaNumberConstraint = (value, key, {integer = false, minimum = 0} = {}) => (
    typeof value[key] === "number"
      && Number.isFinite(value[key])
      && (!integer || Number.isInteger(value[key]))
      && value[key] >= minimum
  );

  const setStructuredPath = (target, path, value) => {
    if (!target || typeof target !== "object" || !Array.isArray(path) || !path.length) return false;
    let cursor = target;
    for (let index = 0; index < path.length; index += 1) {
      const part = path[index];
      if (typeof part !== "string" || !SAFE_IDENTIFIER.test(part)) return false;
      if (index === path.length - 1) cursor[part] = value;
      else {
        if (!cursor[part] || typeof cursor[part] !== "object" || Array.isArray(cursor[part])) cursor[part] = {};
        cursor = cursor[part];
      }
    }
    return true;
  };

  function safeDescriptorOptions(value, idKey) {
    if (!Array.isArray(value)) return [];
    return value
      .filter((item) => item && typeof item === "object" && !Array.isArray(item))
      .map((item) => ({
        id: safeText(item[idKey]),
        name: safeText(item.name || item.display_name || item[idKey]),
        system: safeText(item.system || item.kind),
        status: safeText(item.status || item.status_label),
        usable: item.available !== false && item.binding_usable !== false && item.is_active !== false,
      }))
      .filter((item) => item.id && SAFE_BINDING_ID.test(item.id) && item.name)
      .slice(0, 256);
  }

  function safeConfigNode(value, depth) {
    if (!value || typeof value !== "object" || Array.isArray(value) || depth > 8) return null;
    const type = safeText(value.type);
    if (type === "object") {
      if (!hasOnlySchemaKeys(value, type)
        || value.additionalProperties !== false || !value.properties
        || typeof value.properties !== "object" || Array.isArray(value.properties)) return null;
      const properties = {};
      const propertyKeys = Object.keys(value.properties);
      if (propertyKeys.length > 100) return null;
      for (const key of propertyKeys) {
        if (!SAFE_IDENTIFIER.test(key)) return null;
        const child = safeConfigNode(value.properties[key], depth + 1);
        if (!child) return null;
        properties[key] = child;
      }
      const required = value.required === undefined ? [] : value.required;
      if (!Array.isArray(required)
        || required.some((item) => typeof item !== "string"
          || !Object.prototype.hasOwnProperty.call(properties, item))
        || required.length !== new Set(required).size) return null;
      return {
        type,
        title: safeText(value.title),
        description: safeText(value.description),
        properties,
        required: [...new Set(required)],
        additionalProperties: false,
      };
    }
    if (!SAFE_CONFIG_TYPES.has(type)) return null;
    if (!hasOnlySchemaKeys(value, type)) return null;
    const result = {
      type,
      title: safeText(value.title),
      description: safeText(value.description),
    };
    if (value.enum !== undefined) {
      if (!Array.isArray(value.enum) || value.enum.length === 0 || value.enum.length > 100
        || value.enum.some((item) => !schemaValueMatches(type, item))) {
        return null;
      }
      result.enum = value.enum.slice();
    }
    if (type === "string") {
      if (value.minLength !== undefined && !schemaNumberConstraint(value, "minLength", {integer: true})) return null;
      if (value.maxLength !== undefined && !schemaNumberConstraint(value, "maxLength", {integer: true})) return null;
      if (value.minLength !== undefined && value.maxLength !== undefined && value.minLength > value.maxLength) return null;
      if (value.minLength !== undefined) result.minLength = value.minLength;
      if (value.maxLength !== undefined) result.maxLength = value.maxLength;
    }
    if (type === "integer" || type === "number") {
      if (value.minimum !== undefined && !schemaNumberConstraint(value, "minimum", {minimum: Number.NEGATIVE_INFINITY})) return null;
      if (value.maximum !== undefined && !schemaNumberConstraint(value, "maximum", {minimum: Number.NEGATIVE_INFINITY})) return null;
      if (value.minimum !== undefined && value.maximum !== undefined && value.minimum > value.maximum) return null;
      if (value.minimum !== undefined) result.minimum = value.minimum;
      if (value.maximum !== undefined) result.maximum = value.maximum;
    }
    if (type === "array") {
      if (!value.items || typeof value.items !== "object" || Array.isArray(value.items)) return null;
      const itemSchema = safeConfigNode(value.items, depth + 1);
      if (!itemSchema || !["string", "integer", "number"].includes(itemSchema.type)) return null;
      result.items = itemSchema;
      if (value.minItems !== undefined && !schemaNumberConstraint(value, "minItems", {integer: true})) return null;
      if (value.maxItems !== undefined && !schemaNumberConstraint(value, "maxItems", {integer: true})) return null;
      if (value.minItems !== undefined && value.maxItems !== undefined && value.minItems > value.maxItems) return null;
      if (value.minItems !== undefined) result.minItems = value.minItems;
      if (value.maxItems !== undefined) result.maxItems = value.maxItems;
    }
    if (Object.prototype.hasOwnProperty.call(value, "default")) {
      if (!schemaValueMatches(type, value.default, result.items || null)) return null;
      result.default = Array.isArray(value.default) ? value.default.slice() : value.default;
    }
    return result;
  }

  function safeConfigSchema(value, depth = 0) {
    if (!value || typeof value !== "object" || Array.isArray(value) || depth > 8) return null;
    if (value.type !== "object" || value.additionalProperties !== false || !value.properties
      || typeof value.properties !== "object" || Array.isArray(value.properties)) {
      return null;
    }
    const schema = safeConfigNode(value, depth);
    return schema && schema.type === "object" ? schema : null;
  }

  function safeScheduling(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || typeof value.supported !== "boolean") return null;
    const rawDefault = value.default_schedule;
    if (!rawDefault || typeof rawDefault !== "object" || Array.isArray(rawDefault)) return null;
    const kind = safeText(rawDefault.kind);
    if (!["none", "daily_times"].includes(kind) || !Array.isArray(rawDefault.times)
      || rawDefault.times.length > MAX_SCHEDULE_TIMES
      || !rawDefault.times.every((item) => typeof item === "string" && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(item))
      || rawDefault.times.length !== new Set(rawDefault.times).size
      || typeof rawDefault.enabled !== "boolean") return null;
    const times = rawDefault.times.slice();
    if ((kind === "none" && (times.length || rawDefault.enabled))
      || (kind === "daily_times" && (!times.length || !rawDefault.enabled))) return null;
    return {
      supported: value.supported,
      default_schedule: {kind, times, enabled: rawDefault.enabled},
    };
  }

  const safeProjection = (raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const pluginId = safeText(raw.plugin_id);
    const version = safeText(raw.version);
    const name = safeText(raw.name, pluginId);
    if (!pluginId || !version || !name) return null;

    const hostApi = raw.host_api && typeof raw.host_api === "object" && !Array.isArray(raw.host_api)
      ? {
          minimum: safeText(raw.host_api.minimum),
          maximum_exclusive: safeText(raw.host_api.maximum_exclusive),
        }
      : {minimum: "", maximum_exclusive: ""};

    const permissions = Array.isArray(raw.permissions)
      ? raw.permissions
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          name: safeText(item.name),
          operations: Array.isArray(item.operations)
            ? item.operations.map((value) => safeEntryPoint(value)).filter(Boolean).slice(0, 64)
            : [],
          account_role: safeRole(item.account_role),
          resource_role: safeRole(item.resource_role),
        }))
        .filter((item) => item.name)
        .slice(0, 128)
      : [];

    const accountOptions = safeDescriptorOptions(raw.account_options, "account_id");
    const resourceOptions = safeDescriptorOptions(raw.resource_options, "resource_id");

    const accountRoles = Array.isArray(raw.account_roles)
      ? raw.account_roles
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => {
          const allowedSystems = Array.isArray(item.allowed_systems)
            ? item.allowed_systems.map((value) => safeText(value)).filter(Boolean).slice(0, 16)
            : [];
          const roleOptions = safeDescriptorOptions(item.options, "account_id");
          return {
            role: safeRole(item.role),
            allowed_systems: allowedSystems,
            required: item.required === true,
            options: (roleOptions.length ? roleOptions : accountOptions)
              .filter((option) => allowedSystems.includes(option.system)),
          };
        })
        .filter((item) => item.role && item.allowed_systems.length)
        .slice(0, 64)
      : [];

    const resourceRoles = Array.isArray(raw.resource_roles)
      ? raw.resource_roles
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => {
          const allowedKinds = Array.isArray(item.allowed_kinds)
            ? item.allowed_kinds.map((value) => safeText(value)).filter(Boolean).slice(0, 16)
            : [];
          const roleOptions = safeDescriptorOptions(item.options, "resource_id");
          return {
            role: safeRole(item.role),
            allowed_kinds: allowedKinds,
            required: item.required === true,
            options: (roleOptions.length ? roleOptions : resourceOptions)
              .filter((option) => allowedKinds.includes(option.system)),
          };
        })
        .filter((item) => item.role && item.allowed_kinds.length)
        .slice(0, 64)
      : [];

    const contributions = Array.isArray(raw.contributions)
      ? raw.contributions
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          id: safeEntryPoint(item.id),
          kind: SAFE_KINDS.has(safeText(item.kind)) ? safeText(item.kind) : "",
          title: safeText(item.title),
          default_enabled: item.default_enabled === true,
        }))
        .filter((item) => item.id && item.kind)
        .slice(0, 128)
      : [];

    const warnings = Array.isArray(raw.warnings)
      ? raw.warnings
        .map((item) => safeText(item).slice(0, 512))
        .filter(Boolean)
        .slice(0, 16)
      : [];

    const configSchema = safeConfigSchema(raw.config_schema);
    const scheduling = safeScheduling(raw.scheduling);
    if (!configSchema || !scheduling) return null;

    return {
      plugin_id: pluginId,
      name,
      version,
      host_api: hostApi,
      permissions,
      account_roles: accountRoles,
      resource_roles: resourceRoles,
      config_schema: configSchema,
      contributions,
      scheduling,
      account_options: accountOptions,
      resource_options: resourceOptions,
      account_pool_available: raw.account_pool_available === true,
      resource_pool_available: raw.resource_pool_available === true,
      warnings,
    };
  };

  const fileIsZip = (file) => file instanceof File && /\.zip$/i.test(file.name || "");

  const filenameFallback = (file) => {
    if (!fileIsZip(file)) return "";
    return String(file.name || "")
      .replace(/\.zip$/i, "")
      .replace(/[\u0000-\u001f\u007f]/g, "")
      .trim()
      .slice(0, MAX_NAME_LENGTH);
  };

  const buildConfigInput = (field, path) => {
    const label = createNode("label", "extension-config-field");
    const labelText = createNode("span", "extension-config-label", field.title || path[path.length - 1]);
    if (field.required) labelText.appendChild(createNode("em", "extension-required", " *"));
    label.appendChild(labelText);
    if (field.description) label.appendChild(createNode("small", "extension-config-hint", field.description));

    const id = `extension-config-${path.map((part) => encodeURIComponent(part).replace(/%/g, "_")).join("__")}`;
    let control;
    if (field.type === "boolean") {
      const wrapper = createNode("span", "extension-checkbox-wrap");
      control = document.createElement("input");
      control.type = "checkbox";
      control.id = id;
      control.checked = field.default === true;
      wrapper.appendChild(control);
      wrapper.appendChild(createNode("span", "extension-checkbox-label", "启用此项"));
      label.appendChild(wrapper);
    } else if (field.type === "array") {
      control = document.createElement("textarea");
      control.rows = 3;
      control.placeholder = "每行一个值";
      control.id = id;
      if (Array.isArray(field.default)) control.value = field.default.join("\n");
      label.appendChild(control);
    } else if (Array.isArray(field.enum) && field.enum.length) {
      control = document.createElement("select");
      control.id = id;
      const emptyOption = new Option(field.required ? "请选择" : "不设置", "");
      emptyOption.disabled = field.required;
      emptyOption.selected = true;
      control.appendChild(emptyOption);
      field.enum.forEach((value) => {
        let serialized = "";
        try { serialized = JSON.stringify(value); } catch (_error) { serialized = ""; }
        if (!serialized) return;
        const option = new Option(String(value), serialized);
        control.appendChild(option);
      });
      if (Object.prototype.hasOwnProperty.call(field, "default")) {
        const serialized = JSON.stringify(field.default);
        if (Array.from(control.options).some((option) => option.value === serialized)) control.value = serialized;
      }
      label.appendChild(control);
    } else {
      control = document.createElement("input");
      control.type = field.type === "number" || field.type === "integer" ? "number" : "text";
      if (field.type === "integer") control.step = "1";
      if (typeof field.minimum === "number") control.min = String(field.minimum);
      if (typeof field.maximum === "number") control.max = String(field.maximum);
      if (typeof field.minLength === "number") control.minLength = field.minLength;
      if (typeof field.maxLength === "number") control.maxLength = field.maxLength;
      control.id = id;
      if (Object.prototype.hasOwnProperty.call(field, "default")) control.value = String(field.default);
      label.appendChild(control);
    }
    control.classList.add("review-input", "extension-config-control");
    if (field.type !== "boolean") control.required = field.required;
    control.dataset.extensionConfigPath = JSON.stringify(path);
    control.dataset.extensionConfigType = field.type;
    if (field.type === "array") {
      control.dataset.extensionConfigItemType = field.items.type;
      if (typeof field.minItems === "number") control.dataset.extensionConfigMinItems = String(field.minItems);
      if (typeof field.maxItems === "number") control.dataset.extensionConfigMaxItems = String(field.maxItems);
      if (typeof field.items.minLength === "number") control.dataset.extensionConfigItemMinLength = String(field.items.minLength);
      if (typeof field.items.maxLength === "number") control.dataset.extensionConfigItemMaxLength = String(field.items.maxLength);
      if (typeof field.items.minimum === "number") control.dataset.extensionConfigItemMinimum = String(field.items.minimum);
      if (typeof field.items.maximum === "number") control.dataset.extensionConfigItemMaximum = String(field.items.maximum);
    }
    control.dataset.extensionConfigRequired = field.required ? "true" : "false";
    control.dataset.extensionConfigHasDefault = Object.prototype.hasOwnProperty.call(field, "default") ? "true" : "false";
    return label;
  };

  const renderConfigFields = (container, schema) => {
    clearNode(container);
    const walk = (node, prefix, requiredKeys) => {
      const fragment = document.createDocumentFragment();
      Object.entries(node || {}).forEach(([key, field]) => {
        const path = [...prefix, key];
        const required = requiredKeys.has(key);
        if (field?.type === "object") {
          const group = createNode("fieldset", "extension-config-group");
          group.dataset.extensionConfigObjectPath = JSON.stringify(path);
          group.dataset.extensionConfigObjectRequired = required ? "true" : "false";
          group.appendChild(createNode("legend", "extension-config-group-title", field.title || key));
          if (field.description) group.appendChild(createNode("p", "extension-config-hint", field.description));
          group.appendChild(walk(field.properties || {}, path, new Set(field.required || [])));
          fragment.appendChild(group);
          return;
        }
        fragment.appendChild(buildConfigInput({...field, required}, path));
      });
      return fragment;
    };
    const properties = schema?.properties || {};
    container.appendChild(walk(properties, [], new Set(Array.isArray(schema?.required) ? schema.required : [])));
    if (!container.children.length) container.appendChild(createNode("p", "extension-empty-copy", "此扩展不需要额外配置。"));
  };

  const renderRoleOptions = (role, type, options, placeholder) => {
    const select = document.createElement("select");
    select.className = "review-input extension-binding-control";
    select.dataset.extensionBindingRole = role.role;
    select.dataset.extensionBindingType = type;
    select.dataset.extensionBindingRequired = role.required ? "true" : "false";
    const hasUsableOptions = options.some((item) => item.usable);
    const emptyOption = new Option(hasUsableOptions ? placeholder : "暂无可用绑定", "");
    emptyOption.disabled = hasUsableOptions;
    select.appendChild(emptyOption);
    select.disabled = !hasUsableOptions;
    options.forEach((item) => {
      const option = new Option(item.name, item.id);
      option.disabled = !item.usable;
      if (item.status) option.textContent = `${item.name}（${item.status}）`;
      select.appendChild(option);
    });
    return select;
  };

  const renderRoles = (container, roles, type) => {
    clearNode(container);
    if (!roles.length) {
      container.appendChild(createNode("p", "extension-empty-copy", type === "account" ? "此扩展未声明业务账号。" : "此扩展未声明业务资源。"));
      return;
    }
    roles.forEach((role, index) => {
      const label = createNode("label", "extension-binding-field");
      const baseLabel = type === "account" ? "业务账号" : "业务资源";
      const labelText = createNode(
        "span",
        "extension-config-label",
        `${baseLabel}${roles.length > 1 ? ` ${index + 1}` : ""}`,
      );
      if (role.required) labelText.appendChild(createNode("em", "extension-required", " *"));
      label.appendChild(labelText);
      label.appendChild(renderRoleOptions(
        role,
        type,
        role.options,
        type === "account" ? "选择业务账号" : "选择业务资源",
      ));
      container.appendChild(label);
    });
  };

  const renderPermissions = (container, permissions) => {
    clearNode(container);
    if (!permissions.length) {
      container.appendChild(createNode("p", "extension-empty-copy", "此扩展未声明额外权限。"));
      return;
    }
    permissions.forEach((permission) => {
      const item = createNode("div", "extension-permission-item");
      item.appendChild(createNode("strong", "extension-permission-name", permission.name));
      item.appendChild(createNode("span", "extension-permission-scope", "仅在扩展运行时使用，并保留操作记录。"));
      container.appendChild(item);
    });
  };

  const renderSummary = (container, projection) => {
    clearNode(container);
    const rows = [
      ["扩展名称", projection.name],
      ["版本", projection.version],
      ["包含功能", `${projection.contributions.length} 项`],
      ["需要授权", `${projection.permissions.length} 项`],
    ];
    rows.forEach(([term, value]) => {
      const wrapper = createNode("div");
      wrapper.appendChild(createNode("dt", "", term));
      wrapper.appendChild(createNode("dd", "", value || "未提供"));
      container.appendChild(wrapper);
    });
  };

  const renderWarnings = (container, warnings) => {
    clearNode(container);
    if (!warnings.length) return;
    warnings.forEach((warning) => {
      container.appendChild(createNode("p", "extension-wizard-warning", warning));
    });
  };

  const renderEntrypoints = (container, contributions) => {
    clearNode(container);
    if (!contributions.length) {
      container.appendChild(createNode("p", "extension-empty-copy", "此扩展没有可选择的入口。"));
      return;
    }
    contributions.forEach((contribution) => {
      const label = createNode("label", "extension-entrypoint-option");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = contribution.id;
      input.checked = contribution.default_enabled;
      input.dataset.extensionEntrypoint = contribution.id;
      input.setAttribute("aria-label", contribution.title || contribution.id);
      label.appendChild(input);
      const copy = createNode("span", "extension-entrypoint-copy");
      copy.appendChild(createNode("strong", "", contribution.title || contribution.id));
      copy.appendChild(createNode("small", "", contribution.default_enabled ? "安装后开启" : "可按需开启"));
      label.appendChild(copy);
      container.appendChild(label);
    });
  };

  const renderSchedule = (container, scheduling) => {
    clearNode(container);
    if (!scheduling.supported) {
      container.appendChild(createNode("p", "extension-empty-copy", "此扩展未声明系统定时入口。"));
      return;
    }
    const schedule = scheduling.default_schedule;
    const label = createNode("label", "extension-schedule-field");
    label.appendChild(createNode("span", "extension-config-label", "定时方式"));
    const select = document.createElement("select");
    select.className = "review-input extension-schedule-kind";
    select.dataset.extensionScheduleKind = "true";
    [["none", "不设置定时"], ["daily_times", "每天指定时间"]].forEach(([value, text]) => {
      select.appendChild(new Option(text, value));
    });
    select.value = schedule.kind;
    label.appendChild(select);
    container.appendChild(label);

    const timesGroup = createNode("div", "extension-schedule-times");
    timesGroup.dataset.extensionScheduleTimes = "true";
    const times = Array.isArray(schedule.times) ? schedule.times : [];
    (times.length ? times : [""]).slice(0, MAX_SCHEDULE_TIMES).forEach((time) => appendScheduleTime(timesGroup, time));
    container.appendChild(timesGroup);
    const add = document.createElement("button");
    add.type = "button";
    add.className = "ghost-btn extension-add-time";
    add.dataset.extensionAddTime = "true";
    add.textContent = "增加时间";
    container.appendChild(add);
    const update = () => {
      const active = select.value === "daily_times";
      timesGroup.hidden = !active;
      add.hidden = !active;
      if (active && !timesGroup.querySelector("input")) appendScheduleTime(timesGroup, "");
    };
    select.addEventListener("change", update);
    add.addEventListener("click", () => {
      if (timesGroup.querySelectorAll("input").length < MAX_SCHEDULE_TIMES) appendScheduleTime(timesGroup, "");
    });
    timesGroup.addEventListener("click", (event) => {
      const button = event.target.closest("[data-extension-remove-time]");
      if (!(button instanceof HTMLButtonElement)) return;
      button.closest(".extension-schedule-row")?.remove();
      if (select.value === "daily_times" && !timesGroup.querySelector("input")) appendScheduleTime(timesGroup, "");
    });
    update();
  };

  function appendScheduleTime(container, value) {
    const row = createNode("div", "extension-schedule-row");
    const input = document.createElement("input");
    input.type = "time";
    input.className = "review-input extension-schedule-time";
    input.value = /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value) ? value : "";
    input.dataset.extensionScheduleTime = "true";
    input.setAttribute("aria-label", "定时时间");
    row.appendChild(input);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-btn extension-remove-time";
    remove.dataset.extensionRemoveTime = "true";
    remove.textContent = "删除";
    row.appendChild(remove);
    container.appendChild(row);
  }

  const setupInstallWizard = () => {
    const wizard = document.querySelector("[data-extension-wizard]");
    const dialog = document.querySelector("[data-extension-dialog]");
    const inspectForm = wizard?.querySelector("[data-extension-inspect-form]");
    const finalForm = wizard?.querySelector("[data-extension-final-form]");
    if (!(wizard instanceof HTMLElement)
      || !(dialog instanceof HTMLDialogElement)
      || !(inspectForm instanceof HTMLFormElement)
      || !(finalForm instanceof HTMLFormElement)) return;

    const fileInput = inspectForm.querySelector('input[name="package"]');
    const nameInput = wizard.querySelector("[data-extension-instance-name]");
    const inspectButton = inspectForm.querySelector("[data-extension-inspect-submit]");
    const inspectFeedback = inspectForm.querySelector("[data-extension-feedback]");
    const inspection = wizard.querySelector("[data-extension-inspection]");
    const finalButton = finalForm.querySelector("[data-extension-install-submit]");
    const finalFeedback = finalForm.querySelector("[data-extension-final-feedback]");
    const resetButton = wizard.querySelector("[data-extension-reset]");
    const dropzone = wizard.querySelector("[data-extension-dropzone]");
    const fileName = wizard.querySelector("[data-extension-file-name]");
    const inspectionName = wizard.querySelector("[data-extension-inspection-name]");
    const state = {requestId: "", packageFile: null, projection: null, finalSent: false, frozen: null, sending: false};

    const lockAfterFinalSend = () => {
      wizard.querySelectorAll("input, select, textarea, button").forEach((control) => {
        if (control === finalButton) return;
        control.disabled = true;
      });
    };

    const resetInspection = () => {
      if (state.finalSent) return;
      state.projection = null;
      state.requestId = "";
      state.packageFile = null;
      clearNode(wizard.querySelector("[data-extension-inspection-summary]"));
      clearNode(wizard.querySelector("[data-extension-permissions]"));
      clearNode(wizard.querySelector("[data-extension-account-roles]"));
      clearNode(wizard.querySelector("[data-extension-resource-roles]"));
      clearNode(wizard.querySelector("[data-extension-config-fields]"));
      clearNode(wizard.querySelector("[data-extension-entrypoints]"));
      clearNode(wizard.querySelector("[data-extension-schedule]"));
      clearNode(wizard.querySelector("[data-extension-inspection-warnings]"));
      const permissionConfirmation = wizard.querySelector("[data-extension-permissions-confirmed]");
      if (permissionConfirmation instanceof HTMLInputElement) permissionConfirmation.checked = false;
      if (inspection instanceof HTMLElement) inspection.hidden = true;
      if (finalButton instanceof HTMLButtonElement) finalButton.disabled = true;
      if (fileName instanceof HTMLElement) fileName.textContent = "仅支持 .zip 文件";
    };

    const renderInspection = (projection) => {
      renderSummary(wizard.querySelector("[data-extension-inspection-summary]"), projection);
      renderWarnings(wizard.querySelector("[data-extension-inspection-warnings]"), projection.warnings);
      renderPermissions(wizard.querySelector("[data-extension-permissions]"), projection.permissions);
      renderRoles(wizard.querySelector("[data-extension-account-roles]"), projection.account_roles, "account");
      renderRoles(wizard.querySelector("[data-extension-resource-roles]"), projection.resource_roles, "resource");
      renderConfigFields(wizard.querySelector("[data-extension-config-fields]"), projection.config_schema);
      renderEntrypoints(wizard.querySelector("[data-extension-entrypoints]"), projection.contributions);
      renderSchedule(wizard.querySelector("[data-extension-schedule]"), projection.scheduling);
      const derivedName = filenameFallback(state.packageFile);
      if (nameInput instanceof HTMLInputElement && !nameInput.value.trim() && derivedName) nameInput.value = derivedName;
      if (inspectionName instanceof HTMLElement) inspectionName.textContent = `${projection.name} 已通过检查，请确认设置。`;
      if (inspection instanceof HTMLElement) inspection.hidden = false;
      inspection.scrollIntoView({block: "start", behavior: "smooth"});
    };

    const readValue = (control) => {
      const type = control.dataset.extensionConfigType || "string";
      if (type === "boolean") return control.checked === true;
      if (type === "array") {
        const itemType = control.dataset.extensionConfigItemType || "string";
        const values = String(control.value || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
        const minItems = control.dataset.extensionConfigMinItems === undefined
          ? null : Number(control.dataset.extensionConfigMinItems);
        const maxItems = control.dataset.extensionConfigMaxItems === undefined
          ? null : Number(control.dataset.extensionConfigMaxItems);
        const required = control.dataset.extensionConfigRequired === "true";
        if ((minItems !== null && values.length < minItems && (required || values.length > 0))
          || (maxItems !== null && values.length > maxItems)) {
          control.setCustomValidity("数组项数量不符合扩展要求。");
          return [];
        }
        if (itemType === "string") {
          const minLength = control.dataset.extensionConfigItemMinLength === undefined
            ? null : Number(control.dataset.extensionConfigItemMinLength);
          const maxLength = control.dataset.extensionConfigItemMaxLength === undefined
            ? null : Number(control.dataset.extensionConfigItemMaxLength);
          if (values.some((value) => (minLength !== null && value.length < minLength)
            || (maxLength !== null && value.length > maxLength))) {
            control.setCustomValidity("数组中的文本长度不符合扩展要求。");
            return [];
          }
          control.setCustomValidity("");
          return values;
        }
        if (itemType === "integer") {
          const parsed = [];
          for (const value of values) {
            if (!/^-?\d+$/.test(value) || !Number.isInteger(Number(value)) || !Number.isSafeInteger(Number(value))) {
              control.setCustomValidity("数组中的每个值必须是安全整数。");
              return [];
            }
            const number = Number(value);
            const minimum = control.dataset.extensionConfigItemMinimum === undefined
              ? null : Number(control.dataset.extensionConfigItemMinimum);
            const maximum = control.dataset.extensionConfigItemMaximum === undefined
              ? null : Number(control.dataset.extensionConfigItemMaximum);
            if ((minimum !== null && number < minimum) || (maximum !== null && number > maximum)) {
              control.setCustomValidity("数组中的整数不符合扩展范围要求。");
              return [];
            }
            parsed.push(number);
          }
          control.setCustomValidity("");
          return parsed;
        }
        if (itemType === "number") {
          const parsed = [];
          for (const value of values) {
            const number = Number(value);
            if (!Number.isFinite(number)) {
              control.setCustomValidity("数组中的每个值必须是有限数字。");
              return [];
            }
            const minimum = control.dataset.extensionConfigItemMinimum === undefined
              ? null : Number(control.dataset.extensionConfigItemMinimum);
            const maximum = control.dataset.extensionConfigItemMaximum === undefined
              ? null : Number(control.dataset.extensionConfigItemMaximum);
            if ((minimum !== null && number < minimum) || (maximum !== null && number > maximum)) {
              control.setCustomValidity("数组中的数字不符合扩展范围要求。");
              return [];
            }
            parsed.push(number);
          }
          control.setCustomValidity("");
          return parsed;
        }
        control.setCustomValidity("数组类型不受支持。");
        return [];
      }
      if (control instanceof HTMLSelectElement && control.value) {
        try { return JSON.parse(control.value); } catch (_error) { return control.value; }
      }
      if (type === "integer") {
        const value = control.value.trim();
        if (!value) {
          control.setCustomValidity("");
          return "";
        }
        if (!/^-?\d+$/.test(value) || !Number.isInteger(Number(value)) || !Number.isSafeInteger(Number(value))) {
          control.setCustomValidity("请输入安全整数，不会截断小数。");
          return "";
        }
        control.setCustomValidity("");
        return Number(value);
      }
      if (type === "number") {
        const value = control.value.trim();
        if (!value) {
          control.setCustomValidity("");
          return "";
        }
        const number = Number(value);
        if (!Number.isFinite(number)) {
          control.setCustomValidity("请输入有限数字。");
          return "";
        }
        control.setCustomValidity("");
        return number;
      }
      control.setCustomValidity("");
      return String(control.value || "").trim();
    };

    const buildConfig = () => {
      const config = {};
      let configError = "";
      const assign = (pathText, value) => {
        let path;
        try {
          path = JSON.parse(pathText);
        } catch (_error) {
          configError = "配置字段路径无效。";
          return;
        }
        if (!setStructuredPath(config, path, value)) configError = "配置字段路径无效。";
      };
      wizard.querySelectorAll("[data-extension-config-object-required=\"true\"]").forEach((group) => {
        assign(group.dataset.extensionConfigObjectPath, {});
      });
      wizard.querySelectorAll("[data-extension-config-path]").forEach((control) => {
        const raw = readValue(control);
        const required = control.dataset.extensionConfigRequired === "true";
        const hasDefault = control.dataset.extensionConfigHasDefault === "true";
        if (required || hasDefault || (Array.isArray(raw) ? raw.length : raw !== "")) {
          assign(control.dataset.extensionConfigPath, raw);
        }
      });
      return {config, error: configError};
    };

    const buildBindings = (type) => {
      const result = {};
      wizard.querySelectorAll(`[data-extension-binding-type="${type}"]`).forEach((control) => {
        const value = String(control.value || "").trim();
        if (value && SAFE_BINDING_ID.test(value)) result[control.dataset.extensionBindingRole] = value;
      });
      return result;
    };

    const buildSchedule = () => {
      const kindControl = wizard.querySelector("[data-extension-schedule-kind]");
      const kind = kindControl instanceof HTMLSelectElement ? kindControl.value : "none";
      const times = kind === "daily_times"
        ? Array.from(wizard.querySelectorAll("[data-extension-schedule-time]"))
          .map((control) => String(control.value || "").trim())
          .filter((value) => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value))
        : [];
      return {kind, times: [...new Set(times)].slice(0, MAX_SCHEDULE_TIMES), enabled: kind !== "none"};
    };

    const buildIntent = (builtConfig = buildConfig()) => ({
      instance_name: String(nameInput?.value || "").trim(),
      config: builtConfig.config,
      account_bindings: buildBindings("account"),
      resource_bindings: buildBindings("resource"),
      enabled_entrypoints: Array.from(wizard.querySelectorAll("[data-extension-entrypoint]:checked"))
        .map((control) => safeEntryPoint(control.value)).filter(Boolean),
      schedule: buildSchedule(),
      permissions_confirmed: wizard.querySelector("[data-extension-permissions-confirmed]")?.checked === true,
    });

    const validateIntent = (intent, configError = "") => {
      if (!intent.instance_name || intent.instance_name.length > MAX_NAME_LENGTH) return "请填写 1 至 120 个字符的项目名称。";
      if (!state.packageFile || !fileIsZip(state.packageFile)) return "请保留已检查的 ZIP 扩展包。";
      if (!intent.permissions_confirmed) return "请先确认扩展权限。";
      if (configError) return configError;
      const requiredAccountRoleWithoutChoice = state.projection.account_roles.some(
        (role) => role.required && !role.options.some((option) => option.usable),
      );
      if (requiredAccountRoleWithoutChoice || (
        state.projection.account_roles.some((role) => role.required)
        && state.projection.account_pool_available !== true
      )) {
        return "账号列表暂时不可用，无法完成必需账号绑定。";
      }
      const requiredResourceRoleWithoutChoice = state.projection.resource_roles.some(
        (role) => role.required && !role.options.some((option) => option.usable),
      );
      if (requiredResourceRoleWithoutChoice || (
        state.projection.resource_roles.some((role) => role.required)
        && state.projection.resource_pool_available !== true
      )) {
        return "资源列表暂时不可用，无法完成必需资源绑定。";
      }
      for (const control of wizard.querySelectorAll("[data-extension-binding-required=\"true\"]")) {
        const value = String(control.value || "").trim();
        if (!value) return `请填写必需绑定：${control.dataset.extensionBindingRole || "未命名角色"}。`;
        if (!SAFE_BINDING_ID.test(value)) return "账号或资源绑定 ID 格式无效。";
      }
      for (const control of wizard.querySelectorAll("[data-extension-binding-required=\"false\"]")) {
        const value = String(control.value || "").trim();
        if (value && !SAFE_BINDING_ID.test(value)) return "账号或资源绑定 ID 格式无效。";
      }
      for (const control of wizard.querySelectorAll("[data-extension-config-path]")) {
        if (typeof control.checkValidity === "function" && !control.checkValidity()) {
          return control.validationMessage || "配置字段格式无效。";
        }
      }
      for (const control of wizard.querySelectorAll("[data-extension-config-required=\"true\"]")) {
        if (control instanceof HTMLInputElement && control.type === "checkbox") continue;
        if (!String(control.value || "").trim()) return "请填写所有必填配置。";
      }
      if (intent.schedule.kind === "daily_times" && !intent.schedule.times.length) return "每天指定时间至少需要一个有效时间。";
      return "";
    };

    const sendFinal = async () => {
      if (state.sending || !state.frozen) return;
      state.sending = true;
      if (finalButton instanceof HTMLButtonElement) {
        finalButton.disabled = true;
        finalButton.textContent = "提交中…";
        finalButton.setAttribute("aria-busy", "true");
      }
      try {
        const body = new FormData();
        body.set("package", state.frozen.packageFile);
        body.set("request_id", state.frozen.requestId);
        body.set("intent", state.frozen.serializedIntent);
        const response = await fetch(finalForm.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "X-Browser-Request-UUID": state.frozen.requestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body,
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok || payload?.ok !== true) throw new Error(responseMessage(payload, "安装失败，请重试相同请求。"));
        setFeedback(finalFeedback, "扩展安装请求已提交。", "success");
        if (finalButton instanceof HTMLButtonElement) finalButton.textContent = "已提交";
        window.location.assign("/extensions");
      } catch (error) {
        setFeedback(finalFeedback, error instanceof Error ? error.message : "安装失败，请重试相同请求。", "error");
        if (finalButton instanceof HTMLButtonElement) {
          finalButton.disabled = false;
          finalButton.textContent = "重试相同安装";
          finalButton.removeAttribute("aria-busy");
        }
      } finally {
        state.sending = false;
      }
    };

    inspectForm.addEventListener("input", (event) => {
      if (state.finalSent) return;
      if (event.target === fileInput) resetInspection();
      setFeedback(inspectFeedback);
    });
    fileInput?.addEventListener("change", () => {
      if (state.finalSent) return;
      state.packageFile = fileInput.files?.[0] || null;
      resetInspection();
      state.packageFile = fileInput.files?.[0] || null;
      if (!state.packageFile) return;
      if (fileName instanceof HTMLElement) fileName.textContent = state.packageFile.name;
      if (!fileIsZip(state.packageFile)) {
        setFeedback(inspectFeedback, "请选择一个 ZIP 扩展包。", "error");
        return;
      }
      window.requestAnimationFrame(() => inspectForm.requestSubmit());
    });

    if (dropzone instanceof HTMLElement && fileInput instanceof HTMLInputElement) {
      ["dragenter", "dragover"].forEach((eventName) => {
        dropzone.addEventListener(eventName, (event) => {
          event.preventDefault();
          if (!state.finalSent) dropzone.classList.add("is-dragging");
        });
      });
      ["dragleave", "drop"].forEach((eventName) => {
        dropzone.addEventListener(eventName, () => dropzone.classList.remove("is-dragging"));
      });
      dropzone.addEventListener("drop", (event) => {
        event.preventDefault();
        if (state.finalSent || !event.dataTransfer?.files?.length) return;
        const transfer = new DataTransfer();
        transfer.items.add(event.dataTransfer.files[0]);
        fileInput.files = transfer.files;
        fileInput.dispatchEvent(new Event("change", {bubbles: true}));
      });
    }

    inspectForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.finalSent) return;
      const file = fileInput?.files?.[0];
      if (!fileIsZip(file)) {
        setFeedback(inspectFeedback, "请选择一个 ZIP 扩展包。", "error");
        return;
      }
      state.packageFile = file;
      if (!state.requestId) state.requestId = secureRequestId();
      if (!state.requestId) {
        setFeedback(inspectFeedback, "浏览器未提供安全请求标识，未提交检查。", "error");
        return;
      }
      const inspectedFile = file;
      const inspectedRequestId = state.requestId;
      const body = new FormData();
      body.set("package", file);
      body.set("request_id", inspectedRequestId);
      if (inspectButton instanceof HTMLButtonElement) {
        inspectButton.disabled = true;
        inspectButton.textContent = "检查中…";
        inspectButton.setAttribute("aria-busy", "true");
      }
      setFeedback(inspectFeedback, "正在检查 ZIP，未安装项目。", "warning");
      try {
        const response = await fetch(inspectForm.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "X-Browser-Request-UUID": inspectedRequestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body,
        });
        const payload = await response.json().catch(() => null);
        if (state.packageFile !== inspectedFile || state.requestId !== inspectedRequestId) return;
        if (!response.ok || payload?.ok !== true) throw new Error(responseMessage(payload, "ZIP 检查失败，请重试。"));
        const projection = safeProjection(payload.data);
        if (!projection) throw new Error("Agent 返回的检查投影无效，未进入安装流程。");
        state.projection = projection;
        renderInspection(projection);
        setFeedback(inspectFeedback, "ZIP 检查完成，请继续确认。", "success");
      } catch (error) {
        if (state.packageFile !== inspectedFile || state.requestId !== inspectedRequestId) return;
        state.projection = null;
        if (inspection instanceof HTMLElement) inspection.hidden = true;
        setFeedback(inspectFeedback, error instanceof Error ? error.message : "ZIP 检查失败，请重试。", "error");
      } finally {
        if (inspectButton instanceof HTMLButtonElement) {
          inspectButton.disabled = state.finalSent;
          inspectButton.textContent = "检查 ZIP";
          inspectButton.removeAttribute("aria-busy");
        }
      }
    });

    wizard.addEventListener("input", () => {
      if (state.finalSent) return;
      if (state.projection && finalButton instanceof HTMLButtonElement) finalButton.disabled = false;
      setFeedback(finalFeedback);
    });

    finalForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (state.finalSent) {
        sendFinal();
        return;
      }
      if (!state.projection) {
        setFeedback(finalFeedback, "请先完成 ZIP 检查。", "error");
        return;
      }
      const builtConfig = buildConfig();
      const intent = buildIntent(builtConfig);
      const validationError = validateIntent(intent, builtConfig.error);
      if (validationError) {
        setFeedback(finalFeedback, validationError, "error");
        return;
      }
      let serializedIntent;
      try {
        serializedIntent = JSON.stringify(intent);
      } catch (_error) {
        setFeedback(finalFeedback, "安装意图无法序列化，未提交安装。", "error");
        return;
      }
      state.frozen = Object.freeze({
        packageFile: state.packageFile,
        requestId: state.requestId,
        serializedIntent,
      });
      state.finalSent = true;
      lockAfterFinalSend();
      sendFinal();
    });

    resetButton?.addEventListener("click", () => {
      if (state.finalSent) return;
      resetInspection();
      if (fileInput instanceof HTMLInputElement) fileInput.value = "";
      if (nameInput instanceof HTMLInputElement) nameInput.value = "";
      setFeedback(inspectFeedback);
      setFeedback(finalFeedback);
      fileInput?.focus();
    });

    document.querySelectorAll("[data-extension-open]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!dialog.open) dialog.showModal();
        window.requestAnimationFrame(() => fileInput?.focus());
      });
    });
    wizard.querySelectorAll("[data-extension-close]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!state.finalSent) dialog.close();
      });
    });
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog && !state.finalSent) dialog.close();
    });
  };

  document.querySelectorAll("[data-extension-instance]").forEach((card) => {
    const feedback = card.querySelector("[data-extension-feedback]");
    const automationId = encodeURIComponent(card.dataset.automationId || "");
    const recordVersion = Number(card.dataset.recordVersion || "0");
    const upgradeInput = card.querySelector("[data-extension-upgrade]");
    const upgradeButton = card.querySelector('[data-extension-action="upgrade"]');

    upgradeInput?.addEventListener("change", () => {
      if (upgradeButton instanceof HTMLButtonElement) delete upgradeButton.dataset.requestId;
      setFeedback(feedback);
    });

    card.querySelectorAll("[data-extension-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!(button instanceof HTMLButtonElement)) return;
        const action = button.dataset.extensionAction || "";
        if (
          !["upgrade", "enable", "disable", "uninstall"].includes(action)
          || !automationId
          || !Number.isInteger(recordVersion)
          || recordVersion < 1
        ) {
          setFeedback(feedback, "项目状态已失效，请刷新后重试。", "error");
          return;
        }
        if (
          action === "uninstall"
          && !window.confirm(
            "确认卸载此 Service v2 项目？系统会撤销项目权限并停止接收新任务，只删除本应用自有数据；外部系统中已经产生的结果无法撤销。",
          )
        ) return;

        const requestId = button.dataset.requestId || secureRequestId();
        if (!requestId) {
          setFeedback(feedback, "浏览器未提供安全请求标识，操作未提交。", "error");
          return;
        }
        button.dataset.requestId = requestId;
        let body;
        let headers;
        if (action === "upgrade") {
          const packageFile = upgradeInput?.files?.[0];
          if (!(packageFile instanceof File) || !/\.zip$/i.test(packageFile.name || "")) {
            delete button.dataset.requestId;
            setFeedback(feedback, "请先选择 ZIP 扩展包。", "error");
            return;
          }
          body = new FormData();
          body.set("package", packageFile);
          body.set("request_id", requestId);
          body.set("expected_record_version", String(recordVersion));
          headers = {
            Accept: "application/json",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          };
        } else {
          body = JSON.stringify(action === "uninstall"
            ? {
                request_id: requestId,
                expected_record_version: recordVersion,
                current_version: card.dataset.currentVersion || "",
                confirm: true,
              }
            : {request_id: requestId, expected_record_version: recordVersion});
          headers = {
            Accept: "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          };
        }

        button.disabled = true;
        try {
          const response = await fetch(`/extensions/${automationId}/${action}`, {
            method: "POST",
            credentials: "same-origin",
            headers,
            body,
          });
          const payload = await response.json().catch(() => null);
          if (!response.ok || payload?.ok !== true) {
            throw new Error(responseMessage(payload, "操作失败，请重试。"));
          }
          delete button.dataset.requestId;
          window.location.reload();
        } catch (error) {
          setFeedback(
            feedback,
            error instanceof Error ? error.message : "操作失败，请重试。",
            "error",
          );
          button.disabled = false;
        }
      });
    });
  });

  if (window.__EXTENSION_WIZARD_TEST__ === true) {
    window.__extensionWizardTest = {safeProjection, safeConfigSchema, setStructuredPath};
  }

  setupInstallWizard();
})();
