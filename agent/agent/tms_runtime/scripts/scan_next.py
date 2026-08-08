"""
Automate outgoing scan flow in Ronghui TMS (Playwright, headed by default).
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextvars
import datetime as _dt
import json
import os
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

from agent.tms_runtime.scripts.browser_manager import TMSBrowserAuth, launch_browser
from agent.tms_runtime.scripts.shared_login import resolve_primary_credentials


DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_STATION_NAME = ""
DEFAULT_BILL_CODE = ""
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_ACTION_DELAY_SEC = 1.0
DEFAULT_DUMP_DIR = os.path.dirname(os.path.abspath(__file__))
STATION_KEYS = (
    "station_name",
    "stationName",
    "station",
    "next_station",
    "nextStation",
    "destination",
    "dest",
    "site",
    "site_name",
    "siteName",
    "网点",
    "站点",
    "目的地",
    "下一站",
)
BILL_KEYS = (
    "bill_code",
    "billCode",
    "bill",
    "scan_no",
    "scanNo",
    "waybill",
    "waybill_no",
    "waybillNo",
    "order",
    "order_no",
    "单号",
    "扫描单号",
    "运单号",
    "单据号",
)

_ACTION_DELAY_SEC = contextvars.ContextVar("action_delay_sec", default=DEFAULT_ACTION_DELAY_SEC)


def _has_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run_flow_in_playwright_thread(**kwargs: Any) -> Dict[str, Any]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan-next-playwright") as executor:
        return executor.submit(_run_flow_impl, **kwargs).result()

XPATH_MENU_SCAN_MANAGEMENT = (
    '//ul[@class="menu"]/li[@class="has-children"]/a/span[normalize-space(.)="扫描管理"][contains(@class, "menu-text")]'
)
XPATH_MENU_OUT_SCAN = (
    '//ul[@class="menu"]/li/ul[1][contains(@class, "menu-submenu")]/li/a/span[normalize-space(.)="发件扫描"][contains(@class, "menu-text")]'
)

XPATH_SCAN_FRAME = (
    "//div[contains(@id, 'mini-21$body$')][not(contains(@style,'display'))]"
    "//iframe[starts-with(@name, 'mini-iframe-') and contains(@src, '/widget/home?authenticationKey=')]"
)
XPATH_NEXT_STATION_INPUT = '//input[@name="PRE_OR_NEXT_STATION"]'
XPATH_SCAN_INPUT = "//input[@placeholder='单号输入后按回车键']"
XPATH_CHECK_ALL = '//table//tr/td//span[@id="mini-58checkall"]'
XPATH_UPLOAD = (
    '//a[@id="addBtn"]/span[contains(@class, "mini-button-icon-text")]/span[contains(@class, "mini-button-text")]'
)
XPATH_CONFIRM_BUTTON = (
    '//div[contains(@class, "mini-messagebox-buttons")]/a[normalize-space(.)="确定"][contains(@class, "mini-button")]'
)
XPATH_CONFIRM_UPLOAD_MESSAGE = '//div[contains(@class, "mini-messagebox-content")][contains(., "确定上传当前已编数据")]'
XPATH_SUCCESS_MESSAGE = '//div[contains(@class, "mini-messagebox-content")][contains(., "数据保存成功")]'
XPATH_ALREADY_SIGNED_MESSAGE = '//div[contains(@class, "mini-messagebox-content")][contains(., "已做过签收")]'
XPATH_GRID_ROW_ANY = '//div[@id="datagrid"]//table//tr[contains(@class, "mini-grid-row")]'
XPATH_GRID_ROW_UNSELECTED = (
    '//div[@id="datagrid"]//table//tr[@class="mini-grid-row mini-grid-newRow"]'
)
XPATH_GRID_ROW_SELECTED = '//div[@id="datagrid"]//table//tr[contains(@class,"mini-grid-row-selected")]'
XPATH_GRID_ROW_CHECKED = '//div[@id="datagrid"]//table//tr[contains(@class,"mini-grid-row-checked")]'
XPATH_GRID_ROW_ARIA_SELECTED = '//div[@id="datagrid"]//table//tr[@aria-selected="true"]'
XPATH_GRID_CHECKED_CELL = (
    "//div[@id='datagrid']//tr[contains(@class,'mini-grid-row')]"
    "//*[contains(@class,'mini-checkcolumn-checked') "
    "or contains(@class,'mini-grid-checkbox-checked') "
    "or contains(@class,'mini-grid-checkcolumn-checked') "
    "or contains(@class,'mini-checkbox-checked')]"
)
XPATH_CHECK_ALL_CHECKED = (
    "//div[@id='datagrid']//span[contains(@id,'checkall') "
    "and (contains(@class,'checked') or @aria-checked='true')]"
)
STATION_LOOKUP_CALL_IDS = (
    "FIND_SCAN_SEND_SITE_COMBOBOX_BY_CENTER",
    "FIND_SCAN_SEND_SITE_DIST",
    "FIND_SCAN_SEND_SITE_COMBOBOX",
    "FIND_SITE_ALL_COMBOBOX",
)

MINI_SET_STATION_SCRIPT = r"""
async ({ stationName, callIds }) => {
  const clean = (value) => String(value || "").trim();
  const wanted = clean(stationName);
  const result = { ok: false, stationName: wanted, attempts: [] };
  if (!wanted) {
    result.error = "empty_station";
    return result;
  }

  const combo = window.mini && mini.get ? mini.get("PRE_OR_NEXT_STATION_CODE") : null;
  if (!combo) {
    result.error = "combo_not_found";
    return result;
  }

  const nameKeys = ["SITE_NAME", "siteName", "name", "text", "TEXT", "NAME"];
  const codeKeys = ["SITE_CODE", "siteCode", "code", "id", "value", "CODE", "ID", "VALUE"];
  const valueFor = (row, keys) => {
    for (const key of keys) {
      if (row && row[key] !== undefined && row[key] !== null && clean(row[key])) {
        return clean(row[key]);
      }
    }
    return "";
  };
  const rowsFrom = (payload) => {
    if (Array.isArray(payload)) return payload;
    if (!payload || typeof payload !== "object") return [];
    for (const key of ["data", "rows", "records", "result"]) {
      if (Array.isArray(payload[key])) return payload[key];
    }
    return [];
  };
  const pickRow = (rows) => {
    const normalized = rows.filter((row) => row && typeof row === "object");
    const exact = normalized.find((row) => valueFor(row, nameKeys) === wanted);
    if (exact) return exact;
    return normalized.find((row) => valueFor(row, nameKeys).includes(wanted) || wanted.includes(valueFor(row, nameKeys)));
  };
  const applyRow = (row, source) => {
    const name = valueFor(row, nameKeys) || wanted;
    const code = valueFor(row, codeKeys) || name;
    if (combo.setValue) combo.setValue(code);
    if (combo.setText) combo.setText(name);
    if (combo.setIsValid) combo.setIsValid(true);

    const textInput = document.querySelector('input[name="PRE_OR_NEXT_STATION"]');
    if (textInput) {
      textInput.value = name;
      textInput.dispatchEvent(new Event("input", { bubbles: true }));
      textInput.dispatchEvent(new Event("change", { bubbles: true }));
    }
    const valueInput =
      document.querySelector("#PRE_OR_NEXT_STATION_CODE\\$value") ||
      document.querySelector('input[name="PRE_OR_NEXT_STATION_CODE"]');
    if (valueInput) {
      valueInput.value = code;
      valueInput.dispatchEvent(new Event("change", { bubbles: true }));
    }

    return {
      ok: true,
      source,
      stationName: name,
      stationCode: code,
      value: combo.getValue ? combo.getValue() : code,
      text: combo.getText ? combo.getText() : name,
    };
  };

  try {
    if (combo.getData) {
      const row = pickRow(rowsFrom(combo.getData()));
      if (row) return applyRow(row, "combo_data");
    }
  } catch (error) {
    result.attempts.push({ source: "combo_data", error: String(error) });
  }

  const urls = [];
  try {
    const comboUrl = combo.getUrl ? clean(combo.getUrl()) : "";
    if (comboUrl) {
      urls.push({
        source: "combo_url",
        url: comboUrl.startsWith("http") || comboUrl.startsWith("/") ? comboUrl : "/" + comboUrl,
      });
    }
  } catch (error) {
    result.attempts.push({ source: "combo_url", error: String(error) });
  }
  for (const id of callIds || []) {
    urls.push({
      source: id,
      url: "/dataQuery/findPageByCallId?id=" + encodeURIComponent(id),
    });
  }

  const seen = new Set();
  for (const entry of urls) {
    const baseUrl = entry.url;
    const separator = baseUrl.includes("?") ? "&" : "?";
    const url =
      baseUrl +
      separator +
      "pageSize=20&pageIndex=0&key=" +
      encodeURIComponent(wanted) +
      "&_=" +
      Date.now();
    if (seen.has(url)) continue;
    seen.add(url);
    try {
      const response = await fetch(url, {
        credentials: "include",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      const text = await response.text();
      let payload = null;
      try {
        payload = JSON.parse(text);
      } catch (_) {
        payload = null;
      }
      const rows = rowsFrom(payload);
      result.attempts.push({ source: entry.source, status: response.status, rows: rows.length });
      const row = pickRow(rows);
      if (row) return applyRow(row, entry.source);
    } catch (error) {
      result.attempts.push({ source: entry.source, error: String(error) });
    }
  }

  result.error = "station_not_found";
  return result;
}
"""

MINI_ADD_BILL_CODE_SCRIPT = r"""
(billCode) => {
  const bill = String(billCode || "").trim();
  if (!bill) return { ok: false, error: "empty_bill_code" };
  if (!window.mini || !mini.get) return { ok: false, error: "mini_not_found" };
  if (!window.$U || !$U.httpUtils || !$U.httpUtils.syncPostJson) {
    return { ok: false, error: "http_utils_not_found" };
  }

  const control = mini.get("BILL_CODE");
  if (!control) return { ok: false, error: "bill_code_control_not_found" };
  control.setValue(bill);
  if (control.setIsValid) control.setIsValid(true);

  const textInput = document.querySelector('input[name="BILL_CODE"]');
  if (textInput) {
    textInput.value = bill;
    textInput.dispatchEvent(new Event("input", { bubbles: true }));
    textInput.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const base = window.basepath || "";
  let allowed = false;
  $U.httpUtils.syncPostJson(
    base + "/dataQuery/findAllByCallId?id=FIND_TMS_SYS_SHARE_SET",
    { SHARE_CODE_IN: "MainBill,AllBill,ReturnBill,SubBill" },
    (data) => {
      if (!Array.isArray(data)) return;
      for (const row of data) {
        const pattern = row && row.SHARE_VALUE;
        if (!pattern) continue;
        try {
          const regex = new RegExp(pattern);
          if (regex.test(bill)) {
            allowed = true;
            break;
          }
        } catch (_) {}
      }
    }
  );
  if (!allowed) return { ok: false, error: "invalid_bill_code" };

  let signed = false;
  $U.httpUtils.syncPostJson(
    base + "/dataQuery/findAllByCallId?id=FIND_WAYBILL_SIGN_STATE",
    { BILL_CODE: bill },
    (data) => {
      signed = Array.isArray(data) && data.length > 0;
    }
  );
  if (signed) return { ok: false, signed: true, error: "already_signed" };

  const station = mini.get("PRE_OR_NEXT_STATION_CODE");
  const stationText = station && station.getText ? String(station.getText() || "").trim() : "";
  const stationCode = station && station.getValue ? String(station.getValue() || "").trim() : "";
  if (!stationText) return { ok: false, error: "empty_station" };

  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const listingCode = String(now.getFullYear()) + pad(now.getMonth() + 1) + pad(now.getDate()) + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
  const listing = mini.get("LISTING_CODE");
  if (listing && listing.setValue) listing.setValue(listingCode);
  const scanDate = mini.get("SCAN_DATE");
  if (scanDate && scanDate.setValue) scanDate.setValue(now);

  const form = new mini.Form("#searchForm");
  form.validate();
  if (form.isValid && form.isValid() === false) {
    const invalidFields = [];
    for (const id of ["BILL_CODE", "PRE_OR_NEXT_STATION_CODE", "SCAN_DATE", "FAST_TYPE", "LISTING_CODE"]) {
      const field = mini.get(id);
      if (!field || !field.isValid || field.isValid()) continue;
      invalidFields.push(id);
    }
    return { ok: false, error: "form_invalid", invalidFields };
  }

  const grid = mini.get("datagrid");
  if (!grid || !grid.getData || !grid.addRow) return { ok: false, error: "datagrid_not_found" };
  const beforeRows = grid && grid.getData ? grid.getData() : [];
  const before = Array.isArray(beforeRows) ? beforeRows.length : 0;
  for (const row of beforeRows) {
    if (row && row.BILL_CODE === bill) {
      return { ok: true, rows: before, duplicate: true };
    }
  }

  let userInfo = {};
  for (const scope of [window, window.parent, window.top]) {
    try {
      const candidate = scope && scope.$Z && scope.$Z.user && scope.$Z.user.getUserInfo
        ? scope.$Z.user.getUserInfo()
        : null;
      if (candidate) {
        userInfo = candidate;
        break;
      }
    } catch (_) {}
  }
  let headerUser = "";
  let headerSite = "";
  try {
    const topText = window.top && window.top.document && window.top.document.body
      ? String(window.top.document.body.innerText || "")
      : "";
    const userMatch = topText.match(/登录用户[:：]\s*([^\s]+)/);
    const siteMatch = topText.match(/登录网点[:：]\s*([^\s]+)/);
    headerUser = userMatch ? userMatch[1] : "";
    headerSite = siteMatch ? siteMatch[1] : "";
  } catch (_) {}
  const loginUserName = userInfo.loginUserName || headerUser || "TMS";
  const loginUserAccount = userInfo.loginUserAccount || headerUser || loginUserName;
  const loginSiteName = userInfo.loginSiteName || headerSite || "";
  const loginSiteCode = userInfo.loginSiteCode || "73901";
  const formData = form.getData();
  formData.PRE_OR_NEXT_STATION_CODE = stationCode;
  formData.PRE_OR_NEXT_STATION = stationText;
  formData.BILL_CODE = bill;
  formData.SCAN_SITE = loginSiteName;
  formData.SCAN_SITE_CODE = loginSiteCode;
  formData.SCAN_MAN = loginUserName;
  formData.SCAN_MAN_CODE = loginUserAccount;
  formData.SCAN_TYPE = "发件";
  formData.DATA_FROM = "K13";
  formData.DISPATCH_SITE = window.DISPATCH_OR_SEND_MAN || "";
  formData.REGISTER_DATE = new Date();

  if (grid.setTotalCount) grid.setTotalCount(before + 1);
  grid.addRow(formData);
  control.setValue("");
  if (control.setIsValid) control.setIsValid(true);
  if (listing && listing.setValue) listing.setValue(listingCode);
  if (scanDate && scanDate.setValue) scanDate.setValue(new Date());

  const rows = grid && grid.getData ? grid.getData() : [];
  const after = Array.isArray(rows) ? rows.length : 0;
  if (after > before) {
    return { ok: true, rows: after, trigger: "direct_datagrid_add" };
  }
  return { ok: false, error: "direct_datagrid_add_failed", before, after };
}
"""

MINI_UPLOAD_ROWS_SCRIPT = r"""
() => new Promise((resolve) => {
  try {
    if (!window.mini || !mini.get) return resolve({ ok: false, error: "mini_not_found" });
    if (!window.$Z || !$Z.Request || !$Z.Parameter) return resolve({ ok: false, error: "z_request_not_found" });
    const grid = mini.get("datagrid");
    if (!grid || !grid.getData) return resolve({ ok: false, error: "datagrid_not_found" });
    if (grid.validate) grid.validate();
    const rows = grid.getData();
    if (!Array.isArray(rows) || rows.length <= 0) return resolve({ ok: false, error: "empty_rows" });

    const request = new $Z.Request("/dataOperation/saveTables");
    const param = new $Z.Parameter("TAB_SCAN_SEND_ADD");
    param.push(rows);
    request.push(param);
    request.post((result) => {
      const message = result && result.message ? String(result.message) : "";
      const success = !!(result && result.success);
      if (success && grid.setData) grid.setData();
      resolve({ ok: success, message, result, rows: rows.length });
    });
  } catch (error) {
    resolve({ ok: false, error: String(error) });
  }
})
"""


def _ts() -> str:
    ts = _dt.datetime.now()
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(message: str) -> None:
    print(f"[{_ts()}] {message}", flush=True)


def _xpath_literal(text: str) -> str:
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    parts = text.split('"')
    return "concat(" + ", '\"', ".join(f'"{part}"' for part in parts) + ")"


def _get_first_value(raw: Dict[str, Any], keys: tuple[str, ...]) -> Optional[Any]:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def _normalize_item(raw: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw, dict) and "json" in raw and isinstance(raw["json"], dict):
        raw = raw["json"]
    if not isinstance(raw, dict):
        return None
    station = _get_first_value(raw, STATION_KEYS)
    bill = _get_first_value(raw, BILL_KEYS)
    station_text = str(station).strip() if station is not None else ""
    bill_text = str(bill).strip() if bill is not None else ""
    if not station_text or not bill_text:
        return None
    return {"station_name": station_text, "bill_code": bill_text}


def _coerce_items(raw: Any) -> list[Dict[str, str]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except Exception:
            return []
    if isinstance(raw, dict):
        for key in ("items", "data", "records", "rows"):
            if key in raw:
                return _coerce_items(raw.get(key))
        item = _normalize_item(raw)
        return [item] if item else []
    if isinstance(raw, list):
        items: list[Dict[str, str]] = []
        for entry in raw:
            item = _normalize_item(entry)
            if item:
                items.append(item)
        return items
    return []


def _load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _wait_xpath_visible(scope, xpath: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    return scope.wait_for_selector(f"xpath={xpath}", state="visible", timeout=timeout_ms)


def _pause(scope, delay_sec: Optional[float] = None) -> None:
    if delay_sec is None:
        delay_sec = float(_ACTION_DELAY_SEC.get())
    if delay_sec <= 0:
        return
    try:
        scope.wait_for_timeout(int(delay_sec * 1000))
    except Exception:
        time.sleep(delay_sec)


def _click_xpath(scope, xpath: str, *, label: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    log(f"点击：{label}")
    el = _wait_xpath_visible(scope, xpath, timeout_ms=timeout_ms)
    el.scroll_into_view_if_needed()
    el.click()
    _pause(scope)


def _fill_xpath(scope, xpath: str, value: str, *, label: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    log(f"输入：{label}")
    el = _wait_xpath_visible(scope, xpath, timeout_ms=timeout_ms)
    el.click()
    el.fill(value)
    _pause(scope)


def _is_visible(scope, xpath: str) -> bool:
    try:
        return scope.is_visible(f"xpath={xpath}")
    except Exception:
        return False


def _get_scan_frame(page, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            iframe = page.query_selector(f"xpath={XPATH_SCAN_FRAME}")
            if iframe is None:
                page.wait_for_timeout(200)
                continue
            frame = iframe.content_frame()
            if frame is None:
                page.wait_for_timeout(200)
                continue
            return frame
        except BaseException as exc:
            last_error = exc
            page.wait_for_timeout(200)
    raise TimeoutError("未找到发件扫描 iframe") from last_error


def _resolve_credentials(
    config_path: str,
    *,
    username: str = "",
    password: str = "",
) -> Tuple[str, str]:
    _ = config_path
    return resolve_primary_credentials(username=username, password=password)


def _set_station_by_mini_api(frame, station_name: str) -> dict[str, Any]:
    try:
        return frame.evaluate(
            MINI_SET_STATION_SCRIPT,
            {
                "stationName": station_name,
                "callIds": list(STATION_LOOKUP_CALL_IDS),
            },
        )
    except BaseException as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _add_bill_code_by_mini_api(frame, bill_code: str) -> dict[str, Any]:
    try:
        return frame.evaluate(MINI_ADD_BILL_CODE_SCRIPT, bill_code)
    except BaseException as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _upload_rows_by_mini_api(frame) -> dict[str, Any]:
    try:
        return frame.evaluate(MINI_UPLOAD_ROWS_SCRIPT)
    except BaseException as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _select_station(frame, station_name: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    _wait_xpath_visible(frame, XPATH_NEXT_STATION_INPUT, timeout_ms=timeout_ms)
    mini_result = _set_station_by_mini_api(frame, station_name)
    if mini_result.get("ok"):
        log(
            "下一站已通过 MiniUI 设置: "
            f"{mini_result.get('text') or mini_result.get('stationName')} "
            f"({mini_result.get('value') or mini_result.get('stationCode')})"
        )
        return True
    log(f"MiniUI 设置下一站失败，回退输入法: {mini_result.get('error') or mini_result}")

    _fill_xpath(frame, XPATH_NEXT_STATION_INPUT, station_name, label="下一站")
    try:
        input_el = _wait_xpath_visible(frame, XPATH_NEXT_STATION_INPUT, timeout_ms=timeout_ms)
        input_el.press("Enter")
        _pause(frame)
    except Exception:
        pass
    try:
        input_el = _wait_xpath_visible(frame, XPATH_NEXT_STATION_INPUT, timeout_ms=timeout_ms)
        current_value = input_el.input_value()
    except Exception:
        current_value = ""
    return station_name in (current_value or "")


def _safe_filename(text: str) -> str:
    keep = []
    for ch in (text or ""):
        if ch.isalnum() or ch in ("_", "-", "."):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep) or "scan_next"


def dump_page_debug(page, frame=None, *, out_dir: str, prefix: str) -> Dict[str, str]:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    safe_prefix = _safe_filename(prefix)
    unique = f"{os.getpid()}_{threading.get_ident()}"
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"{safe_prefix}_{ts}_{unique}")
    png_path = base + ".png"
    html_path = base + ".html"
    frame_path = base + ".frame.html"
    frames_path = base + ".frames.json"
    results: Dict[str, str] = {}

    try:
        page.screenshot(path=png_path, full_page=True)
        results["screenshot"] = png_path
    except Exception as exc:
        log(f"保存截图失败: {type(exc).__name__}: {exc}")

    try:
        html = page.content() or ""
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(html)
        results["html"] = html_path
    except Exception as exc:
        log(f"保存页面HTML失败: {type(exc).__name__}: {exc}")

    if frame is not None:
        try:
            frame_html = frame.content() or ""
            with open(frame_path, "w", encoding="utf-8") as handle:
                handle.write(frame_html)
            results["frame_html"] = frame_path
        except Exception as exc:
            log(f"保存iframe HTML失败: {type(exc).__name__}: {exc}")

    try:
        frames = []
        try:
            frames = [{"url": f.url, "name": getattr(f, "name", "")} for f in (page.frames or [])]
        except Exception:
            frames = []
        with open(frames_path, "w", encoding="utf-8") as handle:
            json.dump(frames, handle, ensure_ascii=False, indent=2)
        results["frames"] = frames_path
    except Exception as exc:
        log(f"保存frames列表失败: {type(exc).__name__}: {exc}")

    return results


def _wait_table_cleared(frame, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    selected = frame.locator(f"xpath={XPATH_GRID_ROW_SELECTED}")
    unselected = frame.locator(f"xpath={XPATH_GRID_ROW_UNSELECTED}")
    rows = frame.locator(f"xpath={XPATH_GRID_ROW_ANY}")
    while time.time() < deadline:
        try:
            selected_count = selected.count()
            unselected_count = unselected.count()
            total_count = rows.count()
        except Exception:
            selected_count = 0
            unselected_count = 0
            total_count = 0
        if selected_count == 0 and (unselected_count >= 1 or total_count == 0):
            return True
        frame.wait_for_timeout(300)
    return False


def _grid_row_count(frame) -> int:
    try:
        return int(frame.locator(f"xpath={XPATH_GRID_ROW_ANY}").count())
    except Exception:
        return 0


def _wait_grid_has_rows(frame, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    rows = frame.locator(f"xpath={XPATH_GRID_ROW_ANY}")
    while time.time() < deadline:
        try:
            count = rows.count()
        except Exception:
            count = 0
        if count > 0:
            return True
        frame.wait_for_timeout(300)
    return False


def _wait_bill_code_added(
    page,
    frame,
    bill_code: str,
    *,
    previous_count: int,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    skip_message_xpath: Optional[str] = None,
) -> tuple[bool, bool]:
    deadline = time.time() + timeout_ms / 1000.0
    literal = _xpath_literal(str(bill_code))
    row_xpath = (
        "//div[@id='datagrid']//table//tr[contains(@class,'mini-grid-row')]"
        "//*[contains(normalize-space(.), "
        + literal
        + ")]"
    )
    row_locator = frame.locator(f"xpath={row_xpath}")
    rows = frame.locator(f"xpath={XPATH_GRID_ROW_ANY}")
    while time.time() < deadline:
        if skip_message_xpath:
            try:
                if _is_visible(frame, skip_message_xpath) or _is_visible(page, skip_message_xpath):
                    _click_confirm_any(page, frame, message_xpath=skip_message_xpath, timeout_ms=2_000)
                    return False, True
            except Exception:
                pass
        try:
            if row_locator.count() > 0:
                return True, False
        except Exception:
            pass
        try:
            if rows.count() > previous_count:
                return True, False
        except Exception:
            pass
        frame.wait_for_timeout(300)
    return False, False


def _wait_rows_selected(frame, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    selected = frame.locator(f"xpath={XPATH_GRID_ROW_SELECTED}")
    checked_rows = frame.locator(f"xpath={XPATH_GRID_ROW_CHECKED}")
    aria_selected = frame.locator(f"xpath={XPATH_GRID_ROW_ARIA_SELECTED}")
    checked_cells = frame.locator(f"xpath={XPATH_GRID_CHECKED_CELL}")
    checkall_checked = frame.locator(f"xpath={XPATH_CHECK_ALL_CHECKED}")
    rows = frame.locator(f"xpath={XPATH_GRID_ROW_ANY}")
    while time.time() < deadline:
        try:
            if (
                selected.count() > 0
                or checked_rows.count() > 0
                or aria_selected.count() > 0
                or checked_cells.count() > 0
            ):
                return True
            if checkall_checked.count() > 0 and rows.count() > 0:
                return True
        except Exception:
            pass
        frame.wait_for_timeout(300)
    return False


def _click_confirm_any(
    page,
    frame,
    *,
    message_xpath: Optional[str] = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> None:
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            if message_xpath and _is_visible(frame, message_xpath):
                _click_xpath(frame, XPATH_CONFIRM_BUTTON, label="确定", timeout_ms=timeout_ms)
                return
            if _is_visible(frame, XPATH_CONFIRM_BUTTON):
                _click_xpath(frame, XPATH_CONFIRM_BUTTON, label="确定", timeout_ms=timeout_ms)
                return
        except BaseException as exc:
            last_error = exc
        try:
            if message_xpath and _is_visible(page, message_xpath):
                _click_xpath(page, XPATH_CONFIRM_BUTTON, label="确定", timeout_ms=timeout_ms)
                return
            if _is_visible(page, XPATH_CONFIRM_BUTTON):
                _click_xpath(page, XPATH_CONFIRM_BUTTON, label="确定", timeout_ms=timeout_ms)
                return
        except BaseException as exc:
            last_error = exc
        time.sleep(0.2)
    raise TimeoutError("未等到确认弹窗") from last_error


def _wait_signed_popup(page, frame, *, timeout_ms: int = 2_000) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if _is_visible(frame, XPATH_ALREADY_SIGNED_MESSAGE) or _is_visible(page, XPATH_ALREADY_SIGNED_MESSAGE):
                _click_confirm_any(page, frame, message_xpath=XPATH_ALREADY_SIGNED_MESSAGE, timeout_ms=2_000)
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _run_flow_impl(
    *,
    station_name: str,
    bill_code: str,
    items: Optional[list[Dict[str, str]]],
    username: str,
    password: str,
    config_path: str,
    headless: bool,
    slow_mo_ms: int,
    max_login_attempts: int,
    action_delay_sec: float,
    dump_on_error: bool,
    dump_dir: str,
) -> Dict[str, Any]:
    started = time.time()
    stage = "init"
    scan_items: list[Dict[str, str]] = []
    station_results: list[Dict[str, Any]] = []
    pending_codes: list[str] = []
    skipped_signed_codes: list[str] = []
    current_station = ""
    current_bill_code = ""
    processed_count = 0
    p = browser = context = page = None
    frame = None
    delay_token = None
    try:
        delay_token = _ACTION_DELAY_SEC.set(max(0.0, float(action_delay_sec)))
        scan_items = _coerce_items(items)
        if not scan_items:
            station_text = str(station_name).strip()
            bill_text = str(bill_code).strip()
            if not station_text or not bill_text:
                raise RuntimeError("未提供扫描数据")
            scan_items = [{"station_name": station_text, "bill_code": bill_text}]
        stage = "launch_browser"
        p, browser, context, page = launch_browser(headless=headless, slow_mo_ms=slow_mo_ms)

        stage = "login"
        uid, pwd = _resolve_credentials(config_path, username=username, password=password)
        auth = TMSBrowserAuth(max_attempts=max(1, int(max_login_attempts)))
        log("开始登录")
        auth.login(page, username=uid, password=pwd)
        log(f"登录完成，当前URL：{page.url}")

        stage = "open_menu"
        if not _is_visible(page, XPATH_MENU_OUT_SCAN):
            _click_xpath(page, XPATH_MENU_SCAN_MANAGEMENT, label="扫描管理")
        _click_xpath(page, XPATH_MENU_OUT_SCAN, label="发件扫描")

        stage = "wait_frame"
        frame = _get_scan_frame(page)
        _wait_xpath_visible(frame, XPATH_SCAN_INPUT, timeout_ms=DEFAULT_TIMEOUT_MS)

        def _upload_current_station() -> None:
            nonlocal stage, pending_codes, station_results, current_station
            if not pending_codes:
                return
            stage = "select_all"
            _click_xpath(frame, XPATH_CHECK_ALL, label="全选复选框")
            if not _wait_rows_selected(frame, timeout_ms=DEFAULT_TIMEOUT_MS):
                if not _wait_grid_has_rows(frame, timeout_ms=3_000):
                    raise RuntimeError("表格未发现可上传数据")
                log("全选未检测到选中行，继续尝试上传")
            stage = "upload"
            upload_result = _upload_rows_by_mini_api(frame)
            if not upload_result.get("ok"):
                if upload_result.get("message"):
                    raise RuntimeError(f"上传失败: {upload_result.get('message')}")
                log(f"MiniUI 直接上传失败，回退点击上传: {upload_result.get('error') or upload_result}")
                _click_xpath(frame, XPATH_UPLOAD, label="上传")
                _click_confirm_any(page, frame, message_xpath=XPATH_CONFIRM_UPLOAD_MESSAGE)
                stage = "upload_success"
                _click_confirm_any(page, frame, message_xpath=XPATH_SUCCESS_MESSAGE)
            else:
                log(f"扫描数据上传成功: {upload_result.get('message') or upload_result}")
                stage = "upload_success"
            stage = "verify_clear"
            if not _wait_table_cleared(frame, timeout_ms=DEFAULT_TIMEOUT_MS):
                raise RuntimeError("表格未清空或未刷新完成")
            station_results.append(
                {
                    "station_name": current_station,
                    "count": len(pending_codes),
                    "bill_codes": list(pending_codes),
                }
            )
            pending_codes.clear()

        for item in scan_items:
            station = item["station_name"]
            bill = item["bill_code"]
            current_bill_code = bill
            if station != current_station:
                if current_station:
                    log(f"切换站点: {current_station} -> {station}")
                    _upload_current_station()
                stage = "select_station"
                log(f"输入下一站: {station}")
                selected = _select_station(frame, station)
                if not selected:
                    log(f"下一站未确认: {station}")
                current_station = station

            stage = "input_bill_code"
            log(f"录入单号: {bill}")
            row_count_before = _grid_row_count(frame)
            add_result = _add_bill_code_by_mini_api(frame, bill)
            if add_result.get("ok"):
                log(f"扫描单号已提交到页面逻辑: {bill}")
                _pause(frame)
            elif add_result.get("signed"):
                log(f"单号已做过签收，跳过: {bill}")
                skipped_signed_codes.append(bill)
                continue
            else:
                log(f"MiniUI 提交扫描单号失败，回退输入法: {add_result.get('error') or add_result}")
                _fill_xpath(frame, XPATH_SCAN_INPUT, bill, label="扫描单号")
                try:
                    scan_input = _wait_xpath_visible(frame, XPATH_SCAN_INPUT, timeout_ms=DEFAULT_TIMEOUT_MS)
                    scan_input.press("Enter")
                    _pause(frame)
                except Exception:
                    pass
            if _wait_signed_popup(page, frame):
                log(f"单号已做过签收，跳过: {bill}")
                skipped_signed_codes.append(bill)
                continue
            added, skipped = _wait_bill_code_added(
                page,
                frame,
                bill,
                previous_count=row_count_before,
                timeout_ms=DEFAULT_TIMEOUT_MS,
                skip_message_xpath=XPATH_ALREADY_SIGNED_MESSAGE,
            )
            if skipped:
                log(f"单号已做过签收扫描，跳过: {bill}")
                skipped_signed_codes.append(bill)
                continue
            if not added:
                raise RuntimeError(f"扫描单号未写入表格: {bill}")
            pending_codes.append(bill)
            processed_count += 1

        _upload_current_station()
        stage = "done"

        return {
            "ok": True,
            "stage": stage,
            "message": "success",
            "detail": {
                "station_name": current_station or station_name,
                "bill_code": current_bill_code or bill_code,
                "items": scan_items,
                "stations": station_results,
                "total_scanned": processed_count,
                "skipped_signed_codes": list(skipped_signed_codes),
                "skipped_signed_count": len(skipped_signed_codes),
                "url": page.url if page is not None else "",
            },
            "ts": _ts(),
            "cost_sec": round(time.time() - started, 3),
        }
    except BaseException as exc:
        debug_info: Dict[str, str] = {}
        if dump_on_error and page is not None:
            try:
                debug_info = dump_page_debug(
                    page,
                    frame=frame,
                    out_dir=dump_dir or DEFAULT_DUMP_DIR,
                    prefix=f"scan_next_{stage}",
                )
                if debug_info:
                    log(f"调试文件已保存: {debug_info}")
            except Exception as dump_exc:
                log(f"调试文件保存失败: {type(dump_exc).__name__}: {dump_exc}")
        return {
            "ok": False,
            "stage": stage,
            "message": f"{type(exc).__name__}: {exc}",
            "detail": {
                "station_name": current_station or station_name,
                "bill_code": current_bill_code or bill_code,
                "items": scan_items,
                "stations": station_results,
                "processed": processed_count,
                "pending_codes": list(pending_codes),
                "skipped_signed_codes": list(skipped_signed_codes),
                "skipped_signed_count": len(skipped_signed_codes),
                "url": page.url if page is not None else "",
                "debug": debug_info,
            },
            "ts": _ts(),
            "cost_sec": round(time.time() - started, 3),
        }
    finally:
        if delay_token is not None:
            try:
                _ACTION_DELAY_SEC.reset(delay_token)
            except Exception:
                pass
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass


def run_flow(
    *,
    station_name: str,
    bill_code: str,
    items: Optional[list[Dict[str, str]]],
    username: str,
    password: str,
    config_path: str,
    headless: bool,
    slow_mo_ms: int,
    max_login_attempts: int,
    action_delay_sec: float,
    dump_on_error: bool,
    dump_dir: str,
) -> Dict[str, Any]:
    kwargs = {
        "station_name": station_name,
        "bill_code": bill_code,
        "items": items,
        "username": username,
        "password": password,
        "config_path": config_path,
        "headless": headless,
        "slow_mo_ms": slow_mo_ms,
        "max_login_attempts": max_login_attempts,
        "action_delay_sec": action_delay_sec,
        "dump_on_error": dump_on_error,
        "dump_dir": dump_dir,
    }
    if _has_running_event_loop():
        return _run_flow_in_playwright_thread(**kwargs)
    return _run_flow_impl(**kwargs)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ronghui TMS 发件扫描自动化")
    parser.add_argument("--station-name", default=DEFAULT_STATION_NAME, help="下一站名称")
    parser.add_argument("--bill-code", default=DEFAULT_BILL_CODE, help="扫描单号")
    parser.add_argument("--items-json", default="", help="JSON列表，元素包含站点与单号")
    parser.add_argument("--items-file", default="", help="包含JSON列表的文件路径")
    parser.add_argument("--username", default="", help="账号（为空则读取 config.json）")
    parser.add_argument("--password", default="", help="密码（为空则读取 config.json）")
    parser.add_argument("--config-path", default=DEFAULT_CONFIG_PATH, help="config.json 路径")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="无头模式运行（默认无头）",
    )
    parser.add_argument("--slow-mo-ms", type=int, default=0, help="每步操作延迟毫秒")
    parser.add_argument("--max-login-attempts", type=int, default=6, help="登录重试次数")
    parser.add_argument("--action-delay-sec", type=float, default=DEFAULT_ACTION_DELAY_SEC, help="动作之间的延迟秒数")
    parser.add_argument("--dump-on-error", action="store_true", help="失败时保存截图/HTML")
    parser.add_argument("--dump-dir", default=DEFAULT_DUMP_DIR, help="调试输出目录")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    items: list[Dict[str, str]] = []
    if args.items_json:
        items = _coerce_items(str(args.items_json))
    elif args.items_file:
        items = _coerce_items(_load_json_file(str(args.items_file)))
    result = run_flow(
        station_name=str(args.station_name),
        bill_code=str(args.bill_code),
        items=items,
        username=str(args.username),
        password=str(args.password),
        config_path=str(args.config_path),
        headless=bool(args.headless),
        slow_mo_ms=int(args.slow_mo_ms),
        max_login_attempts=int(args.max_login_attempts),
        action_delay_sec=float(args.action_delay_sec),
        dump_on_error=bool(args.dump_on_error),
        dump_dir=str(args.dump_dir),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_param(params: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    if not isinstance(params, dict):
        return default
    for key in keys:
        if key in params and params[key] is not None:
            return params[key]
    return default


def run_once(params: Dict[str, Any]) -> Any:
    params = params or {}
    station_name = str(_get_param(params, "station_name", "stationName", default=DEFAULT_STATION_NAME))
    bill_code = str(_get_param(params, "bill_code", "billCode", default=DEFAULT_BILL_CODE))
    raw_items = _get_param(params, "items", "data", "records", "rows", default=None)
    if raw_items is None:
        raw_items = _get_param(params, "items_json", "itemsJson", default="")
    username = str(_get_param(params, "username", default=""))
    password = str(_get_param(params, "password", default=""))
    config_path = str(_get_param(params, "config_path", "configPath", default=DEFAULT_CONFIG_PATH))

    headless = _coerce_bool(_get_param(params, "headless", default=True))
    slow_mo_ms = int(_get_param(params, "slow_mo_ms", "slowMoMs", default=0))
    max_login_attempts = int(_get_param(params, "max_login_attempts", "maxLoginAttempts", default=6))
    action_delay_sec = float(_get_param(params, "action_delay_sec", "actionDelaySec", default=DEFAULT_ACTION_DELAY_SEC))
    dump_on_error = _coerce_bool(_get_param(params, "dump_on_error", "dumpOnError", default=True))
    dump_dir = str(_get_param(params, "dump_dir", "dumpDir", default=DEFAULT_DUMP_DIR))

    items = _coerce_items(raw_items)
    if not items:
        items_file = _get_param(params, "items_file", "itemsFile", default="")
        if items_file:
            items = _coerce_items(_load_json_file(str(items_file)))

    return run_flow(
        station_name=station_name,
        bill_code=bill_code,
        items=items,
        username=username,
        password=password,
        config_path=config_path,
        headless=headless,
        slow_mo_ms=slow_mo_ms,
        max_login_attempts=max_login_attempts,
        action_delay_sec=action_delay_sec,
        dump_on_error=dump_on_error,
        dump_dir=dump_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
