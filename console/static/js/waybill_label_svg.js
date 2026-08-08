(function (global) {
  "use strict";

  const WIDTH_MM = "74mm";
  const HEIGHT_MM = "92mm";
  const SVG_VIEW_BOX = "0 0 74 92";
  const TEMPLATE_URL = "/static/assets/waybill_label_template.svg";
  const SVG_NS = "http://www.w3.org/2000/svg";

  let templatePromise = null;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));

  const firstText = (data, names) => {
    for (const name of names) {
      const value = String(data?.[name] ?? "").trim();
      if (value) return value;
    }
    return "";
  };

  const normalizeDate = (value) => String(value ?? "").trim().replaceAll("/", "-");

  const normalizeData = (data = {}, options = {}) => {
    const blank = Boolean(options.blank);
    const field = (canonical, aliases = []) => {
      if (blank) return "";
      return firstText(data, [canonical, ...aliases]);
    };
    const remark = field("remark");
    return {
      waybillNo: field("waybillNo", ["waybill_no"]),
      date: normalizeDate(field("date", ["open_date"])),
      station: field("station", ["destination_site"]),
      service: normalizeService(field("service", ["delivery_method"])),
      recipientName: field("recipientName", ["receiver_name"]),
      recipientPhone: field("recipientPhone", ["receiver_phone"]),
      recipientAddress: field("recipientAddress", ["receiver_address"]),
      senderName: field("senderName", ["sender_name"]),
      senderPhone: field("senderPhone", ["sender_phone"]),
      senderAddress: field("senderAddress", ["sender_address"]),
      cargoName: field("cargoName", ["goods_name_lines"]),
      packageType: field("packageType", ["package_type_lines"]),
      pieces: field("pieces", ["quantity_lines"]),
      weightOrVolume: field("weightOrVolume", ["weight_volume"]),
      freight: amountWithYuan(field("freight", ["freight_fee"])),
      deliveryFee: amountWithYuan(field("deliveryFee", ["delivery_fee"])),
      paymentMethod: field("paymentMethod", ["payment_method"]),
      insuranceAmount: labelAmount("保价金额", amountWithYuan(field("insuranceAmount", ["insurance_amount"]))),
      codAmount: labelAmount("代收金额", amountWithYuan(field("codAmount", ["cod_amount"]))),
      remark: blank ? "" : (remark || "易碎物品，请轻拿轻放"),
      remarkExtra: blank ? "" : (remark || "易碎物品，请轻拿轻放"),
    };
  };

  function amountWithYuan(value) {
    const text = String(value ?? "").trim();
    if (!text) return "";
    if (/[元¥￥]/.test(text)) return text;
    return `${text}元`;
  }

  function normalizeService(value) {
    const text = String(value ?? "").trim();
    if (text === "送货") return "送货";
    if (text === "自提") return "自提";
    return "";
  }

  function labelAmount(label, value) {
    return value ? `${label}：${value}` : "";
  }

  function splitText(value, maxChars, maxLines) {
    const text = String(value ?? "").trim();
    if (!text) return [];
    const lines = [];
    for (let start = 0; start < text.length && lines.length < maxLines; start += maxChars) {
      lines.push(text.slice(start, start + maxChars));
    }
    return lines;
  }

  async function loadTemplateSource() {
    if (!templatePromise) {
      templatePromise = fetch(TEMPLATE_URL, { cache: "no-cache" }).then((response) => {
        if (!response.ok) throw new Error(`Waybill label template load failed: ${response.status}`);
        return response.text();
      });
    }
    return templatePromise;
  }

  function setField(doc, name, value) {
    const node = doc.querySelector(`[data-field="${name}"]`);
    if (!node) return;
    const text = String(value ?? "").trim();
    node.textContent = "";
    const baseSize = node.getAttribute("font-size");
    const longSize = node.dataset.longSize;
    if (baseSize) node.setAttribute("font-size", baseSize);
    if (longSize && text.length >= 8) node.setAttribute("font-size", longSize);

    const maxLines = Number(node.dataset.lines || 1);
    if (maxLines > 1) {
      const maxChars = Number(node.dataset.maxChars || 18);
      const lineHeight = Number(node.dataset.lineHeight || 3);
      const x = node.getAttribute("x") || "0";
      const y = Number(node.getAttribute("y") || 0);
      splitText(text, maxChars, maxLines).forEach((line, index) => {
        const tspan = doc.createElementNS(SVG_NS, "tspan");
        tspan.setAttribute("x", x);
        tspan.setAttribute("y", String(+(y + index * lineHeight).toFixed(2)));
        tspan.textContent = line;
        node.appendChild(tspan);
      });
      return;
    }
    node.textContent = text;
  }

  async function buildWaybillLabelSvg(data = {}, options = {}) {
    const source = await loadTemplateSource();
    const doc = new DOMParser().parseFromString(source, "image/svg+xml");
    const parserError = doc.querySelector("parsererror");
    if (parserError) throw new Error("Waybill label template XML is invalid");
    const normalized = normalizeData(data, options);
    Object.entries(normalized).forEach(([name, value]) => setField(doc, name, value));
    const svg = doc.documentElement;
    svg.setAttribute("width", WIDTH_MM);
    svg.setAttribute("height", HEIGHT_MM);
    svg.setAttribute("viewBox", SVG_VIEW_BOX);
    return new XMLSerializer().serializeToString(svg);
  }

  const toBase64 = (text) => {
    const bytes = new TextEncoder().encode(text);
    let binary = "";
    bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
    return btoa(binary);
  };

  async function buildWaybillLabelSvgDataUri(data = {}, options = {}) {
    return `data:image/svg+xml;base64,${toBase64(await buildWaybillLabelSvg(data, options))}`;
  }

  async function buildWaybillLabelPrintHtml(data = {}, options = {}) {
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <style>
    @page { size: 74mm 92mm; margin: 0; }
    html, body { margin: 0; width: 74mm; height: 92mm; overflow: hidden; background: #fff; }
    .label { width: 74mm; height: 92mm; position: relative; box-sizing: border-box; }
  </style>
</head>
<body><div class="label">${await buildWaybillLabelSvg(data, options)}</div></body>
</html>`;
  }

  async function renderWaybillLabelPreview(target, data = {}, options = {}) {
    const element = typeof target === "string" ? document.querySelector(target) : target;
    if (element) element.innerHTML = await buildWaybillLabelSvg(data, options);
  }

  global.WaybillLabelSvg = {
    width: WIDTH_MM,
    height: HEIGHT_MM,
    viewBox: SVG_VIEW_BOX,
    templateUrl: TEMPLATE_URL,
    normalizeData,
    buildSvg: buildWaybillLabelSvg,
    buildDataUri: buildWaybillLabelSvgDataUri,
    buildPrintHtml: buildWaybillLabelPrintHtml,
    renderPreview: renderWaybillLabelPreview,
  };
  global.buildWaybillLabelSvg = buildWaybillLabelSvg;
})(window);
