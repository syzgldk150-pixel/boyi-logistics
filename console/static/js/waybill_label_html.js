(function (global) {
  "use strict";

  const WIDTH_MM = "74mm";
  const HEIGHT_MM = "92mm";
  const PAGE_NAME = "74mm×92mm 博益物流主单";
  const BACKGROUND_URL = "/static/assets/waybill_label_background.jpg?v=20260916-master-v2";
  // Coordinates and type sizes follow the supplied 1162 × 1450 master/sample.
  const SOURCE_WIDTH = 1162;
  const SOURCE_HEIGHT = 1450;
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

  // Padded content regions in source pixels, shared by HTML and native printing.
  const FIELD_LAYOUT = [
    { field: "waybillNo", x: 33, y: 264, w: 280, h: 85, fontPx: 64 },
    { field: "date", x: 348, y: 270, w: 255, h: 73, fontPx: 56 },
    { field: "station", x: 640, y: 264, w: 242, h: 87, fontPx: 62 },
    { field: "recipientName", x: 305, y: 396, w: 330, h: 72, fontPx: 54 },
    { field: "recipientPhone", x: 834, y: 396, w: 303, h: 72, fontPx: 52 },
    { field: "recipientAddress", x: 342, y: 482, w: 782, h: 110, fontPx: 36, lines: 2, leading: 1.38, valign: "top" },
    { field: "senderName", x: 305, y: 644, w: 330, h: 72, fontPx: 54 },
    { field: "senderPhone", x: 834, y: 644, w: 303, h: 72, fontPx: 52 },
    { field: "senderAddress", x: 342, y: 724, w: 782, h: 102, fontPx: 36, lines: 2, leading: 1.38, valign: "top" },
    { field: "cargoName", x: 260, y: 864, w: 277, h: 53 },
    { field: "packageType", x: 782, y: 864, w: 343, h: 53 },
    { field: "pieces", x: 188, y: 923, w: 349, h: 52, fontPx: 46 },
    { field: "weight", x: 820, y: 923, w: 305, h: 52, fontPx: 46 },
    { field: "volume", x: 271, y: 980, w: 266, h: 52, fontPx: 46 },
    { field: "freight", x: 206, y: 1064, w: 331, h: 52, fontPx: 42 },
    { field: "pickupFee", x: 206, y: 1119, w: 331, h: 52, fontPx: 42 },
    { field: "deliveryFee", x: 206, y: 1175, w: 331, h: 52, fontPx: 42 },
    { field: "transferFee", x: 206, y: 1230, w: 331, h: 52, fontPx: 42 },
    { field: "transportMethod", x: 789, y: 1064, w: 336, h: 52 },
    { field: "paymentMethod", x: 789, y: 1120, w: 336, h: 52 },
    { field: "insuranceAmount", x: 789, y: 1175, w: 336, h: 52, fontPx: 42 },
    { field: "codAmount", x: 789, y: 1230, w: 336, h: 52, fontPx: 42 },
    { field: "remark", x: 158, y: 1320, w: 967, h: 96, fontPx: 36, lines: 2, valign: "top" },
  ];

  const FIELD_LABELS = {
    waybillNo: "运单编号", date: "日期", station: "目的地",
    recipientName: "收货人", recipientPhone: "收货电话", recipientAddress: "收件地址",
    senderName: "发货人", senderPhone: "发货电话", senderAddress: "发件地址",
    cargoName: "货物名称", packageType: "包装类型", pieces: "件数", weight: "重量", volume: "体积",
    freight: "运费", pickupFee: "接货费", deliveryFee: "送货费", transferFee: "中转费",
    transportMethod: "送货方式", paymentMethod: "结算方式", insuranceAmount: "保价金额", codAmount: "代收金额",
    remark: "备注",
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
      for (let px = (item.fontPx || CONTENT_SIZE_PX) * settings.fontScale; px >= 22; px -= 0.5) {
        context.font = `${CONTENT_WEIGHT} ${px}px "${font}"`;
        const lines = wrapText(value, item.w * 0.95, context);
        const lineHeight = px * (item.leading || 1.18);
        if (lines.length > (item.lines || 1) || lines.length * lineHeight > item.h) continue;
        return [{
          field: item.field, content: lines.join("\n"), font, fontWeight: CONTENT_WEIGHT, align: "left",
          x: item.x * 74 / SOURCE_WIDTH,
          y: (item.y + (item.valign === "top" ? 0 : (item.h - lines.length * lineHeight) / 2)) * 92 / SOURCE_HEIGHT,
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
