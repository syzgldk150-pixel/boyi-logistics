(function (global) {
  "use strict";

  const WIDTH_MM = "74mm";
  const HEIGHT_MM = "92mm";
  const PAGE_NAME = "74mm×92mm 博益物流主单";
  const BACKGROUND_URL = "/static/assets/waybill_label_background.png";
  const DEFAULT_REMARK = "易碎物品，请轻拿轻放";
  const CHINESE_FONT = "思源黑体";
  const CHINESE_FALLBACK_FONT = "SimHei";
  const NUMBER_FONT = "Arial";
  const BLACK = "#000000";

  let backgroundDataUriPromise = null;

  const clampNumber = (value, fallback, min, max) => {
    const number = Number.parseFloat(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.max(min, Math.min(number, max));
  };

  const stripZeros = (number) => Number(number).toFixed(3).replace(/\.?0+$/, "");
  const pxToMm = (value) => Number(value) / 8;
  const mmValue = (value) => pxToMm(value);
  const mm = (value, offset = 0, scale = 1) => `${stripZeros(Number(offset || 0) + Number(value) * Number(scale || 1))}mm`;

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

  const fallbackNormalizeData = (data = {}, options = {}) => {
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

  const normalizeWaybillData = (data, options) => {
    const normalizer = global.WaybillLabelHtml?.normalizeData
      || global.WaybillLabelSvg?.normalizeData
      || fallbackNormalizeData;
    return normalizer(data, options);
  };

  const splitText = (value, maxChars, maxLines) => {
    const source = cleanText(value);
    if (!source) return "";
    const chunks = [];
    const paragraphs = source.split("\n").map((line) => line.trim()).filter(Boolean);
    for (const paragraph of paragraphs) {
      for (let start = 0; start < paragraph.length && chunks.length < maxLines; start += maxChars) {
        chunks.push(paragraph.slice(start, start + maxChars));
      }
      if (chunks.length >= maxLines) break;
    }
    return chunks.join("\n");
  };

  const truncateText = (value, maxChars) => {
    const text = cleanText(value);
    if (!maxChars || text.length <= maxChars) return text;
    return text.slice(0, maxChars);
  };

  const readSettings = (settings = {}) => ({
    orientation: String(settings.print_orientation || "1") === "2" ? 2 : 1,
    offsetX: clampNumber(settings.print_offset_x, 0, -20, 20),
    offsetY: clampNumber(settings.print_offset_y, 0, -20, 20),
    fontScale: clampNumber(settings.print_font_scale, 100, 85, 115) / 100,
    templateScale: clampNumber(settings.print_template_scale, 100, 94, 106) / 100,
  });

  const setupPage = (lodop, settings = {}) => {
    const parsed = readSettings(settings);
    lodop.SET_PRINT_PAGESIZE(parsed.orientation, WIDTH_MM, HEIGHT_MM, PAGE_NAME);
    if (typeof lodop.SET_PRINT_MODE === "function") {
      lodop.SET_PRINT_MODE("POS_BASEON_PAPER", true);
    }
  };

  const blobToDataUri = (blob) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("Background image read failed"));
    reader.readAsDataURL(blob);
  });

  const loadBackgroundDataUri = async () => {
    if (!backgroundDataUriPromise) {
      backgroundDataUriPromise = fetch(BACKGROUND_URL, { cache: "no-cache" }).then(async (response) => {
        if (!response.ok) throw new Error(`Waybill label background load failed: ${response.status}`);
        return blobToDataUri(await response.blob());
      });
    }
    return backgroundDataUriPromise;
  };

  const escapeAttr = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));

  const imageHtml = (src) => `<img border='0' src='${escapeAttr(src)}'>`;
  const formatPhone = (value) => cleanText(value).replace(/\s+/g, "");

  const alignCode = (align) => {
    if (align === "center") return 2;
    if (align === "right") return 3;
    return 1;
  };

  const FIELD_LAYOUT = [
    { field: "waybillNo", x: mmValue(35), y: mmValue(150), w: mmValue(128), h: mmValue(22), px: 21, bold: true, maxChars: 18, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "date", x: mmValue(235), y: mmValue(150), w: mmValue(110), h: mmValue(22), px: 20, bold: true, maxChars: 10, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "station", x: mmValue(455), y: mmValue(148), w: mmValue(95), h: mmValue(24), px: 21, longPx: 18, bold: true, maxChars: 8, align: "center", noWrap: true },
    { field: "senderName", x: mmValue(171), y: mmValue(197), w: mmValue(97), h: mmValue(18), px: 17, longPx: 15, bold: true, maxChars: 8, noWrap: true },
    { field: "senderPhone", x: mmValue(354), y: mmValue(197), w: mmValue(163), h: mmValue(18), px: 17, longPx: 15, bold: true, maxChars: 13, noWrap: true, font: NUMBER_FONT },
    { field: "senderAddress", x: mmValue(171), y: mmValue(241), w: mmValue(306), h: mmValue(20), px: 17, longPx: 15, bold: true, lines: 1, maxChars: 22 },
    { field: "recipientName", x: mmValue(171), y: mmValue(289), w: mmValue(97), h: mmValue(18), px: 17, longPx: 15, bold: true, maxChars: 8, noWrap: true },
    { field: "recipientPhone", x: mmValue(354), y: mmValue(289), w: mmValue(163), h: mmValue(18), px: 17, longPx: 15, bold: true, maxChars: 13, noWrap: true, font: NUMBER_FONT },
    { field: "recipientAddress", x: mmValue(171), y: mmValue(331), w: mmValue(356), h: mmValue(20), px: 17, longPx: 15, bold: true, lines: 1, maxChars: 25 },
    { field: "cargoName", x: mmValue(40), y: mmValue(398), w: mmValue(115), h: mmValue(26), px: 19, bold: true, maxChars: 8, align: "center", noWrap: true },
    { field: "packageType", x: mmValue(160), y: mmValue(398), w: mmValue(135), h: mmValue(26), px: 19, bold: true, maxChars: 8, align: "center", noWrap: true },
    { field: "pieces", x: mmValue(310), y: mmValue(398), w: mmValue(110), h: mmValue(26), px: 19, bold: true, maxChars: 7, align: "center", noWrap: true },
    { field: "weightOrVolume", x: mmValue(425), y: mmValue(398), w: mmValue(145), h: mmValue(26), px: 18, bold: true, maxChars: 12, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "transportMethod", x: mmValue(122), y: mmValue(447), w: mmValue(80), h: mmValue(24), px: 19, bold: true, maxChars: 5, align: "center", noWrap: true },
    { field: "paymentMethod", x: mmValue(390), y: mmValue(447), w: mmValue(70), h: mmValue(24), px: 19, bold: true, maxChars: 5, align: "center", noWrap: true },
    { field: "freight", x: mmValue(60), y: mmValue(508), w: mmValue(80), h: mmValue(20), px: 18, bold: true, maxChars: 8, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "packageFee", x: mmValue(205), y: mmValue(508), w: mmValue(80), h: mmValue(20), px: 18, bold: true, maxChars: 8, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "deliveryFee", x: mmValue(350), y: mmValue(508), w: mmValue(80), h: mmValue(20), px: 18, bold: true, maxChars: 8, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "otherFee", x: mmValue(485), y: mmValue(508), w: mmValue(80), h: mmValue(20), px: 18, bold: true, maxChars: 8, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "insuranceFee", x: mmValue(120), y: mmValue(565), w: mmValue(120), h: mmValue(22), px: 18, bold: true, maxChars: 9, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "returnFee", x: mmValue(390), y: mmValue(565), w: mmValue(120), h: mmValue(22), px: 18, bold: true, maxChars: 9, align: "center", noWrap: true, font: NUMBER_FONT },
    { field: "remark", x: mmValue(66), y: mmValue(612), w: mmValue(458), h: mmValue(28), px: 16, bold: false, lines: 2, lineHeightPx: 19, maxChars: 27 },
  ];

  const fieldTextValue = (data, field) => {
    const value = cleanText(data[field]);
    if (field === "recipientPhone" || field === "senderPhone") {
      return formatPhone(value);
    }
    return value;
  };

  const fontSize = (px, context) => Math.max(
    5,
    Math.round(pxToMm(Number(px)) * 2.834 * context.fontScale * context.templateScale),
  );

  const buildDynamicItems = (data) => FIELD_LAYOUT.map((item) => {
    const value = fieldTextValue(data, item.field);
    if (!value) return null;
    const textLength = cleanText(value).replace(/\n/g, "").length;
    const px = textLength >= 8 ? (item.longPx || item.px) : item.px;
    const maxLines = item.lines || 1;
    const content = maxLines > 1
      ? splitText(value, item.maxChars || 18, maxLines)
      : truncateText(cleanText(value).replace(/\n/g, ""), item.maxChars || 18);
    if (!content) return null;
    return { ...item, px, content };
  }).filter(Boolean);

  const addBackground = (lodop, dataUri, context) => {
    lodop.ADD_PRINT_IMAGE(
      mm(0, context.offsetY, context.templateScale),
      mm(0, context.offsetX, context.templateScale),
      mm(74, 0, context.templateScale),
      mm(92, 0, context.templateScale),
      imageHtml(dataUri),
    );
    if (typeof lodop.SET_PRINT_STYLEA === "function") {
      lodop.SET_PRINT_STYLEA(0, "Stretch", 1);
    }
  };

  const addDynamicText = (lodop, item, context) => {
    lodop.ADD_PRINT_TEXT(
      mm(item.y, context.offsetY, context.templateScale),
      mm(item.x, context.offsetX, context.templateScale),
      mm(item.w, 0, context.templateScale),
      mm(item.h, 0, context.templateScale),
      item.content,
    );
    lodop.SET_PRINT_STYLEA(0, "FontName", item.font || CHINESE_FONT);
    lodop.SET_PRINT_STYLEA(0, "FontSize", fontSize(item.px, context));
    lodop.SET_PRINT_STYLEA(0, "FontColor", BLACK);
    lodop.SET_PRINT_STYLEA(0, "Bold", item.bold ? 1 : 0);
    lodop.SET_PRINT_STYLEA(0, "Alignment", alignCode(item.align));
    if (item.noWrap) {
      lodop.SET_PRINT_STYLEA(0, "WordWrap", 0);
    }
    if (item.content.includes("\n")) {
      lodop.SET_PRINT_STYLEA(0, "LineSpacing", 0);
    }
  };

  const applyTemplate = async (lodop, data = {}, settings = {}) => {
    setupPage(lodop, settings);
    const parsed = readSettings(settings);
    const context = {
      ...parsed,
      data: normalizeWaybillData(data, { blank: false }),
    };
    addBackground(lodop, await loadBackgroundDataUri(), context);
    buildDynamicItems(context.data).forEach((item) => addDynamicText(lodop, item, context));
  };

  const addLine = (lodop, item, context) => {
    lodop.ADD_PRINT_LINE(
      mm(item.y1, context.offsetY),
      mm(item.x1, context.offsetX),
      mm(item.y2, context.offsetY),
      mm(item.x2, context.offsetX),
      item.style ?? 0,
      item.width ?? 1,
    );
  };

  const addText = (lodop, item, context) => {
    lodop.ADD_PRINT_TEXT(mm(item.y, context.offsetY), mm(item.x, context.offsetX), mm(item.w), mm(item.h), item.text);
    lodop.SET_PRINT_STYLEA(0, "FontName", CHINESE_FALLBACK_FONT);
    lodop.SET_PRINT_STYLEA(0, "FontSize", Math.max(5, Math.round(Number(item.size || 8) * context.fontScale)));
    lodop.SET_PRINT_STYLEA(0, "FontColor", item.color || BLACK);
    lodop.SET_PRINT_STYLEA(0, "Bold", item.bold ? 1 : 0);
    if (item.align) lodop.SET_PRINT_STYLEA(0, "Alignment", item.align);
  };

  const calibrationLines = [
    { x1: 4, y1: 4, x2: 70, y2: 4, style: 0, width: 1 },
    { x1: 4, y1: 4, x2: 4, y2: 88, style: 0, width: 1 },
    { x1: 4, y1: 88, x2: 70, y2: 88, style: 2, width: 1 },
    { x1: 70, y1: 4, x2: 70, y2: 88, style: 2, width: 1 },
    { x1: 4, y1: 46, x2: 70, y2: 46, style: 2, width: 1 },
    { x1: 37, y1: 4, x2: 37, y2: 88, style: 2, width: 1 },
  ];

  const calibrationText = [
    { x: 6, y: 7, w: 62, h: 6, text: "博益物流主单校准页", size: 13, bold: true, align: 2 },
    { x: 6, y: 17, w: 62, h: 5, text: "这行应在标签上边，且从左到右阅读", size: 9, bold: true, align: 2 },
    { x: 6, y: 28, w: 62, h: 5, text: "纸张：74mm × 92mm", size: 9, align: 2 },
    { x: 6, y: 38, w: 62, h: 5, text: "如整体偏移，请调整 X / Y 偏移；如大小偏差，请调整整体缩放", size: 8, align: 2 },
    { x: 6, y: 49, w: 20, h: 5, text: "左上", size: 10, bold: true },
    { x: 48, y: 82, w: 20, h: 5, text: "右下", size: 10, bold: true, align: 3 },
  ];

  const applyCalibration = (lodop, settings = {}) => {
    setupPage(lodop, settings);
    const parsed = readSettings(settings);
    const context = { ...parsed, data: {} };
    calibrationLines.forEach((item) => addLine(lodop, item, context));
    calibrationText.forEach((item) => addText(lodop, item, context));
  };

  global.WaybillLabelLodop = {
    width: WIDTH_MM,
    height: HEIGHT_MM,
    pageName: PAGE_NAME,
    backgroundUrl: BACKGROUND_URL,
    setupPage,
    applyTemplate,
    applyCalibration,
  };
})(window);
