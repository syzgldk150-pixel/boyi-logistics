(function (global) {
  "use strict";

  const WIDTH_MM = "74mm";
  const HEIGHT_MM = "92mm";
  const PAGE_NAME = "74mm×92mm 博益物流主单";
  const BACKGROUND_URL = "/static/assets/waybill_label_background.png";
  const DEFAULT_REMARK = "易碎物品，请轻拿轻放";
  const CHINESE_FONT = "'Source Han Sans SC', '思源黑体', 'Noto Sans SC', 'Microsoft YaHei', SimHei, sans-serif";
  const NUMBER_FONT = "Arial, sans-serif";

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));

  const cleanText = (value) => String(value ?? "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/[ \t]+/g, " ")
    .trim();

  const firstText = (data, names) => {
    for (const name of names) {
      const value = cleanText(data?.[name]);
      if (value) return value;
    }
    return "";
  };

  const normalizeDate = (value) => cleanText(value).replaceAll("-", "/");

  const normalizeMoney = (value, fallback = "") => {
    const raw = cleanText(value).replace(/[元块]/g, "");
    if (!raw) return fallback;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return raw;
    if (parsed === 0) return "0.00";
    if (Number.isInteger(parsed)) return String(parsed);
    return parsed.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  };

  const normalizeService = (value) => {
    const text = cleanText(value);
    if (text === "送货") return "送货";
    if (text === "自提") return "自提";
    return text;
  };

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
      recipientName: field("recipientName", ["receiver_name"]),
      recipientPhone: field("recipientPhone", ["receiver_phone"]),
      recipientAddress: field("recipientAddress", ["receiver_address"]),
      senderName: field("senderName", ["sender_name"]),
      senderPhone: field("senderPhone", ["sender_phone"]),
      senderAddress: field("senderAddress", ["sender_address"]),
      cargoName: field("cargoName", ["cargo_name", "goods_name", "goods_name_lines"]),
      packageType: field("packageType", ["package_type", "package_type_lines"]),
      pieces: field("pieces", ["quantity", "quantity_lines"]),
      weightOrVolume: field("weightOrVolume", ["weight_volume"]) || "-",
      freight: normalizeMoney(field("freight", ["freight_fee"])),
      insuranceFee: normalizeMoney(field("insuranceFee", ["insurance_fee", "insurance_amount"]), "0.00"),
      packageFee: normalizeMoney(field("packageFee", ["package_fee", "packing_fee", "pickup_fee"]), "0.00"),
      otherFee: normalizeMoney(field("otherFee", ["other_fee", "transfer_fee"]), "0.00"),
      transportMethod: normalizeService(field("transportMethod", ["delivery_method", "service", "service_type"])),
      paymentMethod: field("paymentMethod", ["payment_method"]),
      deliveryFee: normalizeMoney(field("deliveryFee", ["delivery_fee"]), "0.00"),
      returnFee: normalizeMoney(field("returnFee", ["return_fee", "receipt_fee", "cod_amount"]), "0.00"),
      remark: blank || remark === DEFAULT_REMARK ? "" : remark,
    };
  };

  const clampNumber = (value, fallback, min, max) => {
    const number = Number.parseFloat(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.max(min, Math.min(number, max));
  };

  const readSettings = (settings = {}) => ({
    orientation: String(settings.print_orientation || "1") === "2" ? 2 : 1,
    offsetX: clampNumber(settings.print_offset_x, 0, -20, 20),
    offsetY: clampNumber(settings.print_offset_y, 0, -20, 20),
    fontScale: clampNumber(settings.print_font_scale, 100, 85, 115) / 100,
    templateScale: clampNumber(settings.print_template_scale, 100, 94, 106) / 100,
  });

  const stripZeros = (number) => Number(number).toFixed(3).replace(/\.?0+$/, "");
  const pxToMm = (value) => Number(value) / 8;
  const mm = (value) => pxToMm(value);
  const placedMm = (value, offset, settings) => `${stripZeros(Number(offset || 0) + Number(value) * settings.templateScale)}mm`;
  const sizedMm = (value, settings) => `${stripZeros(Number(value) * settings.templateScale)}mm`;
  const scaledPx = (value, settings) => `${stripZeros(pxToMm(Number(value)) * settings.fontScale * settings.templateScale)}mm`;
  const htmlLines = (value) => escapeHtml(value).replace(/\n/g, "<br>");

  const formatPhone = (value) => cleanText(value).replace(/\s+/g, "");

  const splitText = (value, maxChars, maxLines) => {
    const text = cleanText(value);
    if (!text) return [];
    const lines = [];
    const paragraphs = text.split("\n").map((line) => line.trim()).filter(Boolean);
    for (const paragraph of paragraphs) {
      for (let start = 0; start < paragraph.length && lines.length < maxLines; start += maxChars) {
        lines.push(paragraph.slice(start, start + maxChars));
      }
      if (lines.length >= maxLines) break;
    }
    return lines;
  };

  const FIELD_LAYOUT = [
    { field: "waybillNo", x: mm(35), y: mm(150), w: mm(128), h: mm(22), px: 21, weight: 900, maxChars: 18, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "date", x: mm(235), y: mm(150), w: mm(110), h: mm(22), px: 20, weight: 900, maxChars: 10, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "station", x: mm(455), y: mm(148), w: mm(95), h: mm(24), px: 21, longPx: 18, weight: 900, maxChars: 8, align: "center", noWrap: true },
    { field: "senderName", x: mm(171), y: mm(197), w: mm(97), h: mm(18), px: 17, longPx: 15, weight: 800, maxChars: 8, noWrap: true },
    { field: "senderPhone", x: mm(354), y: mm(197), w: mm(163), h: mm(18), px: 17, longPx: 15, weight: 800, maxChars: 13, noWrap: true, font: NUMBER_FONT },
    { field: "senderAddress", x: mm(171), y: mm(241), w: mm(306), h: mm(20), px: 17, longPx: 15, weight: 800, lines: 1, maxChars: 22 },
    { field: "recipientName", x: mm(171), y: mm(289), w: mm(97), h: mm(18), px: 17, longPx: 15, weight: 800, maxChars: 8, noWrap: true },
    { field: "recipientPhone", x: mm(354), y: mm(289), w: mm(163), h: mm(18), px: 17, longPx: 15, weight: 800, maxChars: 13, noWrap: true, font: NUMBER_FONT },
    { field: "recipientAddress", x: mm(171), y: mm(331), w: mm(356), h: mm(20), px: 17, longPx: 15, weight: 800, lines: 1, maxChars: 25 },
    { field: "cargoName", x: mm(40), y: mm(398), w: mm(115), h: mm(26), px: 19, weight: 800, maxChars: 8, align: "center", vCenter: true, noWrap: true },
    { field: "packageType", x: mm(160), y: mm(398), w: mm(135), h: mm(26), px: 19, weight: 800, maxChars: 8, align: "center", vCenter: true, noWrap: true },
    { field: "pieces", x: mm(310), y: mm(398), w: mm(110), h: mm(26), px: 19, weight: 800, maxChars: 7, align: "center", vCenter: true, noWrap: true },
    { field: "weightOrVolume", x: mm(425), y: mm(398), w: mm(145), h: mm(26), px: 18, weight: 800, maxChars: 12, align: "center", vCenter: true, noWrap: true, font: NUMBER_FONT },
    { field: "transportMethod", x: mm(122), y: mm(447), w: mm(80), h: mm(24), px: 19, weight: 900, maxChars: 5, align: "center", vCenter: true, noWrap: true },
    { field: "paymentMethod", x: mm(390), y: mm(447), w: mm(70), h: mm(24), px: 19, weight: 900, maxChars: 5, align: "center", vCenter: true, noWrap: true },
    { field: "freight", x: mm(60), y: mm(508), w: mm(80), h: mm(20), px: 18, weight: 900, maxChars: 8, align: "center", vCenter: true, noWrap: true, font: NUMBER_FONT },
    { field: "packageFee", x: mm(205), y: mm(508), w: mm(80), h: mm(20), px: 18, weight: 900, maxChars: 8, align: "center", vCenter: true, noWrap: true, font: NUMBER_FONT },
    { field: "deliveryFee", x: mm(350), y: mm(508), w: mm(80), h: mm(20), px: 18, weight: 900, maxChars: 8, align: "center", vCenter: true, noWrap: true, font: NUMBER_FONT },
    { field: "otherFee", x: mm(485), y: mm(508), w: mm(80), h: mm(20), px: 18, weight: 900, maxChars: 8, align: "center", vCenter: true, noWrap: true, font: NUMBER_FONT },
    { field: "insuranceFee", x: mm(120), y: mm(565), w: mm(120), h: mm(22), px: 18, weight: 900, maxChars: 9, align: "center", vCenter: true, noWrap: true, font: NUMBER_FONT },
    { field: "returnFee", x: mm(390), y: mm(565), w: mm(120), h: mm(22), px: 18, weight: 900, maxChars: 9, align: "center", vCenter: true, noWrap: true, font: NUMBER_FONT },
    { field: "remark", x: mm(66), y: mm(612), w: mm(458), h: mm(28), px: 16, weight: 700, lines: 2, lineHeightPx: 19, maxChars: 27 },
  ];

  function fieldTextValue(data, field) {
    const value = cleanText(data[field]);
    if (field === "recipientPhone" || field === "senderPhone") {
      return formatPhone(value);
    }
    return value;
  }

  function buildField(item, data, settings) {
    const value = fieldTextValue(data, item.field);
    if (!value) return "";
    const textLength = cleanText(value).replace(/\n/g, "").length;
    const px = textLength >= 8 ? (item.longPx || item.px) : item.px;
    const maxLines = item.lines || 1;
    const maxChars = item.maxChars || 18;
    const content = maxLines > 1
      ? splitText(value, maxChars, maxLines).join("\n")
      : cleanText(value).replace(/\n/g, "").slice(0, maxChars);
    if (!content) return "";
    const lineHeightPx = item.lineHeightPx || Math.round(px * 1.12);
    const fontFamily = item.font || CHINESE_FONT;
    const wrapStyle = item.noWrap ? "white-space:nowrap;word-break:keep-all;" : "";
    const alignStyle = item.align ? `text-align:${item.align};` : "";
    const centerStyle = item.vCenter ? "display:grid;place-items:center;line-height:1;" : "";
    return `<div class="ys-field ys-field-${escapeHtml(item.field)}" data-field="${escapeHtml(item.field)}" style="left:${placedMm(item.x, settings.offsetX, settings)};top:${placedMm(item.y, settings.offsetY, settings)};width:${sizedMm(item.w, settings)};height:${sizedMm(item.h, settings)};font-size:${scaledPx(px, settings)};line-height:${scaledPx(lineHeightPx, settings)};font-weight:${item.weight};font-family:${fontFamily};${alignStyle}${wrapStyle}${centerStyle}">${htmlLines(content)}</div>`;
  }

  function buildDynamicFields(data, settings) {
    return FIELD_LAYOUT.map((item) => buildField(item, data, settings)).join("");
  }

  const labelCss = () => `<style>
.ys-waybill-label, .ys-waybill-label * { box-sizing: border-box; }
.ys-waybill-label {
  width: 74mm;
  height: 92mm;
  position: relative;
  overflow: hidden;
  background: #fff;
  color: #000;
  font-family: ${CHINESE_FONT};
  line-height: 1.05;
}
.ys-waybill-background {
  position: absolute;
  display: block;
  user-select: none;
  pointer-events: none;
  image-rendering: crisp-edges;
}
.ys-field {
  position: absolute;
  overflow: hidden;
  white-space: pre-line;
  word-break: break-all;
  color: #000;
}
</style>`;

  async function buildHtml(data = {}, options = {}) {
    const settings = readSettings(options);
    const normalized = normalizeData(data, options);
    const background = `<img class="ys-waybill-background" src="${BACKGROUND_URL}" alt="" style="left:${placedMm(0, settings.offsetX, settings)};top:${placedMm(0, settings.offsetY, settings)};width:${sizedMm(74, settings)};height:${sizedMm(92, settings)};">`;
    return `${labelCss()}<div class="ys-waybill-label" data-waybill-background-template="true">${background}${buildDynamicFields(normalized, settings)}</div>`;
  }

  async function renderPreview(target, data = {}, options = {}) {
    const element = typeof target === "string" ? document.querySelector(target) : target;
    if (!element) return;
    element.innerHTML = await buildHtml(data, options);
  }

  global.WaybillLabelHtml = {
    width: WIDTH_MM,
    height: HEIGHT_MM,
    pageName: PAGE_NAME,
    backgroundUrl: BACKGROUND_URL,
    normalizeData,
    buildHtml,
    renderPreview,
  };
})(window);
