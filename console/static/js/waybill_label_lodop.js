(function (global) {
  "use strict";

  const template = global.WaybillLabelHtml;
  if (!template) throw new Error("Waybill Lodop renderer not loaded: shared template missing");
  const { width: WIDTH_MM, height: HEIGHT_MM, pageName: PAGE_NAME, backgroundUrl: BACKGROUND_URL, readSettings } = template;
  const CHINESE_FALLBACK_FONT = "SimHei";
  const BLACK = "#000000";
  const stripZeros = (number) => Number(number).toFixed(3).replace(/\.?0+$/, "");
  const mm = (value, offset = 0, scale = 1) => `${stripZeros(offset + value * scale)}mm`;

  const setupPage = (lodop, settings = {}) => {
    const parsed = readSettings(settings);
    lodop.SET_PRINT_PAGESIZE(parsed.orientation, WIDTH_MM, HEIGHT_MM, PAGE_NAME);
    if (typeof lodop.SET_PRINT_MODE === "function") {
      lodop.SET_PRINT_MODE("POS_BASEON_PAPER", true);
    }
  };

  const escapeAttr = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));

  const imageHtml = (src) => `<img border='0' src='${escapeAttr(src)}'>`;

  const addBackground = (lodop, dataUri, context) => {
    lodop.ADD_PRINT_IMAGE(
      mm(0, context.offsetY, context.templateScale),
      mm(0, context.offsetX, context.templateScale),
      mm(75, 0, context.templateScale),
      mm(100, 0, context.templateScale),
      imageHtml(dataUri),
    );
    if (typeof lodop.SET_PRINT_STYLEA === "function") {
      lodop.SET_PRINT_STYLEA(0, "Stretch", 1);
    }
  };

  const applyTemplate = async (lodop, data = {}, settings = {}) => {
    const image = await template.buildPrintImage(data, settings);
    setupPage(lodop, settings);
    addBackground(lodop, image, readSettings(settings));
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

  // The solid box sits where the master's outer frame is printed (2.5mm from
  // the sides, 2mm from top and bottom); all four sides must print.
  const calibrationLines = [
    { x1: 2.5, y1: 2, x2: 72.5, y2: 2, style: 0, width: 2 },
    { x1: 2.5, y1: 2, x2: 2.5, y2: 98, style: 0, width: 2 },
    { x1: 2.5, y1: 98, x2: 72.5, y2: 98, style: 0, width: 2 },
    { x1: 72.5, y1: 2, x2: 72.5, y2: 98, style: 0, width: 2 },
    { x1: 2.5, y1: 50, x2: 72.5, y2: 50, style: 2, width: 1 },
    { x1: 37.5, y1: 2, x2: 37.5, y2: 98, style: 2, width: 1 },
  ];

  const calibrationText = [
    { x: 6.5, y: 7, w: 62, h: 6, text: "博益物流主单校准页", size: 13, bold: true, align: 2 },
    { x: 6.5, y: 17, w: 62, h: 5, text: "这行应在标签上边，且从左到右阅读", size: 9, bold: true, align: 2 },
    { x: 6.5, y: 28, w: 62, h: 5, text: "纸张：75mm × 100mm", size: 9, align: 2 },
    { x: 6.5, y: 36, w: 62, h: 10, text: "外框四条边应完整，左右留白相等；缺边或偏移请调整 X / Y 偏移，大小偏差请调整整体缩放", size: 8, align: 2 },
    { x: 5, y: 53, w: 20, h: 5, text: "左上", size: 10, bold: true },
    { x: 50, y: 91, w: 20, h: 5, text: "右下", size: 10, bold: true, align: 3 },
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
