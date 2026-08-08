(function () {
  const ACTIONS = {
    bootstrap: "/ocr/yunda/bootstrap",
    getNumber: "/ocr/yunda/get-logistics-num",
    addressAnalysis: "/ocr/yunda/address-analysis",
    addressResolution: "/ocr/yunda/address-resolution",
    quoteChecks: "/ocr/yunda/quote-checks",
    feedbackAddress: "/ocr/yunda/feedback/address",
    feedbackCost: "/ocr/yunda/feedback/cost",
    feedbackCostUpload: "/ocr/yunda/feedback/cost/upload",
    returnUpload: "/ocr/yunda/return-upload",
    downloadTemplate: "/ocr/yunda/download-template",
    save: "/ocr/yunda/save",
    draftSave: "/ocr/yunda/drafts/save",
    draftList: "/ocr/yunda/drafts/list",
    draftLoad: "/ocr/yunda/drafts/load",
    draftDelete: "/ocr/yunda/drafts/delete",
    templateSave: "/ocr/yunda/templates/save",
    templateList: "/ocr/yunda/templates/list",
    templateLoad: "/ocr/yunda/templates/load",
    templateDelete: "/ocr/yunda/templates/delete",
    templateDefault: "/ocr/yunda/templates/set-default",
    printChild: "/ocr/yunda/print/child",
    printMaster: "/ocr/yunda/print/master",
    printTriplicate: "/ocr/yunda/print/triplicate",
    printReceipt: "/ocr/yunda/print/receipt-label",
  };

  const YUNDA_UI_SCHEMA = {
    top: [
      "OpenDate",
      "LogisticsId",
      "ProductType",
      "IsInternational",
      "PackageByCode",
    ],
    sender: [
      "SenderName",
      "CreatedDotname",
      "SenderCompany",
      "SenderAddress",
      "SenderMobile",
      "SenderPhone",
      "SenderDistributionName",
      "CrmCustomerId",
      "IdNumber",
      "DeliversSms",
    ],
    receiver: [
      "BuyerName",
      "BuyerCompany",
      "BuyerProvince",
      "BuyerCity",
      "BuyerArea",
      "BuyerTown",
      "BuyerAddress",
      "BuyerDestinationDotName",
      "BuyerDestinationDistributionName",
      "DispatchRemark",
      "BuyerMobile",
      "BuyerPhone",
      "BuyerSms",
    ],
    cargo: [
      "VolDetail",
      "ItemName",
      "ItemTotalNumber",
      "PackingType1",
      "PackingType2",
      "PackingType3",
      "PackingType4",
      "Piece",
      "GoodsType",
      "GrossWeight",
      "Tfr",
      "Volume",
      "Del",
      "InGoodsTypeText",
    ],
    service: ["Unpacking", "ServiceMode", "DispatchMode"],
    cost: [
      "TransferCost",
      "DotSendCost",
      "AddedServiceCost",
      "DotOperateCost",
      "PlatformCost",
      "MDFY",
      "DotFixedCost",
      "OtherCost",
      "Total",
    ],
    fee: ["PaymentType", "Freight", "InsuredAmount", "InsuredAmountMoney", "OtherMoney", "TotalMoney", "Remarks"],
  };

  const FIELD_LABELS = {
    OpenDate: "寄件日期",
    LogisticsId: "运单号",
    ProductType: "产品类型",
    IsInternational: "国际件",
    PackageByCode: "收件业务员",
    SenderName: "寄件人",
    CreatedDotname: "寄件网点",
    SenderCompany: "寄件公司",
    SenderAddress: "寄件地址",
    SenderMobile: "寄件手机",
    SenderPhone: "座机号码",
    SenderDistributionName: "首发分拨",
    CrmCustomerId: "客户名称",
    IdNumber: "寄件身份证",
    DeliversSms: "签收短信",
    BuyerName: "收件人",
    BuyerCompany: "收件公司",
    BuyerProvince: "省",
    BuyerCity: "市",
    BuyerArea: "区",
    BuyerTown: "乡镇",
    BuyerAddress: "收方地址",
    BuyerDestinationDotName: "目的网点",
    BuyerDestinationDistributionName: "目的分拨",
    DispatchRemark: "派件说明",
    BuyerMobile: "收件手机",
    BuyerPhone: "座机号码",
    BuyerSms: "派件短信",
    VolDetail: "(长*宽*高*件数)",
    ItemName: "物品名称",
    ItemTotalNumber: "总件数",
    PackingType1: "包装类型",
    PackingType2: "包装类型",
    PackingType3: "包装类型",
    PackingType4: "包装类型",
    Piece: "件数",
    Piece1: "件数",
    Piece2: "件数",
    Piece3: "件数",
    GoodsType: "货物类型",
    GrossWeight: "实际重量",
    Tfr: "结算重量",
    Volume: "体积(M³)",
    Del: "派费重量",
    InGoodsTypeText: "物品品类",
    ReturnLogisticsId: "回单号",
    PaymentType: "支付类型",
    Freight: "运费",
    InsuredAmount: "物品申明价值",
    InsuredAmountMoney: "服务保障费",
    OtherMoney: "其他费用",
    TotalMoney: "总金额",
    Remarks: "备注",
    PictureUrl1: "图片",
    PictureUrl2: "同行图片",
    ReturnAdjunct: "电子回单附件",
    ReturnAdjunctAddr: "电子回单附件地址",
    ReturnAdjunctArr: "电子回单附件列表",
    Unpacking: "拆包服务",
    ServiceMode: "服务方式",
    DispatchMode: "送货方式",
    TransferCost: "中转费",
    DotSendCost: "派送费",
    AddedServiceCost: "增值服务费",
    DotOperateCost: "操作费",
    PlatformCost: "平台费",
    MDFY: "末端费用",
    DotFixedCost: "特惠一口价",
    OtherCost: "其他",
    Total: "总计",
    LimitWeigh: "限制重量",
    LimitWeighTime: "限重时间段",
    pssx: "派送时效",
    manager_name: "经理姓名",
    manager_employee_name: "经理电话",
    cxdh: "客服电话",
    qry_phone: "查件电话",
    sale_phone: "业务电话",
    Site_Address: "网点地址",
  };

  const FIELD_ALIASES = {
    OpenDate: ["OpenDate", "current_time", "start"],
    ProductType: ["ProductType", "ProductType_", "ProductTypeText"],
    IsInternational: ["IsInternational", "IsInternational_", "International"],
    PackageByCode: ["PackageByCode", "PackageByCodeText", "BusinessUser", "ReceiverStaff"],
    SenderName: ["SenderName", "SenderMan", "Sender"],
    SenderMobile: ["SenderMobile", "SenderMoblie", "SenderTelephone"],
    SenderDistributionName: ["SenderDistributionName", "SubLogisticsName", "SubLogistics"],
    CrmCustomerId: ["CrmCustomerId", "CustomerName", "CustomerId"],
    BuyerName: ["BuyerName", "ActualBuyerName"],
    BuyerMobile: ["BuyerMobile", "BuyerMoblie", "ActualBuyerMobile"],
    BuyerProvince: ["BuyerProvince", "ActualBuyerProvince"],
    BuyerCity: ["BuyerCity", "ActualBuyerCity"],
    BuyerArea: ["BuyerArea", "BuyerCounty", "ActualBuyerTown"],
    BuyerTown: ["BuyerTown", "ActualBuyerVillage"],
    BuyerDestinationDotName: ["BuyerDestinationDotName", "DestinationDotName", "BuyerDestinationDotNamefeedback"],
    BuyerDestinationDistributionName: ["BuyerDestinationDistributionName", "DestinationSubLogisticsName"],
    DispatchRemark: ["DispatchRemark", "DeliveryRemark", "BuyerDestinationDotNamefeedback"],
    GoodsType: ["GoodsType", "InGoodsType", "InGoodsTypeText"],
    ServiceMode: ["ServiceMode", "ServiceType", "ServiceType_"],
    DispatchMode: ["DispatchMode", "ShippingMethods", "DispatchType"],
    PaymentType: ["PaymentType", "SettlementType", "SettlementType_"],
    TransferCost: ["TransferCost", "DotTransferCost"],
    AddedServiceCost: ["AddedServiceCost", "ValueAddedCost", "33"],
    PlatformCost: ["PlatformCost", "5"],
    OtherCost: ["OtherCost", "333"],
  };

  const REQUIRED_FIELDS = new Set([
    "LogisticsId",
    "SenderName",
    "CreatedDotname",
    "SenderAddress",
    "SenderMobile",
    "SenderDistributionName",
    "BuyerName",
    "BuyerAddress",
    "BuyerDestinationDotName",
    "BuyerDestinationDistributionName",
    "DispatchRemark",
    "BuyerMobile",
    "ItemName",
    "ItemTotalNumber",
    "PackingType1",
    "Piece",
    "GoodsType",
    "GrossWeight",
    "Tfr",
    "Volume",
  ]);

  const QUOTE_FIELDS = new Set([
    "ItemTotalNumber",
    "PackingType1",
    "PackingType2",
    "PackingType3",
    "PackingType4",
    "Piece",
    "Piece1",
    "Piece2",
    "Piece3",
    "GoodsType",
    "GrossWeight",
    "Volume",
    "ServiceMode",
    "DispatchMode",
    "PaymentType",
    "Freight",
    "InsuredAmount",
    "InsuredAmountMoney",
    "OtherMoney",
  ]);

  const ADDRESS_FIELDS = new Set([
    "BuyerProvince",
    "BuyerCity",
    "BuyerArea",
    "BuyerTown",
    "BuyerAddress",
  ]);

  const state = {
    bootstrapped: false,
    bootstrapPending: null,
    pageUrl: "",
    defaultForm: {},
    form: {},
    hiddenFields: {},
    fieldMap: {},
    uiOptions: {},
    authState: null,
    lastMessage: "",
    lastSavedWaybillNo: "",
    localWaybillId: null,
    printUrl: "",
    printEnabled: false,
    drafts: [],
    templates: [],
    selectedDraftId: "",
    selectedTemplateId: "",
    modal: null,
    panels: {
      addressAnalysis: null,
      addressResolution: null,
      checks: null,
      price: null,
      route: null,
      destination: null,
      contacts: null,
      children: [],
      print: null,
      returnUploads: [],
      feedbackCostUpload: {},
    },
  };

  let root;
  let sideRoot;
  let statusChip;
  let debounceTimer = null;

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function cleanText(value) {
    return String(value ?? "").trim();
  }

  function firstStockText(value, depth = 0) {
    if (value === null || value === undefined || depth > 5) return "";
    if (typeof value !== "object") return cleanText(value);
    if (Array.isArray(value)) {
      for (const item of value) {
        const text = firstStockText(item, depth + 1);
        if (text) return text;
      }
      return "";
    }

    const preferredKeys = [
      "electronic_stock",
      "elecStock",
      "remain_num_elec",
      "remainNumElec",
      "available_num_elec",
      "availableNumElec",
      "balance",
      "available",
      "availableNum",
      "usable",
      "remain",
      "remaining",
      "surplus",
      "stock",
      "num",
      "count",
      "quantity",
      "qty",
      "total",
      "data",
      "result",
    ];
    for (const key of preferredKeys) {
      if (Object.prototype.hasOwnProperty.call(value, key)) {
        const text = firstStockText(value[key], depth + 1);
        if (text) return text;
      }
    }

    const ignoredKeys = new Set(["ok", "code", "status", "success", "message", "msg", "error", "info"]);
    for (const [key, nested] of Object.entries(value)) {
      if (ignoredKeys.has(key)) continue;
      const text = firstStockText(nested, depth + 1);
      if (text) return text;
    }
    return "";
  }

  function formatElectronicStock(value) {
    return firstStockText(value) || "-";
  }

  function formatNow() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }

  function fallbackDefaults() {
    return {
      OpenDate: formatNow(),
      current_time: formatNow(),
    };
  }

  function aliasesFor(name) {
    return [name, ...(FIELD_ALIASES[name] || [])].filter((item, index, list) => item && list.indexOf(item) === index);
  }

  function labelFor(name) {
    return FIELD_LABELS[name] || name;
  }

  function valueOf(name, fallback = "") {
    for (const key of aliasesFor(name)) {
      if (Object.prototype.hasOwnProperty.call(state.form, key)) {
        const value = state.form[key];
        if (value !== undefined && value !== null && String(value) !== "") return String(value);
      }
      if (Object.prototype.hasOwnProperty.call(state.hiddenFields, key)) {
        const value = state.hiddenFields[key];
        if (value !== undefined && value !== null && String(value) !== "") return String(value);
      }
    }
    return fallback;
  }

  function setValue(name, value) {
    const text = typeof value === "boolean" ? (value ? "1" : "0") : String(value ?? "");
    aliasesFor(name).forEach((key) => {
      state.form[key] = text;
    });
  }

  function optionKeyMatches(field, alias) {
    if (!field || typeof field !== "object") return false;
    return field.name === alias || field.id === alias || field.label === alias;
  }

  function normalizeOptions(options) {
    if (!Array.isArray(options)) return [];
    return options
      .map((option) => ({
        value: cleanText(option?.value ?? option?.id ?? option?.code ?? option?.text ?? ""),
        text: cleanText(option?.text ?? option?.name ?? option?.label ?? option?.value ?? ""),
      }))
      .filter((option, index, list) => option.value || option.text)
      .filter((option, index, list) => list.findIndex((item) => item.value === option.value && item.text === option.text) === index);
  }

  function optionsFor(name) {
    const options = [];
    for (const alias of aliasesFor(name)) {
      if (Array.isArray(state.uiOptions[alias])) options.push(...normalizeOptions(state.uiOptions[alias]));
      const direct = state.fieldMap[alias];
      if (Array.isArray(direct?.options)) options.push(...normalizeOptions(direct.options));
      Object.values(state.fieldMap || {}).forEach((field) => {
        if (optionKeyMatches(field, alias) && Array.isArray(field.options)) {
          options.push(...normalizeOptions(field.options));
        }
      });
    }
    const current = valueOf(name);
    if (current && !options.some((option) => option.value === current || option.text === current)) {
      options.unshift({ value: current, text: current });
    }
    return options.filter((option, index, list) => list.findIndex((item) => item.value === option.value && item.text === option.text) === index);
  }

  function isAuthBlocked() {
    const code = cleanText(state.authState?.code);
    return code === "AUTH_REQUIRED" || code === "AUTH_PENDING_CODE";
  }

  function disabledForAction(actionName, extraDisabled = false) {
    if (extraDisabled) return "disabled";
    if (actionName === "reset" || actionName === "close-modal") return "";
    return isAuthBlocked() ? "disabled" : "";
  }

  function statusText() {
    const code = cleanText(state.authState?.code);
    if (code === "AUTH_REQUIRED") return "需要登录";
    if (code === "AUTH_PENDING_CODE") return "等待验证码";
    if (state.lastSavedWaybillNo) return `已保存 ${state.lastSavedWaybillNo}`;
    if (state.bootstrapped) return "登录态可用";
    return "初始化中";
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
    return response.json().catch(() => ({
      ok: false,
      message: `Request failed: ${response.status}`,
      data: {},
      field_errors: {},
      auth_state: null,
    }));
  }

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || "");
        resolve(text.includes(",") ? text.split(",", 2)[1] : text);
      };
      reader.onerror = () => reject(reader.error || new Error("文件读取失败。"));
      reader.readAsDataURL(file);
    });
  }

  function payloadBody(extra = {}) {
    return {
      form: {
        ...state.hiddenFields,
        ...state.form,
        ...(extra.form || {}),
      },
      context: {
        page_url: state.pageUrl,
        ...(extra.context || {}),
      },
      client_meta: {
        action: cleanText(extra.action || ""),
        selectedDraftId: state.selectedDraftId,
        selectedTemplateId: state.selectedTemplateId,
        localWaybillId: state.localWaybillId,
        printUrl: state.printUrl,
      },
    };
  }

  function scalarPatchFromObject(node, patch = {}, depth = 0) {
    if (!node || depth > 4) return patch;
    if (typeof node === "string") {
      const text = cleanText(node);
      if (!text) return patch;
      if (/^[\[{]/.test(text)) {
        try {
          return scalarPatchFromObject(JSON.parse(text), patch, depth + 1);
        } catch (_) {
          return patch;
        }
      }
      return patch;
    }
    if (Array.isArray(node)) {
      node.slice(0, 20).forEach((item) => scalarPatchFromObject(item, patch, depth + 1));
      return patch;
    }
    if (typeof node !== "object") return patch;
    Object.entries(node).forEach(([rawKey, rawValue]) => {
      const key = cleanText(rawKey);
      if (!key) return;
      const value = rawValue == null ? "" : rawValue;
      const isScalar = typeof value !== "object";
      const canonical = Object.keys(FIELD_LABELS).find((name) => aliasesFor(name).includes(key)) || key;
      if (isScalar && (FIELD_LABELS[canonical] || FIELD_LABELS[key] || Object.prototype.hasOwnProperty.call(state.form, key))) {
        patch[canonical] = typeof value === "boolean" ? (value ? "1" : "0") : String(value);
      }
      if (!isScalar) scalarPatchFromObject(value, patch, depth + 1);
    });
    return patch;
  }

  function patchLogisticsFromResult(action, data, patch) {
    if (!/get-number|get-logistics-num/.test(action)) return patch;
    const candidates = [
      data?.waybill_no,
      data?.logistics,
      data?.logistics_no,
      data?.logisticsNo,
      data?.LogisticsId,
      data?.logisticsId,
      data?.result?.LogisticsId,
      data?.result?.logistics,
      data?.result?.logisticsId,
      data?.result?.logistics_no,
      data?.result?.logisticsNo,
      data?.result?.mailno,
      data?.result?.MailNo,
      data?.result?.data?.LogisticsId,
      data?.result?.data?.logistics,
      data?.result?.data?.logisticsId,
      data?.result?.data?.logistics_no,
      data?.result?.data?.logisticsNo,
      data?.result?.data?.mailno,
      data?.result?.data?.MailNo,
    ];
    if (typeof data?.result === "string") candidates.push(data.result);
    if (typeof data?.result?.data === "string") candidates.push(data.result.data);
    const waybillNo = candidates.map(cleanText).find((value) => /^[A-Za-z0-9-]{6,}$/.test(value));
    if (waybillNo) patch.LogisticsId = waybillNo;
    return patch;
  }

  function applyFormPatch(patch) {
    Object.entries(patch || {}).forEach(([key, value]) => {
      if (!cleanText(key)) return;
      const canonical = Object.keys(FIELD_LABELS).find((name) => aliasesFor(name).includes(key)) || key;
      setValue(canonical, value);
    });
  }

  function absorbActionPayload(payload, explicitAction = "") {
    const data = payload && typeof payload === "object" ? (payload.data || {}) : {};
    const action = cleanText(payload?.action || explicitAction);
    state.authState = payload?.auth_state || state.authState;
    state.lastMessage = cleanText(payload?.message || state.lastMessage);
    state.localWaybillId = data.local_waybill_id || state.localWaybillId;
    state.printEnabled = Boolean(data.print_enabled || state.printEnabled || valueOf("LogisticsId"));
    state.printUrl = cleanText(data.preview_url || data.print_url || state.printUrl);
    state.lastSavedWaybillNo = cleanText(data.waybill_no || valueOf("LogisticsId") || state.lastSavedWaybillNo);

    const patch = {};
    scalarPatchFromObject(data.normalized_form, patch);
    scalarPatchFromObject(data.item, patch);
    scalarPatchFromObject(data.patch_form, patch);
    scalarPatchFromObject(data.result, patch);
    patchLogisticsFromResult(action, data, patch);
    applyFormPatch(patch);

    if (Array.isArray(data.items) && /draft/.test(action)) state.drafts = data.items;
    if (Array.isArray(data.items) && /template/.test(action)) state.templates = data.items;
    if (data.panels && typeof data.panels === "object") {
      state.panels = { ...state.panels, ...data.panels };
    }
    if (data.result && /address-analysis/.test(action)) state.panels.addressAnalysis = data.result;
    if (data.result && /address-resolution/.test(action)) state.panels.addressResolution = data.result;
    if (data.checks) state.panels.checks = data.checks;
    if (data.price) state.panels.price = data.price;
    if (data.print) state.panels.print = data.print;
  }

  function inputAttrs(name, extra = {}) {
    const required = extra.required || REQUIRED_FIELDS.has(name);
    const readonly = extra.readonly ? "readonly" : "";
    const disabled = extra.disabled ? "disabled" : "";
    return {
      labelClass: `yd-label${required ? " required" : ""}`,
      readonly,
      disabled,
    };
  }

  function renderInput(name, extra = {}) {
    const attrs = inputAttrs(name, extra);
    const type = extra.type || "text";
    const value = valueOf(name, extra.value || "");
    const input = `
      <input
        type="${escapeHtml(type)}"
        class="yd-input${extra.readonly ? " yd-readonly" : ""}"
        value="${escapeHtml(value)}"
        data-yunda-field="${escapeHtml(name)}"
        placeholder="${escapeHtml(extra.placeholder || "")}"
        ${attrs.readonly}
        ${attrs.disabled}
      >
    `;
    if (extra.bare) return input;
    return `
      <label class="${attrs.labelClass}">${escapeHtml(extra.label || labelFor(name))}</label>
      ${input}
    `;
  }

  function renderTextArea(name, extra = {}) {
    const attrs = inputAttrs(name, extra);
    return `
      <label class="${attrs.labelClass}">${escapeHtml(extra.label || labelFor(name))}</label>
      <textarea
        class="yd-textarea${extra.readonly ? " yd-readonly" : ""}"
        data-yunda-field="${escapeHtml(name)}"
        placeholder="${escapeHtml(extra.placeholder || "")}"
        ${attrs.readonly}
        ${attrs.disabled}
      >${escapeHtml(valueOf(name, extra.value || ""))}</textarea>
    `;
  }

  function renderSelect(name, extra = {}) {
    const attrs = inputAttrs(name, extra);
    const options = optionsFor(name);
    const current = valueOf(name, extra.value || "");
    if (options.length <= 1 && extra.allowInput !== false) {
      return renderInput(name, extra);
    }
    const optionHtml = options.length
      ? options.map((option) => {
          const optionValue = option.value || option.text;
          const selected = String(optionValue) === String(current) || String(option.text) === String(current) ? "selected" : "";
          return `<option value="${escapeHtml(optionValue)}" ${selected}>${escapeHtml(option.text || optionValue)}</option>`;
        }).join("")
      : `<option value="${escapeHtml(current)}">${escapeHtml(current || "请选择")}</option>`;
    const select = `
      <select class="yd-select" data-yunda-field="${escapeHtml(name)}" ${attrs.disabled}>
        ${optionHtml}
      </select>
    `;
    if (extra.bare) return select;
    return `
      <label class="${attrs.labelClass}">${escapeHtml(extra.label || labelFor(name))}</label>
      ${select}
    `;
  }

  function renderCheck(name, label, extra = {}) {
    const checked = String(valueOf(name, extra.checked ? "1" : "")) === "1";
    return `
      <label class="yd-check">
        <input type="checkbox" data-yunda-field="${escapeHtml(name)}" ${checked ? "checked" : ""} ${extra.disabled ? "disabled" : ""}>
        <span>${escapeHtml(label || labelFor(name))}</span>
      </label>
    `;
  }

  function renderReadonlyInput(name, extra = {}) {
    return `
      <label class="yd-label">${escapeHtml(extra.label || labelFor(name))}</label>
      <input type="text" class="yd-input yd-readonly" value="${escapeHtml(valueOf(name, extra.value || ""))}" readonly>
    `;
  }

  function renderToolbar() {
    return `
      <div class="yunda-original-toolbar">
        <button type="button" class="yd-btn" data-yunda-action="reset">新增</button>
        <button type="button" class="yd-btn" data-yunda-action="save" ${disabledForAction("save")}>保存</button>
        <button type="button" class="yd-btn" data-yunda-action="print-child" ${disabledForAction("print-child", !state.printEnabled)}>打印子单</button>
        <button type="button" class="yd-btn" data-yunda-action="print-receipt" ${disabledForAction("print-receipt", !state.printEnabled)}>打印回单标签</button>
        <button type="button" class="yd-btn yd-btn-blue" data-yunda-action="download-template" ${disabledForAction("download-template")}>下载</button>
        <button type="button" class="yd-btn yd-btn-red" data-yunda-action="draft-save" ${disabledForAction("draft-save")}>保存草稿</button>
        <button type="button" class="yd-btn yd-btn-orange" data-yunda-action="draft-list" ${disabledForAction("draft-list")}>打开草稿箱</button>
        <button type="button" class="yd-btn yd-btn-green" data-yunda-action="template-save" ${disabledForAction("template-save")}>保存模板</button>
        <button type="button" class="yd-btn yd-btn-green" data-yunda-action="template-list" ${disabledForAction("template-list")}>打开模板</button>
        <label class="yd-toolbar-check">
          <input type="checkbox" data-yunda-field="PopupMap">
          <span>是否弹出地图</span>
        </label>
        <span class="yd-status-mini">${escapeHtml(statusText())}</span>
      </div>
    `;
  }

  function renderTopCard() {
    const balance = formatElectronicStock(state.panels?.electronicStock);
    return `
      <div class="yd-top-card">
        <div class="yd-top-row">
          <div class="yd-field yd-field-wide">${renderInput("OpenDate", { type: "text" })}</div>
          <div class="yd-field yd-field-medium">${renderCheck("UseOriginalPrice", "使用原价", { checked: true })}</div>
        </div>
        <div class="yd-top-row">
          <div class="yd-field yd-field-wide">
            <label class="yd-label required">${escapeHtml(labelFor("LogisticsId"))}</label>
            <input type="text" class="yd-input" value="${escapeHtml(valueOf("LogisticsId"))}" data-yunda-field="LogisticsId">
          </div>
          <div class="yd-field yd-field-medium">
            <button type="button" class="yd-btn yd-btn-blue" data-yunda-action="get-number" ${disabledForAction("get-number")}>获取电子单号</button>
          </div>
          <div class="yd-field yd-field-medium"><span class="yd-label">电子余量：</span><span class="yd-hint">${escapeHtml(balance)}</span></div>
          <div class="yd-field yd-field-medium">${renderSelect("ProductType", { allowInput: true })}</div>
          <div class="yd-field yd-field-medium">${renderSelect("IsInternational", { allowInput: true })}</div>
          <div class="yd-field yd-field-wide">${renderInput("PackageByCode")}</div>
        </div>
      </div>
    `;
  }

  function renderSenderPanel() {
    return `
      <section class="yd-panel">
        <div class="yd-panel-title">寄方</div>
        <div class="yd-panel-body">
          <div class="yd-line">${renderInput("SenderName")}${renderReadonlyInput("CreatedDotname")}</div>
          <div class="yd-line-single">${renderInput("SenderCompany")}</div>
          <div class="yd-inline-action">${renderInput("SenderAddress")}<button type="button" class="yd-btn yd-btn-red" data-yunda-action="address-analysis" ${disabledForAction("address-analysis")}>解析</button></div>
          <div class="yd-line">${renderInput("SenderMobile")}${renderInput("SenderPhone")}</div>
          <div class="yd-line">${renderSelect("SenderDistributionName", { allowInput: true })}${renderInput("CrmCustomerId")}</div>
          <div class="yd-line-single">${renderInput("IdNumber")}</div>
          <div class="yd-check-row">
            ${renderCheck("SenderSignSms", "签收短信(寄件人)")}
            ${renderCheck("DeliversSms", "签收短信(收件人)")}
            <span class="yd-hint">手机、座机二选一必填</span>
          </div>
        </div>
      </section>
    `;
  }

  function renderReceiverPanel() {
    return `
      <section class="yd-panel">
        <div class="yd-panel-title">收方</div>
        <div class="yd-panel-body">
          <div class="yd-line">${renderInput("BuyerName")}${renderInput("BuyerCompany")}</div>
          <div class="yd-address-row">
            <label class="yd-label required">收方地址</label>
            ${renderSelect("BuyerProvince", { bare: true, allowInput: true })}
            ${renderSelect("BuyerCity", { bare: true, allowInput: true })}
            ${renderSelect("BuyerArea", { bare: true, allowInput: true })}
            ${renderSelect("BuyerTown", { bare: true, allowInput: true })}
          </div>
          <div class="yd-address-action">
            ${renderInput("BuyerAddress", { placeholder: "输入完整地址后回车即可自动匹配省市区与网点" })}
            <button type="button" class="yd-btn yd-btn-blue" data-yunda-action="address-resolution" ${disabledForAction("address-resolution")}>地址匹配</button>
            <button type="button" class="yd-btn yd-btn-red" data-yunda-action="address-analysis" ${disabledForAction("address-analysis")}>解析</button>
          </div>
          <div class="yd-line">${renderInput("BuyerDestinationDotName", { readonly: true })}${renderInput("BuyerDestinationDistributionName", { readonly: true })}</div>
          <div class="yd-line-single">${renderInput("DispatchRemark")}</div>
          <div class="yd-line">${renderInput("BuyerMobile")}${renderInput("BuyerPhone")}</div>
          <div class="yd-check-row">
            ${renderCheck("SenderSms", "寄件短信")}
            ${renderCheck("BuyerSms", "派件短信", { checked: true })}
            <button type="button" class="yd-btn" data-yunda-action="feedback-address" ${disabledForAction("feedback-address")}>GIS错误反馈</button>
          </div>
        </div>
      </section>
    `;
  }

  function renderCargoPanel() {
    const packRows = [
      ["PackingType1", "Piece"],
      ["PackingType2", "Piece1"],
      ["PackingType3", "Piece2"],
      ["PackingType4", "Piece3"],
    ];
    return `
      <section class="yd-panel">
        <div class="yd-panel-title yd-panel-title-blue">
          货物信息
          ${renderCheck("YunZhunDa", "韵准达")}
          ${renderCheck("HeavyCargo", "韵重货")}
          ${renderCheck("FixedPrice", "一口价")}
          ${renderCheck("YunAnDa", "韵安达")}
          ${renderCheck("YunAnDaMedicine", "韵安达药品")}
        </div>
        <div class="yd-panel-body">
          <div class="yd-cargo-grid">
            <div class="yd-volume-box">
              <div class="yd-check-row">
                <span class="yd-label">${escapeHtml(labelFor("VolDetail"))}</span>
                <button type="button" class="yd-btn yd-btn-blue" data-yunda-action="quote-checks" ${disabledForAction("quote-checks")}>计算</button>
              </div>
              <textarea class="yd-textarea" data-yunda-field="VolDetail" placeholder="请输入内容">${escapeHtml(valueOf("VolDetail"))}</textarea>
            </div>
            <div class="yd-pack-lines">
              <div class="yd-line">${renderInput("ItemName")}${renderInput("ItemTotalNumber", { readonly: true, value: valueOf("ItemTotalNumber", "0") })}</div>
              ${packRows.map(([packing, piece]) => `
                <div class="yd-pack-line">
                  <label class="yd-label required">包装类型</label>
                  ${renderSelect(packing, { bare: true, allowInput: true })}
                  <label class="yd-label required">件数</label>
                  <input type="number" class="yd-input" value="${escapeHtml(valueOf(piece, piece === "Piece" ? "" : "0"))}" data-yunda-field="${escapeHtml(piece)}" placeholder="0">
                </div>
              `).join("")}
              <div class="yd-line">${renderSelect("GoodsType", { allowInput: true })}${renderInput("GrossWeight", { type: "number", placeholder: "0.00" })}</div>
              <div class="yd-line">${renderInput("Tfr", { readonly: true, value: valueOf("Tfr", "0.00") })}${renderInput("Volume", { type: "number", placeholder: "0.0000" })}</div>
              <div class="yd-line">${renderInput("Del", { readonly: true, value: valueOf("Del", "0.00") })}${renderInput("InGoodsTypeText")}</div>
            </div>
          </div>
          <div class="yd-check-row">
            ${renderCheck("DeliversReturn", "签收回单")}
            <label class="yd-label">回单类型</label>
            ${renderSelect("ReturnType", { label: "", allowInput: true })}
          </div>
          <div class="yd-check-row">
            <span class="yd-label">签单要求</span>
            ${renderCheck("SignDate", "签名/日期")}
            ${renderCheck("SignId", "身份证号")}
            ${renderCheck("SignStamp", "盖章")}
            ${renderCheck("SignOther", "其他")}
            <input type="text" class="yd-input yd-readonly" value="${escapeHtml(valueOf("SignRemark"))}" readonly style="max-width:170px;">
          </div>
          <div class="yd-check-row">
            <label class="yd-btn yd-btn-blue">
              电子回单上传
              <input type="file" data-yunda-upload="return-upload" style="display:none;">
            </label>
            <span class="yd-hint">${escapeHtml(valueOf("ReturnAdjunct") || valueOf("ReturnAdjunctAddr") || "未上传")}</span>
          </div>
        </div>
      </section>
    `;
  }

  function renderServiceAndCost() {
    return `
      <section class="yd-panel">
        <div class="yd-panel-title">服务信息 ${renderCheck("Unpacking", "拆包服务")}</div>
        <div class="yd-panel-body">
          <div class="yd-line">${renderSelect("ServiceMode", { allowInput: true })}${renderSelect("DispatchMode", { allowInput: true })}</div>
        </div>
      </section>
      <section class="yd-panel">
        <div class="yd-panel-title">代收货款 ${renderCheck("CollectionMoneyEnabled", "")}</div>
      </section>
      <section class="yd-panel">
        <div class="yd-panel-title">成本信息</div>
        <div class="yd-panel-body">
          <div class="yd-cost-grid">
            ${YUNDA_UI_SCHEMA.cost.map((name) => `
              <label class="yd-label">${escapeHtml(labelFor(name))}</label>
              <div style="display:grid;grid-template-columns:minmax(0,1fr) 22px;gap:4px;align-items:center;">
                <input type="number" class="yd-input yd-readonly" value="${escapeHtml(valueOf(name, "0.00"))}" readonly>
                <span class="yd-mini-search">⌕</span>
              </div>
            `).join("")}
          </div>
          <div class="yd-check-row" style="justify-content:flex-end;">
            <label class="yd-btn">
              上传图片
              <input type="file" accept="image/*" data-yunda-upload="feedback-cost-1" style="display:none;">
            </label>
            <span class="yd-hint">${escapeHtml(valueOf("PictureUrl1") ? "图片已上传" : "图片未上传")}</span>
            <label class="yd-btn">
              上传同行图片
              <input type="file" accept="image/*" data-yunda-upload="feedback-cost-2" style="display:none;">
            </label>
            <span class="yd-hint">${escapeHtml(valueOf("PictureUrl2") ? "同行图片已上传" : "同行图片未上传")}</span>
            <button type="button" class="yd-btn yd-btn-red" data-yunda-action="feedback-cost" ${disabledForAction("feedback-cost")}>超高派费反馈</button>
          </div>
        </div>
      </section>
    `;
  }

  function renderFeePanel() {
    return `
      <section class="yd-panel">
        <div class="yd-panel-title">收费信息</div>
        <div class="yd-panel-body">
          <div class="yd-line-three">
            ${renderSelect("PaymentType", { allowInput: true })}
            ${renderInput("Freight", { type: "number", value: valueOf("Freight", "0.00") })}
            ${renderInput("InsuredAmount", { type: "number", value: valueOf("InsuredAmount", "0.00") })}
          </div>
          <div class="yd-line-three">
            ${renderInput("InsuredAmountMoney", { type: "number", value: valueOf("InsuredAmountMoney", "0.00") })}
            ${renderInput("OtherMoney", { type: "number", value: valueOf("OtherMoney", "0.00") })}
            ${renderInput("TotalMoney", { type: "number", value: valueOf("TotalMoney", "0.00") })}
          </div>
          <div class="yd-line-single">${renderTextArea("Remarks", { label: "备注" })}</div>
        </div>
      </section>
    `;
  }

  function panelValue(panelName, keys, fallback = "") {
    const panel = state.panels[panelName];
    const source = panel && typeof panel === "object" ? panel : {};
    for (const key of keys) {
      const value = source[key];
      if (value !== undefined && value !== null && cleanText(value)) return cleanText(value);
    }
    return fallback;
  }

  function renderRoutePanel() {
    const route = state.panels.route || state.panels.addressResolution || {};
    const routeName = panelValue("route", ["route", "route_name", "RouteName"], valueOf("RouteName", "站"));
    const signTime = cleanText(route.sign_time || route.estimated_sign_time || route.EstimatedSignTime || "");
    const totalTime = cleanText(route.total_time || route.operation_total_time || route.TotalTime || "");
    return `
      <div class="yd-route">
        <div class="yd-route-title">● 路由：</div>
        <div class="yd-route-line"><span>运单预计时效</span><strong>${escapeHtml(routeName || "站")}</strong></div>
        <div class="yd-route-line"><span>途径：</span><span>${escapeHtml(routeName || "站")}</span></div>
        <div class="yd-route-line"><span>预计签收时间：</span><span>${escapeHtml(signTime || "-")}</span></div>
        <div class="yd-route-line"><span>预计作业总时长：</span><span>${escapeHtml(totalTime || "-")}</span></div>
      </div>
    `;
  }

  function renderRightTabs() {
    const destination = state.panels.destination || state.panels.addressResolution || {};
    const special = state.panels.checks || {};
    const children = Array.isArray(state.panels.children) ? state.panels.children : [];
    const dotName = cleanText(destination.dot_name || destination.destination_dot || valueOf("BuyerDestinationDotName"));
    const dotAddress = cleanText(destination.dot_address || destination.destination_address || "");
    const town = cleanText(destination.town || valueOf("BuyerTown"));
    const distribution = cleanText(destination.distribution || valueOf("BuyerDestinationDistributionName"));
    return `
      <div class="yd-tabs">
          <div class="yd-tab-head">
            <button type="button" aria-selected="true">目的网点</button>
            <button type="button" aria-selected="false">子单号</button>
          </div>
        <div class="yd-tab-body">
          <table class="yd-table">
            <tbody>
              <tr><th colspan="2">特殊范围</th></tr>
              <tr><td>特殊区域加收<br><span class="yd-red">（请填写体积重量后查看）</span></td><td>备注</td></tr>
              <tr><td>${escapeHtml(cleanText(special.special_range || special.specialRange || ""))}</td><td>${escapeHtml(cleanText(special.remark || ""))}</td></tr>
              <tr><td>特殊区域地址</td><td>备注</td></tr>
              <tr><td>${escapeHtml(cleanText(special.address || ""))}</td><td>${escapeHtml(cleanText(special.address_remark || ""))}</td></tr>
            </tbody>
          </table>
          <table class="yd-table">
            <thead>
              <tr><th>目的网点</th><th>目的网点-收件地址</th><th>收件地址-所属乡镇</th><th>目的网点-目的分拨</th></tr>
            </thead>
            <tbody>
              <tr><td>${escapeHtml(dotName)}</td><td>${escapeHtml(dotAddress)}</td><td>${escapeHtml(town)}</td><td>${escapeHtml(distribution)}</td></tr>
            </tbody>
          </table>
          <table class="yd-table">
            <thead>
              <tr><th>子单号</th><th>目的网点</th><th>备注</th></tr>
            </thead>
            <tbody>
              ${children.length ? children.map((item) => `
                <tr>
                  <td>${escapeHtml(cleanText(item.waybill_no || item.LogisticsId || item.mailno || ""))}</td>
                  <td>${escapeHtml(cleanText(item.destination || item.dotName || item.siteName || ""))}</td>
                  <td>${escapeHtml(cleanText(item.remark || item.remarks || item.memo || ""))}</td>
                </tr>
              `).join("") : '<tr><td colspan="3">暂无子单号</td></tr>'}
            </tbody>
          </table>
          <div class="yd-contact">
            <div class="yd-contact-row">${renderReadonlyInput("pssx", { label: "派送时效" })}</div>
            <div class="yd-contact-row">${renderReadonlyInput("LimitWeigh", { label: "限制重量" })}</div>
            <div class="yd-contact-row">${renderReadonlyInput("LimitWeighTime", { label: "限重时间段" })}</div>
            <h4>网点联系方式</h4>
            <div class="yd-contact-row">${renderReadonlyInput("manager_name")}</div>
            <div class="yd-contact-row">${renderReadonlyInput("manager_employee_name")}</div>
            <div class="yd-contact-row">${renderReadonlyInput("cxdh")}</div>
            <div class="yd-contact-row">${renderReadonlyInput("qry_phone")}</div>
            <div class="yd-contact-row">${renderReadonlyInput("sale_phone")}</div>
            <div class="yd-contact-row">${renderReadonlyInput("Site_Address")}</div>
          </div>
        </div>
      </div>
    `;
  }

  function renderRightColumn() {
    return `
      <div class="yd-column">
        <div class="yd-right-map">
          <div>
            <div class="yd-map-pin">⌖</div>
            <div>${valueOf("BuyerAddress") ? "等待地图/网点信息回填" : "暂无地图信息"}</div>
          </div>
        </div>
        ${renderRoutePanel()}
        ${renderRightTabs()}
      </div>
    `;
  }

  function itemIdOf(item) {
    if (!item || typeof item !== "object") return "";
    return cleanText(item.id || item.Id || item.draft_id || item.template_id || item.ModuleId || item.moduleId);
  }

  function itemTitleOf(item, fallbackPrefix) {
    if (!item || typeof item !== "object") return fallbackPrefix;
    return cleanText(
      item.name ||
      item.Name ||
      item.TemplateName ||
      item.templateName ||
      item.DraftName ||
      item.draftName ||
      `${fallbackPrefix} ${itemIdOf(item) || ""}`
    ) || fallbackPrefix;
  }

  function itemSubtitleOf(item) {
    if (!item || typeof item !== "object") return "";
    return cleanText(item.LogisticsId || item.logisticsId || item.OpenDate || item.openDate || item.updated_at || item.created_at);
  }

  function renderModal() {
    if (!state.modal) return "";
    const isTemplate = state.modal.kind === "template";
    const title = isTemplate ? "打开模板" : "打开草稿箱";
    const items = isTemplate ? state.templates : state.drafts;
    const prefix = isTemplate ? "模板" : "草稿";
    return `
      <div class="yd-modal-backdrop" data-yunda-modal>
        <div class="yd-modal">
          <div class="yd-modal-head">
            <h3>${escapeHtml(title)}</h3>
            <button type="button" class="yd-btn" data-yunda-action="close-modal">关闭</button>
          </div>
          <div class="yd-modal-body">
            ${items.length ? items.map((item) => `
              <div class="yd-list-item">
                <div>
                  <strong>${escapeHtml(itemTitleOf(item, prefix))}</strong>
                  <span>${escapeHtml(itemSubtitleOf(item) || itemIdOf(item))}</span>
                </div>
                <div class="yd-list-actions">
                  <button type="button" class="yd-btn yd-btn-blue" data-yunda-action="${isTemplate ? "template-load" : "draft-load"}" data-item-id="${escapeHtml(itemIdOf(item))}">回填</button>
                  <button type="button" class="yd-btn" data-yunda-action="${isTemplate ? "template-delete" : "draft-delete"}" data-item-id="${escapeHtml(itemIdOf(item))}">删除</button>
                  ${isTemplate ? `<button type="button" class="yd-btn" data-yunda-action="template-default" data-item-id="${escapeHtml(itemIdOf(item))}">设为默认</button>` : ""}
                </div>
              </div>
            `).join("") : `<div class="yunda-empty">暂无${escapeHtml(prefix)}。</div>`}
          </div>
        </div>
      </div>
    `;
  }

  function renderMain() {
    if (!root) return;
    if (statusChip) statusChip.textContent = statusText();
    const alert = isAuthBlocked()
      ? `<div class="yunda-login-alert">韵达登录态不可用，远端动作已禁用。请到 <a href="/automations">自动化</a> 刷新韵达登录。</div>`
      : "";
    const notice = state.lastMessage && !isAuthBlocked()
      ? `<div class="yunda-action-notice">${escapeHtml(state.lastMessage)}</div>`
      : "";
    root.innerHTML = `
      <div class="yunda-shell">
        ${alert}
        ${notice}
        ${renderToolbar()}
        ${renderTopCard()}
        <div class="yd-main-grid">
          <div class="yd-column">
            ${renderSenderPanel()}
            ${renderCargoPanel()}
            ${renderFeePanel()}
          </div>
          <div class="yd-column">
            ${renderReceiverPanel()}
            ${renderServiceAndCost()}
          </div>
          ${renderRightColumn()}
        </div>
      </div>
      ${renderModal()}
    `;
  }

  function renderSide() {
    if (sideRoot) sideRoot.innerHTML = "";
  }

  function renderAll() {
    renderMain();
    renderSide();
  }

  function syncFieldValue(target) {
    const name = cleanText(target?.dataset?.yundaField);
    if (!name) return;
    const value = target.type === "checkbox" ? (target.checked ? "1" : "0") : target.value;
    setValue(name, value);
    if (ADDRESS_FIELDS.has(name)) {
      scheduleRemoteAction("address-resolution", 850);
    } else if (QUOTE_FIELDS.has(name)) {
      scheduleRemoteAction("quote-checks", 650);
    }
  }

  function scheduleRemoteAction(actionName, delay) {
    if (isAuthBlocked()) return;
    if (debounceTimer) window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(() => {
      runAction(actionName, { silent: true }).catch(() => {});
    }, delay);
  }

  async function bootstrap() {
    if (state.bootstrapPending) return state.bootstrapPending;
    state.bootstrapPending = (async () => {
      const payload = await postJson(ACTIONS.bootstrap, payloadBody({ action: "bootstrap" }));
      state.authState = payload.auth_state || null;
      state.lastMessage = cleanText(payload.message || "");
      const data = payload.data || {};
      state.pageUrl = cleanText(data.page_url || "");
      state.fieldMap = data.fields || {};
      state.uiOptions = data.ui_options || {};
      state.hiddenFields = data.hidden_fields || {};
      state.defaultForm = {
        ...fallbackDefaults(),
        ...(data.default_form || {}),
        ...(data.defaults || {}),
      };
      state.form = { ...state.hiddenFields, ...state.defaultForm, ...state.form };
      if (data.remote_context?.electronic_stock) state.panels.electronicStock = data.remote_context.electronic_stock;
      if (data.remote_context?.current_time) setValue("OpenDate", data.remote_context.current_time);
      state.printEnabled = Boolean(data.print_enabled || valueOf("LogisticsId"));
      state.bootstrapped = Boolean(payload.ok);
      renderAll();
    })().finally(() => {
      state.bootstrapPending = null;
    });
    return state.bootstrapPending;
  }

  const actionMap = {
    "get-number": ACTIONS.getNumber,
    "address-analysis": ACTIONS.addressAnalysis,
    "address-resolution": ACTIONS.addressResolution,
    "quote-checks": ACTIONS.quoteChecks,
    "feedback-address": ACTIONS.feedbackAddress,
    "feedback-cost": ACTIONS.feedbackCost,
    "feedback-cost-upload": ACTIONS.feedbackCostUpload,
    "return-upload": ACTIONS.returnUpload,
    "download-template": ACTIONS.downloadTemplate,
    save: ACTIONS.save,
    "draft-save": ACTIONS.draftSave,
    "draft-list": ACTIONS.draftList,
    "draft-load": ACTIONS.draftLoad,
    "draft-delete": ACTIONS.draftDelete,
    "template-save": ACTIONS.templateSave,
    "template-list": ACTIONS.templateList,
    "template-load": ACTIONS.templateLoad,
    "template-delete": ACTIONS.templateDelete,
    "template-default": ACTIONS.templateDefault,
    "print-child": ACTIONS.printChild,
    "print-master": ACTIONS.printMaster,
    "print-triplicate": ACTIONS.printTriplicate,
    "print-receipt": ACTIONS.printReceipt,
  };

  async function runAction(actionName, options = {}) {
    const url = actionMap[actionName];
    if (!url) return;
    const payload = await postJson(url, payloadBody({
      action: actionName,
      form: options.form,
      context: options.context,
    }));
    state.authState = payload.auth_state || state.authState;
    state.lastMessage = cleanText(payload.message || "");
    if (!payload.ok) {
      renderAll();
      if (!options.silent) window.alert(state.lastMessage || "韵达操作失败。");
      return;
    }
    absorbActionPayload({ ...payload, action: actionName }, actionName);
    if (actionName === "draft-list") state.modal = { kind: "draft" };
    if (actionName === "template-list") state.modal = { kind: "template" };
    if (/draft-load|template-load/.test(actionName)) state.modal = null;
    renderAll();
    if (/draft-delete|template-delete/.test(actionName)) {
      await runAction(actionName.startsWith("template") ? "template-list" : "draft-list", { silent: true });
    }
    if (actionName === "download-template") {
      const downloadUrl = cleanText(payload.data?.download_url);
      if (downloadUrl) window.open(downloadUrl, "_blank", "noopener");
    }
    if (actionName.startsWith("print-")) {
      const data = payload.data || {};
      const preview = data.panels?.print?.preview_html || data.print?.preview_html || data.preview_html;
      const urlToOpen = data.panels?.print?.remote_url || data.print?.remote_url || data.remote_url || data.panels?.print?.preview_url || data.print?.preview_url || data.preview_url || data.print_url;
      if (urlToOpen) {
        window.open(urlToOpen, "_blank", "noopener");
      } else if (preview) {
        const previewWindow = window.open("", "_blank");
        if (previewWindow) {
          previewWindow.document.open();
          previewWindow.document.write(preview);
          previewWindow.document.close();
        }
      }
    }
  }

  function resetForm() {
    state.form = { ...state.hiddenFields, ...state.defaultForm };
    state.lastSavedWaybillNo = "";
    state.localWaybillId = null;
    state.printUrl = "";
    state.printEnabled = Boolean(valueOf("LogisticsId"));
    state.panels = {
      ...state.panels,
      addressAnalysis: null,
      addressResolution: null,
      checks: null,
      price: null,
      route: null,
      destination: null,
      contacts: null,
      print: null,
      children: [],
      returnUploads: [],
      feedbackCostUpload: {},
    };
    renderAll();
  }

  async function runUploadAction(uploadKind, file) {
    if (!file) return;
    const dataBase64 = await readFileAsBase64(file);
    const context = {
      upload_file: {
        filename: file.name || "upload.bin",
        content_type: file.type || "application/octet-stream",
        data_base64: dataBase64,
      },
    };
    let actionName = "return-upload";
    if (uploadKind === "feedback-cost-1" || uploadKind === "feedback-cost-2") {
      actionName = "feedback-cost-upload";
      context.target_field = uploadKind === "feedback-cost-2" ? "PictureUrl2" : "PictureUrl1";
    }
    const payload = await postJson(actionMap[actionName], payloadBody({ action: actionName, context }));
    state.authState = payload.auth_state || state.authState;
    state.lastMessage = cleanText(payload.message || "");
    absorbActionPayload({ ...payload, action: actionName }, actionName);
    renderAll();
    if (!payload.ok) window.alert(state.lastMessage || "韵达上传失败。");
  }

  async function runFeedbackAddress() {
    const abnormalType = cleanText(window.prompt("异常类型：1=匹配网点不正确，2=匹配乡镇不正确", valueOf("AbnormalType", "1")));
    if (!abnormalType) return;
    const form = {
      BuyerAddress: valueOf("BuyerAddress"),
      BuyerProvince: valueOf("BuyerProvince"),
      BuyerCity: valueOf("BuyerCity"),
      BuyerArea: valueOf("BuyerArea"),
      MatchingBuyerAddress: valueOf("MatchingBuyerAddress") || valueOf("BuyerAddress"),
      MatchingBuyerDotCode: valueOf("MatchingBuyerDotCode") || valueOf("BuyerDestinationDotCode"),
      MatchingBuyerTownCode: valueOf("MatchingBuyerTownCode") || valueOf("BuyerTown"),
      AbnormalType: abnormalType === "2" ? "2" : "1",
      ActualBuyerDotCode: abnormalType === "1" ? cleanText(window.prompt("实际目的网点编码", valueOf("ActualBuyerDotCode"))) : valueOf("ActualBuyerDotCode"),
      ActualBuyerTown: abnormalType === "2" ? cleanText(window.prompt("实际所属乡镇", valueOf("ActualBuyerTown"))) : valueOf("ActualBuyerTown"),
      PreWay: valueOf("PreWay"),
    };
    await runAction("feedback-address", { form });
  }

  async function handleActionClick(event) {
    const button = event.target.closest("[data-yunda-action]");
    if (!button || button.disabled) return;
    const actionName = button.dataset.yundaAction;
    const itemId = cleanText(button.dataset.itemId);
    if (actionName === "close-modal") {
      state.modal = null;
      renderAll();
      return;
    }
    if (actionName === "reset") {
      resetForm();
      return;
    }
    if (actionName === "draft-save") {
      const name = window.prompt("草稿名称", "");
      await runAction(actionName, { form: name ? { DraftName: name, draftName: name, Name: name } : undefined });
      return;
    }
    if (actionName === "template-save") {
      const name = window.prompt("模板名称", "");
      await runAction(actionName, { form: name ? { TemplateName: name, templateName: name, Name: name } : undefined });
      return;
    }
    if (actionName === "feedback-address") {
      await runFeedbackAddress();
      return;
    }
    if (actionName === "feedback-cost" && (!valueOf("PictureUrl1") || !valueOf("PictureUrl2"))) {
      window.alert("请先上传超高派费反馈所需的两张图片。");
      return;
    }
    if (itemId) {
      const context = { item_id: itemId };
      const form = { item_id: itemId };
      if (actionName === "draft-load") state.selectedDraftId = itemId;
      if (actionName === "template-load" || actionName === "template-default") state.selectedTemplateId = itemId;
      await runAction(actionName, { context, form });
      return;
    }
    await runAction(actionName);
  }

  function attachEvents() {
    if (!root || root.dataset.yundaBound === "1") return;
    root.dataset.yundaBound = "1";
    root.addEventListener("input", (event) => syncFieldValue(event.target));
    root.addEventListener("change", (event) => {
      const uploadKind = cleanText(event.target?.dataset?.yundaUpload);
      if (uploadKind) {
        const file = event.target.files && event.target.files[0];
        runUploadAction(uploadKind, file).finally(() => {
          event.target.value = "";
        }).catch((error) => {
          state.lastMessage = cleanText(error?.message || error);
          renderAll();
        });
        return;
      }
      syncFieldValue(event.target);
    });
    root.addEventListener("click", (event) => {
      handleActionClick(event).catch((error) => {
        state.lastMessage = cleanText(error?.message || error);
        renderAll();
      });
    });
  }

  function initRonghuiLiveInstance(ronghuiRoot) {
    const ronghuiStatusChip = ronghuiRoot.querySelector("[data-ronghui-status-chip]") || document.querySelector("[data-ronghui-status-chip]");
    const ronghuiFrame = ronghuiRoot.querySelector("[data-ronghui-live-frame]");
    const ronghuiFallback = ronghuiRoot.querySelector("[data-ronghui-live-fallback]");
    if (!ronghuiRoot || ronghuiRoot.dataset.ronghuiLive !== "1") return;
    if (ronghuiStatusChip) ronghuiStatusChip.textContent = "原页模式";
    if (!ronghuiFrame || !ronghuiFallback || ronghuiRoot.dataset.ronghuiLiveBound === "1") return;

    ronghuiRoot.dataset.ronghuiLiveBound = "1";
    const sessionUrl = cleanText(ronghuiRoot.dataset.ronghuiSessionUrl || "/automations");
    const showAuthFallback = () => {
      if (ronghuiStatusChip) ronghuiStatusChip.textContent = "需要登录";
      ronghuiFallback.hidden = false;
      ronghuiFallback.innerHTML = `融辉登录态不可用，请到 <a href="${escapeHtml(sessionUrl)}">自动化登录态</a> 刷新后重试。`;
    };
    const hideFallback = () => {
      if (ronghuiStatusChip) ronghuiStatusChip.textContent = "原页模式";
      ronghuiFallback.hidden = true;
    };
    const inspectFrame = () => {
      let text = "";
      try {
        text = cleanText(ronghuiFrame.contentDocument?.body?.innerText || "");
      } catch (_) {
        return;
      }
      if (!text) return;
      if (/AUTH_REQUIRED|AUTH_PENDING_CODE|当前未登录|登录态已失效|登录态已过期|缺少登录配置|验证码/.test(text)) {
        showAuthFallback();
        return;
      }
      hideFallback();
    };

    ronghuiFrame.addEventListener("load", inspectFrame);
    window.setTimeout(inspectFrame, 700);
  }

  function initAllRonghuiLiveInstances() {
    document.querySelectorAll("[data-ronghui-root]").forEach((ronghuiRoot) => {
      initRonghuiLiveInstance(ronghuiRoot);
    });
  }

  function initYundaLiveInstance(yundaRoot) {
    if (!yundaRoot || yundaRoot.dataset.yundaLive !== "1") return;
    const yundaStatusChip = yundaRoot.querySelector("[data-yunda-status-chip]") || document.querySelector("[data-yunda-status-chip]");
    const yundaFrame = yundaRoot.querySelector("[data-yunda-live-frame]");
    const yundaFallback = yundaRoot.querySelector("[data-yunda-live-fallback]");
    if (yundaStatusChip) yundaStatusChip.textContent = "原页模式";
    if (!yundaFrame || !yundaFallback || yundaRoot.dataset.yundaLiveBound === "1") return;
    yundaRoot.dataset.yundaLiveBound = "1";
    const sessionUrl = cleanText(yundaRoot.dataset.yundaSessionUrl || "/automations");
    const showAuthFallback = () => {
      if (yundaStatusChip) yundaStatusChip.textContent = "需要登录";
      yundaFallback.hidden = false;
      yundaFallback.innerHTML = `韵达登录态不可用，请到 <a href="${escapeHtml(sessionUrl)}">自动化登录态</a> 刷新后重试。`;
    };
    const hideFallback = () => {
      if (yundaStatusChip) yundaStatusChip.textContent = "原页模式";
      yundaFallback.hidden = true;
    };
    const inspectFrame = () => {
      let text = "";
      try {
        text = cleanText(yundaFrame.contentDocument?.body?.innerText || "");
      } catch (_) {
        return;
      }
      if (!text) return;
      if (/AUTH_REQUIRED|AUTH_PENDING_CODE|当前未登录|登录态已失效|登录态已过期|缺少登录配置|验证码|账号密码登录/.test(text)) {
        showAuthFallback();
        return;
      }
      hideFallback();
    };
    yundaFrame.addEventListener("load", inspectFrame);
    window.setTimeout(inspectFrame, 700);
  }

  function initAllYundaLiveInstances() {
    document.querySelectorAll("[data-yunda-root]").forEach((yundaRoot) => {
      initYundaLiveInstance(yundaRoot);
    });
  }

  function initIfNeeded(mode) {
    initAllRonghuiLiveInstances();
    initAllYundaLiveInstances();
    root = document.querySelector('[data-yunda-root]:not([data-yunda-live="1"])') || document.querySelector("[data-yunda-root]");
    sideRoot = document.querySelector("[data-yunda-side-root]");
    statusChip = document.querySelector("[data-yunda-status-chip]");
    if (!root || !sideRoot) return;
    if (root.dataset.yundaLive === "1") {
      if (statusChip) statusChip.textContent = "原页模式";
      return;
    }
    attachEvents();
    if (mode === "yunda" && !state.bootstrapped) {
      bootstrap().catch((error) => {
        state.lastMessage = cleanText(error?.message || error);
        renderAll();
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const modeRoot = document.querySelector("[data-mode-root]");
    initIfNeeded(cleanText(modeRoot?.dataset?.activeMode || "manual"));
  });

  window.addEventListener("console:ocr-mode-change", (event) => {
    initIfNeeded(cleanText(event?.detail?.mode || "manual"));
  });
})();
