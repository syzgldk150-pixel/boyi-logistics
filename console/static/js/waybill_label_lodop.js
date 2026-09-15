(function (global) {
  "use strict";

  const template = global.WaybillLabelHtml;
  if (!template) throw new Error("Waybill Lodop renderer not loaded: shared template missing");
  const { width: WIDTH_MM, height: HEIGHT_MM, pageName: PAGE_NAME, backgroundUrl: BACKGROUND_URL, readSettings } = template;
  const CHINESE_FALLBACK_FONT = "SimHei";
  const BLACK = "#000000";
  let backgroundDataUriPromise = null;
  const stripZeros = (number) => Number(number).toFixed(3).replace(/\.?0+$/, "");
  const mm = (value, offset = 0, scale = 1) => `${stripZeros(offset + value * scale)}mm`;

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
  const alignCode = (align) => align === "center" ? 2 : align === "right" ? 3 : 1;

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
    item.content.split("\n").forEach((line, index) => {
      lodop.ADD_PRINT_TEXT(
        mm(item.y + index * item.lineHeightMm, context.offsetY, context.templateScale),
        mm(item.x, context.offsetX, context.templateScale),
        mm(item.w, 0, context.templateScale),
        mm(item.lineHeightMm + 0.3, 0, context.templateScale),
        line,
      );
      lodop.SET_PRINT_STYLEA(0, "FontName", item.font);
      lodop.SET_PRINT_STYLEA(0, "FontSize", item.fontPt * context.templateScale);
      lodop.SET_PRINT_STYLEA(0, "FontColor", BLACK);
      lodop.SET_PRINT_STYLEA(0, "Bold", item.fontWeight >= 700 ? 1 : 0);
      lodop.SET_PRINT_STYLEA(0, "Alignment", alignCode(item.align));
      lodop.SET_PRINT_STYLEA(0, "WordWrap", 0);
    });
  };
  const applyTemplate = async (lodop, data = {}, settings = {}) => {
    setupPage(lodop, settings);
    const parsed = readSettings(settings);
    const context = {
      ...parsed,
      data: template.normalizeData(data),
    };
    await template.loadContentFont();
    const items = template.buildDynamicItems(context.data, parsed);
    addBackground(lodop, await loadBackgroundDataUri(), context);
    items.forEach((item) => addDynamicText(lodop, item, context));
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
