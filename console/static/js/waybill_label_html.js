(function (global) {
  "use strict";

  const WIDTH_MM = "75mm";
  const HEIGHT_MM = "100mm";
  const PAGE_NAME = "75mm×100mm 博益物流主单";
  const BACKGROUND_URL = "/static/assets/waybill_label_75x100.png?v=20261011-75x100";
  const RECEIPT_BACKGROUND_URL = "/static/assets/waybill_label_75x100_receipt.png?v=20261011-75x100";
  const receiptRequired = (value) => {
    if (value === undefined || value === null || value === false || value === 0 || value === "0") return false;
    if (value === true || value === 1 || value === "1") return true;
    throw new Error("回单选项无效，请重新勾选");
  };
  const backgroundUrlFor = (data = {}) => receiptRequired(data.receiptRequired ?? data.receipt_required)
    ? RECEIPT_BACKGROUND_URL : BACKGROUND_URL;
  // Coordinates and type sizes follow the supplied 1200 × 1600 master/sample
  // (16 source px per mm, exactly two source px per 203dpi printer dot).
  const SOURCE_WIDTH = 1200;
  const SOURCE_HEIGHT = 1600;
  // The 填写样张 sample sets every value in Source Han Sans Bold, served with the
  // Console, so printing needs no locally installed font.
  const LABEL_FONT = { family: "BoyiLabelSans", weight: 700, name: "思源黑体",
    source: 'url("/static/assets/fonts/SourceHanSansCN-VF.ttf.woff2") format("woff2")' };
  const fontCss = (px) => `${LABEL_FONT.weight} ${px}px "${LABEL_FONT.family}"`;
  let labelFontPromise;
  const loadContentFont = () => {
    if (!labelFontPromise) {
      const face = new FontFace(LABEL_FONT.family, LABEL_FONT.source, { weight: "250 900" });
      labelFontPromise = face.load().then((loaded) => {
        document.fonts.add(loaded);
      }).catch(() => {
        labelFontPromise = undefined;
        throw new Error(`打印字体“${LABEL_FONT.name}”加载失败，请检查网络后刷新页面重试`);
      });
    }
    return labelFontPromise;
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
    // Whole amounts print without ".00", as on the sample label.
    return /^0*$/.test(fraction) ? whole : `${whole}.${fraction.padEnd(2, "0")}`;
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
  // Measured from the 填写样张 sample in source pixels: text origin, baseline and
  // font size. Longer content shrinks within maxWidth, which stops about 10px
  // before the next divider or 24px inside the frame.
  const MIN_FONT_PX = 22;
  const ADDRESS_STYLE = { px: 40, maxWidth: 680, lines: 2, pitch: 57.5 };
  const LEFT_DETAIL = { px: 34, x: 305, maxWidth: 276 };
  const RIGHT_DETAIL = { px: 34, x: 860.5, maxWidth: 270 };
  const FIELD_LAYOUT = [
    { field: "waybillNo", px: 44, x: 62.5, baseline: 348.5, maxWidth: 240 },
    { field: "date", px: 44, x: 336, baseline: 348.5, maxWidth: 250 },
    { field: "station", px: 44, x: 617.5, baseline: 348.5, maxWidth: 250 },
    { field: "recipientName", px: 40, x: 413.5, baseline: 481.5, maxWidth: 228 },
    { field: "recipientPhone", px: 40, x: 845.5, baseline: 481.5, maxWidth: 284 },
    // A long recipient address takes a third line at full size; the lines then
    // close up so the last baseline stays at 683, clear of the divider.
    { field: "recipientAddress", ...ADDRESS_STYLE, lines: 3, lastBaseline: 683, x: 450.5, baseline: 579 },
    { field: "senderName", px: 40, x: 413.5, baseline: 795.5, maxWidth: 228 },
    { field: "senderPhone", px: 40, x: 845.5, baseline: 795.5, maxWidth: 284 },
    // The sender block leaves room for a single address line above its divider.
    { field: "senderAddress", ...ADDRESS_STYLE, lines: 1, x: 450.5, baseline: 882.5 },
    { field: "cargoName", ...LEFT_DETAIL, baseline: 1002.5 },
    { field: "packageType", ...RIGHT_DETAIL, baseline: 1002.5 },
    { field: "pieces", ...LEFT_DETAIL, baseline: 1060.5 },
    { field: "weight", ...RIGHT_DETAIL, baseline: 1060.5 },
    { field: "volume", ...LEFT_DETAIL, baseline: 1119.5 },
    { field: "freight", ...LEFT_DETAIL, baseline: 1204.5 },
    { field: "transportMethod", ...RIGHT_DETAIL, baseline: 1204.5 },
    { field: "pickupFee", ...LEFT_DETAIL, baseline: 1263.5 },
    { field: "paymentMethod", ...RIGHT_DETAIL, baseline: 1263.5 },
    { field: "deliveryFee", ...LEFT_DETAIL, baseline: 1322.5 },
    { field: "insuranceAmount", ...RIGHT_DETAIL, baseline: 1322.5 },
    { field: "transferFee", ...LEFT_DETAIL, baseline: 1381.5 },
    { field: "codAmount", ...RIGHT_DETAIL, baseline: 1381.5 },
    { field: "remark", ...ADDRESS_STYLE, x: 214.5, baseline: 1463.5, maxWidth: 916 },
  ];

  const FIELD_LABELS = {
    waybillNo: "运单编号", date: "日期", station: "目的地",
    recipientName: "收货人", recipientPhone: "收货电话", recipientAddress: "收件地址",
    senderName: "发货人", senderPhone: "发货电话", senderAddress: "发货地址",
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

  const linePitch = (item, px, count) => {
    const pitch = (item.pitch || 0) * px / item.px;
    return item.lastBaseline && count > 1
      ? Math.min(pitch, (item.lastBaseline - item.baseline) / (count - 1)) : pitch;
  };

  const buildDynamicItems = (data, settings = readSettings()) => {
    const context = document.createElement("canvas").getContext("2d");
    if (!context) throw new Error("无法测量打印文字，请刷新页面后重试");
    const items = FIELD_LAYOUT.flatMap((item) => {
      const value = cleanText(data[item.field]);
      if (!value) return [];
      const basePx = item.px * settings.fontScale;
      for (let px = basePx; px >= MIN_FONT_PX; px -= 0.5) {
        context.font = fontCss(px);
        // Fit by reducing font size; glyphs are never squeezed horizontally.
        const lines = wrapText(value, item.maxWidth, context);
        if (lines.length > (item.lines || 1)) continue;
        return [{
          field: item.field, lines, px,
          x: item.x, baseline: item.baseline, pitch: linePitch(item, px, lines.length),
        }];
      }
      throw new Error(`${FIELD_LABELS[item.field]}内容过长，请缩短内容后重试`);
    });
    // Header values share one baseline; only an overlong value shrinks, so a
    // long destination never makes the waybill number or date unreadable.
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

  // The original master is used unchanged; this in-memory copy drops its
  // anti-aliased gray edges and separates frame rules from the printed labels.
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
    const width = Math.round(75 * dotsPerMm);
    const height = Math.round(100 * dotsPerMm);

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
    const scaleX = width / SOURCE_WIDTH;
    const scaleY = height / SOURCE_HEIGHT;
    for (const item of items) {
      text.context.font = fontCss(item.px);
      item.lines.forEach((line, index) => {
        text.context.save();
        text.context.translate(item.x * scaleX, (item.baseline + index * item.pitch) * scaleY);
        text.context.scale(scaleX, scaleY);
        text.context.fillText(line, 0, 0);
        text.context.restore();
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
    return `<div class="ys-waybill-label" data-waybill-background-template="true" style="width:75mm;height:100mm;position:relative;overflow:hidden;background:#fff"><img alt="博益运单打印预览" style="position:absolute;left:${settings.offsetX}mm;top:${settings.offsetY}mm;width:${75 * settings.templateScale}mm;height:${100 * settings.templateScale}mm;max-width:none" src="${escapeHtml(source)}"></div>`;
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
