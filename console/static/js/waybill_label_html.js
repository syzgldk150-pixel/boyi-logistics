(function (global) {
  "use strict";

  const WIDTH_MM = "74mm";
  const HEIGHT_MM = "92mm";
  const PAGE_NAME = "74mm×92mm 博益物流主单";
  const BACKGROUND_URL = "/static/assets/waybill_label_background.jpg";
  // Coordinates refer to the supplied, unmodified 1122 × 1402 blank master.
  const SOURCE_WIDTH = 1122;
  const SOURCE_HEIGHT = 1402;
  const CONTENT_FONT = "黑体";
  const CONTENT_WEIGHT = 700;
  const CONTENT_SIZE_PX = 44;
  let contentFontPromise;
  const loadContentFont = () => {
    if (!contentFontPromise) {
      const face = new FontFace(CONTENT_FONT, `local("SimHei"), local("${CONTENT_FONT}")`);
      contentFontPromise = face.load().then((loaded) => {
        document.fonts.add(loaded);
      }).catch(() => {
        throw new Error("本机黑体字体加载失败，请安装黑体后刷新页面重试");
      });
    }
    return contentFontPromise;
  };
  const cleanText = (value) => String(value ?? "").replace(/\r\n?/g, "\n").trim();
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));

  const money = (value) => {
    const text = cleanText(value).replace(/[元块]$/, "").trim();
    if (!text) return "";
    if (!/^\d+(?:\.\d+)?$/.test(text)) throw new Error("费用格式无法识别，请检查运单金额");
    const [whole, fraction = ""] = text.split(".");
    return `${whole}.${fraction.padEnd(2, "0")}`;
  };

  const readWeightVolume = (data) => {
    const weight = cleanText(data.weight ?? data.weight_kg);
    const volume = cleanText(data.volume ?? data.volume_m3);
    if (weight || volume) return { weight, volume };
    const combined = cleanText(data.weight_volume);
    if (!combined) return { weight: "", volume: "" };
    const result = { weight: "", volume: "" };
    for (const part of combined.split(/\s*\/\s*/)) {
      const match = part.match(/^(\d+(?:\.\d+)?)\s*(kg|m³)$/i);
      if (!match) throw new Error("重量/体积格式无法识别，请分别填写带 kg、m³ 单位的值");
      const key = match[2].toLowerCase() === "kg" ? "weight" : "volume";
      if (result[key]) throw new Error("重量/体积存在重复值，请检查运单");
      result[key] = match[1];
    }
    return result;
  };

  const normalizeData = (data = {}, options = {}) => {
    if (options.blank) return {};
    const field = (canonical, source) => cleanText(data[canonical] ?? data[source]);
    return {
      waybillNo: field("waybillNo", "waybill_no"),
      date: field("date", "open_date").replaceAll("-", "/"),
      station: field("station", "destination_site"),
      recipientName: field("recipientName", "receiver_name"),
      recipientPhone: field("recipientPhone", "receiver_phone").replace(/\s+/g, ""),
      recipientAddress: field("recipientAddress", "receiver_address"),
      senderName: field("senderName", "sender_name"),
      senderPhone: field("senderPhone", "sender_phone").replace(/\s+/g, ""),
      senderAddress: field("senderAddress", "sender_address"),
      cargoName: field("cargoName", "goods_name_lines"),
      packageType: field("packageType", "package_type_lines"),
      pieces: field("pieces", "quantity_lines"),
      ...readWeightVolume(data),
      freight: money(field("freight", "freight_fee")),
      pickupFee: money(field("pickupFee", "pickup_fee")),
      deliveryFee: money(field("deliveryFee", "delivery_fee")),
      transferFee: money(field("transferFee", "transfer_fee")),
      insuranceAmount: money(field("insuranceAmount", "insurance_amount")),
      codAmount: money(field("codAmount", "cod_amount")),
      transportMethod: field("transportMethod", "delivery_method"),
      paymentMethod: field("paymentMethod", "payment_method"),
      remark: field("remark", "remark"),
      makerName: field("makerName", "maker_name"),
      handlerName: field("handlerName", "handler_name"),
    };
  };

  const clampNumber = (value, fallback, min, max) => {
    const number = Number.parseFloat(value);
    return Number.isFinite(number) ? Math.max(min, Math.min(number, max)) : fallback;
  };
  const readSettings = (settings = {}) => ({
    orientation: String(settings.print_orientation || "1") === "2" ? 2 : 1,
    offsetX: clampNumber(settings.print_offset_x, 0, -20, 20),
    offsetY: clampNumber(settings.print_offset_y, 0, -20, 20),
    fontScale: clampNumber(settings.print_font_scale, 100, 85, 115) / 100,
    templateScale: clampNumber(settings.print_template_scale, 100, 94, 106) / 100,
  });
  const stripZeros = (number) => Number(number).toFixed(3).replace(/\.?0+$/, "");
  const placedMm = (value, offset, settings) => `${stripZeros(offset + value * settings.templateScale)}mm`;
  const sizedMm = (value, settings) => `${stripZeros(value * settings.templateScale)}mm`;

  // Padded cell rectangles in source pixels, shared by HTML and native printing.
  const FIELD_LAYOUT = [
    { field: "waybillNo", x: 190, y: 229, w: 194, h: 69 },
    { field: "date", x: 531, y: 229, w: 187, h: 69 },
    { field: "station", x: 891, y: 229, w: 191, h: 69 },
    { field: "recipientName", x: 271, y: 314, w: 365, h: 73 },
    { field: "recipientPhone", x: 825, y: 314, w: 257, h: 73 },
    { field: "recipientAddress", align: "left", x: 271, y: 401, w: 810, h: 80, lines: 2 },
    { field: "senderName", x: 271, y: 499, w: 365, h: 72 },
    { field: "senderPhone", x: 825, y: 499, w: 257, h: 72 },
    { field: "senderAddress", align: "left", x: 271, y: 588, w: 810, h: 78, lines: 2 },
    { field: "cargoName", x: 122, y: 769, w: 224, h: 85, align: "center", lines: 2 },
    { field: "packageType", x: 372, y: 769, w: 146, h: 85, align: "center" },
    { field: "pieces", x: 544, y: 769, w: 131, h: 85, align: "center" },
    { field: "weight", x: 700, y: 769, w: 186, h: 85, align: "center" },
    { field: "volume", x: 911, y: 769, w: 170, h: 85, align: "center" },
    { field: "freight", x: 270, y: 872, w: 126, h: 60 },
    { field: "pickupFee", x: 575, y: 872, w: 154, h: 60 },
    { field: "deliveryFee", x: 913, y: 872, w: 168, h: 60 },
    { field: "transferFee", x: 270, y: 948, w: 126, h: 62 },
    { field: "transportMethod", x: 575, y: 948, w: 154, h: 62 },
    { field: "paymentMethod", x: 913, y: 948, w: 168, h: 62 },
    { field: "insuranceAmount", x: 270, y: 1026, w: 126, h: 71 },
    { field: "codAmount", x: 575, y: 1026, w: 506, h: 71 },
    { field: "remark", align: "left", x: 191, y: 1116, w: 890, h: 94, lines: 2 },
    { field: "makerName", x: 191, y: 1229, w: 154, h: 98 },
    { field: "handlerName", x: 529, y: 1229, w: 144, h: 98 },
  ];

  const FIELD_LABELS = {
    waybillNo: "运单编号", date: "日期", station: "目的地",
    recipientName: "收货人", recipientPhone: "收货电话", recipientAddress: "收件地址",
    senderName: "发货人", senderPhone: "发货电话", senderAddress: "发件地址",
    cargoName: "货物名称", packageType: "包装类型", pieces: "件数", weight: "重量", volume: "体积",
    freight: "运费", pickupFee: "接货费", deliveryFee: "送货费", transferFee: "中转费",
    transportMethod: "送货方式", paymentMethod: "结算方式", insuranceAmount: "保价金额", codAmount: "代收金额",
    remark: "备注", makerName: "制单人", handlerName: "经办人",
  };
  const wrapText = (text, width, context) => {
    const lines = [];
    for (const paragraph of text.split("\n")) {
      let line = "";
      for (const char of paragraph) {
        if (line && context.measureText(line + char).width > width) {
          lines.push(line);
          line = "";
        }
        line += char;
      }
      lines.push(line);
    }
    return lines;
  };

  const buildDynamicItems = (data, settings = readSettings()) => {
    const context = document.createElement("canvas").getContext("2d");
    if (!context) throw new Error("无法测量打印文字，请刷新页面后重试");
    return FIELD_LAYOUT.flatMap((item) => {
      const value = cleanText(data[item.field]);
      if (!value) return [];
      const font = CONTENT_FONT;
      for (let px = CONTENT_SIZE_PX * settings.fontScale; px >= 22; px -= 0.5) {
        context.font = `${CONTENT_WEIGHT} ${px}px "${font}"`;
        const lines = wrapText(value, item.w * 0.95, context);
        const lineHeight = px * 1.18;
        if (lines.length > (item.lines || 1) || lines.length * lineHeight > item.h) continue;
        return [{
          field: item.field, content: lines.join("\n"), font, fontWeight: CONTENT_WEIGHT, align: item.align || "center",
          x: item.x * 74 / SOURCE_WIDTH,
          y: (item.y + (item.h - lines.length * lineHeight) / 2) * 92 / SOURCE_HEIGHT,
          w: item.w * 74 / SOURCE_WIDTH,
          h: (lines.length * lineHeight + 4) * 92 / SOURCE_HEIGHT,
          fontPt: px * 92 / SOURCE_HEIGHT * 72 / 25.4,
          lineHeightMm: lineHeight * 92 / SOURCE_HEIGHT,
        }];
      }
      throw new Error(`${FIELD_LABELS[item.field]}内容过长，请缩短内容后重试`);
    });
  };

  const labelCss = () => `<style>
.ys-waybill-label, .ys-waybill-label * { box-sizing: border-box; }
.ys-waybill-label { width:74mm; height:92mm; position:relative; overflow:hidden; background:#fff; color:#000; }
.ys-waybill-background { position:absolute; display:block; user-select:none; pointer-events:none; }
.ys-field { position:absolute; white-space:pre; color:#000; font-weight:${CONTENT_WEIGHT}; }
</style>`;

  async function buildHtml(data = {}, options = {}) {
    await loadContentFont();
    const settings = readSettings(options);
    const fields = buildDynamicItems(normalizeData(data, options), settings).map((item) =>
      `<div class="ys-field" data-field="${item.field}" style="left:${placedMm(item.x, settings.offsetX, settings)};top:${placedMm(item.y, settings.offsetY, settings)};width:${sizedMm(item.w, settings)};height:${sizedMm(item.h, settings)};font-family:'${item.font}',sans-serif;font-size:${item.fontPt * settings.templateScale}pt;line-height:${sizedMm(item.lineHeightMm, settings)};text-align:${item.align}">${escapeHtml(item.content)}</div>`
    ).join("");
    const background = `<img class="ys-waybill-background" src="${BACKGROUND_URL}" alt="" style="left:${placedMm(0, settings.offsetX, settings)};top:${placedMm(0, settings.offsetY, settings)};width:${sizedMm(74, settings)};height:${sizedMm(92, settings)};">`;
    return `${labelCss()}<div class="ys-waybill-label" data-waybill-background-template="true">${background}${fields}</div>`;
  }

  async function renderPreview(target, data = {}, options = {}) {
    const element = typeof target === "string" ? document.querySelector(target) : target;
    if (element) element.innerHTML = await buildHtml(data, options);
  }

  global.WaybillLabelHtml = {
    width: WIDTH_MM, height: HEIGHT_MM, pageName: PAGE_NAME, backgroundUrl: BACKGROUND_URL,
    normalizeData, readSettings, loadContentFont, buildDynamicItems, buildHtml, renderPreview,
  };
})(window);
