(function (global) {
  "use strict";

  const SETTINGS_KEY = "shipnow.manual.printerSettings";

  function readSettings() {
    try {
      return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  function findPrinterIndex(lodop, printerName) {
    const wanted = String(printerName || "").trim();
    if (!wanted || wanted === "系统默认打印机") return null;
    const count = Number(lodop.GET_PRINTER_COUNT?.() || 0);
    for (let index = 0; index < count; index += 1) {
      if (String(lodop.GET_PRINTER_NAME(index) || "") === wanted) return index;
    }
    return null;
  }

  function openPreview(lodop) {
    if (typeof lodop.SET_PREVIEW_WINDOW === "function") {
      lodop.SET_PREVIEW_WINDOW(1, 0, 0, 760, 680, "打印预览.打印");
    }
    lodop.PREVIEW();
  }

  async function preview(waybill, settings = readSettings()) {
    if (!global.BoyiCLodop?.getInstance) throw new Error("C-Lodop loader not available");
    const lodop = await global.BoyiCLodop.getInstance();
    if (!lodop) throw new Error("C-Lodop service not found");
    if (!global.WaybillLabelLodop?.applyTemplate) throw new Error("Waybill Lodop renderer not loaded");
    const printerIndex = findPrinterIndex(lodop, settings.printer_name);
    const copies = Math.max(1, Math.min(Number(settings.print_copies || 1) || 1, 5));
    lodop.PRINT_INIT("博益物流运单预览");
    if (printerIndex !== null) lodop.SET_PRINTER_INDEX(printerIndex);
    lodop.SET_PRINT_COPIES(copies);
    await global.WaybillLabelLodop.applyTemplate(lodop, waybill, settings);
    openPreview(lodop);
  }

  global.BoyiWaybillPrint = { preview, findPrinterIndex, openPreview };
})(window);
