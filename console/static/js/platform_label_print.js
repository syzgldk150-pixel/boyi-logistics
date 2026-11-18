(function (global) {
  "use strict";

  // Original-site labels (融辉/韵达): Agent reads the platform's own label and
  // returns its C-Lodop calls. Only these calls are applied; no platform script
  // runs here. The list matches agent/agent/tms_runtime/platform_waybill_print.py.
  const CALLS = new Set([
    "PRINT_INITA", "SET_PRINT_PAGESIZE", "SET_PRINT_MODE", "SET_SHOW_MODE", "NEWPAGE",
    "ADD_PRINT_TEXT", "ADD_PRINT_HTM", "ADD_PRINT_LINE", "ADD_PRINT_IMAGE", "ADD_PRINT_BARCODE",
    "ADD_PRINT_RECT", "SET_PRINT_STYLEA", "SET_PREVIEW_WINDOW",
  ]);
  const SETTINGS_KEY = "shipnow.manual.printerSettings";

  function checkedCalls(label) {
    const calls = Array.isArray(label?.calls) ? label.calls : [];
    if (!calls.length || calls[0]?.[0] !== "PRINT_INITA") throw new Error("原站面单数据不完整");
    calls.forEach((call) => {
      const [name, args] = Array.isArray(call) ? call : [];
      if (!CALLS.has(name) || !Array.isArray(args)
          || !args.every((arg) => ["string", "number", "boolean"].includes(typeof arg))) {
        throw new Error("原站面单含未核实的打印指令");
      }
    });
    return calls;
  }

  function savedPrinterName() {
    try {
      return String((JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {}).printer_name || "");
    } catch {
      return "";
    }
  }

  // Opens the C-Lodop preview of the label. Resolves once the preview is open
  // with ``printed``: a promise of how many times the operator printed from it
  // (C-Lodop reports it when the preview closes; null when not reported).
  async function preview(label) {
    const calls = checkedCalls(label);
    if (!global.BoyiCLodop?.getInstance) throw new Error("C-Lodop loader not available");
    const lodop = await global.BoyiCLodop.getInstance();
    if (!lodop) throw new Error("C-Lodop service not found");
    calls.forEach(([name, args]) => {
      if (typeof lodop[name] !== "function") throw new Error(`C-Lodop 不支持 ${name}，请升级 C-Lodop`);
      lodop[name](...args);
    });
    const printerIndex = global.BoyiWaybillPrint?.findPrinterIndex?.(lodop, savedPrinterName());
    if (printerIndex !== null && printerIndex !== undefined) lodop.SET_PRINTER_INDEX(printerIndex);
    const printed = new Promise((resolve) => {
      lodop.On_Return = (_taskId, value) => {
        lodop.On_Return = null;
        const count = Number(value);
        resolve(Number.isFinite(count) ? count : null);
      };
    });
    lodop.PREVIEW();
    return { printed };
  }

  global.BoyiPlatformLabel = { preview, checkedCalls };
})(window);
