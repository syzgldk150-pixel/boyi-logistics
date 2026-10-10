(function (global) {
  "use strict";

  const WIDTH_MM = "74mm";
  const HEIGHT_MM = "92mm";
  const PAGE_NAME = "74mm×92mm 博益物流主单";
  const BACKGROUND_URL = "/static/assets/waybill_label_background.jpg?v=20260916-receipt-v3";
  const RECEIPT_BACKGROUND_URL = "/static/assets/waybill_label_receipt_background.jpg?v=20260916-receipt-v3";
  const receiptRequired = (value) => {
    if (value === undefined || value === null || value === false || value === 0 || value === "0") return false;
    if (value === true || value === 1 || value === "1") return true;
    throw new Error("回单选项无效，请重新勾选");
  };
  const backgroundUrlFor = (data = {}) => receiptRequired(data.receiptRequired ?? data.receipt_required)
    ? RECEIPT_BACKGROUND_URL : BACKGROUND_URL;
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
      receiptRequired: receiptRequired(data.receiptRequired ?? data.receipt_required),
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
  // Thermal heads print only black or white dots. The label is produced at the
  // printer's own dot grid as a pure black/white image so the driver neither
  // resamples nor dithers it (dithered gray turned rules dotted and text fuzzy).
  const PRINT_DOTS_PER_MM = { 203: 8, 300: 300 / 25.4 };
  const BACKGROUND_INK_LEVEL = 160; // source pixels darker than this are template ink
  const RULE_MIN_RUN = 80; // straight template ink runs this long (source px, ~5mm) are frame rules
  const RULE_COVERAGE_LEVEL = 205; // rule dots at least ~20% covered print, keeping rules >= 2 dots
  const BACKGROUND_COVERAGE_LEVEL = 150; // other template ink prints when at least ~40% covered
  const TEXT_COVERAGE_ALPHA = 102; // text dots at least 40% covered print black
  const readSettings = (settings = {}) => ({
    printDpi: String(settings.print_dpi || "203") === "300" ? 300 : 203,
    orientation: String(settings.print_orientation || "1") === "2" ? 2 : 1,
    offsetX: clampNumber(settings.print_offset_x, 0, -20, 20),
    offsetY: clampNumber(settings.print_offset_y, 0, -20, 20),
    fontScale: clampNumber(settings.print_font_scale, 100, 85, 115) / 100,
    templateScale: clampNumber(settings.print_template_scale, 100, 94, 106) / 100,
  });
  // Padded content regions in source pixels, shared by HTML and native printing.
  const HEADER_FIELDS = new Set(["waybillNo", "date", "station"]);
  const HEADER_TEXT_LAYOUT = { y: 270, h: 73, fontPx: 48 };
  const FIELD_LAYOUT = [
    { field: "waybillNo", x: 33, w: 280, ...HEADER_TEXT_LAYOUT },
    { field: "date", x: 348, w: 255, ...HEADER_TEXT_LAYOUT },
    { field: "station", x: 640, w: 242, ...HEADER_TEXT_LAYOUT },
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
    const items = FIELD_LAYOUT.flatMap((item) => {
      const value = cleanText(data[item.field]);
      if (!value) return [];
      const font = CONTENT_FONT;
      for (let px = (item.fontPx || CONTENT_SIZE_PX) * settings.fontScale; px >= 22; px -= 0.5) {
        context.font = `${CONTENT_WEIGHT} ${px}px "${font}"`;
        const availableWidth = item.w * 0.95;
        // Fit by reducing font size, preserving the font's natural proportions.
        const lines = wrapText(value, availableWidth, context);
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
    // Keep the entire header row at the smallest fitted size, on one baseline.
    const headerItems = items.filter((item) => HEADER_FIELDS.has(item.field));
    if (headerItems.length) {
      const fontPt = Math.min(...headerItems.map((item) => item.fontPt));
      for (const item of headerItems) {
        const lineHeightMm = item.lineHeightMm * fontPt / item.fontPt;
        const heightReduction = item.lineHeightMm - lineHeightMm;
        item.y += heightReduction / 2;
        item.h -= heightReduction;
        item.fontPt = fontPt;
        item.lineHeightMm = lineHeightMm;
      }
    }
    return items;
  };

  const backgroundImages = new Map();
  const loadBackground = (url) => {
    if (!backgroundImages.has(url)) {
      backgroundImages.set(url, new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => {
          if (image.naturalWidth !== SOURCE_WIDTH || image.naturalHeight !== SOURCE_HEIGHT) {
            reject(new Error("Waybill label background load failed: 底图尺寸不匹配"));
          } else resolve(image);
        };
        image.onerror = () => reject(new Error("Waybill label background load failed: 底图无法读取"));
        image.src = url;
      }).catch((error) => {
        backgroundImages.delete(url);
        throw error;
      }));
    }
    return backgroundImages.get(url);
  };

  const createCanvas = (width, height) => {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) throw new Error("无法生成打印面单，请刷新页面后重试");
    return { canvas, context };
  };
  const luminance = (pixels, index) => 0.299 * pixels[index] + 0.587 * pixels[index + 1] + 0.114 * pixels[index + 2];

  const maskCanvas = (mask) => {
    const { canvas, context } = createCanvas(SOURCE_WIDTH, SOURCE_HEIGHT);
    const frame = context.createImageData(SOURCE_WIDTH, SOURCE_HEIGHT);
    for (let pixel = 0; pixel < mask.length; pixel += 1) {
      const value = mask[pixel] ? 0 : 255;
      const index = pixel * 4;
      frame.data[index] = frame.data[index + 1] = frame.data[index + 2] = value;
      frame.data[index + 3] = 255;
    }
    context.putImageData(frame, 0, 0);
    return canvas;
  };
  const markLongRuns = (ink, rules, length, step, count, stride) => {
    for (let line = 0; line < count; line += 1) {
      let start = -1;
      for (let position = 0; position <= length; position += 1) {
        const pixel = line * stride + position * step;
        if (position < length && ink[pixel]) {
          if (start < 0) start = position;
        } else if (start >= 0) {
          if (position - start >= RULE_MIN_RUN) {
            for (let run = start; run < position; run += 1) rules[line * stride + run * step] = 1;
          }
          start = -1;
        }
      }
    }
  };

  // The original master is used unchanged; this in-memory copy drops its JPEG
  // gray noise and separates frame rules from the printed labels.
  const cleanBackgrounds = new Map();
  const loadCleanBackground = (url) => {
    if (!cleanBackgrounds.has(url)) {
      cleanBackgrounds.set(url, loadBackground(url).then((image) => {
        const { context } = createCanvas(SOURCE_WIDTH, SOURCE_HEIGHT);
        context.drawImage(image, 0, 0);
        const pixels = context.getImageData(0, 0, SOURCE_WIDTH, SOURCE_HEIGHT).data;
        const ink = new Uint8Array(SOURCE_WIDTH * SOURCE_HEIGHT);
        for (let pixel = 0; pixel < ink.length; pixel += 1) {
          ink[pixel] = luminance(pixels, pixel * 4) < BACKGROUND_INK_LEVEL ? 1 : 0;
        }
        const rules = new Uint8Array(ink.length);
        markLongRuns(ink, rules, SOURCE_WIDTH, 1, SOURCE_HEIGHT, SOURCE_WIDTH);
        markLongRuns(ink, rules, SOURCE_HEIGHT, SOURCE_WIDTH, SOURCE_WIDTH, 1);
        return { ink: maskCanvas(ink), rules: maskCanvas(rules) };
      }).catch((error) => {
        cleanBackgrounds.delete(url);
        throw error;
      }));
    }
    return cleanBackgrounds.get(url);
  };

  // Rasterize once on the printer's dot grid. C-Lodop must not reflow or resample it.
  async function buildPrintImage(data = {}, options = {}) {
    await loadContentFont();
    const normalized = normalizeData(data, options);
    const settings = readSettings(options);
    const background = await loadCleanBackground(backgroundUrlFor(normalized));
    const items = buildDynamicItems(normalized, settings);
    const dotsPerMm = PRINT_DOTS_PER_MM[settings.printDpi] * settings.templateScale;
    const width = Math.round(74 * dotsPerMm);
    const height = Math.round(92 * dotsPerMm);

    // Downscale each template layer; gray then measures how much of a dot it covers.
    const scaled = (source) => {
      const layer = createCanvas(width, height);
      layer.context.imageSmoothingEnabled = true;
      layer.context.imageSmoothingQuality = "high";
      layer.context.drawImage(source, 0, 0, width, height);
      return layer;
    };
    const label = scaled(background.ink);
    const rulePixels = scaled(background.rules).context.getImageData(0, 0, width, height).data;

    // Text is drawn on its own layer directly at printer resolution.
    const text = createCanvas(width, height);
    text.context.fillStyle = "#000000";
    text.context.textBaseline = "alphabetic";
    for (const item of items) {
      const px = item.fontPt * 25.4 / 72 * dotsPerMm;
      const lineHeight = item.lineHeightMm * dotsPerMm;
      text.context.font = `${item.fontWeight} ${px}px "${item.font}"`;
      const metrics = text.context.measureText("国Ag");
      const ascent = metrics.actualBoundingBoxAscent;
      const descent = metrics.actualBoundingBoxDescent;
      const baseline = (lineHeight - ascent - descent) / 2 + ascent;
      item.content.split("\n").forEach((line, index) => {
        text.context.fillText(line, item.x * dotsPerMm, item.y * dotsPerMm + index * lineHeight + baseline);
      });
    }

    const frame = label.context.getImageData(0, 0, width, height);
    const pixels = frame.data;
    const textPixels = text.context.getImageData(0, 0, width, height).data;
    for (let index = 0; index < pixels.length; index += 4) {
      const ink = luminance(pixels, index) < BACKGROUND_COVERAGE_LEVEL
        || luminance(rulePixels, index) < RULE_COVERAGE_LEVEL
        || textPixels[index + 3] >= TEXT_COVERAGE_ALPHA;
      const value = ink ? 0 : 255;
      pixels[index] = pixels[index + 1] = pixels[index + 2] = value;
      pixels[index + 3] = 255;
    }
    label.context.putImageData(frame, 0, 0);
    return label.canvas.toDataURL("image/png");
  }

  async function buildHtml(data = {}, options = {}) {
    const settings = readSettings(options);
    const source = await buildPrintImage(data, options);
    return `<div class="ys-waybill-label" data-waybill-background-template="true" style="width:74mm;height:92mm;position:relative;overflow:hidden;background:#fff"><img alt="博益运单打印预览" style="position:absolute;left:${settings.offsetX}mm;top:${settings.offsetY}mm;width:${74 * settings.templateScale}mm;height:${92 * settings.templateScale}mm;max-width:none" src="${escapeHtml(source)}"></div>`;
  }

  async function renderPreview(target, data = {}, options = {}) {
    const element = typeof target === "string" ? document.querySelector(target) : target;
    if (element) element.innerHTML = await buildHtml(data, options);
  }

  global.WaybillLabelHtml = {
    width: WIDTH_MM, height: HEIGHT_MM, pageName: PAGE_NAME, backgroundUrl: BACKGROUND_URL,
    normalizeData, readSettings, loadContentFont, buildDynamicItems, backgroundUrlFor, buildPrintImage, buildHtml, renderPreview,
  };
})(window);
